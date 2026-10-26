"""TV Thing's feed: the media plane (see INVARIANTS.md).

What the hardware is and which lanes run on it (feed.json), and the state the feed owns:

* its **epoch**, new every time the feed starts;
* each lane's **revision** and **lifecycle** (idle, starting, running, stopping, error). The
  revision moves when, and only when, what the lane outputs or its lifecycle changes;
* the **commands** applied to a lane: each `op_id` at most once, and only at the
  `(epoch, revision)` it was issued against. A stale command is refused (`Conflict`), so a
  retry after a crash, or one delayed past a feed restart, can never retune twice;
* each lane's **navigation** and **selection** (the Roku's): a navigation moves the revision
  before its first key, and the selection it verified is stored here, so a control restart
  reads both back and the Roku's generation, ``<capture id>.<revision>``, stays put.

The processes themselves still live in server.py, which reports each change here. They move
into the feed lane by lane (the plan's Phase 1).
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

KINDS = ("dvb", "analog", "capture")
LIFECYCLES = ("idle", "starting", "running", "stopping", "error")
# Completed commands remembered for replies to retries. A retry of one forgotten since is
# still refused: the command moved the lane's revision on.
OP_MEMORY = 1000
OP_MEMORY_SECONDS = 24 * 3600
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class FeedConfigError(ValueError):
    pass


class Conflict(Exception):
    """A command issued against a state the lane is no longer in (HTTP 409)."""

    def __init__(self, message, *, current):
        super().__init__(message)
        self.current = current


class InProgress(Exception):
    """A retry of a command that is still running."""


@dataclass(frozen=True)
class Device:
    id: str
    kind: str
    name: str
    detail: str
    adapter: int | None = None


@dataclass(frozen=True)
class Lane:
    id: str
    device: str
    name: str
    description: str
    backend: str
    playlist: str
    segment_prefix: str
    files: tuple
    process: str
    video: bool


@dataclass(frozen=True)
class FeedConfig:
    node: str
    devices: tuple
    lanes: tuple

    def lane(self, lane_id):
        return next(lane for lane in self.lanes if lane.id == lane_id)

    def device(self, device_id):
        return next(device for device in self.devices if device.id == device_id)


def parse_config(data):
    """A FeedConfig from feed.json's contents, or FeedConfigError saying what is wrong."""
    if not isinstance(data, dict):
        raise FeedConfigError("feed.json must be an object")
    node = data.get("node")
    if not isinstance(node, str) or not _NAME.fullmatch(node):
        raise FeedConfigError("node must be a plain name")
    devices, lanes = [], []
    for raw in data.get("devices") or []:
        try:
            device = Device(id=raw["id"], kind=raw["kind"], name=raw["name"], detail=raw.get("detail", ""),
                            adapter=raw.get("adapter"))
        except (KeyError, TypeError) as exc:
            raise FeedConfigError(f"a device needs id, kind and name ({exc})") from None
        if not _NAME.fullmatch(str(device.id)) or device.kind not in KINDS:
            raise FeedConfigError(f"device {device.id!r}: kind must be one of {', '.join(KINDS)}")
        devices.append(device)
    for raw in data.get("lanes") or []:
        try:
            lane = Lane(id=raw["id"], device=raw["device"], name=raw["name"], description=raw.get("description", ""),
                        backend=raw["backend"], playlist=raw["playlist"], segment_prefix=raw["segment_prefix"],
                        files=tuple(raw["files"]), process=raw["process"], video=bool(raw.get("video", False)))
        except (KeyError, TypeError) as exc:
            raise FeedConfigError(f"a lane needs id, device, name, backend, playlist, segment_prefix, "
                                  f"files and process ({exc})") from None
        names = (lane.id, lane.playlist, lane.segment_prefix, lane.process, *lane.files)
        if not all(isinstance(n, str) and _NAME.fullmatch(n) for n in names) or not lane.playlist.endswith(".m3u8"):
            raise FeedConfigError(f"lane {lane.id!r}: names must be plain file names, its playlist a .m3u8")
        lanes.append(lane)
    for kind, items in (("device", devices), ("lane", lanes)):
        ids = [item.id for item in items]
        if len(ids) != len(set(ids)):
            raise FeedConfigError(f"two {kind}s share an id")
    known = {device.id for device in devices}
    for lane in lanes:
        if lane.device not in known:
            raise FeedConfigError(f"lane {lane.id!r} names an unknown device {lane.device!r}")
    if not lanes:
        raise FeedConfigError("feed.json lists no lanes")
    return FeedConfig(node=node, devices=tuple(devices), lanes=tuple(lanes))


