"""The feed's HTTP API (``/feed/v1/``) and RemoteFeed, the control plane's client for it.

Phase 2 of the feed/control split (INVARIANTS.md): the feed runs in its own process
(feed_server.py), and control reaches it only through RemoteFeed, which answers the same
calls as feed_node.LocalFeed does in one process.

* **Reads** come from one snapshot (``GET /feed/v1/lanes``), cached for half a second and
  fetched with a short timeout. A feed that is down or hung reads ``unknown``: its lanes are
  never treated as free (running, not alive, not ready), and the Roku's capture as stopped.
* **Commands** carry an ``op_id`` and the ``(feed_epoch, revision)`` they were issued
  against. A retry after a network error reuses the op_id, so it can never apply twice; a 409
  (the lane moved on) is re-read and reissued once with a new op_id. While the feed cannot be
  reached they raise FeedUnavailable, an OSError, so control answers 503.
* **Long operations** (a band sweep, a guide read) stream NDJSON lines; closing the stream
  lets go of the front-end.

The API is served on loopback only (feed_server.py binds 127.0.0.1, and the handler refuses
any other peer); Apache never routes ``/feed/v1/``.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import socket
import threading
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from feed_node import Conflict, InProgress

PREFIX = "/feed/v1/"


class FeedUnavailable(OSError):
    """The feed did not answer (down, hung or restarting). Control answers 503."""


class FeedRefused(Exception):
    """A definite refusal from the feed, other than a Conflict."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def _dumps(payload):
    return json.dumps(payload, default=str)


# -- the feed's side ------------------------------------------------------------------------

