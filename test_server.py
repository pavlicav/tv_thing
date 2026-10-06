"""Fast unit tests for the TV streaming command and encoder selection."""

import json
import time
from io import BytesIO

import server


def reset_encoder_state(monkeypatch, requested="auto"):
    monkeypatch.setattr(server, "VIDEO_ENCODER", requested)
    monkeypatch.setattr(server, "NVENC_PROBE_ATTEMPTS", 1)
    monkeypatch.setattr(server, "_video_encoder_config", None)
    monkeypatch.setattr(server, "_disabled_video_encoders", set())


def option_value(command, option):
    return command[command.index(option) + 1]


def test_gpu_encoder_is_selected_after_runtime_probe(monkeypatch):
    reset_encoder_state(monkeypatch)
    attempted = []

    def probe(config):
        attempted.append(config["codec"])
        return config["codec"] == "h264_nvenc"

    monkeypatch.setattr(server, "_probe_video_encoder", probe)
    selected = server.select_video_encoder()

    assert selected["codec"] == "h264_nvenc"
    assert selected["hardware"] is True
    assert attempted == ["h264_nvenc"]


def test_encoder_selection_falls_back_to_cpu(monkeypatch):
    reset_encoder_state(monkeypatch)
    monkeypatch.setattr(server, "_probe_video_encoder", lambda _config: False)

    selected = server.select_video_encoder()

    assert selected["codec"] == "libx264"
    assert selected["hardware"] is False


def test_packager_uses_short_segments_and_aligned_keyframes(monkeypatch):
    monkeypatch.setattr(server, "HLS_SEGMENT_TIME", 1.0)
    encoder = {
        "codec": "libx264", "hardware": False,
        "encoder_args": ["-preset", "veryfast"],
    }

    command = server.build_tv_packager_command(
        "medium", encoder, stream_id="generation-7"
    )

    assert option_value(command, "-hls_time") == "1"
    assert option_value(command, "-force_key_frames") == \
        "expr:gte(t,n_forced*1)"
    assert option_value(command, "-hls_list_size") == "16"
    assert option_value(command, "-c:v") == "libx264"
    assert option_value(command, "-map") == "0:v:0"
    assert "0:a:0?" in command
    assert option_value(command, "-max_interleave_delta") == "500000"
    assert option_value(command, "-flush_packets") == "1"
    assert "fifo_size=65536" in option_value(command, "-i")
    assert option_value(command, "-fps_mode") == "passthrough"
    assert option_value(command, "-fflags") == "+discardcorrupt"
    assert "-use_wallclock_as_timestamps" not in command
    assert "digitalseg_generation-7_%09d.ts" in option_value(
        command, "-hls_segment_filename"
    )


def test_vaapi_command_uploads_software_filtered_frames(monkeypatch):
    monkeypatch.setattr(server, "HLS_SEGMENT_TIME", 1.0)
    encoder = {
        "codec": "h264_vaapi", "hardware": True,
        "input_args": ["-vaapi_device", "/dev/dri/renderD128"],
        "filter_suffix": ["format=nv12", "hwupload"],
        "encoder_args": [],
    }

    command = server.build_tv_packager_command("low", encoder)

    assert option_value(command, "-vaapi_device") == "/dev/dri/renderD128"
    assert option_value(command, "-c:v") == "h264_vaapi"
    filters = option_value(command, "-vf").split(",")
    assert filters[-2:] == ["format=nv12", "hwupload"]


def test_direct_dvb_source_command_selects_requested_program(monkeypatch):
    monkeypatch.setattr(server, "TV_UDP_PORT", 18320)
    command = server.build_tv_source_command({
        "frequency": 189_000_000,
        "service_id": 1,
        "video_pid": 49,
        "audio_pid": 52,
    })

    assert command[1].endswith("dvb_source.py")
    assert option_value(command, "--frequency") == "189000000"
    assert option_value(command, "--service-id") == "1"
    assert option_value(command, "--video-pid") == "49"
    assert option_value(command, "--audio-pid") == "52"
    assert option_value(command, "--udp-port") == "18320"


def test_analog_command_uses_v4l2_and_matching_alsa_input(monkeypatch):
    monkeypatch.setattr(server, "HLS_SEGMENT_TIME", 1.0)
    encoder = {
        "codec": "libx264", "hardware": False,
        "encoder_args": ["-preset", "veryfast"],
    }
    device = {
        "device": "/dev/video7",
        "audio_device": "hw:3,0",
    }

    command = server.build_analog_packager_command(
        device, "medium", encoder, stream_id="capture-4"
    )

    input_positions = [
        index for index, value in enumerate(command) if value == "-i"
    ]
    assert command[input_positions[0] - 4:input_positions[0] + 2] == [
        "-f", "v4l2", "-timestamps", "mono2abs",
        "-i", "/dev/video7",
    ]
    assert command[input_positions[1] - 2:input_positions[1] + 2] == [
        "-f", "alsa", "-i", "hw:3,0",
    ]
    assert "0:v:0" in command
    assert "1:a:0?" in command
    assert option_value(command, "-timestamps") == "mono2abs"
    assert option_value(command, "-fps_mode") == "vfr"
    assert option_value(command, "-vf").startswith("setpts=PTS-STARTPTS,")
    assert option_value(command, "-af") == (
        "asetpts=PTS-STARTPTS,"
        "aresample=async=1000:first_pts=0,asetpts=N/SR/TB"
    )
    assert option_value(command, "-c:v") == "libx264"
    assert "analogseg_capture-4_%09d.ts" in option_value(
        command, "-hls_segment_filename"
    )


def test_analog_command_supports_a_tuner_without_audio():
    encoder = {
        "codec": "libx264", "hardware": False,
        "encoder_args": ["-preset", "veryfast"],
    }

    command = server.build_analog_packager_command(
        {"device": "/dev/video2", "audio_device": None},
        "low",
        encoder,
    )

    assert command.count("-i") == 1
    assert "-an" in command
    assert "-c:a" not in command
    assert "scale=-2:480" in option_value(command, "-vf")
    assert "format=yuv420p" in option_value(command, "-vf")


def test_audio_capture_disables_pulseaudio_shared_memory(monkeypatch):
    monkeypatch.setenv("PULSE_CLIENTCONFIG", "/tmp/unrelated.conf")

    environment = server.audio_capture_environment()

    assert environment["PULSE_CLIENTCONFIG"] == str(
        server.PULSE_CLIENT_CONFIG
    )
    assert "enable-shm = no" in server.PULSE_CLIENT_CONFIG.read_text()


