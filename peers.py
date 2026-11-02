"""The peer node: the other TV Thing whose digital tuners join this one's in one pool.

Two nodes (viviserv and viviserv2) each keep their own tuners, locks and feed; neither ever
commands the other's feed. A node reaches its peer only through the peer's public front
door (``:8080``), the way any client does:

* **Reads** come from a poller thread that asks ``GET /api/v1/tuners?local=1`` every couple
  of seconds. Nothing on a request path waits on the peer: a peer that is down, hung or
  slow reads ``unknown``, and an unknown tuner is never available (INVARIANTS.md 9).
* **Changes** (a tune, a lock, a release) are the peer's own public API calls, sent with
  ``local_only`` so the peer answers from its own tuners and never asks back.
* **Its plan** (``GET /api/v1/plan``: its antenna's channels, its market and their numbers)
  is read by the same poller whenever its ``plan_revision`` changes, and at least every
  ``plan_interval``. The last one read is kept in ``plan_store``, so the peer's channels
  stay on the dial while it is down (their tuners then read unknown).

Streams are not relayed here: the front door proxies ``/stream/peer/`` to the peer's
``/stream/`` (deploy/apache/tv-thing-routes.conf), so a viewer only ever talks to its own
node. ``stream_path`` rewrites a peer's playlist path into that local one.
"""
from __future__ import annotations

import copy
import json
import socket
import threading
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# The front door's route to the peer's /stream/.
RELAY_PREFIX = "/stream/peer/"


class PeerUnavailable(OSError):
    """The peer did not answer. Control answers 503 for a change it was asked to make."""


def stream_path(path):
    """A peer's ``/stream/<file>`` as this node's front door relays it."""
    if not path:
        return path
    return RELAY_PREFIX + path.split("/stream/", 1)[-1].lstrip("/")