class FeedAPI:
    """``/feed/v1/`` over a LocalFeed, in the feed process.

    ``handle(method, path, body, stream)`` returns ``(status, payload)``; for the streaming
    routes it calls ``stream()`` for a line writer instead and returns None.
    """

    def __init__(self, local):
        self.local = local
        self._guide_stops = {}

    def handle(self, method, path, body, stream=None):
        parts = [p for p in path[len(PREFIX):].split("?")[0].split("/") if p]
        body = body or {}
        try:
            return self._route(method, parts, body, stream)
        except Conflict as exc:
            return 409, {"error": str(exc), "code": "conflict", "current": exc.current}
        except InProgress as exc:
            return 409, {"error": str(exc), "code": "in_progress"}
        except (KeyError, StopIteration):
            return 404, {"error": f"no such feed resource: {path}"}
        except (TypeError, ValueError) as exc:
            return 400, {"error": str(exc)}

    def _lane_id(self, lane):
        known = {item.id for item in self.local.node.config.lanes}
        if lane not in known:
            raise KeyError(lane)
        return lane

    def _route(self, method, parts, body, stream):
        local = self.local
        if parts == ["lanes"] and method == "GET":
            return 200, local.snapshot()
        if parts[:1] == ["lanes"] and len(parts) >= 3:
            lane = self._lane_id(parts[1])
            action = parts[2:]
            if action == ["tune"] and method == "POST":
                op = {"op_id": body.get("op_id"), "expected": body.get("expected")}
                if not op["op_id"]:
                    raise ValueError("a tune needs an op_id")
                if lane == local.ops.LANE_DIGITAL:
                    result = local.tune_digital(body["channel"], body.get("quality", "medium"),
                                                taps=tuple(body.get("taps") or ()), **op)
                elif lane == local.ops.LANE_ANALOG:
                    result = local.tune_analog(body["channel"], body.get("quality", "medium"), **op)
                else:
                    raise ValueError(f"{lane} is not tuned through the feed")
                return 200, {"result": list(result)}
            if action == ["stop"] and method == "POST":
                if not body.get("op_id"):
                    raise ValueError("a stop needs an op_id")
                local.stop(lane, op_id=body["op_id"], expected=body.get("expected"))
                return 200, {}
            if action == ["navigation"] and method == "GET":
                return 200, local.navigation(lane)
            if action == ["navigation", "begin"] and method == "POST":
                return 200, {"revision": local.begin_navigation(lane, body["op_id"])}
            if action == ["navigation", "end"] and method == "POST":
                return 200, {"stored": local.end_navigation(lane, body["op_id"], body.get("selection"))}
            if action == ["navigation", "renew"] and method == "POST":
                return 200, {"renewed": local.renew_selection(lane, body["version"], body.get("changes") or {})}
        if parts == ["recorders"]:
            if method == "GET":
                return 200, {"recorders": local.live_recorders()}
            if method == "POST":
                rec_id, tap_port = local.start_recorder(
                    body["kind"], body["part"], body.get("until"), lane=body.get("lane"),
                    exclude=tuple(body.get("exclude") or ()), start_index=body.get("start_index", 0))
                return 200, {"id": rec_id, "tap_port": tap_port}
        if parts[:1] == ["recorders"] and len(parts) >= 2:
            if len(parts) == 2 and method == "GET":
                view = local.recorder(parts[1])
                return (200, view) if view is not None else (404, {"error": "no such recorder"})
            if parts[2:] == ["stop"] and method == "POST":
                local.stop_recorder(parts[1], timeout=body.get("timeout"))
                return 200, {}
        if parts[:1] == ["devices"] and len(parts) >= 3:
            device = parts[1]
            local.node.config.device(device)        # StopIteration: unknown device
            action = parts[2:]
            if action == ["signal"] and method == "GET":
                try:
                    return 200, {"signal": local.measure(device)}
                except OSError as exc:
                    return 502, {"error": str(exc), "code": "device_error"}
            if action == ["scan"] and method == "POST":
                try:
                    return 200, {"result": local.scan_channel(device, body["rf_channel"],
                                                              **(body.get("options") or {}))}
                except OSError as exc:
                    return 502, {"error": str(exc), "code": "device_error"}
            if action == ["sweep"] and method == "POST" and stream is not None:
                self._sweep(device, body, stream)
                return None
            if action == ["guide"] and method == "POST" and stream is not None:
                self._guide(device, body, stream)
                return None
            if action == ["guide", "stop"] and method == "POST":
                stop = self._guide_stops.get(device)
                if stop is not None:
                    stop.set()
                return 200, {"stopping": stop is not None}
        raise KeyError("/".join(parts))

    def _sweep(self, device, body, stream):
        write = stream()
        results = self.local.sweep(device, list(body["rf_channels"]), float(body["lock_timeout"]),
                                   before=lambda rf: write({"tuning": rf}))
        try:
            for result in results:
                write({"result": result})
        except OSError as exc:            # the device, or the client gone (the sweep stops)
            try:
                write({"error": str(exc)})
            except OSError:
                pass
        finally:
            results.close()

    def _guide(self, device, body, stream):
        write = stream()
        stop = threading.Event()
        self._guide_stops[device] = stop

        def progress(step):
            try:
                write({"progress": step})
            except OSError:
                stop.set()                  # nobody is listening: let go of the tuner

        try:
            guide = self.local.read_guide(device, stop=stop, progress=progress, **(body.get("options") or {}))
            write({"guide": guide})
        except OSError:
            stop.set()
        except Exception as exc:  # noqa: BLE001 - reported to control, which reports it
            try:
                write({"error": str(exc) or type(exc).__name__})
            except OSError:
                pass
        finally:
            self._guide_stops.pop(device, None)


# -- control's side ---------------------------------------------------------------------------

