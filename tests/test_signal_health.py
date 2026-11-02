"""A tuner's signal grade from its frontend's samples (signal_health.py)."""
import json

import server
import signal_health

LOCKED = {"locked": True, "uncorrected_blocks": 0}
CHOPPY = {"locked": True, "uncorrected_blocks": 262}       # viviserv on 5.1, 2026-11-01
NO_SYNC = {"locked": False, "uncorrected_blocks": 168}
NO_COUNTER = {"locked": True, "uncorrected_blocks": 0xFFFFFFFF}


def test_a_sample_is_errored_unlocked_or_with_uncorrected_blocks():
    assert not signal_health.errored(LOCKED)
    assert signal_health.errored(CHOPPY)
    assert signal_health.errored(NO_SYNC)
    assert signal_health.errored(None)
    assert not signal_health.errored(NO_COUNTER)          # a driver without the counter
    assert not signal_health.errored({"locked": True, "uncorrected_blocks": None})


def test_the_verdict_comes_from_the_errored_share():
    assert signal_health.verdict(0.0) == "good"
    assert signal_health.verdict(0.10) == "good"
    assert signal_health.verdict(0.2) == "fair"
    assert signal_health.verdict(0.30) == "poor"


def test_no_verdict_until_enough_samples(tmp_path):
    health = signal_health.SignalHealth(tmp_path / "health.json")
    for _ in range(signal_health.MIN_SAMPLES - 1):
        health.record("digital", 36, CHOPPY)
    assert health.live("digital", 36)["verdict"] is None
    health.record("digital", 36, CHOPPY)
    assert health.live("digital", 36) == {"rf": 36, "verdict": "poor", "errored": 1.0,
                                          "samples": signal_health.MIN_SAMPLES}


def test_a_verdict_is_stored_per_rf_and_read_back(tmp_path):
    path = tmp_path / "health.json"
    health = signal_health.SignalHealth(path, clock=lambda: 1000)
    for index in range(signal_health.WINDOW):
        health.record("digital", 36, CHOPPY if index % 2 else LOCKED)
        health.record("digital-b", 34, LOCKED)
    assert health.stored() == {"34": "good", "36": "poor"}
    assert json.loads(path.read_text())["rf_channels"]["36"]["errored"] == 0.5
    assert signal_health.SignalHealth(path, clock=lambda: 1000).stored() == {"34": "good", "36": "poor"}


def test_a_new_channel_starts_the_window_over(tmp_path):
    health = signal_health.SignalHealth(tmp_path / "health.json")
    for _ in range(signal_health.WINDOW):
        health.record("digital", 36, CHOPPY)
    health.record("digital", 34, LOCKED)
    assert health.live("digital", 34) == {"rf": 34, "verdict": None, "errored": 0.0, "samples": 1}
    assert health.live("digital", 36)["samples"] == 0
    assert health.stored() == {"36": "poor"}             # what 36 was is kept


def test_a_stale_verdict_is_forgotten(tmp_path):
    now = [1000]
    health = signal_health.SignalHealth(tmp_path / "health.json", clock=lambda: now[0])
    for _ in range(signal_health.MIN_SAMPLES):
        health.record("digital", 36, CHOPPY)
    now[0] += signal_health.MAX_AGE_SECONDS + 1
    assert health.stored() == {}


def test_an_unreadable_file_is_no_verdicts(tmp_path):
    path = tmp_path / "health.json"
    path.write_text("{not json")
    assert signal_health.SignalHealth(path).stored() == {}


class FakeFeed:
    def __init__(self, lanes, samples):
        self.lanes, self.samples, self.measured = lanes, samples, []

    def lane(self, lane):
        return self.lanes[lane]

    def measure(self, device):
        self.measured.append(device)
        return self.samples[device]


def test_each_playing_tuner_is_sampled_and_its_grade_published(tmp_path, monkeypatch):
    feed = FakeFeed({"digital": {"running": True, "selection": {"rf_channel": 36}},
                     "digital-b": {"running": False, "selection": None}},
                    {server.FEED_CONFIG.lane("digital").device: CHOPPY})
    monkeypatch.setattr(server, "feed_client", feed)
    monkeypatch.setattr(server, "DIGITAL_TUNER_LANES", ("digital", "digital-b"))
    monkeypatch.setattr(server, "signal_grades", signal_health.SignalHealth(tmp_path / "health.json"))
    for _ in range(signal_health.MIN_SAMPLES):
        server.sample_signals()
    assert feed.measured == [server.FEED_CONFIG.lane("digital").device] * signal_health.MIN_SAMPLES
    assert server.signal_grades.live("digital", 36)["verdict"] == "poor"
    assert server.signal_grades.stored() == {"36": "poor"}


def test_a_tuner_sweeping_the_band_is_not_graded(tmp_path, monkeypatch):
    feed = FakeFeed({"digital": {"running": True, "selection": {"rf_channel": 36}}}, {})
    monkeypatch.setattr(server, "feed_client", feed)
    monkeypatch.setattr(server, "DIGITAL_TUNER_LANES", ("digital",))
    monkeypatch.setattr(server, "scanning", True)
    monkeypatch.setattr(server, "signal_grades", signal_health.SignalHealth(tmp_path / "health.json"))
    server.sample_signals()
    assert feed.measured == []