def load_config(path):
    try:
        return parse_config(json.loads(Path(path).read_text()))
    except (OSError, ValueError) as exc:
        if isinstance(exc, FeedConfigError):
            raise
        raise FeedConfigError(f"could not read {path}: {exc}") from exc


def service_of(cgroup):
    """The systemd service that a /proc/<pid>/cgroup file's contents put a process in
    ("tv-thing.service", "vizelvision-master.service"), or None outside any service (a
    login session, a terminal)."""
    lines = cgroup.splitlines()
    line = next((l for l in lines if l.startswith("0::")), None) \
        or next((l for l in lines if ":name=systemd:" in l), "")
    path = line.split(":", 2)[-1]
    units = [part for part in path.split("/") if part.endswith(".service")]
    return units[-1] if units else None


def reapable(own_cgroup, cgroup):
    """Whether the orphan reaper may touch a process in ``cgroup``: never one belonging to
    another service. After the split, control's Roku verifier matches the reaper's rules, and
    VizelVision's processes read the same taps. An orphan in a login session (a run from a
    terminal) is still fair game: it is what holds the tuner after a manual run."""
    theirs = service_of(cgroup)
    return theirs is None or theirs == service_of(own_cgroup)


class FeedNode:
    def __init__(self, config, *, clock=time.time):
        self.config, self.clock = config, clock
        self.epoch = secrets.token_hex(8)
        self._lock = threading.Lock()
        self._lanes = {lane.id: {"revision": 0, "lifecycle": "idle", "generation": None,
                                 "navigation": None, "selection": None, "selection_version": 0}
                       for lane in config.lanes}
        # One command at a time per lane, so two commands issued against the same revision
        # cannot both pass their check before either has moved it.
        self._commanding = {lane.id: threading.Lock() for lane in config.lanes}
        self._ops = OrderedDict()   # op_id -> {"lane", "at", "done", "result", "error"}

    def view(self, lane):
        with self._lock:
            state = self._lanes[lane]
            return {"revision": state["revision"], "lifecycle": state["lifecycle"],
                    "navigating": bool(state["navigation"] and state["navigation"]["ended_at"] is None)}

    def precondition(self, lane):
        """What a command on this lane must be issued against now: (epoch, revision)."""
        with self._lock:
            return self.epoch, self._lanes[lane]["revision"]

    def transition(self, lane, lifecycle, generation=None):
        """Record what a lane is doing now; the revision moves if its lifecycle or its
        stream generation changed. Hardware moves on its own (a crash), so any lifecycle
        may follow any other. Returns the lane's revision."""
        if lifecycle not in LIFECYCLES:
            raise ValueError(f"unknown lifecycle {lifecycle!r}")
        with self._lock:
            state = self._lanes[lane]
            if lifecycle == "stopping" and state["lifecycle"] == "idle":
                return state["revision"]        # stopping a stopped lane changes nothing
            if lifecycle != "running":
                generation = None
            if lifecycle != state["lifecycle"] or generation != state["generation"]:
                state["revision"] += 1
                state["lifecycle"], state["generation"] = lifecycle, generation
            return state["revision"]

    def command(self, op_id, lane, expected, action):
        """Run ``action()`` for this op_id at most once, only while the lane is still at
        ``expected`` (an (epoch, revision) pair from precondition()).

        A retry of a finished command returns its first result (or raises its first error);
        a retry of one still running raises InProgress; a command against a state the lane
        has left raises Conflict."""
        if not isinstance(op_id, str) or not op_id:
            raise ValueError("a command needs an op_id")
        with self._lock:
            self._forget_old()
            prior = self._ops.get(op_id)
            if prior is not None:
                return self._replay(prior)
        with self._commanding[lane]:
            with self._lock:
                prior = self._ops.get(op_id)
                if prior is not None:           # the same op, sent twice at once
                    return self._replay(prior)
                current = (self.epoch, self._lanes[lane]["revision"])
                if tuple(expected) != current:
                    why = "the feed restarted" if expected[0] != self.epoch else "the lane moved on"
                    raise Conflict(f"{lane}: {why} (now at revision {current[1]})",
                                   current={"feed_epoch": current[0], "revision": current[1]})
                # Registered before the side effect, so a retry never runs it again.
                self._ops[op_id] = {"lane": lane, "at": self.clock(), "done": False, "result": None, "error": None}
            try:
                result = action()
            except Exception as exc:
                with self._lock:
                    self._ops[op_id].update(done=True, error=exc)
                raise
            with self._lock:
                self._ops[op_id].update(done=True, result=result)
            return result

    @staticmethod
    def _replay(prior):
        if not prior["done"]:
            raise InProgress("that command is still running")
        if prior["error"] is not None:
            raise prior["error"]
        return prior["result"]

    def _forget_old(self):
        oldest = self.clock() - OP_MEMORY_SECONDS
        while self._ops:
            op_id, op = next(iter(self._ops.items()))
            if not op["done"] or (len(self._ops) <= OP_MEMORY and op["at"] >= oldest):
                break
            del self._ops[op_id]

    # -- navigation ----------------------------------------------------------------
    # The feed's lock is a leaf: these take nothing else, so control may call them while
    # holding its own locks.
    def begin_navigation(self, lane, op_id):
        """What the lane shows is about to change (before the first key): the revision moves,
        the stored selection is forgotten, and the lane is navigating. A retry of the same
        op_id changes nothing. Returns the revision."""
        if not isinstance(op_id, str) or not op_id:
            raise ValueError("a navigation needs an op_id")
        with self._lock:
            state = self._lanes[lane]
            navigation = state["navigation"]
            if navigation is not None and navigation["op_id"] == op_id:
                return state["revision"]
            state["revision"] += 1
            state["selection"] = None
            state["selection_version"] += 1
            state["navigation"] = {"op_id": op_id, "revision": state["revision"],
                                   "started_at": self.clock(), "ended_at": None}
            return state["revision"]

    def end_navigation(self, lane, op_id, selection=None):
        """Navigation op_id is over. ``selection`` is what it verified (None: nothing, and the
        selection stays unknown). It is stored only if nothing moved the lane since the
        navigation began: no other navigation, no capture restart. Returns whether it was."""
        with self._lock:
            state = self._lanes[lane]
            navigation = state["navigation"]
            if navigation is None or navigation["op_id"] != op_id or navigation["ended_at"] is not None:
                return False
            navigation["ended_at"] = self.clock()
            if selection is None or navigation["revision"] != state["revision"]:
                return False
            state["selection"] = copy.deepcopy(selection)
            state["selection_version"] += 1
            return True

    def renew_selection(self, lane, version, changes):
        """Update fields of the stored selection (a re-verification) if it is still the one at
        ``version``. Never moves the revision. Returns whether it was updated."""
        with self._lock:
            state = self._lanes[lane]
            if state["selection"] is None or state["selection_version"] != version:
                return False
            state["selection"].update(copy.deepcopy(changes))
            return True

    def navigation(self, lane):
        """{revision, selection, selection_version, navigating, navigation}: a copy."""
        with self._lock:
            state = self._lanes[lane]
            navigation = state["navigation"]
            return {"revision": state["revision"], "selection": copy.deepcopy(state["selection"]),
                    "selection_version": state["selection_version"],
                    "navigating": navigation is not None and navigation["ended_at"] is None,
                    "navigation": dict(navigation) if navigation else None}

    def status(self):
        with self._lock:
            return {"node": self.config.node, "feed_epoch": self.epoch,
                    "lanes": {lane: {"revision": s["revision"], "lifecycle": s["lifecycle"],
                                     "navigating": bool(s["navigation"] and s["navigation"]["ended_at"] is None)}
                              for lane, s in self._lanes.items()}}


