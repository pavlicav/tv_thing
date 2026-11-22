"""Tuner B: a second DVB adapter as a live-only lane (host_config's tuner_b, digital-b)."""
import io
import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import feed_api
import feed_node
import server

WUSA = {"id": "wusa_hd_9_1", "name": "WUSA-HD", "number": "9.1", "rf_channel": 9,
        "frequency": 189_000_000, "service_id": 1, "video_pid": 49, "audio_pid": 52}


@pytest.fixture
def tuner_b(monkeypatch):
    """This server as viviserv2 runs it: tuner A on adapter 0, tuner B on adapter 1."""
    base = server.FEED_CONFIG
    a = base.lane("digital")
    device = replace(base.device(a.device), id="digital-b", name="Digital front-end B", adapter=1)
    lane = replace(a, id="digital-b", device="digital-b", name="Digital TV B", playlist="tunerb.m3u8",
                   segment_prefix="tunerbseg", files=("tunerb", "tunerbseg"), process="digital_b_packager")
    config = replace(base, devices=(*base.devices, device), lanes=(*base.lanes, lane))
    for name, value in (
            ("FEED_CONFIG", config), ("ALL_LANES", tuple(l.id for l in config.lanes)),
            ("DIGITAL_LANES", ("digital", "digital-b")), ("EXTRA_DIGITAL_LANES", ("digital-b",)),
            ("DIGITAL_TUNER_LANES", ("digital", "digital-b")), ("DIGITAL_ADAPTERS", {"digital": 0, "digital-b": 1}),
            ("LANE_PLAYLIST", {**server.LANE_PLAYLIST, "digital-b": "tunerb.m3u8"}),
            ("LANE_SEGMENT_PREFIX", {**server.LANE_SEGMENT_PREFIX, "digital-b": "tunerbseg"}),
            ("LANE_FILES", {**server.LANE_FILES, "digital-b": ("tunerb", "tunerbseg")}),
            ("LANE_FRONTEND", {**server.LANE_FRONTEND, "digital-b": "digital-b"}),
            ("procs", {**server.procs, "digital_b_source": None, "digital_b_packager": None}),
            ("lanes", {**server.lanes, "digital-b": server.new_lane_state()}),
            ("feed", feed_node.FeedNode(config)), ("lane_fetched", {})):
        monkeypatch.setattr(server, name, value)
    return config


def test_tuner_b_tunes_its_own_adapter_into_its_own_slots(tuner_b, monkeypatch):
    started, saved = [], []

    class FakeProcess:
        stderr = None

        def poll(self):
            return None

    monkeypatch.setattr(server, "start_digital_packager", lambda q, s, u, lane: (True, None))
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **k: started.append(cmd) or FakeProcess())
    monkeypatch.setattr(server.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda s: None})())
    monkeypatch.setattr(server, "save_playback_state", lambda ch, q: saved.append(ch["id"]))
    monkeypatch.setattr(server, "recording_tap_ports", lambda channel: pytest.fail("tuner B fed a recording"))
    monkeypatch.setattr(server, "primary_video_lane", "digital")
    monkeypatch.setattr(server, "clear_streams", lambda prefixes: None)

    ok, error, _ = server.start_digital(WUSA, "low", lane="digital-b")
    assert ok and error is None
    [command] = started
    assert command[command.index("--adapter") + 1] == "1" and "--tap-port" not in command
    assert server.procs["digital_b_source"] is not None and server.procs["digital_source"] is None
    assert server.lanes["digital-b"]["tuner"] == {"backend": server.TV_SOURCE_BACKEND, "adapter": 1}
    # Not the always-on channel, nor the legacy live.m3u8.
    assert saved == [] and server.primary_video_lane == "digital"
    assert server.lane_fetched["digital-b"]


def test_tuner_bs_encoder_writes_its_own_playlist(tuner_b):
    command = server.build_tv_packager_command("low", {"codec": "libx264", "hardware": False, "args": []},
                                               "g1", 18400, lane="digital-b")
    assert any(arg.endswith("/tunerb.m3u8") for arg in command)
    assert any("tunerbseg_g1_" in arg for arg in command)
    assert server.digital_roles("digital-b") == ("digital_b_source", "digital_b_packager")


