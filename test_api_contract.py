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


def test_a_locked_tuner_can_be_watched_but_not_changed_over_http(api):
    request, calls = api
    code, claim = request("/api/v1/tuner/lock", {"lane": "digital", "owner": "recorder", "duration_seconds": 60})
    token = claim["token"]
    code, tune = request("/api/v1/tune", {"source": "digital", "channel_id": "one", "quality": "high",
                                          "token": token})
    assert code == 200
    # The channel it plays is anyone's to watch, at the quality its holder chose.
    code, watch = request("/api/v1/tune", {"source": "digital", "channel_id": "one", "quality": "low"})
    assert code == 200 and watch["stream_reused"] is True
    assert watch["stream"]["id"] == tune["stream"]["id"] and watch["quality"] == "high"
    # Any other channel is still the holder's to allow.
    code, refusal = request("/api/v1/tune", {"source": "digital", "channel_id": "two"})
    assert code == 423 and refusal["lock"]["owner"] == "recorder"
    assert calls == ["one"]
    _, locks = request("/api/v1/tuner/lock")
    assert locks["locks"]["digital"]["owner"] == "recorder"


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


@pytest.fixture
def roku_api(api, monkeypatch):
    from test_roku_lane import GAME, NFL, Config, FakeController
    from types import SimpleNamespace
    import roku_lane

    request, _ = api
    capture = {"running": True, "stream_id": "cap-1", "tap_url": "udp://127.0.0.1:1"}
    controller = FakeController(capture)
    lane = roku_lane.RokuLane(
        Config(), controller,
        baseball=SimpleNamespace(game=lambda pk, day: dict(GAME),
                                 games=lambda day: {"date": day, "games": [dict(GAME)]}),
        sports=SimpleNamespace(game=lambda *a: dict(NFL),
                               games=lambda league, day: {"date": day, "games": [dict(NFL)]}),
        capture_source=lambda: dict(capture), playing_seconds=.2, sleep=lambda _: None)

    class Process:
        def poll(self):
            return None

    monkeypatch.setattr(server, "roku", lane)
    monkeypatch.setattr(server, "roku_sources_available", lambda: True)
    server.procs["roku_capture"] = Process()
    server.lanes["roku"] = dict(server.new_lane_state(), stream_id="cap-1", quality="source",
                                health={"ok": True}, video_encoder="copy")
    return request, lane


def wait_for(request, predicate):
    import time
    deadline = time.monotonic() + 5
    while True:
        _, status = request("/api/v1/status")
        if predicate(status["lanes"]["roku"]):
            return status["lanes"]["roku"]
        assert time.monotonic() < deadline, status["lanes"]["roku"]
        time.sleep(.02)


def test_roku_lane_lock_tune_and_reuse_over_http(roku_api):
    request, lane = roku_api
    code, sources = request("/api/v1/sources")
    roku_ids = [s["id"] for s in sources["sources"] if s.get("lane") == "roku"]
    assert roku_ids == ["youtube-tv", "mlb", "thursday-night-football", "sunday-ticket", "roku-manual"]
    code, channels = request("/api/v1/sources/youtube-tv")
    assert code == 200 and [i["id"] for i in channels["items"]] == ["NBC", "ESPN"]
    code, games = request("/api/v1/sources/mlb?date=2026-10-01")
    assert code == 200 and games["date"] == "2026-10-01" and games["items"][0]["id"] == "823254"

    code, claim = request("/api/v1/tuner/lock", {"lane": "roku", "owner": "VizelVision master", "duration_seconds": 90})
    assert code == 200
    token = claim["token"]
    code, refusal = request("/api/v1/tune", {"source": "youtube-tv", "channel_id": "ESPN"})
    assert code == 423 and refusal["lock"]["owner"] == "VizelVision master"
    code, refusal = request("/api/v1/roku/keypress", {"key": "Home"})
    assert code == 423 and lane.controller.ecp.keys == []

    code, tune = request("/api/v1/tune", {"source": "youtube-tv", "channel_id": "espn", "token": token})
    assert code == 202 and tune["lane"] == "roku"
    assert tune["operation"]["target"] == {"source_id": "youtube-tv", "channel_id": "ESPN"}
    roku = wait_for(request, lambda l: l["selection"] and l["selection"].get("channel_id") == "ESPN")
    assert roku["stream"]["state"] == "ready" and roku["stream"]["ready"]
    assert roku["stream"]["playback"][0]["url"] == "stream/roku.m3u8"
    assert roku["stream"]["generation"] == lane.generation()
    assert roku["roku"]["operation"]["state"] == "succeeded"

    code, again = request("/api/v1/tune", {"source": "youtube-tv", "channel_id": "ESPN", "token": token})
    assert code == 200 and again["reused"] is True
    assert again["stream"]["generation"] == roku["stream"]["generation"]
    code, absent = request("/api/v1/tune", {"source": "youtube-tv", "channel_id": "NBC", "token": token,
                                            "navigate": False})
    assert code == 409 and absent["error"]["code"] == "not_prepared"
    code, bad = request("/api/v1/tune", {"source": "youtube-tv", "channel_id": "HBO", "token": token})
    assert code == 404 and bad["error"]["code"] == "unknown_channel"