class LocalFeed:
    """How the control plane reaches the feed when both run in one process (Phase 1).

    RemoteFeed (Phase 2) answers the same calls over HTTP. ``ops`` is server.py's module:
    the feed's processes still live there, so every name is looked up when it is called.
    Control code reads lane state and changes lanes only through here, never through
    server.py's ``lanes`` and ``procs`` directly.
    """

    def __init__(self, node, ops):
        self.node, self.ops = node, ops

    # -- reading -------------------------------------------------------------------
    def lane(self, lane):
        """The feed's view of one lane: its state, whether its processes run, whether its
        current generation's playlist is ready (with or without them), and its revision and
        lifecycle. A copy: it never changes under you."""
        ops = self.ops
        with ops.lock:
            state = copy.deepcopy(ops.lanes[lane])
            running = ops.lane_is_running(lane)
            alive = running and ops.lane_processes_alive(lane)
        ready = ops.lane_stream_ready(lane)
        return {**state, "running": running, "alive": alive, "ready": ready, **self.node.view(lane)}

    def running_lanes(self):
        return [lane.id for lane in self.node.config.lanes if self.ops.lane_is_running(lane.id)]

    def primary_video_lane(self):
        return self.ops.primary_video_lane

    # -- commands ------------------------------------------------------------------
    def _command(self, lane, action):
        """Run one change to a lane as a feed command. Under server.py's lock first, then
        the feed's: one lock order everywhere. A Conflict (the lane moved on between the
        read and the command) is re-read and tried once more, with a new op_id."""
        with self.ops.lock:
            for attempt in (1, 2):
                try:
                    return self.node.command(secrets.token_hex(8), lane, self.node.precondition(lane), action)
                except Conflict:
                    if attempt == 2:
                        raise

    def tune_digital(self, channel, quality, taps=()):
        """(ok, error, stream reused) for a fresh digital generation on this channel,
        also feeding these recording tap ports."""
        def tune():
            ok, error, reused = self.ops.start_digital(channel, quality, list(taps)) if taps \
                else self.ops.start_digital(channel, quality)
            if not ok:
                self.ops.lanes[self.ops.LANE_DIGITAL]["error"] = error or "Failed to tune"
            return ok, error, reused
        return self._command(self.ops.LANE_DIGITAL, tune)

    def tune_analog(self, channel, quality):
        """(ok, error, tuner) for a fresh analog generation on this cable channel."""
        def tune():
            ok, error, tuner = self.ops.start_analog(channel, quality)
            if not ok:
                self.ops.lanes[self.ops.LANE_ANALOG]["error"] = error or "Failed to tune"
            return ok, error, tuner
        return self._command(self.ops.LANE_ANALOG, tune)

    def stop(self, lane):
        return self._command(lane, lambda: self.ops.stop_lane(lane))

    def stop_all(self):
        for lane in self.node.config.lanes:
            self.stop(lane.id)

    # -- recorders -----------------------------------------------------------------
    def start_recorder(self, kind, part, until, *, lane=None, exclude=(), start_index=0):
        """Start a recorder writing ``part`` until ``until``: from a recording tap ("tap",
        whose port is chosen here, avoiding ``exclude``) or copying a lane's live stream
        ("live"). Returns (recorder id, tap port or None)."""
        return self.ops.start_recorder(kind, part, until, lane=lane, exclude=exclude, start_index=start_index)

    def stop_recorder(self, rec_id, timeout=None):
        self.ops.recorders.stop(rec_id, timeout=timeout)

    def recorder(self, rec_id):
        """{alive, returncode, ended, overruns, part, until}, or None."""
        return self.ops.recorders.view(rec_id)

    # -- front-end operations --------------------------------------------------------
    # A device measured rather than streamed: a scan, the signal meter, a guide read.
    # Control stops the lane on it first (a command) and keeps its keepers off meanwhile.
    def _adapter(self, device):
        adapter = self.node.config.device(device).adapter
        return 0 if adapter is None else adapter

    def measure(self, device):
        """The device's signal now, without tuning it or disturbing its stream."""
        return self.ops.atsc_scan.measure(self._adapter(device))

    def scan_channel(self, device, rf_channel, **options):
        """Tune one RF channel and report what is on it (lock_timeout, dwell, read_vct)."""
        return self.ops.atsc_scan.scan_channel(rf_channel, adapter=self._adapter(device), **options)

    def sweep(self, device, rf_channels, lock_timeout, before=None):
        """Scan each RF channel in turn on the device's front-end, held open for the sweep,
        yielding each result. ``before(rf)`` is called as each is tuned. Close it
        (contextlib.closing) to let go of the front-end early."""
        scan = self.ops.atsc_scan
        adapter = self._adapter(device)
        frontend = scan.open_frontend(adapter, writable=True)
        try:
            for rf in rf_channels:
                if before is not None:
                    before(rf)
                yield scan.scan_channel(rf, adapter=adapter, lock_timeout=lock_timeout, fd=frontend)
        finally:
            os.close(frontend)

    def read_guide(self, device, **options):
        """Sweep the known frequencies for their PSIP listings (epg.refresh's options)."""
        return self.ops.epg.refresh(adapter=self._adapter(device), **options)

    def status(self):
        """{node, feed_epoch, lanes: {revision, lifecycle, navigating}}."""
        return self.node.status()

    def slides(self):
        """{name: status} of the slide channels' on-demand video (slide_stream.py)."""
        return {name: stream.status() for name, stream in self.ops.SLIDE_STREAMS.items()}

    # -- navigation ----------------------------------------------------------------
    # Never under server.py's lock: roku_lane calls these from threads that may hold its own.
    def begin_navigation(self, lane, op_id):
        return self.node.begin_navigation(lane, op_id)

    def end_navigation(self, lane, op_id, selection=None):
        return self.node.end_navigation(lane, op_id, selection)

    def renew_selection(self, lane, version, changes):
        return self.node.renew_selection(lane, version, changes)

    def navigation(self, lane):
        return self.node.navigation(lane)