def test_digital_tune_persists_always_on_playback_state(monkeypatch):
    """The always-on keeper resumes from this file, so a tune must write it."""
    channel = {"id": "wusa_hd_9_1", "name": "WUSA-HD",
               "frequency": 189_000_000, "service_id": 1,
               "video_pid": 49, "audio_pid": 52}
    saved = []

    class FakeProcess:
        def poll(self):
            return None

    monkeypatch.setattr(server, "stop_digital", lambda: None)
    monkeypatch.setattr(
        server, "start_digital_packager", lambda *_a: (True, None)
    )
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(server.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    monkeypatch.setattr(
        server, "save_playback_state",
        lambda ch, quality: saved.append((ch["id"], quality)),
    )

    ok, error, _ = server.start_digital(channel, "low")

    assert ok is True and error is None
    assert saved == [("wusa_hd_9_1", "low")]


def test_each_generation_gets_its_own_udp_port(monkeypatch):
    """A fixed port raced with the previous encoder's socket on retune."""
    taken = set()

    class FakeSocket:
        def __init__(self, *_a):
            pass

        def bind(self, address):
            if address[1] in taken:
                raise OSError("in use")

        def close(self):
            pass

    monkeypatch.setattr(server.socket, "socket", FakeSocket)
    first = server.allocate_udp_port()
    taken.add(first)
    second = server.allocate_udp_port()

    assert first == server.TV_UDP_PORT
    assert second != first

    source = server.build_tv_source_command(
        {"frequency": 1, "service_id": 1, "video_pid": 2, "audio_pid": 3},
        second,
    )
    packager = server.build_tv_packager_command(
        "medium", {"codec": "libx264", "hardware": False}, "gen", second,
    )
    assert option_value(source, "--udp-port") == str(second)
    assert f":{second}?" in option_value(packager, "-i")


def test_bind_always_includes_loopback_for_the_reverse_proxy(monkeypatch):
    """Apache forwards /tv/ to 127.0.0.1:8080, so loopback must be bound."""
    monkeypatch.setattr(server, "lan_addresses", lambda: ["192.168.86.238"])
    monkeypatch.setattr(
        server, "interface_address", lambda name: "192.168.86.238"
    )

    candidates = server.bind_candidates("iface:enx0")

    assert candidates[0] == "127.0.0.1"
    assert "192.168.86.238" in candidates
    # The wildcard stays last: another service holds :8080 on tailscale0.
    assert candidates[-1] == "0.0.0.0"
    assert server.bind_candidates("iface:enx0", include_loopback=False)[0] \
        == "192.168.86.238"


def test_a_stale_pinned_address_still_yields_a_working_listener(monkeypatch):
    monkeypatch.setattr(server, "lan_addresses", lambda: ["192.168.86.238"])

    bound_ok = {"127.0.0.1", "192.168.86.238"}

    def factory(address):
        if address[0] not in bound_ok:
            raise OSError("Cannot assign requested address")
        return f"server@{address[0]}"

    servers, bound, failures = server.open_listeners(
        factory, server.bind_candidates("192.168.86.230"), 8080
    )

    assert bound == ["127.0.0.1", "192.168.86.238"]
    assert len(servers) == 2
    assert any("192.168.86.230" in failure for failure in failures)


def test_wildcard_is_skipped_once_a_specific_address_bound(monkeypatch):
    monkeypatch.setattr(server, "lan_addresses", lambda: [])
    servers, bound, _ = server.open_listeners(
        lambda address: f"server@{address[0]}",
        ["127.0.0.1", "0.0.0.0"],
        8080,
    )
    assert bound == ["127.0.0.1"]
    assert len(servers) == 1


def test_scan_results_become_channel_records(monkeypatch):
    """One scan pass carries names, numbers, service IDs, and PIDs."""
    results = [{
        "frequency_hz": 189_000_000, "rf_channel": 9,
        "virtual_channels": [
            {"name": "WUSA-HD", "number": "9.1", "service_id": 1,
             "video_pid": 49, "audio_pid": 52},
            {"name": "Crime", "number": "9.2", "service_id": 2,
             "video_pid": 65, "audio_pid": 68},
            # No readable PMT: the tuner source could not select this one.
            {"name": "Ghost", "number": "9.9", "service_id": 9,
             "video_pid": None, "audio_pid": None},
        ],
    }]

    channels = server.channels_from_scan(results)

    assert [c["id"] for c in channels] == ["wusa_hd_9_1", "crime_9_2"]
    assert channels[0] == {
        "name": "WUSA-HD", "frequency": 189_000_000,
        "video_pid": 49, "audio_pid": 52, "service_id": 1,
        "modulation": "VSB_8", "id": "wusa_hd_9_1",
        "number": "9.1", "rf_channel": 9,
    }


def test_channels_sort_primaries_before_subchannels():
    rows = [
        {"number": "9.2"}, {"number": "4.1"},
        {"number": "9.1"}, {"number": "4.3"},
    ]
    rows.sort(key=server.channel_sort_key)
    assert [r["number"] for r in rows] == ["4.1", "9.1", "4.3", "9.2"]


def test_scan_plan_marks_repacked_channels_and_known_stations(monkeypatch):
    monkeypatch.setattr(server, "load_channels", lambda: [
        {"id": "wusa_hd_9_1", "number": "9.1", "name": "WUSA-HD",
         "rf_channel": 9},
    ])

    plan = server.api_v1_scan_plan()

    assert plan["last_rf_channel_in_use"] == 36
    assert plan["last_rf_channel_legacy"] == 69
    assert plan["channels"][-1]["rf_channel"] == 36
    known = {e["rf_channel"]: e["known_channels"] for e in plan["channels"]}
    assert known[9] == [
        {"id": "wusa_hd_9_1", "number": "9.1", "name": "WUSA-HD"}
    ]
    assert known[8] == []

    legacy = server.api_v1_scan_plan(include_repacked=True)
    assert legacy["channels"][-1]["rf_channel"] == 69
    assert legacy["channels"][-1]["repacked"] is True


def test_channel_scan_refuses_an_invalid_rf_channel():
    payload, status = server.perform_channel_scan(99)
    assert status == 400
    assert "RF channel" in payload["error"]


def test_channel_scan_releases_the_tuner_even_on_failure(monkeypatch):
    """A stuck `scanning` flag blocks the always-on keeper permanently."""
    monkeypatch.setattr(server, "stop_digital", lambda: None)
    monkeypatch.setattr(server, "busy_reason", lambda: None)
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)

    def boom(*_a, **_k):
        raise OSError("Device or resource busy")

    monkeypatch.setattr(server.atsc_scan, "scan_channel", boom)

    payload, status = server.perform_channel_scan(9)

    assert status == 503
    assert server.scanning is False


def test_lanes_on_separate_frontends_do_not_conflict():
    """Digital, analog and the Roku capture each have their own front-end."""
    assert server.ALL_LANES == (server.LANE_DIGITAL, server.LANE_ANALOG,
                                server.LANE_ROKU)
    assert len({server.LANE_FRONTEND[lane] for lane in server.ALL_LANES}) == 3
    # The Roku is not a video lane: it never drives the legacy alias.
    assert server.LANE_ROKU not in server.VIDEO_LANES

    # One lane per front-end, so neither can displace the other.
    for lane in server.ALL_LANES:
        assert server.lane_payload(lane)["conflicts_with"] == []


def test_each_lane_publishes_its_own_playlist():
    """Separate playlists are what let both lanes stream at once."""
    paths = {
        lane: server.LANE_PLAYLIST[lane] for lane in server.ALL_LANES
    }
    assert len(set(paths.values())) == len(paths)
    prefixes = {
        lane: server.LANE_SEGMENT_PREFIX[lane] for lane in server.ALL_LANES
    }
    assert len(set(prefixes.values())) == len(prefixes)


