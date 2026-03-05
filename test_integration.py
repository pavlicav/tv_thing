#!/usr/bin/env python3
"""Integration tests for TV Thing server.

Requires the server to be running:
    python3 server.py

Requires DVB hardware (Hauppauge HVR-1600) for tune and rescan tests.

Usage:
    pytest test_integration.py -v           # all tests (rescan takes ~5 min)
    pytest test_integration.py -v -m "not slow"  # skip rescan
"""

import json
import random
import time

import pytest
import requests

BASE_URL = "http://localhost:8080"
TUNE_TIMEOUT = 45   # seconds to wait for first HLS segment after tuning
SEGMENT_MIN = 1_000  # minimum bytes for a valid .ts segment (some subchannels are low-bitrate)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def server_running():
    """Skip the entire session if the server isn't reachable."""
    try:
        requests.get(f"{BASE_URL}/api/status", timeout=3)
    except requests.exceptions.ConnectionError:
        pytest.skip("Server not running — start it with: python3 server.py")


@pytest.fixture(scope="session")
def channels():
    r = requests.get(f"{BASE_URL}/api/channels", timeout=5)
    r.raise_for_status()
    data = r.json()
    if not data:
        pytest.skip("No channels loaded — run a scan first")
    return data


@pytest.fixture(scope="session")
def good_channel(channels):
    """Find a channel that reliably produces a stream (tries up to 5 at random)."""
    sample = random.sample(channels, min(5, len(channels)))
    for ch in sample:
        r = requests.post(f"{BASE_URL}/api/tune",
                          json={"channel_id": ch["id"], "quality": "low"},
                          timeout=10)
        if r.json().get("ok"):
            m3u8 = wait_for_m3u8(timeout=30)
            requests.post(f"{BASE_URL}/api/stop", timeout=5)
            time.sleep(3)
            if m3u8:
                print(f"\n  [good_channel] using {ch['number']} {ch['name']}")
                return ch
    pytest.skip("Could not find a channel that produces a stream")


@pytest.fixture(autouse=True)
def stop_after():
    """Stop any active stream after each test and wait for DVB device to release."""
    yield
    requests.post(f"{BASE_URL}/api/stop", timeout=5)
    time.sleep(3)


# ---------------------------------------------------------------------------
# API smoke tests (no hardware needed)
# ---------------------------------------------------------------------------