def test_a_tune_on_tuner_b_leaves_the_scan_and_the_vcr_alone(tuner_b, monkeypatch):
    tuned = []

    class Feed:
        def lane(self, lane):
            return {**server.new_lane_state(), "running": False, "ready": False, "selection": None,
                    "health": None, "error": None, "stream_id": "g2" if tuned else None}

        def tune_digital(self, channel, quality, taps=(), lane="digital"):
            tuned.append((channel["id"], taps, lane))
            return True, None, False

    monkeypatch.setattr(server, "feed_client", Feed())
    monkeypatch.setattr(server, "load_channels", lambda: [WUSA])
    monkeypatch.setattr(server, "preempt_guide_refresh", lambda: pytest.fail("preempted tuner A's guide"))
    monkeypatch.setattr(server, "scanning", True)            # tuner A sweeps; B is free
    monkeypatch.setattr(server, "lane_payload", lambda lane: {"lane": lane})
    result, status = server.perform_digital_tune("wusa_hd_9_1", "low", lane="digital-b")
    assert status == 200 and result["lane"] == "digital-b"
    assert tuned == [("wusa_hd_9_1", (), "digital-b")]


def test_with_tuner_a_locked_the_pool_takes_tuner_b_with_no_peer(tuner_b, monkeypatch):
    monkeypatch.setattr(server, "peer", None)
    lock = {"lane": "digital", "owner": "VCR", "seconds_remaining": 600}

    def tuner(lane, locked, active):
        return {"id": f"{server.FEED_CONFIG.node}:{lane}", "node": server.FEED_CONFIG.node, "lane": lane,
                "local": True, "reachable": True, "lock": lock if locked else None, "available": not locked,
                "rf_channels": None, "selection": None, "stream": {"active": active, "ready": active}}

    monkeypatch.setattr(server, "local_tuners", lambda: [tuner("digital", True, True), tuner("digital-b", False, False)])
    monkeypatch.setattr(server, "load_channels", lambda: [WUSA])
    monkeypatch.setattr(server, "active_lock", lambda lane, now=None: lock if lane == "digital" else None)
    asked = []
    monkeypatch.setattr(server, "perform_digital_tune",
                        lambda channel_id, quality, token=None, override=False, lane="digital":
                        asked.append(lane) or ({"ok": True}, 200))
    monkeypatch.setattr(server, "lane_tune_response", lambda result, status, lane: ({"ok": True, "lane": lane}, 200))
    payload, status = server.api_v1_digital_tune({"channel_id": "wusa_hd_9_1"}, "low")
    assert status == 200 and asked == ["digital-b"] and payload["tuner"].endswith(":digital-b")
    # Named, it is tuner B whatever the pool would choose.
    asked.clear()
    server.api_v1_digital_tune({"channel_id": "wusa_hd_9_1", "tuner": f"{server.FEED_CONFIG.node}:digital-b"}, "low")
    assert asked == ["digital-b"]


def test_an_unwatched_unlocked_tuner_b_is_stopped_and_tuner_a_never(tuner_b, monkeypatch):
    monkeypatch.setattr(server, "EXTRA_TUNER_IDLE_SECONDS", 300)
    monkeypatch.setattr(server, "active_lock", lambda lane, now=None: None)
    now = 10_000.0
    running = {"running": True, "started_at": now - 900}
    assert server.extra_tuner_idle("digital-b", {**running, "fetched_at": now - 301}, now)
    assert not server.extra_tuner_idle("digital-b", {**running, "fetched_at": now - 60}, now)
    assert not server.extra_tuner_idle("digital-b", {**running, "fetched_at": None, "started_at": now - 60}, now)
    assert not server.extra_tuner_idle("digital-b", {**running, "fetched_at": now - 301, "unknown": True}, now)
    assert not server.extra_tuner_idle("digital", {**running, "fetched_at": now - 3000}, now)
    monkeypatch.setattr(server, "active_lock", lambda lane, now=None: {"owner": "me"})
    assert not server.extra_tuner_idle("digital-b", {**running, "fetched_at": now - 3000}, now)


def test_a_segment_fetch_marks_its_lane_watched(tuner_b):
    server.note_lane_fetch("tunerbseg_g7_000000012.ts")
    assert set(server.lane_fetched) == {"digital-b"}
    server.note_lane_fetch("digitalseg_g3_000000001.ts")
    server.note_lane_fetch("tunerb.m3u8")                     # a playlist alone is not a viewer
    assert set(server.lane_fetched) == {"digital-b", "digital"}


def test_the_feed_api_tunes_tuner_b_through_its_own_lane(tuner_b):
    import feed_api
    calls = []

    class Local:
        ops = server

        def tune_digital(self, channel, quality, taps=(), op_id=None, expected=None, lane=None):
            calls.append(lane)
            return True, None, False

    api = feed_api.FeedAPI.__new__(feed_api.FeedAPI)
    api.local = Local()
    api._lane_id = lambda lane: lane
    for lane in ("digital-b", "digital"):
        assert api._route("POST", ["lanes", lane, "tune"], {"op_id": "x", "channel": WUSA}, None)[0] == 200
    assert calls == ["digital-b", None]


