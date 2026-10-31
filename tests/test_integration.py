#!/usr/bin/env python3
"""Integration tests for TV Thing server.

Requires the server to be running:
    python3 server.py

Requires DVB hardware (Hauppauge HVR-1600) for tune and rescan tests.

It is never collected unless asked for (tests/conftest.py):
    TV_THING_INTEGRATION=1 pytest tests/test_integration.py -v           # all tests (rescan takes ~5 min)
    TV_THING_INTEGRATION=1 pytest tests/test_integration.py -v -m "not slow"  # skip rescan
"""

import json
import os
import random
import sys
import time
from pathlib import Path

import pytest

if os.environ.get("TV_THING_INTEGRATION") != "1":
    pytest.skip("drives a running server: set TV_THING_INTEGRATION=1 to run it", allow_module_level=True)

import requests  # noqa: E402

# Override to point the suite at a development instance:
#     TV_THING_INTEGRATION=1 TV_THING_URL=http://localhost:8099 pytest tests/test_integration.py
BASE_URL = os.environ.get("TV_THING_URL", "http://localhost:8080")
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "scripts"))   # fetch_vct, parse_channels
TUNE_TIMEOUT = 45   # seconds to wait for first HLS segment after tuning
SEGMENT_MIN = 1_000  # minimum bytes for a valid .ts segment (some subchannels are low-bitrate)
CHANNEL_CHANGE_MAX_SECONDS = float(
    os.environ.get("TV_THING_MAX_CHANNEL_CHANGE_SECONDS", "8")
)
GLITCH_TEST_SEGMENTS = int(os.environ.get("TV_THING_GLITCH_TEST_SEGMENTS", "6"))
MAX_SEGMENT_DURATION = float(
    os.environ.get("TV_THING_MAX_SEGMENT_DURATION", "3")
)
PLAYLIST_POLL_SECONDS = 0.1


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
    """Find a channel that produces a stream, preferring the active channel."""
    sample = []
    try:
        status = requests.get(f"{BASE_URL}/api/status", timeout=5).json()
        active_id = (status.get("channel") or {}).get("id")
        active = next((c for c in channels if c["id"] == active_id), None)
        if active:
            sample.append(active)
    except requests.exceptions.RequestException:
        pass

    # Then try one primary service from each RF multiplex. This is stable
    # across test runs and avoids a random sample made entirely of weak or
    # low-bitrate subchannels.
    seen_frequencies = {c["frequency"] for c in sample}
    ordered = sorted(
        channels,
        key=lambda c: (
            not str(c.get("number", "")).endswith(".1"),
            c.get("rf_channel", 999),
            c.get("number", ""),
        ),
    )
    for channel in ordered:
        if channel["frequency"] not in seen_frequencies:
            sample.append(channel)
            seen_frequencies.add(channel["frequency"])
        if len(sample) == 5:
            break

    for ch in sample:
        m3u8, tune_data = tune_and_wait(ch["id"], timeout=30)
        if tune_data.get("ok") and m3u8:
            print(f"\n  [good_channel] using {ch['number']} {ch['name']}")
            return ch
    pytest.skip("Could not find a channel that produces a stream")


# ---------------------------------------------------------------------------
# API smoke tests (no hardware needed)
# ---------------------------------------------------------------------------