class TestAPI:
    def test_status_fields(self):
        r = requests.get(f"{BASE_URL}/api/status", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert "streaming" in data
        assert "quality" in data
        assert "channel" in data

    def test_channels_nonempty(self, channels):
        assert len(channels) > 0

    def test_channel_schema(self, channels):
        required = {"id", "name", "number", "frequency", "service_id"}
        for ch in channels:
            assert required <= ch.keys(), f"Channel missing fields: {ch}"

    def test_tune_unknown_channel(self):
        r = requests.post(f"{BASE_URL}/api/tune",
                          json={"channel_id": "does_not_exist"},
                          timeout=5)
        assert r.status_code == 404

    def test_stop_when_idle(self):
        r = requests.post(f"{BASE_URL}/api/stop", timeout=5)
        assert r.status_code == 200
        assert r.json().get("ok")

    def test_static_index(self):
        r = requests.get(f"{BASE_URL}/", timeout=5)
        assert r.status_code == 200
        assert "text/html" in r.headers["Content-Type"]

    def test_static_404(self):
        r = requests.get(f"{BASE_URL}/nonexistent.html", timeout=5)
        assert r.status_code == 404

    def test_stream_404_before_tuning(self):
        requests.post(f"{BASE_URL}/api/stop", timeout=5)
        time.sleep(0.5)
        r = requests.get(f"{BASE_URL}/stream/live.m3u8", timeout=5)
        assert r.status_code == 404

    def test_path_traversal_blocked(self):
        for path in ["/stream/../server.py", "/stream/../../etc/passwd"]:
            r = requests.get(f"{BASE_URL}{path}", timeout=5)
            assert r.status_code in (400, 404), \
                f"Path traversal not blocked for {path}"


# ---------------------------------------------------------------------------
# Tuner tests (require DVB hardware)
# ---------------------------------------------------------------------------

def wait_for_m3u8(timeout=TUNE_TIMEOUT):
    """Poll until live.m3u8 exists and is non-empty. Returns response or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{BASE_URL}/stream/live.m3u8", timeout=5)
            if r.status_code == 200 and r.content:
                return r
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    return None


def tune_and_wait(channel_id, quality="low"):
    """Tune to a channel and wait for the first segment. Returns (m3u8_response, tune_response)."""
    r = requests.post(f"{BASE_URL}/api/tune",
                      json={"channel_id": channel_id, "quality": quality},
                      timeout=10)
    r.raise_for_status()
    m3u8 = wait_for_m3u8()
    return m3u8, r.json()


def first_segment_url(m3u8_text):
    """Extract the first .ts segment name from an m3u8 playlist."""
    for line in m3u8_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


class TestTuner:
    def test_tune_three_random_channels(self, channels):
        """Tune to 3 random channels; each must produce a non-empty .ts segment."""
        sample = random.sample(channels, min(3, len(channels)))

        for ch in sample:
            print(f"\n  Tuning: {ch['number']} {ch['name']} "
                  f"({ch['frequency']/1e6:.0f} MHz, SID={ch['service_id']})")

            m3u8_resp, tune_data = tune_and_wait(ch["id"])

            assert tune_data.get("ok"), \
                f"Tune rejected for {ch['name']}: {tune_data}"
            assert m3u8_resp is not None, \
                f"No m3u8 appeared for {ch['name']} within {TUNE_TIMEOUT}s"

            seg_name = first_segment_url(m3u8_resp.text)
            assert seg_name, f"No segment listed in m3u8 for {ch['name']}"

            seg_resp = requests.get(f"{BASE_URL}/stream/{seg_name}", timeout=10)
            assert seg_resp.status_code == 200, \
                f"Segment {seg_name} returned {seg_resp.status_code}"
            assert len(seg_resp.content) >= SEGMENT_MIN, \
                (f"Segment too small for {ch['name']}: "
                 f"{len(seg_resp.content)} bytes (expected >= {SEGMENT_MIN})")

            print(f"    OK: segment {seg_name} = {len(seg_resp.content):,} bytes")

            requests.post(f"{BASE_URL}/api/stop", timeout=5)
            time.sleep(2)

    def test_status_reflects_active_stream(self, good_channel):
        """Status endpoint shows the correct channel while streaming."""
        ch = good_channel
        m3u8_resp, _ = tune_and_wait(ch["id"])
        assert m3u8_resp is not None, "No stream appeared"

        status = requests.get(f"{BASE_URL}/api/status", timeout=5).json()
        assert status["streaming"] is True
        assert status["channel"] is not None
        assert status["channel"]["id"] == ch["id"]

    def test_status_clears_after_stop(self, good_channel):
        """Status shows not streaming after /api/stop."""
        ch = good_channel
        tune_and_wait(ch["id"])
        requests.post(f"{BASE_URL}/api/stop", timeout=5)
        time.sleep(1)
        status = requests.get(f"{BASE_URL}/api/status", timeout=5).json()
        assert status["streaming"] is False
        assert status["channel"] is None

    def test_quality_low(self, good_channel):
        """Low quality stream still produces data."""
        ch = good_channel
        m3u8_resp, tune_data = tune_and_wait(ch["id"], quality="low")
        assert tune_data.get("ok")
        assert m3u8_resp is not None
        status = requests.get(f"{BASE_URL}/api/status", timeout=5).json()
        assert status["quality"] == "low"

    def test_retune_to_different_channel(self, channels, good_channel):
        """Switching channels stops the old stream and starts a new one."""
        others = [c for c in channels if c["id"] != good_channel["id"]]
        if not others:
            pytest.skip("Need at least 2 channels")

        ch1, ch2 = good_channel, random.choice(others)

        tune_and_wait(ch1["id"])
        # Tune to second channel without explicitly stopping first
        m3u8_resp, tune_data = tune_and_wait(ch2["id"])

        assert tune_data.get("ok")
        assert m3u8_resp is not None

        status = requests.get(f"{BASE_URL}/api/status", timeout=5).json()
        assert status["channel"]["id"] == ch2["id"]


# ---------------------------------------------------------------------------
# Rescan test (slow — runs w_scan across all ATSC frequencies, ~5 minutes)
# ---------------------------------------------------------------------------

def parse_sse(response):
    """Yield (event, data) pairs from a streaming SSE response."""
    event = "message"
    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line.startswith("event:"):
            event = raw_line[len("event:"):].strip()
        elif raw_line.startswith("data:"):
            data = raw_line[len("data:"):].strip()
            yield event, data
            event = "message"


@pytest.mark.slow
class TestRescan:
    def test_rescan_finds_channels(self):
        """Full ATSC scan completes and populates the channel list."""
        with requests.get(f"{BASE_URL}/api/rescan/stream",
                          stream=True, timeout=600) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers.get("Content-Type", "")

            events = []
            result = None

            for event, data in parse_sse(r):
                events.append(event)
                print(f"  SSE {event}: {data[:80]}")
                if event == "done":
                    result = json.loads(data)
                    break

        assert result is not None, "Rescan never sent 'done' event"
        assert result.get("ok"), f"Rescan failed: {result.get('error')}"
        assert result.get("count", 0) > 0, "Rescan found no channels"

        seen = set(events)
        assert "frequency" in seen, "No frequency events during scan"
        assert "progress" in seen, "No progress events during scan"

        # Channels should now load
        channels = requests.get(f"{BASE_URL}/api/channels", timeout=5).json()
        assert len(channels) == result["count"]
        print(f"\n  Rescan found {result['count']} channels")

    def test_tune_blocked_during_scan(self, channels):
        """Tuning while a scan is in progress returns 409."""
        import threading

        scan_started = threading.Event()
        scan_result = {}

        def run_scan():
            with requests.get(f"{BASE_URL}/api/rescan/stream",
                              stream=True, timeout=600) as r:
                for event, data in parse_sse(r):
                    if event in ("status", "frequency"):
                        scan_started.set()
                    if event == "done":
                        scan_result["done"] = json.loads(data)
                        break

        t = threading.Thread(target=run_scan, daemon=True)
        t.start()

        scan_started.wait(timeout=30)

        r = requests.post(f"{BASE_URL}/api/tune",
                          json={"channel_id": channels[0]["id"]},
                          timeout=5)
        assert r.status_code == 409, \
            f"Expected 409 while scanning, got {r.status_code}: {r.text}"

        t.join(timeout=600)
