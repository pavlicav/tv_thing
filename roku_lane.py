"""TV Thing's Roku lane: the source Roku and what it is showing.

One source Roku feeds one Hauppauge HD PVR. The server owns the capture
process (the HD PVR's own H.264 and AAC, copied to HLS and to a private
loopback tap). This module owns everything about *what* is on it:

* the **selection** -- a verified YouTube TV channel or game, or "manual";
* the **generation** -- ``<capture id>.<revision>``. It changes whenever the
  picture stops being the verified selection (a navigation starts, a remote
  key is pressed) or the capture restarts, so a client holding a lease on
  the lane fails closed exactly when it should. The revision and the
  selection are kept by the feed (``feed_node.LaneRecord``), so a restart of
  this side reads them back and changes neither;
* **navigation**, run by ``roku_control``'s controller in a background
  operation, with screen recognition on the lane's tap;
* the **remote** for Manual Roku: allowlisted ECP keys, text and app launch.

Locks are the server's business: callers check the roku lane's lock before
calling anything here that changes the Roku.
"""
from __future__ import annotations

import copy
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import date

import feed_node
from roku_control import days
from roku_control.errors import (ConfigError, ControlBusyError, CueCancelledError, EcpError,
                                 NavigationError, RokuStreamError, SundayTicketBlackoutError)
from roku_control.sports import TYPICAL, nhl_on_espn_unlimited

LANE = "roku"
YOUTUBE_TV = "youtube-tv"
MLB = "mlb"
THURSDAY_NIGHT_FOOTBALL = "thursday-night-football"
SUNDAY_TICKET = "sunday-ticket"
MANUAL = "roku-manual"
NHL = "nhl"

# Source id -> (name, what its items are). The ids match vizelVision's, so
# its saved queue and schedule targets keep working unchanged.
SOURCES = {
    YOUTUBE_TV: ("YouTube TV", "channels"),
    MLB: ("Baseball", "games"),
    THURSDAY_NIGHT_FOOTBALL: ("Thursday Night Football", "games"),
    SUNDAY_TICKET: ("NFL Sunday Ticket", "games"),
    # The NHL games ESPN Unlimited streams inside YouTube TV, found on its ESPN
    # page (roku_control.espn_unlimited). TV Thing's own: vizelVision has no such source.
    NHL: ("NHL on ESPN Unlimited", "games"),
    MANUAL: ("Manual Roku", "manual"),
}
GAME_SOURCES = (MLB, THURSDAY_NIGHT_FOOTBALL, SUNDAY_TICKET, NHL)
# Games the lineup numbers and the guide lists though the Roku cannot be sent
# to them, so they are in no source list and refused by tune(). None now: the
# NHL's were, until the ESPN page could be browsed (2026-10-19).
LISTED_SOURCES = {}
# The league each game source lists, for how long its games usually run.
GAME_LEAGUES = {MLB: "mlb", THURSDAY_NIGHT_FOOTBALL: "nfl", SUNDAY_TICKET: "nfl", NHL: "nhl"}
# How long the Roku can take to reach a target and verify it: YouTube TV
# took 39-124 s and Sunday Ticket 93-205 s when measured on 2026-10-06.
NAVIGATION_SECONDS = 210
# A Sunday Ticket game normally plays from its local station's row when it is
# blacked out. Only when no station showing it was found is the failure
# remembered: a repeat is refused at once (force navigates), since vizelVision
# retries a Sunday Ticket game 10 times, ~2.5 min of Roku each. Short, because
# YouTube TV's market (and so the blackout) can change.
BLACKOUT_SECONDS = 30 * 60

# What the on-screen remote may send. Power and volume are left out: the
# source Roku's HDMI goes to the capture card, not a television.
REMOTE_KEYS = ("Home", "Back", "Up", "Down", "Left", "Right", "Select",
               "Play", "Rev", "Fwd", "InstantReplay", "Info", "Search",
               "Backspace", "Enter")
TEXT_LIMIT = 64

# The still-playing check (vizelVision's "the Roku still playing"): the app
# it was verified in, its player playing, and a moving picture within
# PLAYING_SECONDS. A failed feed is nearly always a capture blip, not the Roku.
PLAYING_SECONDS = 4.0
MOTION_STEP = 211
MOTION_LEVELS = 24
MOTION_SHARE = .005
PLAYER_CACHE_SECONDS = 5.0
APPS_CACHE_SECONDS = 60.0