def test_a_source_that_never_locks_leaves_its_reason_on_the_lane(tuner_b, monkeypatch):
    """The lane is cleared when its source dies, but why stays on it until the next start:
    a lease holder reads "Could not retrieve signal for WUSA-HD (RF 9): …", not an empty tuner."""
    threads = []

    class FakeProcess:
        returncode = 1
        stderr = iter(["DVB source failed: Could not retrieve signal: the antenna got no lock in 3 s\n"])

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 1

    class FakeThread:
        def __init__(self, target=None, args=(), **_):
            self.target, self.args = target, args

        def start(self):
            threads.append(self)

    monkeypatch.setattr(server, "start_digital_packager", lambda q, s, u, lane: (True, None))
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **k: FakeProcess())
    monkeypatch.setattr(server.threading, "Thread", FakeThread)
    monkeypatch.setattr(server, "clear_streams", lambda prefixes: None)

    ok, _, _ = server.start_digital(WUSA, "low", lane="digital-b")
    assert ok
    [monitor] = threads
    monitor.target(*monitor.args)
    state = server.lanes["digital-b"]
    assert state["selection"] is None and not state["active"]
    assert state["error"] == "Could not retrieve signal for WUSA-HD (RF 9): the antenna got no lock in 3 s"


# -- a feed that started without tuner B ------------------------------------------------------
# viviserv2, 2026-11-22: the WinTV-dualHD enumerated after the services started, so the feed
# had no digital-b; control, restarted later, had. Every API request failed on KeyError.

class _Response(io.BytesIO):
    status = 200


class FakeFeedHTTP:
    """The feed's /feed/v1/ as RemoteFeed's opener: its snapshot, and every request asked."""

    def __init__(self, snapshot):
        self.snap, self.asked = snapshot, []

    def __call__(self, request, timeout=None):
        path = request.full_url.split(feed_api.PREFIX, 1)[1]
        self.asked.append((request.get_method(), path))
        if path == "lanes":
            body = self.snap
        elif path.endswith("/tune"):
            body = {"result": [True, None, False]}
        else:
            body = {}
        return _Response(json.dumps(body).encode())


def feed_snapshot(config):
    """What a feed started with ``config`` answers at GET /feed/v1/lanes, over the wire."""
    return json.loads(json.dumps(feed_node.LocalFeed(feed_node.FeedNode(config), server).snapshot(), default=str))


@pytest.fixture
def feed_without_b(tuner_b, monkeypatch):
    """Control with tuner B (the tuner_b fixture), its feed without: started before adapter 1."""
    fake = FakeFeedHTTP(feed_snapshot(BASE_CONFIG))
    assert "digital-b" not in fake.snap["lanes"] and "digital" in fake.snap["lanes"]
    remote = server.ControlFeed("http://127.0.0.1:1/", tuner_b, server.new_lane_state,
                                cache_seconds=0, opener=fake, sleep=lambda s: None)
    monkeypatch.setattr(server, "feed_client", remote)
    monkeypatch.setattr(server, "peer", None)
    monkeypatch.setattr(server, "scanning", False)
    monkeypatch.setattr(server, "tuning_lanes", set())
    monkeypatch.setattr(server, "active_lock", lambda lane, now=None: None)
    return SimpleNamespace(remote=remote, http=fake)


BASE_CONFIG = server.FEED_CONFIG


def test_a_lane_the_feed_lacks_reads_unknown_never_free(feed_without_b, capsys):
    remote = feed_without_b.remote
    b = remote.lane("digital-b")
    assert b["unknown"] and b["absent"] and b["lifecycle"] == "unknown"
    # Busy and broken, as every lane of a feed that is not answering: never free.
    assert b["running"] and not b["alive"] and not b["ready"] and b["selection"] is None
    assert "has no digital-b" in b["error"]
    a = remote.lane("digital")
    assert not a.get("unknown") and not a.get("absent")
    assert remote.status()["lanes"]["digital-b"]["lifecycle"] == "unknown"
    # What runs is the feed's word: nothing of digital-b does.
    assert "digital-b" not in remote.running_lanes()
    remote.lane("digital-b")
    assert capsys.readouterr().out.count("the feed has no digital-b") == 1    # said once