class PeerClient:
    """The peer's tuners, as last read, and its tune and lock calls.

    ``base_url``: the peer's front door (host_config.Host.peer_url()). ``max_age``: how long a
    read stands for the peer; older, every tuner of the peer reads unknown.
    """

    def __init__(self, base_url, *, timeout=1.5, command_timeout=25.0, interval=2.0,
                 max_age=6.0, plan_interval=60.0, plan_store=None, clock=time.monotonic,
                 opener=urlopen):
        self.base_url = base_url.rstrip("/")
        self.timeout, self.command_timeout = timeout, command_timeout
        self.interval, self.max_age, self.clock, self.opener = interval, max_age, clock, opener
        self.plan_interval, self.plan_store = plan_interval, plan_store
        self._lock = threading.Lock()
        self._read, self._at, self.last_error = None, None, None
        self._plan, self._plan_at = self._load_plan(), None
        self._wakeup = threading.Event()
        self._stopping = threading.Event()

    # -- transport ---------------------------------------------------------------------------
    def _request(self, method, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode()
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
            if exc.code in (502, 503, 504) and not isinstance(payload.get("error"), dict):
                # Apache's own answer: the peer's control is not there behind it.
                raise PeerUnavailable(f"the peer answered {exc.code}") from None
            return exc.code, payload
        except (URLError, socket.timeout, TimeoutError, ConnectionError, HTTPException,
                ValueError) as exc:
            raise PeerUnavailable(f"the peer did not answer: {exc}") from None

    # -- reading -----------------------------------------------------------------------------
    def refresh(self):
        """Read the peer's own tuners once. Returns whether it answered."""
        try:
            status, payload = self._request("GET", "/api/v1/tuners?local=1")
            if status != 200 or not isinstance(payload.get("tuners"), list):
                raise PeerUnavailable(f"the peer answered {status} without its tuners")
        except PeerUnavailable as exc:
            with self._lock:
                self.last_error = str(exc)
            return False
        with self._lock:
            self._read, self._at, self.last_error = payload, self.clock(), None
            plan, plan_at = self._plan, self._plan_at
        if (plan_at is None or self.clock() - plan_at >= self.plan_interval
                or payload.get("plan_revision") != (plan or {}).get("revision")):
            self.refresh_plan()
        return True

    def _load_plan(self):
        try:
            plan = json.loads(self.plan_store.read_text()) if self.plan_store else None
        except (OSError, ValueError):
            return None
        return plan if isinstance(plan, dict) and isinstance(plan.get("rows"), list) else None

    def refresh_plan(self):
        """Read the peer's plan once, and keep it. Returns whether it answered."""
        try:
            status, plan = self._request("GET", "/api/v1/plan")
        except PeerUnavailable:
            return False
        if status != 200 or not isinstance(plan.get("rows"), list) or not plan.get("market"):
            return False        # a peer from before plans: nothing to merge
        with self._lock:
            changed = plan != self._plan
            self._plan, self._plan_at = plan, self.clock()
        if changed and self.plan_store:
            try:
                self.plan_store.parent.mkdir(parents=True, exist_ok=True)
                temp = self.plan_store.with_suffix(".tmp")
                temp.write_text(json.dumps(plan, indent=2) + "\n")
                temp.replace(self.plan_store)
            except OSError:
                pass            # kept in memory; the next change tries the file again
        return True

    def keep_plan_in(self, path):
        """Keep the peer's plan in ``path``, and start from what it holds."""
        self.plan_store = path
        loaded = self._load_plan()
        with self._lock:
            self._plan = self._plan or loaded

    def plan(self):
        """The peer's plan as last read (kept across restarts), or None."""
        with self._lock:
            return copy.deepcopy(self._plan)

    def run(self):
        """The poller: read the peer every ``interval`` seconds until ``stop()``."""
        while not self._stopping.is_set():
            self.refresh()
            self._wakeup.wait(self.interval)
            self._wakeup.clear()

    def start(self):
        threading.Thread(target=self.run, name="peer-poller", daemon=True).start()
        return self

    def stop(self):
        self._stopping.set()
        self._wakeup.set()

    def poke(self):
        """Read again now, after a change on the peer."""
        self._wakeup.set()

    @property
    def reachable(self):
        with self._lock:
            return self._at is not None and self.clock() - self._at < self.max_age

    def node(self):
        with self._lock:
            return (self._read or {}).get("node")

    def location(self):
        with self._lock:
            return (self._read or {}).get("location")

    def tuners(self):
        """The peer's tuners as this node shows them: ``local`` false, their streams on the
        relay, and every one unknown and unavailable when the peer has not answered lately."""
        with self._lock:
            read, at, error = copy.deepcopy(self._read), self._at, self.last_error
        fresh = at is not None and self.clock() - at < self.max_age
        result = []
        for tuner in (read or {}).get("tuners") or []:
            if not isinstance(tuner, dict) or not tuner.get("local"):
                continue            # the peer's view of this node's own tuners
            tuner = {**tuner, "local": False}
            stream = tuner.get("stream")
            if isinstance(stream, dict) and stream.get("playlist_path"):
                stream["playlist_path"] = stream_path(stream["playlist_path"])
            if isinstance(tuner.get("lock"), dict):
                tuner["lock"] = {**tuner["lock"], "lane": tuner["id"]}   # released by tuner id
            if not fresh:
                tuner.update(reachable=False, available=False, state="unknown",
                             error=error or "the peer has not answered")
                if isinstance(stream, dict):
                    stream["ready"] = False
            result.append(tuner)
        return result

    def get(self, path, timeout=None):
        """One of the peer's documents, read now: its JSON, or PeerUnavailable."""
        status, payload = self._request("GET", path, timeout=timeout)
        if status != 200:
            raise PeerUnavailable(f"the peer answered {status} for {path}")
        return payload

    def post(self, path, body):
        """One of the peer's own changes: ``(payload, status)``."""
        return self._command(path, body)

    # -- changes -----------------------------------------------------------------------------
    def tune(self, body):
        """The peer's ``POST /api/v1/tune``, from its own tuners only: ``(payload, status)``."""
        payload, status = self._command("/api/v1/tune", {**body, "local_only": True})
        if status == 200 and isinstance(payload.get("stream"), dict):
            payload["stream"]["playlist_path"] = stream_path(payload["stream"].get("playlist_path"))
            for playback in payload["stream"].get("playback") or []:
                if isinstance(playback, dict) and playback.get("url"):
                    playback["url"] = stream_path("/" + playback["url"].lstrip("/")).lstrip("/")
        return payload, status

    def lock(self, body):
        return self._command("/api/v1/tuner/lock", body)

    def release(self, body):
        return self._command("/api/v1/tuner/lock/release", body)

    def _command(self, path, body):
        try:
            status, payload = self._request("POST", path, body, timeout=self.command_timeout)
        finally:
            self.poke()
        return payload, status