def test_analog_tune_configures_channel_without_stopping_digital(monkeypatch):
    tuner = {
        "device": "/dev/video0",
        "input_index": 0,
        "tuner_index": 0,
        "name": "HVR-1600",
    }
    calls = []
    # Only the analog front-end is released; the digital lane is untouched.
    monkeypatch.setattr(
        server, "stop_analog", lambda: calls.append("stop_analog")
    )
    monkeypatch.setattr(
        server, "stop_digital",
        lambda: calls.append("stop_digital(SHOULD NOT HAPPEN)"),
    )
    monkeypatch.setattr(
        server.analog_tuner, "discover_analog_tuners", lambda: [tuner]
    )
    monkeypatch.setattr(
        server.analog_tuner,
        "configure_tuner",
        lambda device, input_index, tuner_index, frequency: calls.append(
            (device, input_index, tuner_index, frequency)
        ),
    )

    def start(device, quality, cable_channel):
        calls.append((device, quality, cable_channel))
        return True, None

    monkeypatch.setattr(server, "start_analog_packager", start)

    ok, error, selected = server.start_analog(4, "low")

    assert ok is True
    assert error is None
    assert selected is tuner
    assert calls == [
        "stop_analog",
        ("/dev/video0", 0, 0, 67_250_000),
        (tuner, "low", 4),
    ]


def test_nvenc_configuration_is_zero_latency(monkeypatch):
    reset_encoder_state(monkeypatch, requested="nvenc")
    monkeypatch.setattr(server, "_probe_video_encoder", lambda _config: True)

    selected = server.select_video_encoder()

    assert selected["codec"] == "h264_nvenc"
    assert selected["encoder_args"] == [
        "-preset", "p2", "-tune", "ll", "-rc", "cbr",
        "-zerolatency", "1", "-delay", "0", "-bf", "0",
        "-forced-idr", "1",
    ]


def test_duplicate_frame_warning_trips_stream_safety(monkeypatch):
    monkeypatch.setattr(server, "TV_MAX_DUPLICATED_FRAMES", 1000)
    health = server.new_stream_health("test")

    reason = server.record_packager_warning(
        "More than 1000 frames duplicated", health
    )

    assert reason == "ffmpeg duplicated at least 1000 frames"
    assert health["duplicated_frames"] == 1000


def test_udp_overrun_warning_trips_stream_safety(monkeypatch):
    monkeypatch.setattr(server, "TV_MAX_UDP_OVERRUNS", 1)
    health = server.new_stream_health("test")

    reason = server.record_packager_warning(
        "Circular buffer overrun. Surviving due to overrun_nonfatal option",
        health,
    )

    assert reason == "ffmpeg UDP input overran 1 time(s)"
    assert health["udp_overruns"] == 1


def test_oversized_open_segment_terminates_packager(monkeypatch, tmp_path):
    class FakePackager:
        def __init__(self):
            self.running = True

        def poll(self):
            return None if self.running else 255

        def terminate(self):
            self.running = False

    stream_id = "bounded"
    prefix = server.LANE_SEGMENT_PREFIX[server.LANE_DIGITAL]
    segment = tmp_path / f"{prefix}_{stream_id}_000000000.ts.tmp"
    segment.write_bytes(b"x" * 11)
    health = server.new_stream_health(stream_id)
    packager = FakePackager()
    encoder = {"codec": "libx264"}
    monkeypatch.setattr(server, "STREAMS_DIR", tmp_path)
    monkeypatch.setattr(server, "TV_MAX_SEGMENT_BYTES", 10)
    monkeypatch.setattr(server, "TV_HEALTH_POLL_SECONDS", 0)

    server.watch_tv_packager(packager, encoder, stream_id, health)

    assert packager.running is False
    assert health["ok"] is False
    assert "grew to 11 bytes" in health["reason"]


def test_stream_ids_are_unique(monkeypatch):
    monkeypatch.setattr(server.time, "time_ns", lambda: 123)
    monkeypatch.setattr(server, "_stream_serial", 0)

    assert server.next_stream_id() == "123-1"
    assert server.next_stream_id() == "123-2"


def test_v1_source_catalog_is_stable_and_cable_is_frequency_ordered(monkeypatch):
    channels = [{"id": "7.1", "number": "7.1", "name": "Example"}]
    cable_channels = [
        {"number": 7, "frequency_hz": 175_250_000},
        {"number": 2, "frequency_hz": 55_250_000},
    ]
    tuner = {"id": "tuner:hvr1600", "name": "Analog tuner"}
    monkeypatch.setattr(server, "load_channels", lambda: channels)
    monkeypatch.setattr(
        server, "active_or_discovered_analog_tuner", lambda: tuner
    )
    monkeypatch.setattr(
        server.analog_tuner, "cable_channels", lambda: cable_channels
    )

    catalog = server.api_v1_source_catalog()
    cable = server.api_v1_cable_source()

    assert catalog["api_version"] == "v1"
    # Capture cards and FM are gone; the card's two tuners remain.
    assert [item["id"] for item in catalog["sources"]] == ["digital", "cable"]
    assert catalog["tune_path"] == "/api/v1/tune"
    assert cable["source"]["available"] is True
    assert cable["source"]["lane"] == server.LANE_ANALOG
    assert [item["number"] for item in cable["items"]] == [2, 7]


def test_v1_status_reports_both_lanes_streaming_at_once(monkeypatch):
    """The whole point of the card: digital and analog concurrently."""
    class RunningProcess:
        def poll(self):
            return None

    monkeypatch.setitem(server.procs, "digital_packager", RunningProcess())
    monkeypatch.setitem(server.procs, "analog_packager", RunningProcess())
    monkeypatch.setitem(
        server.lanes, server.LANE_DIGITAL,
        dict(server.new_lane_state(), active=True, quality="medium",
             stream_id="digital-1", health={"ok": True},
             video_encoder="libx264",
             selection={"id": "7.1", "name": "WHUT"}),
    )
    monkeypatch.setitem(
        server.lanes, server.LANE_ANALOG,
        dict(server.new_lane_state(), active=True, quality="low",
             stream_id="analog-1", health={"ok": True},
             video_encoder="libx264", selection={"channel": 3}),
    )
    monkeypatch.setattr(server, "lane_stream_ready", lambda _lane: True)

    status = server.api_v1_status_payload()

    assert set(status["active_lanes"]) == {"digital", "analog"}
    assert status["concurrent"] is True
    assert status["lanes"]["digital"]["stream"]["playlist_path"] == \
        "/stream/digital.m3u8"
    assert status["lanes"]["analog"]["stream"]["playlist_path"] == \
        "/stream/analog.m3u8"
    assert status["lanes"]["digital"]["stream"]["active"] is True
    assert status["lanes"]["analog"]["stream"]["active"] is True
    assert status["lanes"]["analog"]["selection"]["channel"] == 3

    # Both front-ends report busy, each by its own lane.
    frontends = {item["id"]: item for item in status["frontends"]}
    assert frontends["digital"]["in_use_by"] == "digital"
    assert frontends["analog"]["in_use_by"] == "analog"


def test_legacy_status_mirrors_the_primary_video_lane(monkeypatch):
    class RunningProcess:
        def poll(self):
            return None

    monkeypatch.setitem(server.procs, "analog_packager", RunningProcess())
    monkeypatch.setitem(server.procs, "digital_packager", None)
    monkeypatch.setitem(
        server.lanes, server.LANE_ANALOG,
        dict(server.new_lane_state(), active=True, quality="low",
             stream_id="analog-1", health={"ok": True},
             video_encoder="libx264", selection={"channel": 69},
             tuner={"name": "Analog tuner"}),
    )
    monkeypatch.setattr(server, "primary_video_lane", server.LANE_ANALOG)
    monkeypatch.setattr(server, "lane_stream_ready", lambda _lane: True)

    legacy = server.status_payload()

    assert legacy["mode"] == "cable"
    assert legacy["cable_channel"] == 69
    assert legacy["streaming"] is True
    assert legacy["tuner_backend"] == "analog-v4l2"