class TestAPI:
    def test_status_fields(self):
        r = requests.get(f"{BASE_URL}/api/status", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert "streaming" in data
        assert "stream_ready" in data
        # The legacy view mirrors whichever video lane is primary, so the
        # backend depends on whether digital or analog was tuned last.
        assert data["tuner_backend"] in ("linux-dvb", "analog-v4l2")
        assert "quality" in data
        assert "channel" in data
        assert "video_encoder" in data
        assert "gpu_encoding" in data
        assert "stream_id" in data
        assert "stream_health" in data
        assert data["hls_segment_time"] > 0

    def test_v1_exposes_lanes_and_frontends(self):
        r = requests.get(f"{BASE_URL}/api/v1/status", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert set(data["lanes"]) == {"digital", "analog", "fm"}
        assert isinstance(data["active_lanes"], list)
        assert isinstance(data["concurrent"], bool)
        for lane, payload in data["lanes"].items():
            stream = payload["stream"]
            assert stream["playlist_path"] == f"/stream/{lane}.m3u8" \
                or lane == "fm"
            assert "ready" in stream and "active" in stream
        # Digital and analog sit on different front-ends, so neither lists
        # the other as a conflict.
        assert data["lanes"]["digital"]["conflicts_with"] == []
        assert data["lanes"]["analog"]["conflicts_with"] == ["fm"]
        assert {f["id"] for f in data["frontends"]} == {"digital", "analog"}

    def test_capture_card_endpoints_are_gone(self):
        r = requests.get(f"{BASE_URL}/api/v1/sources/capture", timeout=5)
        assert r.status_code == 404
        catalog = requests.get(f"{BASE_URL}/api/v1/sources", timeout=5).json()
        assert [s["id"] for s in catalog["sources"]] == [
            "digital", "cable", "fm",
        ]

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

    def test_stop_endpoint_keeps_always_on_stream(self):
        r = requests.post(f"{BASE_URL}/api/stop", timeout=5)
        assert r.status_code == 200
        assert r.json().get("ok")
        assert r.json().get("always_on") is True

    def test_static_index(self):
        r = requests.get(f"{BASE_URL}/", timeout=5)
        assert r.status_code == 200
        assert "text/html" in r.headers["Content-Type"]

    def test_static_404(self):
        r = requests.get(f"{BASE_URL}/nonexistent.html", timeout=5)
        assert r.status_code == 404

    def test_always_on_stream_available(self):
        assert wait_for_m3u8(timeout=45) is not None

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


def tune_and_wait(channel_id, quality="low", timeout=TUNE_TIMEOUT):
    """Tune to a channel and wait for the first segment. Returns (m3u8_response, tune_response)."""
    r = requests.post(f"{BASE_URL}/api/tune",
                      json={"channel_id": channel_id, "quality": quality},
                      timeout=10)
    r.raise_for_status()
    tune_data = r.json()
    m3u8 = wait_for_m3u8(timeout=timeout)
    return m3u8, tune_data


def first_segment_url(m3u8_text):
    """Extract the first .ts segment name from an m3u8 playlist."""
    for line in m3u8_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def playlist_segments(m3u8_text):
    """Return ``[(URI, EXTINF duration)]`` in playlist order."""
    segments = []
    duration = None
    for line in m3u8_text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            duration = float(line[len("#EXTINF:"):].split(",", 1)[0])
        elif line and not line.startswith("#"):
            segments.append((line, duration))
            duration = None
    return segments


def transport_stream_errors(data):
    """Count sync, transport-error, and in-segment continuity failures."""
    first_sync = data.find(b"\x47", 0, 188)
    if first_sync < 0:
        return {"sync": 1, "transport": 0, "continuity": 0}

    sync_errors = 0
    transport_errors = 0
    continuity_errors = 0
    counters = {}
    for offset in range(first_sync, len(data) - 187, 188):
        packet = data[offset:offset + 188]
        if packet[0] != 0x47:
            sync_errors += 1
            continue
        if packet[1] & 0x80:
            transport_errors += 1

        pid = ((packet[1] & 0x1f) << 8) | packet[2]
        adaptation_control = (packet[3] >> 4) & 0x03
        has_payload = adaptation_control in (1, 3)
        discontinuity = False
        if adaptation_control in (2, 3) and packet[4] > 0:
            discontinuity = bool(packet[5] & 0x80)

        if has_payload and pid != 0x1fff:
            counter = packet[3] & 0x0f
            previous = counters.get(pid)
            if (previous is not None and not discontinuity
                    and counter != (previous + 1) % 16):
                continuity_errors += 1
            counters[pid] = counter

    return {
        "sync": sync_errors,
        "transport": transport_errors,
        "continuity": continuity_errors,
    }


def different_channel(channels, channel):
    """Prefer another service on the same reliable RF multiplex."""
    others = [c for c in channels if c["id"] != channel["id"]]
    siblings = [c for c in others if c["frequency"] == channel["frequency"]]
    return (siblings or others)[0] if others else None


def media_sequence(m3u8_text):
    """Return the HLS media sequence number, or None if not yet present."""
    prefix = "#EXT-X-MEDIA-SEQUENCE:"
    for line in m3u8_text.splitlines():
        if line.startswith(prefix):
            return int(line[len(prefix):].strip())
    return None


class TestTuner:
    def test_tune_three_services(self, channels, good_channel):
        """Tune three services from a known-good RF multiplex."""
        siblings = [c for c in channels
                    if c["frequency"] == good_channel["frequency"]
                    and c["id"] != good_channel["id"]]
        sample = [good_channel] + siblings[:2]
        if len(sample) < 3:
            sample += [c for c in channels if c not in sample][:3-len(sample)]

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

    def test_status_reflects_active_stream(self, good_channel):
        """Status endpoint shows the correct channel while streaming."""
        ch = good_channel
        m3u8_resp, _ = tune_and_wait(ch["id"])
        assert m3u8_resp is not None, "No stream appeared"

        status = requests.get(f"{BASE_URL}/api/status", timeout=5).json()
        assert status["streaming"] is True
        assert status["channel"] is not None
        assert status["channel"]["id"] == ch["id"]

    def test_status_survives_stop_request(self, good_channel):
        """The public stop endpoint cannot tear down the always-on stream."""
        ch = good_channel
        tune_and_wait(ch["id"])
        requests.post(f"{BASE_URL}/api/stop", timeout=5)
        time.sleep(1)
        status = requests.get(f"{BASE_URL}/api/status", timeout=5).json()
        assert status["streaming"] is True
        assert status["channel"]["id"] == ch["id"]

    def test_quality_low(self, good_channel):
        """Low quality stream still produces data."""
        ch = good_channel
        m3u8_resp, tune_data = tune_and_wait(ch["id"], quality="low")
        assert tune_data.get("ok")
        assert m3u8_resp is not None
        status = requests.get(f"{BASE_URL}/api/status", timeout=5).json()
        assert status["quality"] == "low"

    def test_retune_to_different_channel(self, channels, good_channel):
        """A retune creates a distinct, playable encoder generation."""
        others = [c for c in channels if c["id"] != good_channel["id"]]
        if not others:
            pytest.skip("Need at least 2 channels")

        # A sibling service on the same multiplex is the most reliable second
        # source; fall back to any other saved channel if none exists.
        siblings = [c for c in others
                    if c["frequency"] == good_channel["frequency"]]
        ch1, ch2 = good_channel, random.choice(siblings or others)

        first_playlist, first_tune = tune_and_wait(ch1["id"])
        assert first_tune.get("ok")
        assert first_playlist is not None
        first_segments = {
            uri for uri, _duration in playlist_segments(first_playlist.text)
        }
        first_stream_id = first_tune.get("stream_id")
        assert first_stream_id

        # Tune to second channel without explicitly stopping first
        tune_response = requests.post(
            f"{BASE_URL}/api/tune",
            json={"channel_id": ch2["id"], "quality": "low"},
            timeout=10,
        )
        tune_response.raise_for_status()
        tune_data = tune_response.json()

        assert tune_data.get("ok")
        assert tune_data.get("stream_reused") is False
        assert tune_data.get("stream_id") != first_stream_id

        second_playlist = wait_for_m3u8()
        assert second_playlist is not None, "New playlist did not appear after retune"
        second_segments = {
            uri for uri, _duration in playlist_segments(second_playlist.text)
        }
        assert second_segments.isdisjoint(first_segments)

        seg_name = first_segment_url(second_playlist.text)
        assert seg_name
        seg_resp = requests.get(f"{BASE_URL}/stream/{seg_name}", timeout=10)
        assert seg_resp.status_code == 200
        assert len(seg_resp.content) >= SEGMENT_MIN

        status = requests.get(f"{BASE_URL}/api/status", timeout=5).json()
        assert status["channel"]["id"] == ch2["id"]

    @pytest.mark.hardware
    def test_channel_change_request_to_stream_latency(self, channels,
                                                       good_channel):
        """Measure POST /api/tune to the first newly downloadable segment."""
        target = different_channel(channels, good_channel)
        if target is None:
            pytest.skip("Need at least 2 channels")

        baseline, _ = tune_and_wait(good_channel["id"], quality="low")
        assert baseline is not None
        freshest = requests.get(f"{BASE_URL}/stream/live.m3u8", timeout=3)
        freshest.raise_for_status()
        old_segments = {uri for uri, _ in playlist_segments(freshest.text)}

        started = time.perf_counter()
        response = requests.post(
            f"{BASE_URL}/api/tune",
            json={"channel_id": target["id"], "quality": "low"},
            timeout=10,
        )
        response.raise_for_status()
        tune_data = response.json()
        assert tune_data.get("ok")
        assert tune_data.get("stream_reused") is False

        first_new_segment = None
        deadline = started + CHANNEL_CHANGE_MAX_SECONDS
        while time.perf_counter() < deadline and first_new_segment is None:
            playlist = requests.get(
                f"{BASE_URL}/stream/live.m3u8?_={time.time_ns()}", timeout=2,
            )
            if playlist.status_code == 200:
                for uri, _duration in playlist_segments(playlist.text):
                    if uri not in old_segments:
                        segment = requests.get(
                            f"{BASE_URL}/stream/{uri}", timeout=3,
                        )
                        if (segment.status_code == 200
                                and len(segment.content) >= SEGMENT_MIN):
                            first_new_segment = uri
                            break
            if first_new_segment is None:
                time.sleep(PLAYLIST_POLL_SECONDS)

        elapsed = time.perf_counter() - started
        assert first_new_segment is not None, (
            f"No new segment appeared within {CHANNEL_CHANGE_MAX_SECONDS:.1f}s"
        )
        assert elapsed <= CHANNEL_CHANGE_MAX_SECONDS
        print(
            f"\n  request -> stream: {elapsed:.3f}s "
            f"(API {tune_data.get('request_ms', '?')}ms, {first_new_segment})"
        )

    @pytest.mark.hardware
    def test_stream_glitchiness_during_channel_change(self, channels,
                                                       good_channel):
        """Retune while checking HLS order, timing, availability, and TS CCs."""
        target = different_channel(channels, good_channel)
        if target is None:
            pytest.skip("Need at least 2 channels")

        baseline, _ = tune_and_wait(good_channel["id"], quality="low")
        assert baseline is not None
        freshest = requests.get(f"{BASE_URL}/stream/live.m3u8", timeout=3)
        freshest.raise_for_status()
        baseline_names = {uri for uri, _ in playlist_segments(freshest.text)}

        response = requests.post(
            f"{BASE_URL}/api/tune",
            json={"channel_id": target["id"], "quality": "low"},
            timeout=10,
        )
        response.raise_for_status()
        tune_data = response.json()
        assert tune_data.get("stream_reused") is False
        assert tune_data.get("stream_id")

        checked = {}
        discontinuities = 0
        deadline = time.monotonic() + max(
            TUNE_TIMEOUT, GLITCH_TEST_SEGMENTS * MAX_SEGMENT_DURATION + 5
        )
        while len(checked) < GLITCH_TEST_SEGMENTS and time.monotonic() < deadline:
            playlist = requests.get(
                f"{BASE_URL}/stream/live.m3u8?_={time.time_ns()}", timeout=2,
            )
            if playlist.status_code != 200:
                time.sleep(PLAYLIST_POLL_SECONDS)
                continue

            sequence = media_sequence(playlist.text)
            assert sequence is not None
            discontinuities += playlist.text.count("#EXT-X-DISCONTINUITY")

            for uri, duration in playlist_segments(playlist.text):
                if uri in baseline_names or uri in checked:
                    continue
                assert duration is not None
                assert duration <= MAX_SEGMENT_DURATION, (
                    f"{uri} duration {duration:.3f}s exceeds "
                    f"{MAX_SEGMENT_DURATION:.3f}s"
                )
                segment = requests.get(f"{BASE_URL}/stream/{uri}", timeout=3)
                assert segment.status_code == 200, (
                    f"Playlist referenced unavailable segment {uri}"
                )
                assert len(segment.content) >= SEGMENT_MIN
                errors = transport_stream_errors(segment.content)
                assert errors == {"sync": 0, "transport": 0, "continuity": 0}, (
                    f"MPEG-TS errors in {uri}: {errors}"
                )
                checked[uri] = duration

            time.sleep(PLAYLIST_POLL_SECONDS)

        assert len(checked) >= GLITCH_TEST_SEGMENTS, (
            f"Only received {len(checked)} healthy new segments; "
            f"expected {GLITCH_TEST_SEGMENTS}"
        )
        assert discontinuities == 0
        print(
            f"\n  glitch check: {len(checked)} segments, "
            f"max EXTINF={max(checked.values()):.3f}s, no TS errors"
        )


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


# ---------------------------------------------------------------------------
# VCT / PSIP virtual channel number test (slow — tunes each frequency briefly)
# ---------------------------------------------------------------------------

class TestVCT:
    def test_callback_reports_each_identified_frequency(self, tmp_path,
                                                        monkeypatch):
        """The scan UI receives station data immediately after each tune."""
        import fetch_vct
        import parse_channels

        frequencies = [177_000_000, 593_000_000]
        records = {
            frequencies[0]: [
                {"name": "WETA-HD", "major": 26, "minor": 1,
                 "service_id": 1},
            ],
            frequencies[1]: [
                {"name": "WRC-HD", "major": 4, "minor": 1,
                 "service_id": 3},
            ],
        }
        seen = []

        monkeypatch.setattr(fetch_vct, "VCT_CACHE",
                            tmp_path / "vct_data.json")
        monkeypatch.setattr(fetch_vct, "_get_frequencies",
                            lambda: frequencies)
        monkeypatch.setattr(fetch_vct, "fetch_vct_for_freq",
                            lambda freq: records[freq])
        monkeypatch.setattr(parse_channels, "main", lambda: None)

        fetch_vct.main(channel_cb=lambda freq, channels:
                       seen.append((freq, channels)))

        assert seen == [(freq, records[freq]) for freq in frequencies]

    @pytest.mark.slow
    def test_vct_fetch_writes_data(self, tmp_path, monkeypatch):
        """fetch_vct.main() tunes each frequency and writes vct_data.json
        with at least one frequency worth of real PSIP channel records."""
        import fetch_vct

        # Point VCT cache at a temp file so we don't clobber the real one
        monkeypatch.setattr(fetch_vct, "VCT_CACHE", tmp_path / "vct_data.json")
        # Also redirect parse_channels output to temp dir
        import parse_channels
        monkeypatch.setattr(parse_channels, "VCT_CACHE", tmp_path / "vct_data.json")
        monkeypatch.setattr(parse_channels, "CHANNELS_JSON", tmp_path / "channels.json")

        fetch_vct.main()

        assert (tmp_path / "vct_data.json").exists(), "vct_data.json not written"
        vct = json.loads((tmp_path / "vct_data.json").read_text())
        assert len(vct) > 0, "vct_data.json is empty — section filter returned no data"

        total_channels = sum(len(v) for v in vct.values())
        assert total_channels > 0, "VCT data has no channel records"
        print(f"\n  VCT: {len(vct)} frequencies, {total_channels} channel records")

        # Spot-check: each record should have major/minor in sane range
        for freq_str, freq_entry in vct.items():
            for sid_str, ch in freq_entry.items():
                assert 1 <= ch["major"] <= 99,  f"major {ch['major']} out of range"
                assert 1 <= ch["minor"] <= 999, f"minor {ch['minor']} out of range"

    @pytest.mark.slow
    def test_psip_numbers_used_in_channel_list(self):
        """After a VCT fetch, at least some channels should have a virtual
        channel major number that differs from their RF channel number,
        proving PSIP data was used rather than the RF fallback."""
        vct_path = BASE_DIR / "vct_data.json"
        if not vct_path.exists():
            pytest.skip("vct_data.json not present — run fetch_vct.py first")

        vct = json.loads(vct_path.read_text())
        if not vct:
            pytest.skip("vct_data.json is empty")

        channels = requests.get(f"{BASE_URL}/api/channels", timeout=5).json()
        assert channels, "No channels loaded"

        # Build a set of (rf_channel, major) pairs from channels that have VCT data.
        # If PSIP is working, major != rf_channel for many channels
        # (e.g. WRC-TV is RF 34 but PSIP major 4).
        psip_channels = [
            ch for ch in channels
            if str(ch["frequency"]) in vct
            and str(ch["service_id"]) in vct[str(ch["frequency"])]
        ]
        assert psip_channels, "No channels matched VCT data"

        mismatches = [
            ch for ch in psip_channels
            if int(ch["number"].split(".")[0]) != ch["rf_channel"]
        ]
        assert mismatches, (
            "All PSIP major numbers equal their RF channel — "
            "VCT data may not be applied (or all stations happen to match)"
        )
        print(f"\n  {len(mismatches)} channels with PSIP major ≠ RF channel:")
        for ch in mismatches[:5]:
            print(f"    {ch['number']} {ch['name']}  (RF {ch['rf_channel']})")