def test_a_locked_roku_can_be_watched_but_not_moved_over_http(roku_api):
    request, lane = roku_api
    code, claim = request("/api/v1/tuner/lock", {"lane": "roku", "owner": "VizelVision master", "duration_seconds": 90})
    token = claim["token"]
    code, _ = request("/api/v1/tune", {"source": "youtube-tv", "channel_id": "ESPN", "token": token})
    assert code == 202
    wait_for(request, lambda l: l["selection"] and l["selection"].get("channel_id") == "ESPN")

    code, watch = request("/api/v1/tune", {"source": "youtube-tv", "channel_id": "espn"})
    assert code == 200 and watch["reused"] is True and watch["stream"]["ready"]
    for other in ({"source": "youtube-tv", "channel_id": "NBC"},
                  {"source": "roku-manual", "channel_id": "manual"}):
        code, refusal = request("/api/v1/tune", other)
        assert code == 423 and refusal["lock"]["owner"] == "VizelVision master"
    assert lane.controller.plays == ["ESPN"] and lane.controller.ecp.keys == []
    assert lane.selection_view()["channel_id"] == "ESPN"


def test_every_channel_carries_its_tv_thing_number_over_http(roku_api, monkeypatch):
    request, _ = roku_api
    monkeypatch.setattr(server, "load_channels", lambda: [
        {"id": "wrc_hd_4_1", "number": "4.1", "name": "WRC-HD", "rf_channel": 34},
        {"id": "surprise_7_5", "number": "7.5", "name": "Surprise", "rf_channel": 7}])
    code, digital = request("/api/v1/sources/digital")
    assert code == 200
    # The antenna keeps its own numbers; a channel lineup.json has never heard
    # of (a rescan's surprise) is numbered in the hundreds, and keeps it.
    assert [(i["number"], i["tv_number"]) for i in digital["items"]] == [("4.1", 4), ("7.5", 300)]
    code, feeds = request("/api/v1/sources/youtube-tv")
    assert [(i["id"], i["tv_number"], i["tv_name"]) for i in feeds["items"]] == [
        ("NBC", 16, "WILX"), ("ESPN", 33, "ESPN")]
    code, tnf = request("/api/v1/sources/thursday-night-football?date=2026-10-01")
    assert [(i["id"], i["tv_number"], i["tv_section"]) for i in tnf["items"]] == [
        ("401872964", 61, "Live Sports")]
    code, sources = request("/api/v1/sources")
    assert [view["tv_number"] for view in sources["lineup"]["house"]] == [0, 70]
    # 60 is the station's Sports Guide, a page of slides rather than a stream.
    assert sources["lineup"]["slides"] == [{
        "tv_number": 60, "tv_name": "VizelVision Sports Guide", "tv_section": "Live Sports",
        "channel_id": "sports-guide", "page_path": "sports-guide.html",
        "data_path": "/api/v1/sports-guide"}]
    assert {"first": 1, "last": 29, "name": "Local stations"} in sources["lineup"]["sections"]

    code, card = request("/api/v1/lineup?date=2026-10-01")
    assert code == 200 and card["errors"] == [] and card["lineup_error"] is None
    assert [(row["tv_number"], row["kind"], row.get("channel_id")) for row in card["channels"]] == [
        (0, "vizelvision", None), (4, "digital", "wrc_hd_4_1"), (16, "roku", "NBC"),
        (33, "roku", "ESPN"), (60, "slides", "sports-guide"), (61, "roku", "401872964"),
        (62, "roku", "823254"), (70, "vizelvision", None), (109, "roku", "manual"),
        (300, "digital", "surprise_7_5")]
    assert card["channels"][4]["name"] == "VizelVision Sports Guide"
    assert card["channels"][4]["page_path"] == "sports-guide.html"
    assert card["channels"][5]["source_id"] == "thursday-night-football"
    assert card["channels"][5]["date"] == "2026-10-01"
    code, discovery = request("/api/v1")
    assert discovery["endpoints"]["lineup"]["path"] == "/api/v1/lineup"
    assert discovery["endpoints"]["sports_guide"]["path"] == "/api/v1/sports-guide"
    code, spec = request("/api/v1/openapi.json")
    assert "/sports-guide" in spec["paths"]


def test_roku_remote_and_check_over_http(roku_api):
    request, lane = roku_api
    code, _ = request("/api/v1/tune", {"source": "youtube-tv", "channel_id": "ESPN"})
    assert code == 202
    verified = wait_for(request, lambda l: l["selection"] and l["selection"].get("channel_id") == "ESPN")
    code, check = request("/api/v1/roku/check", {})
    assert code == 200 and check["playing"] is True
    code, pressed = request("/api/v1/roku/keypress", {"key": "Down"})
    assert code == 200 and pressed["selection"]["manual"] is True
    assert pressed["generation"] != verified["stream"]["generation"]
    code, bad = request("/api/v1/roku/keypress", {"key": "PowerOff"})
    assert code == 400
    code, check = request("/api/v1/roku/check", {})
    assert code == 200 and check["playing"] is False
    code, apps = request("/api/v1/roku/apps")
    assert code == 200 and {"id": "14", "name": "MLB"} in apps["apps"]


def test_roku_routes_answer_503_when_unconfigured(api, monkeypatch):
    request, _ = api
    monkeypatch.setattr(server, "roku", None)
    code, body = request("/api/v1/tune", {"source": "youtube-tv", "channel_id": "ESPN"})
    assert code == 503 and body["error"]["code"] == "roku_unavailable"
    code, body = request("/api/v1/roku/keypress", {"key": "Home"})
    assert code == 503
    code, status = request("/api/v1/status")
    assert code == 200 and status["lanes"]["roku"]["available"] is False
    code, roku = request("/api/v1/roku")
    assert code == 200 and roku["configured"] is False


def test_roku_engineering_view_survives_a_failing_probe(roku_api):
    request, lane = roku_api
    code, view = request("/api/v1/roku")
    assert code == 200 and view["configured"] is True
    assert "error" in view["calibration"]  # The fake capture has no signal probe.
    assert view["lane"]["id"] == "roku" and view["channels"] == ["NBC", "ESPN"]
    assert "Select" in view["remote_keys"]