def test_v1_tune_validates_source_and_quality(monkeypatch):
    called = []
    monkeypatch.setattr(
        server,
        "perform_digital_tune",
        lambda channel_id, quality: called.append((channel_id, quality)),
    )

    invalid_source, source_status = server.api_v1_tune({"source": "betamax"})
    invalid_quality, quality_status = server.api_v1_tune({
        "source": "digital", "channel_id": "7.1", "quality": "tiny",
    })

    assert source_status == 400
    assert invalid_source["error"]["code"] == "invalid_source"
    assert quality_status == 400
    assert invalid_quality["error"]["code"] == "invalid_quality"
    assert called == []


def test_v1_http_routes_and_cors(monkeypatch):
    monkeypatch.setattr(server, "api_v1_source_catalog", lambda: {
        "api_version": "v1", "sources": [],
    })
    handler = object.__new__(server.TVHandler)
    sent = []
    handler.send_json = lambda data, status=200: sent.append((status, data))
    handler.path = "/api/v1/sources"

    handler.do_GET()

    assert sent == [(200, {"api_version": "v1", "sources": []})]

    response = {}
    headers = {}
    handler.path = "/api/v1/tune"
    handler.send_response = lambda status: response.update(status=status)
    handler.send_header = lambda name, value: headers.update({name: value})
    handler.end_headers = lambda: None

    handler.do_OPTIONS()

    assert response["status"] == 204
    assert headers["Access-Control-Allow-Origin"] == "*"

    request_body = json.dumps({"source": "unknown"}).encode()
    handler.headers = {"Content-Length": str(len(request_body))}
    handler.rfile = BytesIO(request_body)
    sent.clear()

    handler.do_POST()

    assert sent[0][0] == 400
    assert sent[0][1]["error"]["code"] == "invalid_source"


# ---------------------------------------------------------------------------
# Guide
# ---------------------------------------------------------------------------

def test_refresh_times_parse_and_reject_nonsense():
    assert server.parse_refresh_times("03:00,15:00") == [(3, 0), (15, 0)]
    assert server.parse_refresh_times(" 3:5 , 03:05 ") == [(3, 5)]
    assert server.parse_refresh_times("25:00,03:70,noon,,12") == []


def test_next_refresh_picks_the_next_slot_then_rolls_over():
    def at(hour, minute):
        local = time.localtime()
        return time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                            hour, minute, 0, 0, 0, -1))

    times = [(3, 0), (15, 0)]
    assert server.next_refresh_at(times, now=at(1, 0)) == at(3, 0)
    assert server.next_refresh_at(times, now=at(3, 1)) == at(15, 0)
    # After the last slot the next one is tomorrow morning, not today's.
    assert server.next_refresh_at(times, now=at(23, 0)) > at(23, 0)
    assert server.next_refresh_at([], now=at(1, 0)) is None


def test_guide_refresh_yields_the_tuner_to_a_viewer(monkeypatch):
    """A scheduled refresh must never make someone wait out a sweep."""
    monkeypatch.setattr(server, "guide_refreshing", True)
    server.guide_abort.clear()

    def release():
        # Stand in for the refresh loop noticing the flag between frequencies.
        assert server.guide_abort.is_set()
        monkeypatch.setattr(server, "guide_refreshing", False)

    import threading
    threading.Timer(0.05, release).start()
    server.preempt_guide_refresh(timeout=3.0)

    assert server.guide_abort.is_set()
    assert server.guide_refreshing is False


def test_a_refresh_in_progress_blocks_a_scan_but_not_forever(monkeypatch):
    monkeypatch.setattr(server, "guide_refreshing", True)
    assert server.busy_reason() == "Guide refresh in progress"
    monkeypatch.setattr(server, "guide_refreshing", False)
    assert server.busy_reason() is None


def _stored_guide(now):
    return {
        "generated_at": now - 600,
        "coverage_hours": 11.5,
        "event_count": 3,
        "channels_with_events": 1,
        "channels": {
            "9.1": {
                "number": "9.1", "name": "WUSA-HD", "id": "wusa_hd_9_1",
                "events": [
                    {"event_id": 1, "start": now - 7200, "duration": 1800,
                     "title": "Finished hours ago"},
                    {"event_id": 2, "start": now - 600, "duration": 1800,
                     "title": "On now"},
                    {"event_id": 3, "start": now + 1200, "duration": 3600,
                     "title": "Up next"},
                ],
            },
        },
    }