def moved(before, after):
    """Whether the picture changed between two frames, beyond capture noise."""
    if (before.width, before.height) != (after.width, after.height):
        return True
    a, b = before.rgb[::MOTION_STEP], after.rgb[::MOTION_STEP]
    return sum(1 for x, y in zip(a, b) if abs(x - y) > MOTION_LEVELS) > MOTION_SHARE * len(a)


def _iso_date(value):
    if value is None:   # the games' day, which turns at 3 AM
        return days.today()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ConfigError("date must use YYYY-MM-DD format")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError("date must use YYYY-MM-DD format") from exc
    return value


def same_target(left, right):
    return bool(left and right) and all(
        left.get(key) == right.get(key) for key in ("source_id", "channel_id", "date"))


class RokuLane:
    def __init__(self, config, controller, *, baseball, sports, capture_source,
                 record=None, reuse_max_age=900.0, playing_seconds=PLAYING_SECONDS,
                 clock=time.monotonic, wall_clock=time.time, sleep=time.sleep, config_path=None):
        self.config = config
        # roku.json, whose channel list is taken up when it changes (reload_channels).
        self.config_path = config_path
        self._config_signature = self._signature()
        self.controller = controller
        self.feedback = controller.feedback
        self.ecp = controller.ecp
        self.baseball = baseball
        self.sports = sports
        # () -> {"running", "stream_id", "tap_url", ...} from the server.
        self.capture_source = capture_source
        # The feed's record of this lane: its revision and verified selection. On its own
        # (tests, tools) the lane keeps a private one.
        self.record = record or feed_node.LaneRecord.private(LANE)
        self.reuse_max_age = reuse_max_age
        self.playing_seconds = playing_seconds
        self._clock, self._wall, self._sleep = clock, wall_clock, sleep
        self._lock = threading.RLock()
        self._tune_lock = threading.Lock()
        self.operation = None
        self._thread = None
        self._blackouts = {}  # target key -> (wall time, message)
        self._player = {"at": None, "data": None, "error": None}
        self._player_refreshing = False
        self._apps = {"at": None, "data": None}
        # The navigations _navigate itself ends, with what it verified (per thread).
        self._local = threading.local()
        controller.navigation_guard = self._navigation_guard
        # A navigation left open by a predecessor that died mid-way: what the Roku shows is
        # unknown, and stays so (its revision already moved when it began).
        left = self.record.state()["navigation"]
        if left is not None and left["ended_at"] is None:
            self.record.end_navigation(left["op_id"], None)

    # -- roku.json ----------------------------------------------------------
    def _signature(self):
        try:
            stat = os.stat(self.config_path) if self.config_path else None
        except OSError:
            return None
        return stat and (stat.st_mtime_ns, stat.st_size)

    def reload_channels(self):
        """Take up roku.json's channel list once the file changes, so a YouTube TV row added
        there needs no restart (its other settings still take one). Never mid-navigation,
        which reads the list as it goes: the first look after it ends takes it up. A file
        that does not load keeps the list there is, and says why once. True when it changed."""
        signature = self._signature()
        if signature is None or signature == self._config_signature:
            return False
        from roku_control.config import load_config
        with self._lock:
            navigation = self.record.state()["navigation"]
            if self._running() or (navigation is not None and navigation["ended_at"] is None):
                return False
            self._config_signature = signature
            try:
                fresh = load_config(self.config_path)
            except ConfigError as error:
                print(f"[Roku] roku.json changed but does not load ({error}); its old channel list stays",
                      flush=True)
                return False
            if fresh.channels == self.config.channels:
                return False
            # Everything that resolves a channel by name holds the config.
            lists = {"channels": fresh.channels, "_channel_lookup": fresh._channel_lookup}
            for holder in (self, self.controller, self.baseball, self.sports):
                holder.config = replace(holder.config, **lists)
        print(f"[Roku] roku.json's channel list taken up: {len(fresh.channels)} channels", flush=True)
        return True

    # -- identity ---------------------------------------------------------
    def capture_id(self):
        return self.capture_source().get("stream_id")

    def generation(self):
        capture = self.capture_id()
        return f"{capture}.{self.record.state()['revision']}" if capture else None

    @property
    def selection(self):
        """The verified selection as the feed keeps it (a copy), or None."""
        return self.record.state()["selection"]

    @contextmanager
    def _navigation_guard(self):
        # The controller enters this around every navigation and every
        # calibration that moves the Roku: what it shows is about to change.
        # The feed forgets the selection and moves the revision before the first key.
        op_id = uuid.uuid4().hex
        self.record.begin_navigation(op_id)
        mine = getattr(self._local, "navigations", None)
        if mine is not None:
            mine.append(op_id)      # _navigate ends it, with what it verifies
            yield
            return
        try:
            yield
        finally:
            self.record.end_navigation(op_id, None)

    # -- targets ----------------------------------------------------------
    def normalize(self, body):
        """Validate a target; returns (target, game). Games are looked up now."""
        if not isinstance(body, dict):
            raise ConfigError("JSON object required")
        source = body.get("source_id", body.get("source"))
        if source not in SOURCES:
            raise ConfigError("source must be one of " + ", ".join(SOURCES))
        if source == MANUAL:
            return {"source_id": MANUAL, "channel_id": "manual"}, None
        channel = body.get("channel_id")
        if isinstance(channel, int) and not isinstance(channel, bool):
            channel = str(channel)
        if not isinstance(channel, str) or not channel or len(channel) > 200:
            raise ConfigError("channel_id is required: a YouTube TV channel or a game ID")
        if source == YOUTUBE_TV:
            self.reload_channels()
            _, resolved = self.config.resolve_channel(channel)
            return {"source_id": YOUTUBE_TV, "channel_id": resolved.name}, None
        if not channel.isascii() or not channel.isdecimal() or int(channel) <= 0:
            raise ConfigError("A game's channel_id must be a positive game ID")
        target = {"source_id": source, "channel_id": channel, "date": _iso_date(body.get("date"))}
        if source == MLB:
            game = self.baseball.game(int(channel), target["date"])
            route = game.get("preferred_source") or {}
            if route.get("provider") not in ("youtube_tv", "mlb"):
                raise ConfigError("No controllable source is listed for this game")
            return target, game
        if source == NHL:
            game = self.sports.game("nhl", int(channel), target["date"])
            if nhl_on_espn_unlimited(game) is None:
                raise ConfigError("This NHL game is not on ESPN Unlimited; watch its channel instead")
            return target, game
        game = self.sports.game("nfl", int(channel), target["date"])
        if source == THURSDAY_NIGHT_FOOTBALL:
            from roku_control.prime_video import prime_nfl_game
            if not prime_nfl_game(game):
                raise ConfigError("This NFL game is not listed on Prime Video")
        return target, game

    # -- selection --------------------------------------------------------
    def _fresh(self, target):
        """The verified selection is target, recent, and on this capture."""
        selection = self.selection
        return bool(selection and not selection.get("manual") and same_target(selection, target)
                    and selection.get("capture_id") == self.capture_id()
                    and self._wall() - selection["verified_at"] <= self.reuse_max_age)

    def _running(self):
        return bool(self.operation and self.operation["state"] == "running")

    @staticmethod
    def _key(target):
        return target["source_id"], target["channel_id"], target.get("date")

    def _blackout(self, target):
        """The message of a recent blackout of target, or None."""
        with self._lock:
            at, message = self._blackouts.get(self._key(target), (None, None))
        return message if at is not None and self._wall() - at < BLACKOUT_SECONDS else None

    def _set_manual(self, message):
        with self._lock:
            if not (self.selection and self.selection.get("manual")):
                op_id = uuid.uuid4().hex
                self.record.begin_navigation(op_id)
                self.record.end_navigation(op_id, {"source_id": MANUAL, "channel_id": "manual", "manual": True,
                                                   "name": "Manual Roku", "since": self._wall(), "note": message})

    def tune(self, body):
        """Put target on the Roku. Returns (status, payload).

        200 when nothing had to move (``reused``), 202 with an operation when
        the Roku navigates, 409 when ``navigate`` is false and the Roku does not
        already show target.
        """
        target, game = self.normalize(body)
        force = body.get("force") is True
        navigate = body.get("navigate", True) is not False
        resume = body.get("resume", True) is not False
        with self._tune_lock:
            if target["source_id"] == MANUAL:
                self._stop_operation()
                self._set_manual("Selected by a remote")
                return 200, {"reused": False, "selection": self.selection_view()}
            blackout = None if force else self._blackout(target)
            if blackout is not None:
                return 409, {"error": blackout, "code": SundayTicketBlackoutError.code}
            already = None if force else self._already(target, resume)
            if already is not None:
                return already
            if not navigate:
                return 409, {"error": "The Roku is not showing that verified selection",
                             "code": "not_prepared"}
            self._stop_operation()
            return 202, {"reused": False, "operation": self._start(target, game)}

    def watch(self, body):
        """Serve target only if nothing has to move. Returns (status, payload), or None.

        Anyone may watch a locked Roku: what it already shows, or is already
        on its way to, is served like tune serves it. Never presses a key;
        None means target would need a navigation.
        """
        target, _ = self.normalize(body)
        with self._tune_lock:
            if target["source_id"] == MANUAL:
                with self._lock:
                    manual = bool(self.selection and self.selection.get("manual") and not self._running())
                return (200, {"reused": True, "selection": self.selection_view()}) if manual else None
            return self._already(target, body.get("resume", True) is not False)

    def _already(self, target, resume):
        """tune's answer when target needs no navigation, or None (call with _tune_lock held).

        The Roku shows target, freshly verified; is navigating to it; or still
        plays it by the key-free check.
        """
        with self._lock:
            if self._fresh(target):
                return 200, {"reused": True, "selection": self.selection_view()}
            if self._running() and same_target(self.operation["target"], target):
                return 202, {"reused": False, "operation": self.operation_view()}
        if resume and self._resumable(target):
            check = self.check()
            if check["playing"]:
                return 200, {"reused": True, "resumed": True, "selection": self.selection_view()}
        return None

    def _resumable(self, target):
        with self._lock:
            selection = self.selection
            return bool(selection and not selection.get("manual") and same_target(selection, target)
                        and not self._running())

    def _start(self, target, game):
        operation = {"id": uuid.uuid4().hex, "target": copy.deepcopy(target), "state": "running",
                     "phase": "accepted", "message": "Accepted", "error": None,
                     "started_at": self._wall(), "finished_at": None, "controller_id": None}
        with self._lock:
            self.operation = operation
        thread = threading.Thread(target=self._navigate, args=(operation, target, game),
                                  name=f"roku-{operation['id'][:8]}", daemon=True)
        self._thread = thread
        thread.start()
        return self.operation_view()

    def _navigate(self, operation, target, game):
        source = target["source_id"]
        self._local.navigations = navigations = []
        try:
            if source == SUNDAY_TICKET:
                result = self.controller.play_sunday_ticket(game)
            elif source == THURSDAY_NIGHT_FOOTBALL:
                result = self.controller.play_prime_game(game)
            elif source == MLB:
                result = self.controller.play_baseball_game(game)
            elif source == NHL:
                result = self.controller.play_espn_game(game)
            else:
                result = self.controller.play(target["channel_id"])
            selection = self._verified(target, result)
        except CueCancelledError as exc:
            self._finish(operation, "cancelled", str(exc))
        except Exception as exc:  # The operation records it; the lane stays usable.
            if isinstance(exc, SundayTicketBlackoutError):
                with self._lock:
                    self._blackouts[self._key(target)] = (self._wall(), str(exc))
            self._finish(operation, "failed", str(exc), code=getattr(exc, "code", "navigation_failed"))
        else:
            with self._lock:
                self._blackouts.pop(self._key(target), None)
                # Stored only if this is still the operation and nothing moved the Roku
                # (another navigation, a capture restart) since its keys began.
                stored = (self.operation is operation and bool(navigations)
                          and self.record.end_navigation(navigations[-1], selection))
            if stored:
                self._finish(operation, "succeeded", f"{selection['name']} is playing")
            else:
                self._finish(operation, "failed", "The Roku changed before its selection was verified",
                             code="navigation_failed")
        finally:
            for op_id in navigations:
                self.record.end_navigation(op_id, None)
            self._local.navigations = None

    def _verified(self, target, result):
        """Accept a controller result only with visual and player evidence."""
        verification = result.get("verification") or {}
        if result.get("ok") is not True or verification.get("player_state") != "play":
            raise NavigationError("Roku playback was not verified")
        selected = (verification.get("selected_prime_event") if target["source_id"] == THURSDAY_NIGHT_FOOTBALL
                    else verification.get("visible_channel_template") or verification.get("selected_game_card"))
        if (not isinstance(selected, dict) or selected.get("matched") is not True
                or not verification.get("playback_evidence")):
            raise NavigationError("Roku selection lacks visual verification")
        capture = self.feedback.last_capture or {}
        current = self.capture_id()
        if not capture.get("fresh") or capture.get("stream_id") != current:
            raise NavigationError("Roku capture changed during verification")
        try:
            active = self.ecp.active_app()
            app = {"id": active.id, "name": active.name} if active.id else None
        except EcpError:
            app = None  # Only means a later recovery navigates instead of resuming.
        return {**target, "manual": False,
                "name": result.get("game") or result.get("channel") or target["channel_id"],
                "verified_at": self._wall(), "capture_id": current, "app": app,
                "operation_id": result.get("operation_id"),
                "resolved_source": {"provider": result.get("provider", "youtube_tv"),
                                    "label": result.get("source") or result.get("channel")
                                    or target["channel_id"]}}

    def _finish(self, operation, state, message, code=None):
        with self._lock:
            operation.update(state=state, message=message, finished_at=self._wall(),
                             error={"code": code, "message": message} if state == "failed" else None)

    def _stop_operation(self, timeout=20.0):
        """Cancel a running navigation and wait for it to let go of the Roku."""
        with self._lock:
            running = self._running()
            thread = self._thread
        if running:
            self.controller.cancel_operation()
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                raise ControlBusyError("The Roku is still finishing its last navigation")

    def cancel(self):
        with self._lock:
            running = self._running()
        if running:
            self.controller.cancel_operation()
        return {"cancelled": running, "operation": self.operation_view()}

    # -- the still-playing check -------------------------------------------
    def check(self):
        """Whether the Roku still plays its verified selection, briefly checked.

        Passing renews the verification on the current capture without any
        navigation. Never presses a key.
        """
        state = self.record.state()
        selection = state["selection"]
        with self._lock:
            running = self._running()
        if running:
            return {"playing": False, "reason": "a navigation is running"}
        if not selection or selection.get("manual"):
            return {"playing": False, "reason": "nothing verified is on the Roku"}
        why = self._not_playing(selection)
        if why is not None:
            return {"playing": False, "reason": why}
        if not self.record.renew_selection(state["selection_version"],
                                           {"verified_at": self._wall(), "capture_id": self.capture_id()}):
            return {"playing": False, "reason": "the Roku changed during the check"}
        return {"playing": True, "selection": self.selection_view()}

    def _not_playing(self, selection):
        app = selection.get("app")
        if not app:
            return "the app it was verified in is unknown"
        try:
            active = self.ecp.active_app()
            if active.id != app["id"]:
                return f"the Roku is in {active.name or 'its home screen'}, not {app['name']}"
            player = self.ecp.media_player()
        except EcpError as exc:
            return f"the Roku did not answer ({exc})"
        plugin = player.get("plugin") if isinstance(player.get("plugin"), dict) else {}
        if player.get("state") != "play" or player.get("error") == "true" or plugin.get("id") != app["id"]:
            return f"its player is {player.get('state') or 'stopped'}"
        before = None
        deadline = self._clock() + self.playing_seconds
        while True:
            try:
                frame = self.feedback.observe()
            except RokuStreamError as exc:
                return f"the capture card shows no picture ({exc})"
            if before is not None and moved(before, frame):
                return None
            before = frame
            if self._clock() >= deadline:
                return "its picture did not move"
            self._sleep(.25)

    # -- the remote ---------------------------------------------------------
    @contextmanager
    def _remote(self):
        if self._running():
            raise ControlBusyError("The Roku is navigating; stop it before using the remote")
        if not self.controller._lock.acquire(blocking=False):
            raise ControlBusyError("The Roku is busy")
        try:
            self._set_manual("Moved by the remote")
            yield
        finally:
            self.controller._lock.release()

    def keypress(self, key):
        if key not in REMOTE_KEYS:
            raise ConfigError("key must be one of " + ", ".join(REMOTE_KEYS))
        with self._remote():
            self.ecp.keypress(key)
        return {"key": key, "selection": self.selection_view()}

    def text(self, text):
        if (not isinstance(text, str) or not text or len(text) > TEXT_LIMIT
                or not text.isprintable()):
            raise ConfigError(f"text must be 1 to {TEXT_LIMIT} printable characters")
        with self._remote():
            for character in text:
                # ECP literal keys; EcpClient percent-encodes the character.
                self.ecp.keypress("Lit_" + character)
        return {"sent": len(text), "selection": self.selection_view()}

    def apps(self):
        now = self._clock()
        if self._apps["at"] is None or now - self._apps["at"] > APPS_CACHE_SECONDS:
            apps = [{"id": app.id, "name": app.name} for app in self.ecp.apps() if app.id]
            self._apps = {"at": now, "data": apps}
        return copy.deepcopy(self._apps["data"])

    def launch(self, app_id):
        if not isinstance(app_id, str) or app_id not in {app["id"] for app in self.apps()}:
            raise ConfigError("app_id must be an app installed on the Roku")
        with self._remote():
            self.ecp.launch(app_id)
        return {"launched": app_id, "selection": self.selection_view()}

    # -- what the Roku reports ------------------------------------------------
    def player(self):
        """ECP's active app and player, refreshed in the background.

        Status documents are polled often; a slow or absent Roku must never
        hold a status request for an ECP timeout.
        """
        now = self._clock()
        stale = self._player["at"] is None or now - self._player["at"] > PLAYER_CACHE_SECONDS
        if stale and not self._player_refreshing:
            self._player_refreshing = True
            threading.Thread(target=self._refresh_player, name="roku-player", daemon=True).start()
        view = copy.deepcopy(self._player)
        view["age_seconds"] = round(now - view["at"], 1) if view["at"] is not None else None
        view.pop("at")
        return view

    def _refresh_player(self):
        try:
            active = self.ecp.active_app()
            data = {"app": {"id": active.id, "name": active.name} if active.id else None,
                    "media_player": self.ecp.media_player()}
            self._player = {"at": self._clock(), "data": data, "error": None}
        except EcpError as exc:
            self._player = {"at": self._clock(), "data": None, "error": str(exc)}
        finally:
            self._player_refreshing = False

    # -- views ----------------------------------------------------------------
    def selection_view(self):
        selection = self.selection
        if selection is None:
            return None
        if not selection.get("manual"):
            selection["age_seconds"] = round(self._wall() - selection["verified_at"], 1)
            selection["reusable"] = (selection["capture_id"] == self.capture_id()
                                     and selection["age_seconds"] <= self.reuse_max_age)
        # Present selections like the other lanes do: an "id" to match on.
        selection["id"] = selection["channel_id"]
        return selection

    def operation_view(self):
        with self._lock:
            operation = copy.deepcopy(self.operation)
        if operation is None:
            return None
        if operation["state"] == "running":
            progress = self.controller.operation_status()
            operation.update(phase=progress.get("phase"), message=progress.get("message"),
                             app_state=progress.get("app_state"))
        operation["active"] = operation["state"] == "running"
        return operation

    def payload(self):
        return {"selection": self.selection_view(), "operation": self.operation_view(),
                "generation": self.generation(), "player": self.player(),
                "reuse_max_age_seconds": self.reuse_max_age}

    # -- catalogs -------------------------------------------------------------
    def source_list(self, available):
        return [{"id": source_id, "kind": source_id, "lane": LANE, "name": name,
                 "items": kind, "available": available,
                 "selector": "channel_id" if source_id != MANUAL else None,
                 "dated": source_id in GAME_SOURCES,
                 # First pitch or kickoff to the final, as the schedule projects it.
                 "typical_seconds": (int(TYPICAL[GAME_LEAGUES[source_id]].total_seconds())
                                     if source_id in GAME_LEAGUES else None),
                 "items_path": f"/api/v1/sources/{source_id}"}
                for source_id, (name, kind) in SOURCES.items()]

    def listed_source_list(self, available):
        """LISTED_SOURCES in source_list's shape, with ``tunable: false``."""
        return [{"id": source_id, "kind": source_id, "lane": LANE, "name": name, "items": kind,
                 "available": available, "tunable": False, "selector": None, "dated": True,
                 "typical_seconds": int(TYPICAL[GAME_LEAGUES[source_id]].total_seconds()),
                 "items_path": f"/api/v1/sources/{source_id}"}
                for source_id, (name, kind) in LISTED_SOURCES.items()]

    def catalog(self, source_id, requested_date=None):
        if source_id == MANUAL:
            return {"items": [{"id": "manual", "name": "Manual Roku",
                               "note": "Drive the Roku yourself with the on-screen remote"}]}
        if source_id == YOUTUBE_TV:
            self.reload_channels()
            return {"items": [{"id": channel.name, "name": channel.name, "number": None,
                               "aliases": list(channel.aliases), "guide_index": channel.guide_index}
                              for channel in self.config.channels]}
        day = _iso_date(requested_date)
        if source_id == MLB:
            catalog = self.baseball.games(day)
            games = catalog["games"]
            items = [{"id": str(game["game_pk"]), "name": game["title"], "start_time": game.get("start_time"),
                      "state": str(game.get("state") or "").lower(), "status": game.get("status"),
                      "playable": bool(game.get("preferred_source")), "route": game.get("preferred_source"),
                      "game": game} for game in games]
            return {"date": catalog["date"], "items": items}
        if source_id == NHL:
            catalog = self.sports.games("nhl", day)
            items = []
            for game in catalog["games"]:
                stream = nhl_on_espn_unlimited(game)
                if stream is None:
                    continue  # On ESPN, ABC, TNT or NHL Network: in the guide there.
                state = {"pre": "preview", "in": "live", "post": "final"}.get(game.get("state"), game.get("state"))
                items.append({"id": str(game["game_id"]), "name": game["title"],
                              "start_time": game.get("start_time"), "state": state,
                              "status": game.get("status"), "playable": state != "final",
                              "espn_unlimited": stream, "game": game})
            return {"date": catalog["date"], "items": items}
        catalog = self.sports.games("nfl", day)
        items = []
        for game in catalog["games"]:
            if source_id == THURSDAY_NIGHT_FOOTBALL and not game.get("prime_video_target"):
                continue
            if source_id == SUNDAY_TICKET and game.get("on_sunday_ticket") is False:
                continue  # A national exclusive: Sunday Ticket does not carry it.
            state = {"pre": "preview", "in": "live", "post": "final"}.get(game.get("state"), game.get("state"))
            items.append({"id": str(game["game_id"]), "name": game["title"],
                          "start_time": game.get("start_time"), "state": state,
                          "status": game.get("status"), "playable": state != "final",
                          "game": game})
        return {"date": catalog["date"], "items": items}