def test_stopping_a_lane_the_feed_lacks_asks_nothing_and_tuning_it_is_refused(feed_without_b):
    remote, http = feed_without_b.remote, feed_without_b.http
    remote.stop("digital-b")
    assert [path for method, path in http.asked if method == "POST"] == []
    remote.stop_all()                         # every lane but digital-b, and no 404 from the feed
    stopped = [path for method, path in http.asked if method == "POST"]
    assert stopped == [f"lanes/{lane.id}/stop" for lane in BASE_CONFIG.lanes]
    with pytest.raises(feed_api.FeedUnavailable, match="no digital-b"):
        remote.tune_digital(WUSA, "low", lane="digital-b")
    assert not any(path == "lanes/digital-b/tune" for _, path in http.asked)
    assert remote.tune_digital(WUSA, "low", lane="digital") == (True, None, False)


def test_the_pool_and_the_samplers_pass_over_a_lane_the_feed_lacks(feed_without_b, monkeypatch, capsys):
    tuners = {tuner["lane"]: tuner for tuner in server.local_tuners()}
    b = tuners["digital-b"]
    assert not b["reachable"] and not b["available"] and b["state"] == "unknown"
    assert tuners["digital"]["reachable"] and tuners["digital"]["available"]
    server.sample_signals()
    server.sample_av_skew()
    assert "could not" not in capsys.readouterr().out
    assert not any("digital-b" in path for _, path in feed_without_b.http.asked)
    # Tuner A locked, no peer: tuner B is not chosen, and nothing is tuned.
    now = server.time.time()
    lock = {"lane": "digital", "owner": "VCR", "token": "t", "acquired_at": now, "expires_at": now + 600}
    monkeypatch.setattr(server, "active_lock", lambda lane, now=None: lock if lane == "digital" else None)
    monkeypatch.setattr(server, "load_channels", lambda: [WUSA])
    asked = []
    monkeypatch.setattr(server, "perform_digital_tune",
                        lambda channel_id, quality, token=None, override=False, lane="digital":
                        asked.append(lane) or ({"ok": True}, 200))
    payload, status = server.api_v1_digital_tune({"channel_id": "wusa_hd_9_1"}, "low")
    assert asked == [] and status == 423, payload


def test_the_api_answers_with_a_lane_the_feed_lacks(feed_without_b, monkeypatch):
    monkeypatch.setattr(server, "tuner_locks", {})
    monkeypatch.setattr(server, "load_channels", lambda: [WUSA])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.TVHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(path, payload=None):
        req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}{path}",
                                     data=None if payload is None else json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    try:
        status, payload = request("/api/v1/status")
        assert status == 200, payload
        lanes = {lane["id"]: lane for lane in payload["lanes"]} if isinstance(payload["lanes"], list) \
            else payload["lanes"]
        assert lanes["digital-b"]["lifecycle"] == "unknown"
        status, payload = request("/api/v1/tuners")
        assert status == 200, payload
        b = next(tuner for tuner in payload["tuners"] if tuner["lane"] == "digital-b")
        assert b["state"] == "unknown" and not b["available"]
        # Named, tuner B is refused as a feed that is not answering, not a 500.
        status, payload = request("/api/v1/tune", {"source": "digital", "channel_id": "wusa_hd_9_1",
                                                   "tuner": server.tuner_id("digital-b")})
        assert status == 503, payload
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_the_lane_reads_as_the_feed_says_once_the_feed_has_it(feed_without_b, tuner_b, capsys):
    remote, http = feed_without_b.remote, feed_without_b.http
    assert remote.lane("digital-b")["absent"]
    http.snap = feed_snapshot(tuner_b)          # the feed restarted after adapter 1 came up
    b = remote.lane("digital-b")
    assert not b.get("absent") and not b.get("unknown") and b["lifecycle"] != "unknown"
    assert next(t for t in server.local_tuners() if t["lane"] == "digital-b")["reachable"]
    remote.stop("digital-b")
    assert ("POST", "lanes/digital-b/stop") in http.asked
    assert "the feed has every lane of control's" in capsys.readouterr().out


def test_a_feed_not_answering_still_reads_every_lane_unknown(feed_without_b):
    remote = feed_without_b.remote

    def down(request, timeout=None):
        raise feed_api.URLError("connection refused")

    remote.opener = down
    snap = remote.snapshot()
    assert snap["unknown"] and set(snap["lanes"]) == {lane.id for lane in remote.config.lanes}
    assert not snap["lanes"]["digital-b"].get("absent")
    with pytest.raises(feed_api.FeedUnavailable):
        remote.stop("digital-b")