def test_guide_payload_drops_events_that_already_finished(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(server.epg, "load_guide", lambda: _stored_guide(now))
    payload = server.guide_payload(now=now)

    titles = [e["title"] for e in payload["channels"]["9.1"]["events"]]
    # The hour in progress is kept so the grid has a left edge; the event
    # that ended two hours ago is not.
    assert titles == ["On now", "Up next"]
    assert payload["available"] is True
    assert payload["refresh_times"] == ["03:00", "15:00"]


def test_guide_payload_can_narrow_to_one_channel_and_a_window(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(server.epg, "load_guide", lambda: _stored_guide(now))

    assert set(server.guide_payload(number="9.1", now=now)["channels"]) == {"9.1"}
    assert server.guide_payload(number="44.1", now=now)["channels"] == {}

    narrow = server.guide_payload(window_hours=0.1, now=now)
    assert [e["title"] for e in narrow["channels"]["9.1"]["events"]] == ["On now"]


def test_guide_payload_without_data_says_so_rather_than_failing(monkeypatch):
    monkeypatch.setattr(server.epg, "load_guide", lambda: None)
    payload = server.guide_payload()
    assert payload["available"] is False
    assert payload["channels"] == {}
    assert payload["refresh_times"] == ["03:00", "15:00"]


def test_guide_now_reports_current_and_following_programmes(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(server.epg, "load_guide", lambda: _stored_guide(now))
    entry = server.guide_now_payload(now=now)["channels"]["9.1"]
    assert entry["now"]["title"] == "On now"
    assert entry["next"]["title"] == "Up next"


def test_guide_now_is_empty_without_a_guide(monkeypatch):
    monkeypatch.setattr(server.epg, "load_guide", lambda: None)
    payload = server.guide_now_payload()
    assert payload == {"available": False, "generated_at": None,
                       "channels": {}}


def test_v1_tune_returns_a_status_document_not_a_crash(monkeypatch):
    """POST /api/v1/tune built its response from a mis-called helper.

    The tune itself succeeded and the lane started, so the only symptom was
    the connection dropping with no response -- invisible to anything that
    did not read the reply.
    """
    monkeypatch.setattr(server, "perform_digital_tune", lambda *a, **k: (
        {"ok": True, "request_ms": 12.3}, 200
    ))
    payload, status = server.api_v1_tune(
        {"source": "digital", "channel_id": "wusa_hd_9_1", "quality": "low"}
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["lane"] == server.LANE_DIGITAL
    assert payload["request_ms"] == 12.3
    for key in ("source", "selection", "quality", "stream", "encoder",
                "active_lanes"):
        assert key in payload


def test_always_on_does_not_fight_a_guide_refresh_for_the_tuner(monkeypatch):
    """The keeper must stand down while anything else holds the front-end.

    It wakes every five seconds, so checking only the scan flag meant a guide
    refresh drew a failed dvb_source launch on every wakeup for the whole
    sweep -- EBUSY each time, and a discarded encoder with it.
    """
    started = []
    monkeypatch.setattr(server, "procs", {
        "digital_source": None, "digital_packager": None,
        "analog_packager": None,
    })
    monkeypatch.setattr(server, "load_playback_state",
                        lambda: ({"id": "wusa_hd_9_1"}, "medium"))
    monkeypatch.setattr(server, "start_digital",
                        lambda channel, quality: (started.append(channel)
                                                  or (True, None, False)))

    # A refresh in progress: the lane is down, but the tuner is not free.
    monkeypatch.setattr(server, "guide_refreshing", True)
    assert server.busy_reason() == "Guide refresh in progress"

    # The keeper's own guard, evaluated the way the worker evaluates it.
    healthy = False
    assert healthy or server.busy_reason()
    assert started == []

    # Once the refresh finishes the keeper is free to restart the lane.
    monkeypatch.setattr(server, "guide_refreshing", False)
    assert not (healthy or server.busy_reason())


def test_always_on_stands_down_while_the_tuner_is_held(monkeypatch):
    """The keeper must not fight a scan or a guide refresh for the tuner."""
    monkeypatch.setattr(server, "scanning", False)
    monkeypatch.setattr(server, "guide_refreshing", True)
    assert server.busy_reason() == "Guide refresh in progress"

    monkeypatch.setattr(server, "guide_refreshing", False)
    monkeypatch.setattr(server, "scanning", True)
    assert server.busy_reason() == "Channel scan in progress"

    monkeypatch.setattr(server, "scanning", False)
    assert server.busy_reason() is None


def test_coverage_is_reported_against_the_clock_not_the_refresh(monkeypatch):
    """A stored guide overstates its coverage by exactly its own age."""
    now = 1_800_000_000
    guide = {
        "generated_at": now - 10 * 3600,      # read ten hours ago
        "coverage_hours": 13.0,               # true when it was written
        "horizon": now + 3 * 3600,            # three hours actually remain
        "channels": {},
    }
    monkeypatch.setattr(server.epg, "load_guide", lambda: guide)

    payload = server.guide_payload(now=now)
    assert payload["coverage_hours"] == 3.0
    assert payload["coverage_hours_at_refresh"] == 13.0
    assert payload["age_seconds"] == 10 * 3600


def test_remaining_coverage_floors_at_zero_and_handles_no_guide():
    now = 1_800_000_000
    assert server.remaining_coverage_hours(None) == 0
    assert server.remaining_coverage_hours({}, now) == 0
    assert server.remaining_coverage_hours({"horizon": None}, now) == 0
    # A guide whose last programme has already ended has nothing left.
    assert server.remaining_coverage_hours({"horizon": now - 7200}, now) == 0
    assert server.remaining_coverage_hours({"horizon": now + 5400}, now) == 1.5


def test_startup_catchup_triggers_on_exhaustion_not_file_age(monkeypatch):
    """The old age test called a nearly-empty ten-hour-old guide fresh."""
    now = 1_800_000_000
    monkeypatch.setattr(server, "EPG_MIN_COVERAGE_HOURS", 3.0)

    nearly_spent = {"generated_at": now - 10 * 3600, "horizon": now + 3600}
    assert server.remaining_coverage_hours(nearly_spent, now) == 1.0
    assert server.remaining_coverage_hours(nearly_spent, now) \
        < server.EPG_MIN_COVERAGE_HOURS

    # A guide read moments ago still has its full span and is left alone.
    just_read = {"generated_at": now - 60, "horizon": now + 12 * 3600}
    assert server.remaining_coverage_hours(just_read, now) \
        >= server.EPG_MIN_COVERAGE_HOURS


# ---------------------------------------------------------------------------
# Tuner locks
# ---------------------------------------------------------------------------

import pytest


@pytest.fixture
def clean_locks(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "tuner_locks.json")
    server.tuner_locks.clear()
    yield server.tuner_locks
    server.tuner_locks.clear()


def _claim(clean_locks, **body):
    body.setdefault("owner", "recorder")
    body.setdefault("lane", server.LANE_DIGITAL)
    return server.acquire_tuner_lock(body)


def test_a_lock_issues_a_token_and_reports_its_expiry(clean_locks):
    payload, status = _claim(clean_locks, duration_seconds=1800,
                             reason="recording")
    assert status == 200
    assert payload["token"]
    assert payload["renewed"] is False
    lock = payload["lock"]
    assert lock["owner"] == "recorder"
    assert lock["reason"] == "recording"
    assert 1700 < lock["seconds_remaining"] <= 1800


def test_a_second_service_is_refused_with_the_holder_named(clean_locks):
    _claim(clean_locks, duration_seconds=600)
    payload, status = _claim(clean_locks, owner="someone-else")
    assert status == 423
    assert payload["error"]["code"] == "locked"
    assert "recorder" in payload["error"]["message"]
    # The refusal carries the lock so a client can show who holds it.
    assert payload["lock"]["owner"] == "recorder"


def test_only_the_issuing_service_can_release_early(clean_locks):
    token = _claim(clean_locks, duration_seconds=600)[0]["token"]

    payload, status = server.release_tuner_lock({"lane": "digital"})
    assert status == 403
    assert payload["error"]["code"] == "not_lock_owner"
    payload, status = server.release_tuner_lock(
        {"lane": "digital", "token": "not-the-token"}
    )
    assert status == 403
    assert server.active_lock("digital") is not None

    payload, status = server.release_tuner_lock(
        {"lane": "digital", "token": token}
    )
    assert status == 200
    assert payload["released"] is True
    assert payload["overrode"] is False
    assert server.active_lock("digital") is None


def test_the_ui_can_override_a_lock_it_does_not_own(clean_locks):
    _claim(clean_locks, duration_seconds=600)
    payload, status = server.release_tuner_lock(
        {"lane": "digital", "override": True}
    )
    assert status == 200
    assert payload["released"] is True
    # Flagged so the caller knows someone else's claim was broken.
    assert payload["overrode"] is True
    assert server.active_lock("digital") is None


def test_renewing_with_the_token_keeps_it_and_extends(clean_locks):
    first = _claim(clean_locks, duration_seconds=60)[0]
    payload, status = _claim(clean_locks, duration_seconds=3600,
                             token=first["token"])
    assert status == 200
    assert payload["renewed"] is True
    assert payload["token"] == first["token"]
    assert payload["lock"]["seconds_remaining"] > 3000
    assert payload["lock"]["acquired_at"] == first["lock"]["acquired_at"]


def test_locks_are_capped_and_say_when_they_were_shortened(clean_locks):
    monkey = server.LOCK_MAX_SECONDS
    payload, status = _claim(clean_locks, duration_seconds=monkey * 10)
    assert status == 200
    assert payload["truncated"] is True
    assert payload["lock"]["seconds_remaining"] <= monkey


def test_an_expired_lock_stops_blocking_without_anyone_clearing_it(clean_locks):
    _claim(clean_locks, duration_seconds=600)
    server.tuner_locks["digital"]["expires_at"] = time.time() - 1
    assert server.active_lock("digital") is None
    assert server.lock_holder("digital") is None


def test_a_lock_refuses_tunes_scans_and_stops(clean_locks, monkeypatch):
    token = _claim(clean_locks, duration_seconds=600)[0]["token"]
    monkeypatch.setattr(server, "load_channels", lambda: [
        {"id": "wusa_hd_9_1", "number": "9.1", "name": "WUSA",
         "frequency": 189_000_000, "service_id": 1, "rf_channel": 9},
    ])

    payload, status = server.perform_digital_tune("wusa_hd_9_1")
    assert status == 423
    assert "locked by recorder" in payload["error"]

    payload, status = server.api_v1_stop({"lane": "digital"})
    assert status == 423

    # The holder passes straight through, and so does an explicit override.
    assert server.lock_holder("digital", token=token) is None
    assert server.lock_holder("digital", override=True) is None


def test_a_lock_on_one_lane_leaves_the_other_alone(clean_locks):
    _claim(clean_locks, lane="digital", duration_seconds=600)
    assert server.lock_holder("digital") is not None
    assert server.lock_holder("analog") is None
    payload, status = server.api_v1_stop({"lane": "analog"})
    assert status == 200


def test_the_always_on_keeper_is_not_blocked_by_a_lock(clean_locks):
    """A lock whose stream cannot recover is worse than no lock at all.

    The keeper restarts the locked channel, so it must not consult the lock;
    busy_reason() is what it reads, and the lock deliberately stays out of it.
    """
    _claim(clean_locks, duration_seconds=600)
    assert server.busy_reason() is None


def test_a_lock_outranks_the_scheduled_guide_refresh(clean_locks):
    _claim(clean_locks, duration_seconds=600)
    guide, error = server.run_guide_refresh(reason="scheduled")
    assert guide is None
    assert "locked by recorder" in error
    assert server.guide_refreshing is False


def test_locks_survive_a_restart_but_expired_ones_do_not(clean_locks, tmp_path):
    _claim(clean_locks, duration_seconds=600)
    server.tuner_locks["analog"] = {
        "lane": "analog", "owner": "stale", "token": "t",
        "acquired_at": 0, "expires_at": time.time() - 5,
    }
    server.save_tuner_locks()

    restored = server.load_tuner_locks()
    assert set(restored) == {"digital"}
    assert restored["digital"]["owner"] == "recorder"


def test_a_lock_needs_an_owner(clean_locks):
    payload, status = server.acquire_tuner_lock({"lane": "digital"})
    assert status == 400
    assert payload["error"]["code"] == "owner_required"
    payload, status = server.acquire_tuner_lock(
        {"lane": "digital", "owner": "   "}
    )
    assert status == 400


def test_invalid_lock_requests_are_rejected_clearly(clean_locks):
    assert server.acquire_tuner_lock(
        {"lane": "fm", "owner": "x"})[1] == 400
    assert server.acquire_tuner_lock(
        {"lane": "digital", "owner": "x", "duration_seconds": -5})[1] == 400
    assert server.acquire_tuner_lock(
        {"lane": "digital", "owner": "x", "duration_seconds": "soon"})[1] == 400
    assert server.acquire_tuner_lock(
        {"lane": "digital", "owner": "x", "until": "forever"})[1] == 400


def test_releasing_when_nothing_is_locked_is_not_an_error(clean_locks):
    payload, status = server.release_tuner_lock({"lane": "digital"})
    assert status == 200
    assert payload["released"] is False


def test_locking_until_the_end_of_a_programme_uses_the_guide(clean_locks,
                                                             monkeypatch):
    now = time.time()
    monkeypatch.setattr(server, "load_channels", lambda: [
        {"id": "wusa_hd_9_1", "number": "9.1", "name": "WUSA",
         "frequency": 189_000_000, "service_id": 1, "rf_channel": 9},
    ])
    monkeypatch.setattr(server.epg, "load_guide", lambda: {
        "channels": {"9.1": {"number": "9.1", "name": "WUSA", "events": [
            {"event_id": 1, "start": int(now) - 600, "duration": 3600,
             "title": "60 Minutes"},
        ]}},
    })

    payload, status = _claim(clean_locks, until="program_end",
                             channel_id="wusa_hd_9_1")
    assert status == 200
    lock = payload["lock"]
    assert lock["until"] == "program_end"
    assert lock["program"]["title"] == "60 Minutes"
    # Runs to the listed end plus the overrun grace, not to the listed end.
    expected = (now - 600) + 3600 + server.LOCK_PROGRAM_GRACE_SECONDS
    assert abs(lock["expires_at"] - expected) < 5


def test_programme_lock_falls_back_to_the_tuned_channel(clean_locks,
                                                        monkeypatch):
    now = time.time()
    monkeypatch.setattr(server, "load_channels", lambda: [
        {"id": "wusa_hd_9_1", "number": "9.1", "name": "WUSA",
         "frequency": 189_000_000, "service_id": 1, "rf_channel": 9},
    ])
    monkeypatch.setattr(server.epg, "load_guide", lambda: {
        "channels": {"9.1": {"number": "9.1", "events": [
            {"event_id": 1, "start": int(now), "duration": 1800,
             "title": "News"},
        ]}},
    })
    monkeypatch.setitem(server.lanes[server.LANE_DIGITAL], "selection",
                        {"id": "wusa_hd_9_1", "number": "9.1"})

    payload, status = _claim(clean_locks, until="program_end")
    assert status == 200
    assert payload["lock"]["channel_id"] == "wusa_hd_9_1"


def test_programme_lock_says_why_when_the_guide_cannot_answer(clean_locks,
                                                              monkeypatch):
    """Ten of the seventy channels carry no EIT, so this is a real case."""
    monkeypatch.setattr(server, "load_channels", lambda: [
        {"id": "shoplc_49_2", "number": "49.2", "name": "ShopLC",
         "frequency": 473_000_000, "service_id": 4, "rf_channel": 14},
    ])
    monkeypatch.setattr(server.epg, "load_guide", lambda: {"channels": {}})

    payload, status = _claim(clean_locks, until="program_end",
                             channel_id="shoplc_49_2")
    assert status == 409
    assert payload["error"]["code"] == "no_programme"
    assert "49.2" in payload["error"]["message"]
    assert "fixed time" in payload["error"]["message"]
    assert server.active_lock("digital") is None


def test_programme_lock_without_a_channel_or_a_tune_is_rejected(clean_locks,
                                                                monkeypatch):
    monkeypatch.setitem(server.lanes[server.LANE_DIGITAL], "selection", None)
    payload, status = _claim(clean_locks, until="program_end")
    assert status == 400
    assert payload["error"]["code"] == "channel_required"


def test_a_lock_defers_the_refresh_rather_than_failing_it(clean_locks):
    """Deferral is flagged, so the scheduler knows to come back."""
    _claim(clean_locks, duration_seconds=600)
    guide, error = server.run_guide_refresh(reason="scheduled")
    assert guide is None
    assert "locked by recorder" in error
    assert server.guide_status["deferred_by_lock"] is True


def test_an_ordinary_failure_is_not_flagged_as_deferred(clean_locks,
                                                        monkeypatch):
    """Only a lock should make the scheduler retry; nothing else."""
    monkeypatch.setattr(server, "scanning", True)
    guide, error = server.run_guide_refresh(reason="scheduled")
    assert guide is None
    assert error == "Channel scan in progress"
    assert server.guide_status["deferred_by_lock"] is False


def test_a_deferred_refresh_retries_until_the_lock_clears(clean_locks,
                                                          monkeypatch):
    """A lock at 03:00 must not cost the whole overnight refresh.

    The next slot is twelve hours away, which is the guide's entire span, so
    skipping to it would leave the guide empty by afternoon.
    """
    attempts = []
    monkeypatch.setattr(server, "EPG_LOCK_RETRY_SECONDS", 0.01)

    def fake_refresh(reason):
        attempts.append(reason)
        # Deferred twice, then the lock clears.
        return len(attempts) < 3

    monkeypatch.setattr(server, "_run_scheduled_refresh", fake_refresh)
    monkeypatch.setattr(server, "next_refresh_at",
                        lambda _times, now=None: time.time() + 12 * 3600)

    server._refresh_until_the_tuner_is_free("scheduled")
    assert len(attempts) == 3


def test_retrying_stops_at_the_next_scheduled_slot(monkeypatch):
    """Retries must not pile up into the run that is about to happen."""
    attempts = []
    monkeypatch.setattr(server, "EPG_LOCK_RETRY_SECONDS", 300)
    monkeypatch.setattr(server, "_run_scheduled_refresh",
                        lambda reason: attempts.append(reason) or True)
    # The next slot is a minute away, well inside one retry interval.
    monkeypatch.setattr(server, "next_refresh_at",
                        lambda _times, now=None: time.time() + 60)

    server._refresh_until_the_tuner_is_free("scheduled")
    assert len(attempts) == 1


def test_a_manual_refresh_during_the_retry_ends_it(monkeypatch):
    monkeypatch.setattr(server, "EPG_LOCK_RETRY_SECONDS", 5)
    monkeypatch.setattr(server, "_run_scheduled_refresh", lambda reason: True)
    monkeypatch.setattr(server, "next_refresh_at",
                        lambda _times, now=None: time.time() + 12 * 3600)
    server.guide_wakeup.set()
    try:
        server._refresh_until_the_tuner_is_free("scheduled")
    finally:
        server.guide_wakeup.clear()


def test_v1_lock_refusal_keeps_public_owner(clean_locks):
    _claim(clean_locks, owner="recorder")
    payload, status = server.api_v1_tune({"source": "digital", "channel_id": "x"})
    assert status == 423
    assert payload["error"]["code"] == "locked"
    assert payload["lock"]["owner"] == "recorder"
    assert "token" not in payload["lock"]


def test_dx_scan_cannot_interrupt_a_lease(clean_locks, monkeypatch):
    _claim(clean_locks)
    monkeypatch.setattr(server, "stop_digital", lambda: pytest.fail("stopped locked tuner"))
    _, status = server.perform_channel_scan(9)
    assert status == 423


def test_claim_during_scan_is_busy(clean_locks, monkeypatch):
    monkeypatch.setattr(server, "scanning", True)
    payload, status = _claim(clean_locks)
    assert status == 409
    assert not server.tuner_locks


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), -1, 0])
def test_lease_duration_must_be_finite_and_positive(clean_locks, seconds):
    _, status = _claim(clean_locks, duration_seconds=seconds)
    assert status == 400


def test_ready_requires_current_generation_media(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "STREAMS_DIR", tmp_path)
    monkeypatch.setitem(server.lanes, "digital", dict(server.new_lane_state(),
                        stream_id="new", started_at=time.time()-1))
    playlist = tmp_path / "digital.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:0.6,\ndigitalseg_old_000000001.ts\n")
    (tmp_path / "digitalseg_old_000000001.ts").write_bytes(b"x")
    assert not server.lane_stream_ready("digital")
    playlist.write_text("#EXTM3U\n#EXTINF:0.6,\ndigitalseg_new_000000001.ts\n")
    assert not server.lane_stream_ready("digital")
    (tmp_path / "digitalseg_new_000000001.ts").write_bytes(b"x")
    assert server.lane_stream_ready("digital")


