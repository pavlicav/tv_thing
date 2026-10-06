"""HTTP control contract tests with simulated tuners; never access hardware."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import server


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "tuner_locks", {})
    monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "locks.json")
    monkeypatch.setattr(server, "lanes", {lane: server.new_lane_state() for lane in server.ALL_LANES})
    monkeypatch.setattr(server, "procs", {key: None for key in server.procs})
    monkeypatch.setattr(server, "primary_video_lane", "digital")
    monkeypatch.setattr(server, "scanning", False)
    monkeypatch.setattr(server, "guide_refreshing", False)
    monkeypatch.setattr(server, "preempt_guide_refresh", lambda: None)
    channels = [{"id": "one", "name": "One"}, {"id": "two", "name": "Two"}]
    monkeypatch.setattr(server, "load_channels", lambda: channels)
    monkeypatch.setattr(server, "lane_stream_ready", lambda lane: server.lanes[lane]["stream_id"] is not None)
    calls = []

    class Process:
        def poll(self):
            return None

    def start(channel, quality):
        calls.append(channel["id"])
        server.lanes["digital"] = dict(server.new_lane_state(), selection=channel,
                                       quality=quality, stream_id=f"generation-{len(calls)}",
                                       health={"ok": True})
        server.procs["digital_packager"] = Process()
        return True, None, False

    monkeypatch.setattr(server, "start_digital", start)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.TVHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(path, payload=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    yield request, calls
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


def test_acquire_tune_retry_renew_release_over_http(api):
    request, calls = api
    code, claim = request("/api/v1/tuner/lock", {"lane": "digital", "owner": "recorder", "duration_seconds": 60})
    assert code == 200
    token = claim["token"]
    code, refusal = request("/api/v1/tune", {"source": "digital", "channel_id": "one"})
    assert code == 423
    assert refusal["lock"]["owner"] == "recorder"
    assert "token" not in refusal["lock"]
    body = {"source": "digital", "channel_id": "one", "token": token}
    code, tune = request("/api/v1/tune", body)
    assert code == 200
    assert tune["stream"]["ready"] is True
    assert tune["stream"]["generation"] == tune["stream"]["id"]
    assert tune["stream"]["playback"][0]["url"] == "stream/digital.m3u8"
    _, repeat = request("/api/v1/tune", body)
    assert repeat["stream_reused"] is True
    assert calls == ["one"]
    _, status = request("/api/v1/status")
    assert status["lanes"]["digital"]["stream"]["id"] == tune["stream"]["id"]
    code, renewal = request("/api/v1/tuner/lock", {"lane": "digital", "owner": "recorder", "duration_seconds": 120, "token": token})
    assert code == 200 and renewal["renewed"]
    assert renewal["token"] == token
    code, _ = request("/api/v1/tuner/lock/release", {"lane": "digital", "token": "wrong"})
    assert code == 403
    code, release = request("/api/v1/tuner/lock/release", {"lane": "digital", "token": token})
    assert code == 200 and release["released"]
    assert server.lane_is_running("digital")
    code, next_tune = request("/api/v1/tune", {"source": "digital", "channel_id": "two"})
    assert code == 200 and not next_tune["stream_reused"]
    assert next_tune["stream"]["id"] != tune["stream"]["id"]


def test_contract_discovery_and_legacy_response(api):
    request, _ = api
    code, discovery = request("/api/v1")
    assert code == 200
    assert discovery["openapi_url"] == "api/v1/openapi.json"
    code, spec = request("/api/v1/openapi.json")
    assert code == 200 and spec["openapi"] == "3.1.0"
    assert "/tune" in spec["paths"]
    code, legacy = request("/api/tune", {"channel_id": "one"})
    assert code == 200 and legacy["stream_id"]
    assert "_lane" not in legacy
    code, locked = request("/api/v1/tuner/lock", {"lane": "digital", "owner": "owner"})
    code, public = request("/api/v1/tuner/lock")
    assert locked["token"] not in json.dumps(public)
