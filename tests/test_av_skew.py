"""A/V skew (av_skew.py): a digital stream whose sound runs apart from its picture is not
playing its channel healthily, so the pool and the next tune give it up for a fresh encode."""
import pytest

import av_skew
import server
import tuner_pool

VIDEO_PID, AUDIO_PID, PMT_PID = 0x100, 0x101, 0x1000


def _packet(pid, payload, start=True):
    header = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF, 0x10])
    return (header + payload).ljust(188, b"\xff")


def _section(table_id, body):
    length = len(body) + 4 + 5            # the fields after length, and the CRC
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF, 0, 1, 0xC1, 0, 0])
    return b"\x00" + head + body + b"\x00\x00\x00\x00"


def _pes(stream_id, pts):
    p = bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, 0x01 | ((pts >> 14) & 0xFE),
               (pts >> 7) & 0xFF, 0x01 | ((pts << 1) & 0xFE)])
    return b"\x00\x00\x01" + bytes([stream_id, 0, 0, 0x80, 0x80, 5]) + p


def segment(tmp_path, video_pts, audio_pts, *, audio_type=0x0F, name="seg.ts"):
    pat = _section(0x00, bytes([0, 1, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF]))
    streams = bytes([0x1B, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0])
    if audio_type is not None:
        streams += bytes([audio_type, 0xE0 | (AUDIO_PID >> 8), AUDIO_PID & 0xFF, 0xF0, 0])
    pmt = _section(0x02, bytes([0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0]) + streams)
    data = _packet(0, pat) + _packet(PMT_PID, pmt) + _packet(VIDEO_PID, _pes(0xE0, video_pts)) \
        + _packet(VIDEO_PID, b"\x00" * 100, start=False) + _packet(AUDIO_PID, _pes(0xC0, audio_pts))
    path = tmp_path / name
    path.write_bytes(data)
    return path


# -- reading a segment ------------------------------------------------------------------------
def test_a_segment_in_sync(tmp_path):
    assert av_skew.segment_skew(segment(tmp_path, 90000 * 100, 90000 * 100 - 1800)) == 0.02


def test_sound_behind_its_picture_reads_positive(tmp_path):
    # viviserv's WTTG, 2026-11-08: video 717.632, audio 705.343.
    assert av_skew.segment_skew(segment(tmp_path, 64586897, 63480896)) == pytest.approx(12.289, abs=0.001)
    assert av_skew.segment_skew(segment(tmp_path, 63480896, 64586897)) == pytest.approx(-12.289, abs=0.001)


def test_the_33_bit_clock_wrapping_between_them_is_no_skew(tmp_path):
    assert av_skew.segment_skew(segment(tmp_path, 900, (1 << 33) - 900)) == 0.02


def test_a_segment_without_sound_or_unreadable_says_nothing(tmp_path):
    assert av_skew.segment_skew(segment(tmp_path, 900, 900, audio_type=None)) is None
    assert av_skew.segment_skew(tmp_path / "gone.ts") is None
    junk = tmp_path / "junk.ts"
    junk.write_bytes(b"\x00" * 4000)
    assert av_skew.segment_skew(junk) is None


def test_the_newest_finished_segment_of_the_generation(tmp_path):
    for name in ("digitalseg_g1_000000008.ts", "digitalseg_g1_000000009.ts", "digitalseg_g1_000000010.ts.tmp",
                 "digitalseg_g0_000000099.ts"):
        (tmp_path / name).write_bytes(b"")
    assert av_skew.newest_segment(tmp_path, "digitalseg", "g1").name == "digitalseg_g1_000000009.ts"
    assert av_skew.newest_segment(tmp_path, "digitalseg", "g2") is None
    assert av_skew.newest_segment(tmp_path, "digitalseg", None) is None


# -- the verdict ------------------------------------------------------------------------------
def test_skewed_takes_three_readings_running_on_the_same_stream():
    skews = av_skew.AvSkew()
    assert [skews.record("digital", "g1", 12.4) for _ in range(3)] == [False, False, True]
    assert skews.skewed("digital", "g1") and skews.view("digital", "g1") == (12.4, True)
    assert not skews.record("digital", "g1", 12.5)          # said once
    # A fresh encode starts over.
    assert not skews.skewed("digital", "g2")
    skews.record("digital", "g2", 12.4)
    assert not skews.skewed("digital", "g2") and skews.view("digital", "g1") == (None, False)