class RemoteFeed:
    """The feed client in the control process: LocalFeed's calls, over HTTP.

    ``config`` is the FeedConfig (feed.json), for the lane ids; ``blank_state()`` a lane's
    empty state (server.new_lane_state), the base of an ``unknown`` lane.
    """

    def __init__(self, base_url, config, blank_state, *, timeout=2.0, command_timeout=20.0,
                 scan_timeout=90.0, cache_seconds=0.5, clock=time.monotonic, opener=urlopen,
                 sleep=time.sleep):
        self.base_url = base_url.rstrip("/") + PREFIX
        self.config, self.blank_state = config, blank_state
        self.timeout, self.command_timeout, self.scan_timeout = timeout, command_timeout, scan_timeout
        self.cache_seconds, self.clock, self.opener, self.sleep = cache_seconds, clock, opener, sleep
        self._lock = threading.Lock()
        self._snap, self._at, self.reachable = None, None, False

    # -- transport ---------------------------------------------------------------------------
    def _request(self, method, path, body=None, timeout=None):
        data = None if body is None else _dumps(body).encode()
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"Content-Type": "application/json"} if data else {})
        try:
            with self.opener(request, timeout=timeout or self.timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                payload = {}
            if exc.code in (502, 503, 504) and payload.get("code") != "device_error":
                raise FeedUnavailable(f"the feed answered {exc.code}") from None
            return exc.code, payload
        except (URLError, socket.timeout, TimeoutError, ConnectionError, HTTPException, ValueError) as exc:
            raise FeedUnavailable(f"the feed did not answer: {exc}") from None

    def _checked(self, status, payload):
        if status == 409:
            if payload.get("code") == "in_progress":
                raise InProgress(payload.get("error"))
            raise Conflict(payload.get("error") or "conflict", current=payload.get("current"))
        if status == 502:
            raise OSError(payload.get("error") or "the device failed")
        if status >= 400:
            raise FeedRefused(payload.get("error") or f"the feed refused ({status})", status)
        return payload

    # -- reading -----------------------------------------------------------------------------
    def snapshot(self, max_age=None):
        """The feed's snapshot: fresh within ``cache_seconds``, else fetched; when the feed
        cannot be reached, the last one with every lane ``unknown``."""
        now = self.clock()
        with self._lock:
            snap, at = self._snap, self._at
        limit = self.cache_seconds if max_age is None else max_age
        if snap is not None and at is not None and now - at < limit and self.reachable:
            return snap
        try:
            snap = self._checked(*self._request("GET", "lanes"))
        except (FeedUnavailable, FeedRefused):
            with self._lock:
                self.reachable = False
                last = self._snap
            return self._unknown(last)
        with self._lock:
            self._snap, self._at, self.reachable = snap, self.clock(), True
        return snap

    def _unknown(self, last):
        lanes = {}
        for lane in self.config.lanes:
            known = (last or {}).get("lanes", {}).get(lane.id) or {}
            lanes[lane.id] = {**self.blank_state(), **copy.deepcopy(known),
                              # Never free: a lane the feed cannot vouch for is busy and broken.
                              "running": True, "alive": False, "ready": False, "lifecycle": "unknown",
                              "navigating": bool(known.get("navigating")), "unknown": True,
                              "error": "the feed is not answering"}
        return {"node": (last or {}).get("node", self.config.node), "feed_epoch": (last or {}).get("feed_epoch"),
                "lanes": lanes, "running": [lane.id for lane in self.config.lanes],
                "primary_video_lane": (last or {}).get("primary_video_lane") or self.config.lanes[0].id,
                "slides": (last or {}).get("slides") or {},
                "capture": {"roku": {"running": False, "stream_id": None, "tap_url": None,
                                     "error": "the feed is not answering"}},
                "navigation": (last or {}).get("navigation") or {},
                "unknown": True}

    def lane(self, lane):
        return copy.deepcopy(self.snapshot()["lanes"][lane])

    def running_lanes(self):
        return list(self.snapshot()["running"])

    def primary_video_lane(self):
        return self.snapshot()["primary_video_lane"]

    def capture_source(self, lane):
        return dict(self.snapshot()["capture"].get(lane) or {"running": False, "stream_id": None,
                                                              "tap_url": None, "error": None})

    def status(self):
        snap = self.snapshot()
        return {"node": snap["node"], "feed_epoch": snap["feed_epoch"],
                "lanes": {lane: {"revision": state.get("revision"), "lifecycle": state.get("lifecycle"),
                                 "navigating": state.get("navigating", False)}
                          for lane, state in snap["lanes"].items()}}

    def slides(self):
        return dict(self.snapshot()["slides"])

    # -- commands ----------------------------------------------------------------------------
    def _expected(self, lane, fresh=False):
        snap = self.snapshot(max_age=0 if fresh else None)
        if snap.get("unknown"):
            raise FeedUnavailable("the feed is not answering")
        return [snap["feed_epoch"], snap["lanes"][lane]["revision"]]

    def _command(self, lane, path, body):
        """POST a lane command once: the same op_id through network errors and while it
        runs; one reissue, with a new op_id, after a Conflict."""
        for attempt in (1, 2):
            body = {**body, "op_id": secrets.token_hex(8), "expected": self._expected(lane, fresh=attempt == 2)}
            deadline = self.clock() + self.command_timeout
            lost = 0
            while True:
                try:
                    status, payload = self._request("POST", path, body, timeout=self.command_timeout)
                    result = self._checked(status, payload)
                    break
                except FeedUnavailable:
                    lost += 1
                    if lost > 1 or self.clock() > deadline:
                        raise
                    self.sleep(0.2)                   # the same op_id: it cannot apply twice
                except InProgress:
                    if self.clock() > deadline:
                        raise FeedUnavailable("the feed is still working on it") from None
                    self.sleep(0.5)
                except Conflict:
                    if attempt == 2:
                        raise
                    result = None
                    break
            if result is not None:
                with self._lock:
                    self._at = None                    # the next read sees what it did
                return result
        raise AssertionError("unreachable")

    def tune_digital(self, channel, quality, taps=()):
        ok, error, reused = self._command("digital", "lanes/digital/tune",
                                          {"channel": channel, "quality": quality, "taps": list(taps)})["result"]
        return ok, error, reused

    def tune_analog(self, channel, quality):
        ok, error, tuner = self._command("analog", "lanes/analog/tune",
                                         {"channel": channel, "quality": quality})["result"]
        return ok, error, tuner

    def stop(self, lane):
        self._command(lane, f"lanes/{lane}/stop", {})

    def stop_all(self):
        for lane in self.config.lanes:
            self.stop(lane.id)

    # -- recorders ---------------------------------------------------------------------------
    def start_recorder(self, kind, part, until, *, lane=None, exclude=(), start_index=0):
        # One writer per part file, in the feed: a retry returns the recorder already writing.
        body = {"kind": kind, "part": str(part), "until": until, "lane": lane,
                "exclude": list(exclude), "start_index": start_index}
        try:
            payload = self._checked(*self._request("POST", "recorders", body, timeout=self.command_timeout))
        except FeedUnavailable:
            self.sleep(0.2)
            payload = self._checked(*self._request("POST", "recorders", body, timeout=self.command_timeout))
        return payload["id"], payload["tap_port"]

    def stop_recorder(self, rec_id, timeout=None):
        self._checked(*self._request("POST", f"recorders/{rec_id}/stop", {"timeout": timeout},
                                     timeout=max(self.command_timeout, (timeout or 0) + 5)))

    def recorder(self, rec_id):
        status, payload = self._request("GET", f"recorders/{rec_id}")
        return None if status == 404 else self._checked(status, payload)

    def live_recorders(self):
        return self._checked(*self._request("GET", "recorders"))["recorders"]

    # -- front-end operations ----------------------------------------------------------------
    def measure(self, device):
        return self._checked(*self._request("GET", f"devices/{device}/signal"))["signal"]

    def scan_channel(self, device, rf_channel, **options):
        return self._checked(*self._request("POST", f"devices/{device}/scan",
                                            {"rf_channel": rf_channel, "options": options},
                                            timeout=self.scan_timeout))["result"]

    def _stream(self, path, body):
        request = Request(self.base_url + path, data=_dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
        try:
            return self.opener(request, timeout=self.scan_timeout)
        except HTTPError as exc:
            raise FeedUnavailable(f"the feed answered {exc.code}") from None
        except (URLError, socket.timeout, TimeoutError, ConnectionError, HTTPException) as exc:
            raise FeedUnavailable(f"the feed did not answer: {exc}") from None

    @staticmethod
    def _lines(response):
        for raw in response:
            if raw.strip():
                yield json.loads(raw)

    def sweep(self, device, rf_channels, lock_timeout, before=None):
        response = self._stream(f"devices/{device}/sweep",
                                {"rf_channels": list(rf_channels), "lock_timeout": lock_timeout})
        try:
            for line in self._lines(response):
                if "tuning" in line:
                    if before is not None:
                        before(line["tuning"])
                elif "result" in line:
                    yield line["result"]
                elif "error" in line:
                    raise OSError(line["error"])
        except (socket.timeout, TimeoutError, ConnectionError, HTTPException) as exc:
            raise FeedUnavailable(f"the feed stopped answering mid-sweep: {exc}") from None
        finally:
            response.close()                         # the feed lets go of the front-end

    def read_guide(self, device, stop=None, progress=None, **options):
        response = self._stream(f"devices/{device}/guide", {"options": options})
        done = threading.Event()

        def relay_stop():
            while not done.wait(0.25):
                if stop is not None and stop.is_set():
                    try:
                        self._request("POST", f"devices/{device}/guide/stop", {})
                    except OSError:
                        pass
                    return

        threading.Thread(target=relay_stop, name="feed-guide-stop", daemon=True).start()
        try:
            for line in self._lines(response):
                if "progress" in line:
                    if progress is not None:
                        progress(line["progress"])
                elif "guide" in line:
                    return line["guide"]
                elif "error" in line:
                    raise RuntimeError(line["error"])
        except (socket.timeout, TimeoutError, ConnectionError, HTTPException) as exc:
            raise FeedUnavailable(f"the feed stopped answering mid-guide: {exc}") from None
        finally:
            done.set()
            response.close()
        raise FeedUnavailable("the feed ended the guide read without a guide")

    # -- navigation (the Roku's generation and selection, kept by the feed) -------------------
    def begin_navigation(self, lane, op_id):
        payload = self._checked(*self._request("POST", f"lanes/{lane}/navigation/begin", {"op_id": op_id}))
        with self._lock:
            self._at = None
        return payload["revision"]

    def end_navigation(self, lane, op_id, selection=None):
        payload = self._checked(*self._request("POST", f"lanes/{lane}/navigation/end",
                                               {"op_id": op_id, "selection": selection}))
        with self._lock:
            self._at = None
        return payload["stored"]

    def renew_selection(self, lane, version, changes):
        return self._checked(*self._request("POST", f"lanes/{lane}/navigation/renew",
                                            {"version": version, "changes": changes}))["renewed"]

    def navigation(self, lane):
        """From the snapshot: control is the only writer, and each write clears the cache.
        While the feed is not answering, the last known record."""
        known = self.snapshot().get("navigation", {}).get(lane)
        return copy.deepcopy(known) if known else {"revision": None, "selection": None, "selection_version": 0,
                                                   "navigating": False, "navigation": None}


def loopback(address):
    """Whether a peer address is this host's own (the only one /feed/v1/ answers)."""
    return bool(re.fullmatch(r"127\.\d+\.\d+\.\d+|::1|::ffff:127\.\d+\.\d+\.\d+", address or ""))


def answer(handler, api):
    """Answer one /feed/v1/ request on an http.server request handler: loopback peers only,
    a JSON body for POST, JSON back, or NDJSON lines for a streaming route."""
    if not loopback(handler.client_address[0]):
        return _send(handler, 403, {"error": "the feed answers only this host"})
    body = None
    if handler.command == "POST":
        length = int(handler.headers.get("Content-Length") or 0)
        try:
            body = json.loads(handler.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return _send(handler, 400, {"error": "invalid JSON"})

    def stream():
        handler.send_response(200)
        handler.send_header("Content-Type", "application/x-ndjson")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True

        def write(line):
            handler.wfile.write((_dumps(line) + "\n").encode())
            handler.wfile.flush()
        return write

    result = api.handle(handler.command, handler.path, body, stream)
    if result is not None:
        _send(handler, *result)


def _send(handler, status, payload):
    data = _dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