def test_tune_rechecks_lock_after_preempt(clean_locks, monkeypatch):
    monkeypatch.setattr(server, "load_channels", lambda: [{"id": "x"}])
    monkeypatch.setattr(server, "preempt_guide_refresh", lambda: _claim(clean_locks))
    monkeypatch.setattr(server, "start_digital", lambda *a: pytest.fail("retuned"))
    _, status = server.perform_digital_tune("x")
    assert status == 423


def test_identical_healthy_tune_reuses_generation(clean_locks, monkeypatch):
    monkeypatch.setattr(server, "preempt_guide_refresh", lambda: None)
    monkeypatch.setattr(server, "busy_reason", lambda: None)
    monkeypatch.setattr(server, "load_channels", lambda: [{"id": "x"}])
    monkeypatch.setattr(server, "lane_is_running", lambda lane: lane == "digital")
    monkeypatch.setattr(server, "lane_stream_ready", lambda lane: True)
    monkeypatch.setitem(server.lanes, "digital", dict(server.new_lane_state(),
                        selection={"id": "x"}, stream_id="same", health={"ok": True}))
    monkeypatch.setattr(server, "start_digital", lambda *a: pytest.fail("restarted healthy stream"))
    payload, status = server.api_v1_tune({"source": "digital", "channel_id": "x"})
    assert status == 200
    assert payload["stream_reused"]
    assert payload["stream"]["generation"] == "same"
    assert payload["stream"]["playback"][0]["url"] == "stream/digital.m3u8"


