"""The Roku lane's selection, generation, navigation and remote. No hardware."""
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import roku_lane
from roku_control.config import ChannelConfig
from roku_control.errors import (ChannelNotFoundError, ConfigError, ControlBusyError,
                                 CueCancelledError, EcpError, SundayTicketBlackoutError)
from roku_control.feedback import PpmFrame

YOUTUBE_TV = SimpleNamespace(id="195316", name="YouTube TV")


class FakeEcp:
    base_url = "http://roku.test:8060"

    def __init__(self):
        self.keys, self.launches = [], []
        self.app = YOUTUBE_TV
        self.state = "play"
        self.slow = None

    def active_app(self):
        if self.slow:
            self.slow.wait(5)
        return self.app

    def media_player(self):
        return {"state": self.state, "plugin": {"id": self.app.id}}

    def keypress(self, key):
        self.keys.append(key)

    def launch(self, app_id):
        self.launches.append(app_id)

    def apps(self):
        return [SimpleNamespace(id="195316", name="YouTube TV"), SimpleNamespace(id="14", name="MLB")]


class FakeFeedback:
    def __init__(self, capture):
        self.capture = capture
        self.last_capture = None
        self.motion = True
        self.observed = 0

    def observe(self):
        self.observed += 1
        shade = self.observed % 2 * 200 if self.motion else 50
        return PpmFrame(4, 4, bytes([shade]) * 48)


class FakeController:
    def __init__(self, capture):
        self.capture = capture
        self.feedback = FakeFeedback(capture)
        self.ecp = FakeEcp()
        self._lock = threading.Lock()
        self.navigation_guard = nullcontext
        self.plays = []
        self.cancelled = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.evidence = True

    def _play(self, label, provider="youtube_tv"):
        with self._lock, self.navigation_guard():
            self.plays.append(label)
            self.release.wait(5)
            if self.cancelled.is_set():
                self.cancelled.clear()
                raise CueCancelledError("stopped")
            self.feedback.last_capture = {"fresh": True, "stream_id": self.capture["stream_id"]}
            template = {"matched": self.evidence}
            return {"ok": True, "operation_id": "op-" + label, "channel": label, "provider": provider,
                    "verification": {"player_state": "play", "visible_channel_template": template,
                                     "selected_game_card": template, "playback_evidence": ["play"]}}

    def play(self, name):
        return self._play(name)

    def play_baseball_game(self, game):
        return self._play(game["title"], "mlb")

    def play_sunday_ticket(self, game):
        return self._play(game["title"], "sunday_ticket")

    def play_espn_game(self, game):
        return self._play(game["title"], "espn_unlimited")

    def cancel_operation(self):
        self.cancelled.set()
        self.release.set()

    def operation_status(self):
        return {"phase": "navigating", "message": "Moving", "app_state": None}


class Config:
    channels = (ChannelConfig("NBC", ("WRC",)), ChannelConfig("ESPN", guide_index=2))

    def resolve_channel(self, name):
        for index, channel in enumerate(self.channels):
            if name.casefold() in (channel.name.casefold(), *(a.casefold() for a in channel.aliases)):
                return index, channel
        raise ChannelNotFoundError(f"Unknown YouTube TV channel: {name!r}")


GAME = {"game_pk": 823254, "title": "Phillies at Braves", "state": "Live",
        "preferred_source": {"provider": "youtube_tv", "channel": "ESPN"}}
NFL = {"league": "nfl", "game_id": 401872964, "title": "Steelers at Browns", "state": "in", "status": "In Progress",
       "start_time": "2026-10-02T00:15Z", "prime_video_target": {"source_id": "thursday-night-football"},
       "broadcasts": [{"name": "Prime Video", "market": "national"}],
       "on_sunday_ticket": False}


@pytest.fixture
def lane():
    capture = {"running": True, "stream_id": "cap-1", "tap_url": "udp://127.0.0.1:1"}
    controller = FakeController(capture)
    baseball = SimpleNamespace(game=lambda pk, day: dict(GAME),
                               games=lambda day: {"date": day, "games": [dict(GAME)]})
    sports = SimpleNamespace(game=lambda league, game_id, day: dict(NFL),
                             games=lambda league, day: {"date": day, "games": [dict(NFL)]})
    lane = roku_lane.RokuLane(Config(), controller, baseball=baseball, sports=sports,
                              capture_source=lambda: dict(capture), playing_seconds=.5,
                              sleep=lambda _: None)
    lane.capture = capture
    return lane


