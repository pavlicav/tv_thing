"""Tuner B: a second DVB adapter as a live-only lane (host_config's tuner_b, digital-b)."""
from dataclasses import replace

import pytest

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