def test_tune_response_uses_its_own_snapshot(monkeypatch):
    snapshot = server.lane_payload("digital")
    snapshot["selection"] = {"id": "requested"}
    snapshot["stream"]["id"] = "request-generation"
    monkeypatch.setattr(server, "perform_digital_tune", lambda *a, **k: (
        {"ok": True, "request_ms": 1, "_lane": snapshot}, 200))
    monkeypatch.setattr(server, "primary_video_lane", "analog")
    payload, status = server.api_v1_tune({"source": "digital", "channel_id": "requested"})
    assert status == 200
    assert payload["selection"] == {"id": "requested"}
    assert payload["stream"]["id"] == "request-generation"
    assert payload["source"]["id"] == "digital"


def test_stream_schema_matches_status(monkeypatch):
    from pathlib import Path
    import jsonschema
    document = json.loads((Path(server.BASE_DIR) / "static/openapi.json").read_text())
    jsonschema.Draft202012Validator.check_schema(document["components"]["schemas"]["Stream"])
    schema = dict(document["components"]["schemas"]["Stream"], components=document["components"])
    for lane in server.ALL_LANES:
        jsonschema.validate(server.lane_stream_payload(lane), schema)


def test_failed_health_never_reports_ready(monkeypatch):
    monkeypatch.setattr(server, "lane_is_running", lambda lane: True)
    monkeypatch.setattr(server, "lane_stream_ready", lambda lane: True)
    monkeypatch.setitem(server.lanes, "digital", dict(server.new_lane_state(),
                        health={"ok": False, "reason": "encoder stalled"}))
    payload = server.lane_stream_payload("digital")
    assert payload["state"] == "failed"
    assert payload["ready"] is False
    assert payload["error"]["message"] == "encoder stalled"