def test_one_reading_in_sync_clears_it_and_none_is_no_reading():
    skews = av_skew.AvSkew()
    for seconds in (12.4, 12.4, 0.3, 12.4, 12.4):
        skews.record("digital", "g1", seconds)
    assert not skews.skewed("digital", "g1")
    skews.record("digital", "g1", None)
    assert not skews.skewed("digital", "g1")
    skews.record("digital", "g1", -3.0)
    assert skews.skewed("digital", "g1")
    skews.forget("digital")
    assert not skews.skewed("digital", "g1")


# -- the pool and the tune --------------------------------------------------------------------
WTTG = {"id": "wttg_5_1", "number": "5.1", "rf_channel": 36}


def tuner(node, local, *, skewed=False, available=True):
    return {"id": f"{node}:digital", "node": node, "lane": "digital", "kind": "digital", "local": local,
            "reachable": True, "lock": None, "rf_channels": None, "tsids": {}, "available": available,
            "selection": {"id": "wttg_5_1"}, "quality": "medium",
            "stream": {"id": f"{node}-1", "active": True, "ready": True, "error": None,
                       "av_skew_seconds": 12.4 if skewed else 0.3, "av_skewed": skewed}}


def test_a_skewed_stream_is_not_watched_along():
    here, there = tuner("viviserv", True, skewed=True), tuner("viviserv2", False)
    assert tuner_pool.choose([here, there], WTTG) == (there, "playing")


def test_a_skewed_stream_alone_is_retuned_fresh():
    here = tuner("viviserv", True, skewed=True)
    assert tuner_pool.choose([here], WTTG) == (here, "retune")


@pytest.fixture
def skewed_digital(monkeypatch):
    skews = av_skew.AvSkew()
    monkeypatch.setattr(server, "av_skews", skews)
    monkeypatch.setattr(server, "preempt_guide_refresh", lambda: None)
    monkeypatch.setattr(server, "busy_reason", lambda: None)
    monkeypatch.setattr(server, "load_channels", lambda: [{"id": "x"}])
    monkeypatch.setattr(server, "lane_is_running", lambda lane: lane == "digital")
    monkeypatch.setattr(server, "lane_stream_ready", lambda lane: True)
    monkeypatch.setitem(server.lanes, "digital", dict(server.new_lane_state(),
                        selection={"id": "x"}, stream_id="same", health={"ok": True}))
    for _ in range(av_skew.SAMPLES):
        skews.record("digital", "same", 12.4)
    return skews


def test_a_tune_for_a_skewed_stream_starts_a_fresh_encode(skewed_digital, monkeypatch):
    started = []
    monkeypatch.setattr(server, "start_digital", lambda *a, **k: started.append(a) or (True, None, False))
    assert not server.lane_plays("digital", "id", "x")
    _, status = server.perform_digital_tune("x")
    assert status == 200 and len(started) == 1


def test_the_listing_shows_the_skew(skewed_digital):
    tuner_here = next(t for t in server.local_tuners() if t["lane"] == "digital")
    assert (tuner_here["stream"]["av_skew_seconds"], tuner_here["stream"]["av_skewed"]) == (12.4, True)


def test_the_sampler_reads_the_lanes_newest_segment(tmp_path, monkeypatch):
    skews = av_skew.AvSkew()
    monkeypatch.setattr(server, "av_skews", skews)
    monkeypatch.setattr(server, "STREAMS_DIR", tmp_path)
    monkeypatch.setattr(server, "lane_is_running", lambda lane: lane == "digital")
    monkeypatch.setitem(server.lanes, "digital", dict(server.new_lane_state(),
                        selection={"id": "x", "name": "WTTG"}, stream_id="g1", health={"ok": True}))
    segment(tmp_path, 64586897, 63480896, name=f"{server.LANE_SEGMENT_PREFIX['digital']}_g1_000000001.ts")
    for _ in range(av_skew.SAMPLES):
        server.sample_av_skew()
    assert skews.view("digital", "g1") == (pytest.approx(12.289, abs=0.001), True)