class LaneRecord:
    """One lane's navigation and selection, kept by the feed: what RokuLane reads and writes
    instead of holding them itself. ``feed`` is the feed client (or a FeedNode)."""

    def __init__(self, feed, lane):
        self.feed, self.lane = feed, lane

    @classmethod
    def private(cls, lane="roku"):
        """A record in a feed of its own, for a RokuLane outside the server (tests, tools)."""
        config = parse_config({
            "node": "local", "devices": [{"id": lane, "kind": "capture", "name": lane}],
            "lanes": [{"id": lane, "device": lane, "name": lane, "backend": "none", "playlist": f"{lane}.m3u8",
                       "segment_prefix": f"{lane}seg", "files": [], "process": lane}]})
        return cls(FeedNode(config), lane)

    def begin_navigation(self, op_id):
        return self.feed.begin_navigation(self.lane, op_id)

    def end_navigation(self, op_id, selection=None):
        return self.feed.end_navigation(self.lane, op_id, selection)

    def renew_selection(self, version, changes):
        return self.feed.renew_selection(self.lane, version, changes)

    def state(self):
        return self.feed.navigation(self.lane)


class Recorders:
    """The feed's recorder processes (the VCR's ffmpeg).

    Each is started with the end time the feed enforces itself, so a recording never runs
    long while control is away. Each part file has one writer: starting a second recorder
    on a part already being written returns the first. Starts and ends, and why each
    ended, go to a durable journal that control reconciles with its timers.

    Nothing here runs a thread per recording: ``sweep()`` (the feed's keeper calls it, and
    every ``view()`` does) notices exits and enforces end times.
    """

    def __init__(self, journal_path, spawn, *, clock=time.time, stop_timeout=8.0):
        self.journal_path, self.spawn, self.clock = Path(journal_path), spawn, clock
        self.stop_timeout = stop_timeout
        self._lock = threading.RLock()
        self._recorders = {}

    def start(self, command, part, until, **info):
        """Spawn ``command`` writing ``part`` until the epoch time ``until``. Returns its id."""
        part = str(part)
        with self._lock:
            writing = next((r for r in self._recorders.values()
                            if r["part"] == part and r["ended"] is None), None)
            if writing is not None:
                return writing["id"]
            proc = self.spawn(command)          # OSError: nothing was started
            entry = {"id": secrets.token_hex(8), "proc": proc, "part": part, "until": until,
                     "overruns": 0, "started_at": self.clock(), "ended": None, **info}
            self._recorders[entry["id"]] = entry
        self._journal("start", entry)
        if getattr(proc, "stderr", None) is not None:
            threading.Thread(target=self._read_stderr, args=(entry,), daemon=True).start()
        return entry["id"]

    def _read_stderr(self, entry):
        for line in entry["proc"].stderr:
            line = line.rstrip()
            if "Circular buffer overrun" in line:
                entry["overruns"] += 1
            print(f"recorder ffmpeg: {line}", flush=True)

    def stop(self, rec_id, timeout=None, reason="stopped"):
        """Ask ffmpeg to finish its file ("q"), escalating only if it will not."""
        with self._lock:
            entry = self._recorders.get(rec_id)
        if entry is None or entry["ended"] is not None:
            return
        proc = entry["proc"]
        if proc.poll() is None:
            try:
                proc.stdin.write("q")
                proc.stdin.flush()
                proc.stdin.close()
            except (OSError, ValueError, AttributeError):
                pass
            try:
                proc.wait(timeout=self.stop_timeout if timeout is None else timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        self._ended(entry, reason)

    def _ended(self, entry, reason):
        with self._lock:
            if entry["ended"] is not None:
                return
            entry["ended"] = reason
        try:
            size = Path(entry["part"]).stat().st_size
        except OSError:
            size = None
        self._journal("end", entry, reason=reason, returncode=entry["proc"].returncode, bytes=size)

    def sweep(self):
        """Note recorders that exited on their own, and stop those past their end time."""
        now = self.clock()
        with self._lock:
            live = [entry for entry in self._recorders.values() if entry["ended"] is None]
        for entry in live:
            if entry["proc"].poll() is not None:
                self._ended(entry, "exited")
            elif entry["until"] is not None and now >= entry["until"]:
                self.stop(entry["id"], reason="until")
        with self._lock:      # forget ended ones after an hour; the journal keeps them
            for rec_id in [r for r, e in self._recorders.items()
                           if e["ended"] is not None and now - e["started_at"] > 3600]:
                del self._recorders[rec_id]

    def view(self, rec_id):
        """{alive, returncode, ended, overruns, part, until}, or None for an unknown id."""
        self.sweep()
        with self._lock:
            entry = self._recorders.get(rec_id)
            if entry is None:
                return None
            return {"id": rec_id, "alive": entry["ended"] is None, "returncode": entry["proc"].returncode,
                    "ended": entry["ended"], "overruns": entry["overruns"], "part": entry["part"],
                    "until": entry["until"]}

    def live(self):
        with self._lock:
            return [rec_id for rec_id, entry in self._recorders.items() if entry["ended"] is None]

    def stop_all(self, timeout=None, reason="shutdown"):
        for rec_id in self.live():
            self.stop(rec_id, timeout=timeout, reason=reason)

    def process(self, rec_id):
        """The process itself (tests)."""
        with self._lock:
            entry = self._recorders.get(rec_id)
        return entry["proc"] if entry else None

    def _journal(self, event, entry, **extra):
        record = {"at": self.clock(), "event": event, "id": entry["id"], "part": entry["part"],
                  "until": entry["until"], **{k: entry.get(k) for k in ("kind", "lane", "tap_port")}, **extra}
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.journal_path, "a") as journal:
                journal.write(json.dumps(record) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            print(f"Could not write the recordings journal: {error}", flush=True)

    def journal(self):
        """Every journal record, oldest first (a damaged line is skipped)."""
        try:
            lines = self.journal_path.read_text().splitlines()
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records