def settle(lane):
    deadline = time.monotonic() + 5
    while lane.operation and lane.operation["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(.01)
    return lane.operation_view()


def tune(lane, **body):
    return lane.tune({"source_id": "youtube-tv", "channel_id": "ESPN", **body})


def test_navigation_verifies_then_reuses_without_moving(lane):
    before = lane.generation()
    status, result = tune(lane, channel_id="espn")
    assert status == 202 and result["operation"]["target"]["channel_id"] == "ESPN"
    operation = settle(lane)
    assert operation["state"] == "succeeded"
    selection = lane.selection_view()
    assert selection["channel_id"] == "ESPN" and selection["reusable"] is True
    assert selection["app"] == {"id": "195316", "name": "YouTube TV"}
    assert lane.generation() != before and lane.generation().startswith("cap-1.")
    status, again = tune(lane)
    assert status == 200 and again["reused"] is True
    assert lane.controller.plays == ["ESPN"]


def test_navigation_start_clears_the_selection_and_changes_generation(lane):
    tune(lane)
    settle(lane)
    verified = lane.generation()
    lane.controller.release.clear()
    tune(lane, channel_id="NBC")
    deadline = time.monotonic() + 2
    while lane.selection is not None and time.monotonic() < deadline:
        time.sleep(.01)
    assert lane.selection is None
    assert lane.generation() != verified
    lane.controller.release.set()
    settle(lane)
    assert lane.selection_view()["channel_id"] == "NBC"


def test_capture_restart_resumes_after_the_still_playing_check(lane):
    tune(lane)
    settle(lane)
    verified = lane.generation()
    lane.capture["stream_id"] = "cap-2"
    assert lane.generation() != verified  # A lease on the old capture fails closed.
    assert lane.selection_view()["reusable"] is False
    status, result = tune(lane)
    assert status == 200 and result == {"reused": True, "resumed": True, "selection": result["selection"]}
    assert result["selection"]["capture_id"] == "cap-2"
    assert lane.controller.plays == ["ESPN"]


def test_a_blacked_out_game_is_refused_without_navigating_again(lane):
    def blacked_out(game):
        lane.controller.plays.append(game["title"])
        raise SundayTicketBlackoutError(f"{game['title']} is blacked out on Sunday Ticket: it is airing on FOX")
    lane.controller.play_sunday_ticket = blacked_out
    body = {"source_id": "sunday-ticket", "channel_id": "401872964", "date": "2026-10-18"}
    assert lane.tune(dict(body))[0] == 202
    operation = settle(lane)
    assert operation["state"] == "failed" and operation["error"]["code"] == "sunday_ticket_blackout"
    status, refused = lane.tune(dict(body))  # vizelVision's retry: no second trip through the guide.
    assert status == 409 and refused["code"] == "sunday_ticket_blackout" and "airing on FOX" in refused["error"]
    assert lane.controller.plays == ["Steelers at Browns"]
    assert lane.tune({**body, "date": "2026-10-25"})[0] == 202  # Another week's game is its own target.
    settle(lane)
    assert lane.tune({**body, "force": True})[0] == 202
    settle(lane)
    assert len(lane.controller.plays) == 3
    wall = lane._wall
    lane._wall = lambda: wall() + roku_lane.BLACKOUT_SECONDS + 1
    assert lane.tune(dict(body))[0] == 202  # Expired: the market may have moved.
    settle(lane)
    lane.controller.play_sunday_ticket = lambda game: lane.controller._play(game["title"], "sunday_ticket")
    assert lane.tune({**body, "force": True})[0] == 202
    assert settle(lane)["state"] == "succeeded"
    assert lane._blackout(lane.normalize(dict(body))[0]) is None  # A success forgets the failure.


def test_a_failed_check_navigates(lane):
    tune(lane)
    settle(lane)
    lane.capture["stream_id"] = "cap-2"
    lane.controller.feedback.motion = False
    check = lane.check()
    assert check == {"playing": False, "reason": "its picture did not move"}
    lane.controller.ecp.app = SimpleNamespace(id=None, name="")
    assert "home screen" in lane.check()["reason"]
    status, _ = tune(lane)
    assert status == 202
    settle(lane)
    assert lane.controller.plays == ["ESPN", "ESPN"]


def test_navigate_false_never_moves_the_roku(lane):
    status, result = tune(lane, navigate=False)
    assert status == 409 and result["code"] == "not_prepared"
    assert lane.controller.plays == []


def test_force_navigates_even_when_verified(lane):
    tune(lane)
    settle(lane)
    status, _ = tune(lane, force=True)
    assert status == 202
    settle(lane)
    assert lane.controller.plays == ["ESPN", "ESPN"]


def test_selection_without_visual_evidence_is_refused(lane):
    lane.controller.evidence = False
    tune(lane)
    operation = settle(lane)
    assert operation["state"] == "failed"
    assert "visual verification" in operation["error"]["message"]
    assert lane.selection is None


def test_capture_restart_during_navigation_is_refused(lane):
    lane.controller.release.clear()
    tune(lane)
    time.sleep(.05)
    lane.capture["stream_id"] = "cap-9"
    lane.controller.feedback.last_capture = None
    lane.controller.release.set()

    # The fake records evidence on the capture current at verification, so
    # simulate evidence from the old reader.
    original = lane.controller._play

    def stale(label, provider="youtube_tv"):
        result = original(label, provider)
        lane.controller.feedback.last_capture = {"fresh": True, "stream_id": "cap-1"}
        return result
    lane.controller._play = stale
    tune(lane, force=True)
    operation = settle(lane)
    assert operation["state"] == "failed" and "capture changed" in operation["error"]["message"]


def test_cancel_stops_the_navigation(lane):
    lane.controller.release.clear()
    tune(lane)
    result = lane.cancel()
    assert result["cancelled"] is True
    assert settle(lane)["state"] == "cancelled"
    assert lane.selection is None


def test_remote_switches_to_manual_and_bumps_generation(lane):
    tune(lane)
    settle(lane)
    verified = lane.generation()
    lane.keypress("Down")
    assert lane.controller.ecp.keys == ["Down"]
    assert lane.selection_view()["manual"] is True
    manual = lane.generation()
    assert manual != verified
    lane.keypress("Select")
    assert lane.generation() == manual  # Already manual: one change, not one per key.
    lane.text("news")
    assert lane.controller.ecp.keys[2:] == ["Lit_n", "Lit_e", "Lit_w", "Lit_s"]
    lane.launch("14")
    assert lane.controller.ecp.launches == ["14"]
    with pytest.raises(ConfigError):
        lane.keypress("PowerOff")
    with pytest.raises(ConfigError):
        lane.launch("99999")
    with pytest.raises(ConfigError):
        lane.text("x" * 65)


def test_remote_waits_for_a_navigation(lane):
    lane.controller.release.clear()
    tune(lane)
    with pytest.raises(ControlBusyError):
        lane.keypress("Back")
    assert lane.controller.ecp.keys == []
    lane.controller.release.set()
    settle(lane)


def test_manual_tune_cancels_a_navigation(lane):
    lane.controller.release.clear()
    tune(lane)
    status, result = lane.tune({"source_id": "roku-manual"})
    assert status == 200 and result["selection"]["manual"] is True
    assert lane.operation["state"] == "cancelled"


def test_targets_are_validated_and_games_resolved(lane):
    with pytest.raises(ConfigError):
        lane.normalize({"source_id": "netflix", "channel_id": "x"})
    with pytest.raises(ChannelNotFoundError):
        lane.normalize({"source_id": "youtube-tv", "channel_id": "HBO"})
    assert lane.normalize({"source_id": "youtube-tv", "channel_id": "wrc"})[0] == \
        {"source_id": "youtube-tv", "channel_id": "NBC"}
    for invalid in ("B0GZ57JWXN", "0", "-1", "１２"):
        with pytest.raises(ConfigError):
            lane.normalize({"source_id": "mlb", "channel_id": invalid, "date": "2026-10-01"})
    with pytest.raises(ConfigError):
        lane.normalize({"source_id": "mlb", "channel_id": "823254", "date": "10/01/2026"})
    target, game = lane.normalize({"source_id": "mlb", "channel_id": 823254, "date": "2026-10-01"})
    assert target == {"source_id": "mlb", "channel_id": "823254", "date": "2026-10-01"}
    assert game["title"] == "Phillies at Braves"
    target, _ = lane.normalize({"source_id": "thursday-night-football", "channel_id": "401872964",
                                "date": "2026-10-01"})
    assert target["source_id"] == "thursday-night-football"
    lane.sports.game = lambda *args: {**NFL, "broadcasts": []}
    lane.normalize({"source_id": "sunday-ticket", "channel_id": "401872964", "date": "2026-10-04"})


def test_game_navigation_uses_the_game_route(lane):
    status, _ = lane.tune({"source_id": "mlb", "channel_id": "823254", "date": "2026-10-01"})
    assert status == 202
    settle(lane)
    assert lane.controller.plays == ["Phillies at Braves"]
    selection = lane.selection_view()
    assert selection["date"] == "2026-10-01"
    assert selection["resolved_source"]["provider"] == "mlb"


def test_catalogs(lane):
    channels = lane.catalog("youtube-tv")["items"]
    assert [item["id"] for item in channels] == ["NBC", "ESPN"]
    assert channels[0]["aliases"] == ["WRC"]
    games = lane.catalog("mlb", "2026-10-01")
    assert games["items"][0]["id"] == "823254" and games["items"][0]["playable"]
    tnf = lane.catalog("thursday-night-football", "2026-10-01")["items"]
    assert [item["id"] for item in tnf] == ["401872964"] and tnf[0]["state"] == "live"
    # A national exclusive is not on Sunday Ticket.
    assert lane.catalog("sunday-ticket", "2026-10-01")["items"] == []
    assert lane.catalog("roku-manual")["items"][0]["id"] == "manual"
    sources = {source["id"]: source for source in lane.source_list(True)}
    assert set(sources) == set(roku_lane.SOURCES)
    assert sources["nhl"]["dated"] and sources["nhl"]["name"] == "NHL on ESPN Unlimited"
    assert sources["mlb"]["dated"] and not sources["youtube-tv"]["dated"]
    assert all(source["lane"] == "roku" for source in sources.values())


def _nhl(game_id, away, home, *broadcasts, state="pre"):
    """An NHL game as ESPN's scoreboard normalizes it; broadcasts are (market, name)."""
    return {"league": "nhl", "game_id": game_id, "title": f"{away} at {home}",
            "start_time": "2026-10-20T23:00Z", "state": state, "status": "Scheduled",
            "away": {"abbreviation": away}, "home": {"abbreviation": home},
            "broadcasts": [{"name": name, "market": market} for market, name in broadcasts]}


NHL_SLATE = [
    _nhl(1, "LA", "WSH", ("national", "ESPN+"), ("home", "MNMT"), ("away", "ABTV")),
    _nhl(2, "BOS", "DAL", ("national", "ESPN")),
    _nhl(3, "CHI", "DET", ("national", "Disney+"), ("national", "ESPN+"), ("national", "Hulu")),
    _nhl(4, "TOR", "EDM", ("national", "ESPN+")),
    _nhl(5, "PHI", "WSH", ("national", "TNT"), ("away", "NBC Sports Phil +")),
]


def test_espn_unlimited_carries_the_out_of_market_games_and_the_exclusives():
    from roku_control.sports import nhl_on_espn_unlimited
    caps, national, exclusive, canadian, tnt = map(nhl_on_espn_unlimited, NHL_SLATE)
    assert caps == {"exclusive": False, "blackout": {"WSH": "DC"}, "home": ["MNMT"], "away": ["ABTV"]}
    # On ESPN or TNT: in the guide on that channel, not on ESPN Unlimited.
    assert national is None and tnt is None
    # An exclusive is blacked out nowhere, a Red Wings home game included.
    assert exclusive["exclusive"] and exclusive["blackout"] == {}
    # Two Canadian teams have no US broadcast, and are out of market all the same.
    assert canadian == {"exclusive": False, "blackout": {}, "home": [], "away": []}


def test_the_nhl_catalog_lists_espn_unlimited_games_and_the_roku_finds_them(lane):
    by_id = {game["game_id"]: game for game in NHL_SLATE}
    lane.sports = SimpleNamespace(
        games=lambda league, day: {
            "date": day, "games": [dict(game) for game in NHL_SLATE] if league == "nhl" else []},
        game=lambda league, game_id, day: dict(by_id[game_id]))
    items = lane.catalog(roku_lane.NHL, "2026-10-20")["items"]
    assert [item["id"] for item in items] == ["1", "3", "4"]
    assert all(item["playable"] for item in items)
    assert items[0]["espn_unlimited"]["blackout"] == {"WSH": "DC"}
    assert items[0]["state"] == "preview"
    # A game on ESPN itself is its channel's, not ESPN Unlimited's.
    with pytest.raises(ConfigError):
        lane.tune({"source_id": roku_lane.NHL, "channel_id": "2", "date": "2026-10-20"})
    status, _ = lane.tune({"source_id": roku_lane.NHL, "channel_id": "1", "date": "2026-10-20"})
    assert status == 202
    settle(lane)
    assert lane.controller.plays == ["LA at WSH"]
    selection = lane.selection_view()
    assert (selection["source_id"], selection["channel_id"]) == ("nhl", "1")
    assert selection["resolved_source"]["provider"] == "espn_unlimited"


def test_status_never_waits_for_a_slow_roku(lane):
    lane.controller.ecp.slow = threading.Event()
    started = time.monotonic()
    player = lane.player()
    assert time.monotonic() - started < .5
    assert player["data"] is None
    lane.controller.ecp.slow.set()
    deadline = time.monotonic() + 2
    while lane.player()["data"] is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert lane.player()["data"]["app"]["id"] == "195316"


def test_unreachable_roku_is_reported_not_raised(lane):
    def unreachable():
        raise EcpError("Cannot reach Roku ECP")
    lane.controller.ecp.active_app = unreachable
    lane._refresh_player()
    assert lane.player()["error"] == "Cannot reach Roku ECP"
