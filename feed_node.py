"""TV Thing's feed: the media plane (see INVARIANTS.md).

What the hardware is and which lanes run on it (feed.json), and the state the feed owns:

* its **epoch**, new every time the feed starts;
* each lane's **revision** and **lifecycle** (idle, starting, running, stopping, error). The
  revision moves when, and only when, what the lane outputs or its lifecycle changes;
* the **commands** applied to a lane: each `op_id` at most once, and only at the
  `(epoch, revision)` it was issued against. A stale command is refused (`Conflict`), so a
  retry after a crash, or one delayed past a feed restart, can never retune twice.

The processes themselves still live in server.py, which reports each change here. They move
into the feed lane by lane (the plan's Phase 1).
"""
from __future__ import annotations

import copy
import json
import re
import secrets
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


class FeedNode:
    def __init__(self, config, *, clock=time.time):
        self.config, self.clock = config, clock
        self.epoch = secrets.token_hex(8)
        self._lock = threading.Lock()
        self._lanes = {lane.id: {"revision": 0, "lifecycle": "idle", "generation": None} for lane in config.lanes}
        # One command at a time per lane, so two commands issued against the same revision
        # cannot both pass their check before either has moved it.
        self._commanding = {lane.id: threading.Lock() for lane in config.lanes}
        self._ops = OrderedDict()   # op_id -> {"lane", "at", "done", "result", "error"}

    def view(self, lane):
        with self._lock:
            state = self._lanes[lane]
            return {"revision": state["revision"], "lifecycle": state["lifecycle"]}

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

    def status(self):
        with self._lock:
            return {"node": self.config.node, "feed_epoch": self.epoch,
                    "lanes": {lane: {"revision": s["revision"], "lifecycle": s["lifecycle"]}
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
        playlist is ready, and its revision and lifecycle. A copy: it never changes under you."""
        ops = self.ops
        with ops.lock:
            state = copy.deepcopy(ops.lanes[lane])
            running = ops.lane_is_running(lane)
            alive = running and ops.lane_processes_alive(lane)
        ready = running and ops.lane_stream_ready(lane)
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

    def tune_digital(self, channel, quality):
        """(ok, error, stream reused) for a fresh digital generation on this channel."""
        def tune():
            ok, error, reused = self.ops.start_digital(channel, quality)
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