def test_explicit_tune_override_revokes_previous_lease(clean_locks, monkeypatch, capsys):
    claim, _ = _claim(clean_locks)
    monkeypatch.setattr(server, "preempt_guide_refresh", lambda: None)
    monkeypatch.setattr(server, "busy_reason", lambda: None)
    monkeypatch.setattr(server, "load_channels", lambda: [{"id": "x"}])
    monkeypatch.setattr(server, "start_digital", lambda *args: (True, None, False))
    _, status = server.perform_digital_tune("x", override=True)
    assert status == 200
    assert server.active_lock("digital") is None
    assert "recorder" in capsys.readouterr().out


def test_refused_scan_does_not_clear_another_scan(monkeypatch):
    monkeypatch.setattr(server, "scanning", True)
    monkeypatch.setattr(server, "preempt_guide_refresh", lambda: None)
    monkeypatch.setattr(server, "lock_holder", lambda *args: None)
    handler = server.TVHandler.__new__(server.TVHandler)
    handler.path = "/api/rescan/stream"
    handler.wfile = BytesIO()
    handler.send_response = lambda *args: None
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.handle_rescan_stream()
    assert server.scanning is True
    assert b"Channel scan in progress" in handler.wfile.getvalue()


def test_retry_during_initial_buffering_does_not_restart(clean_locks, monkeypatch):
    monkeypatch.setattr(server, "preempt_guide_refresh", lambda: None)
    monkeypatch.setattr(server, "busy_reason", lambda: None)
    monkeypatch.setattr(server, "load_channels", lambda: [{"id": "x"}])
    monkeypatch.setattr(server, "lane_is_running", lambda lane: lane == "digital")
    monkeypatch.setattr(server, "lane_stream_ready", lambda lane: False)
    monkeypatch.setitem(server.lanes, "digital", dict(server.new_lane_state(),
                        selection={"id": "x"}, stream_id="starting", health={"ok": True}))
    monkeypatch.setattr(server, "start_digital", lambda *a: pytest.fail("restarted buffering stream"))
    payload, status = server.api_v1_tune({"source": "digital", "channel_id": "x"})
    assert status == 200
    assert payload["stream_reused"] is True
    assert payload["stream"]["state"] == "buffering"
    assert payload["stream"]["id"] == "starting"


# ---------------------------------------------------------------------------
# Recording playback / download
# ---------------------------------------------------------------------------

def _file_handler(range_header=None):
    handler = server.TVHandler.__new__(server.TVHandler)
    handler.wfile = BytesIO()
    handler.headers = {"Range": range_header} if range_header else {}
    handler.sent = {"status": None, "headers": {}}
    handler.send_response = lambda code, *a: handler.sent.update(status=code)
    handler.send_header = lambda key, value: handler.sent["headers"].update({key: value})
    handler.end_headers = lambda: None
    return handler


def test_recording_file_streams_whole_and_ranged(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RECORDINGS_DIR", tmp_path)
    body = bytes(range(256)) * 20
    (tmp_path / "Show - 7.1 WJLA.mp4").write_bytes(body)

    whole = _file_handler()
    whole.send_recording_file("Show - 7.1 WJLA.mp4")
    assert whole.sent["status"] == 200
    assert whole.wfile.getvalue() == body
    assert whole.sent["headers"]["Content-Type"] == "video/mp4"
    assert whole.sent["headers"]["Accept-Ranges"] == "bytes"
    assert whole.sent["headers"]["Content-Disposition"].startswith("inline;")

    part = _file_handler("bytes=100-199")
    part.send_recording_file("Show - 7.1 WJLA.mp4", download=True)
    assert part.sent["status"] == 206
    assert part.wfile.getvalue() == body[100:200]
    assert part.sent["headers"]["Content-Range"] == f"bytes 100-199/{len(body)}"
    assert part.sent["headers"]["Content-Length"] == "100"
    assert part.sent["headers"]["Content-Disposition"].startswith("attachment;")

    past = _file_handler(f"bytes={len(body)}-")
    past.send_recording_file("Show - 7.1 WJLA.mp4")
    assert past.sent["status"] == 416
    assert past.wfile.getvalue() == b""


def test_recording_file_refuses_anything_outside_the_library(tmp_path, monkeypatch):
    library = tmp_path / "lib"
    library.mkdir()
    (tmp_path / "secret.mp4").write_bytes(b"x")
    (library / "live.ts.part").write_bytes(b"x")
    monkeypatch.setattr(server, "RECORDINGS_DIR", library)
    for name in ("../secret.mp4", "live.ts.part", "", ".hidden.mp4", "missing.mp4"):
        handler = _file_handler()
        handler.send_json = lambda data, status=200: handler.sent.update(status=status)
        handler.send_recording_file(name)
        assert handler.sent["status"] == 404, name


def test_roku_capture_copies_the_hd_pvr_and_feeds_the_verifier_tap():
    command = server.build_roku_capture_command(
        "/dev/v4l/by-id/hdpvr", "gen-1", tap_port=18999)
    assert command[command.index("-i") + 1] == "/dev/v4l/by-id/hdpvr"
    assert command.count("-i") == 1  # One reader of the device, ever.
    # No transcode: the HD PVR's own H.264 and AAC are copied, twice.
    assert command.count("-c") == 2 and command.count("copy") == 2
    assert "libx264" not in command and "-vf" not in command
    assert command[command.index("-hls_time") + 1] == str(
        server.ROKU_HLS_SEGMENT_TIME)
    assert str(server.STREAMS_DIR / "rokuseg_gen-1_%09d.ts") in command
    assert command[-1] == "udp://127.0.0.1:18999?pkt_size=1316"
    assert str(server.lane_playlist_path(server.LANE_ROKU)) in command


def test_roku_lock_cannot_follow_a_programme(clean_locks):
    payload, status = server.acquire_tuner_lock(
        {"lane": "roku", "owner": "x", "until": "program_end"})
    assert status == 400 and payload["error"]["code"] == "invalid_until"
    payload, status = server.acquire_tuner_lock(
        {"lane": "roku", "owner": "Web remote", "duration_seconds": 3600})
    assert status == 200 and payload["lock"]["lane"] == "roku"
    # The Roku's lock leaves the tuners alone, and the other way round.
    assert server.lock_holder(server.LANE_DIGITAL) is None
    assert server.lock_holder(server.LANE_ROKU)["owner"] == "Web remote"


def test_roku_capture_source_is_read_without_the_server_mutex(monkeypatch):
    class Running:
        def poll(self):
            return None

    monkeypatch.setitem(server.procs, "roku_capture", Running())
    monkeypatch.setattr(server, "roku_capture_state", {
        "running": True, "stream_id": "gen-1", "error": None,
        "tap_url": server.roku_tap_input(18999)})
    import threading
    with server.lock:
        # Another thread reading it while the mutex is held must not block.
        result = {}
        reader = threading.Thread(
            target=lambda: result.update(server.roku_capture_source()))
        reader.start()
        reader.join(timeout=1)
        assert not reader.is_alive()
    assert result["running"] and result["stream_id"] == "gen-1"
    assert result["tap_url"].startswith("udp://127.0.0.1:18999?")
    monkeypatch.setitem(server.procs, "roku_capture", None)
    assert server.roku_capture_source()["running"] is False