def load(config_path, *, capture_source, record=None, reuse_max_age=900.0, ecp=None, feedback_factory=None):
    """Build the lane from roku.json, or return None when it is not configured."""
    from dataclasses import replace
    from pathlib import Path

    from roku_control.baseball import BaseballCatalog
    from roku_control.config import load_config
    from roku_control.controller import RokuStreamController
    from roku_control.lane_feedback import LaneCaptureFeedback
    from roku_control.sports import SportsCatalog
    import yttv_epg

    if not Path(config_path).is_file():
        return None
    config = load_config(config_path)
    # Frames from the lane are always fresh, whatever the file says.
    config = replace(config, feedback=replace(config.feedback, backend="lane"))
    feedback = (feedback_factory or LaneCaptureFeedback)(config.feedback, capture_source)
    controller = RokuStreamController(config, ecp=ecp, feedback=feedback)
    controller.recover_operations()
    baseball = BaseballCatalog(config)
    # YouTube TV's own (Lansing's) listings say which regional NFL games its CBS and FOX show.
    sports = SportsCatalog(config, baseball, listings=lambda: (yttv_epg.load_guide() or {}).get("channels"))
    return RokuLane(config, controller, baseball=baseball, sports=sports,
                    capture_source=capture_source, record=record, reuse_max_age=reuse_max_age,
                    config_path=config_path)

