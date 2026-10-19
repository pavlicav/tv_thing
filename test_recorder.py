"""Fast tests for the VCR: scheduling, guide matching, and the recorder
lifecycle with simulated processes. Nothing here touches the tuner."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import dvb_source
import recorder
import roku_lane
import server

CHANNELS = [
    {"id": "wjla_7_1", "number": "7.1", "name": "WJLA", "frequency": 177000000,
     "service_id": 3, "video_pid": 49, "audio_pid": 52},
    {"id": "weta_26_1", "number": "26.1", "name": "WETA", "frequency": 575000000,
     "service_id": 1, "video_pid": 49, "audio_pid": 52},
]
NOW = 1_790_190_000


def guide(now=NOW):
    return {
        "generated_at": now - 60,
        "channels": {
            "7.1": {"number": "7.1", "name": "WJLA", "id": "wjla_7_1", "events": [
                {"title": "General Hospital", "start": now - 1800, "duration": 3600},
                {"title": "Jeopardy!", "start": now + 1800, "duration": 1800},
                {"title": "7News at 11", "start": now + 3600, "duration": 1800},
            ]},
            "26.1": {"number": "26.1", "name": "WETA", "id": "weta_26_1", "events": [
                {"title": "JEOPARDY", "start": now + 1800, "duration": 1800},
                {"title": "NOVA", "start": now + 7200, "duration": 3600},
            ]},
        },
    }


# ---------------------------------------------------------------------------
# Pure scheduling rules
# ---------------------------------------------------------------------------

def test_any_overlap_conflicts_because_there_is_one_digital_tuner():
    timers = [recorder.make_timer(CHANNELS[0], NOW, NOW + 1800, now=NOW)]
    assert recorder.find_conflict(timers, NOW + 1700, NOW + 3600)
    # Back to back is fine, and so is anything on the other side.
    assert recorder.find_conflict(timers, NOW + 1800, NOW + 3600) is None
    assert recorder.find_conflict(timers, NOW - 600, NOW) is None
    timers[0]["status"] = "cancelled"
    assert recorder.find_conflict(timers, NOW, NOW + 60) is None


def test_guide_name_search_is_case_and_punctuation_blind():
    results = recorder.search_guide(guide(), "jeopardy", NOW)
    assert [(r["channel_number"], r["title"]) for r in results] == [
        ("7.1", "Jeopardy!"), ("26.1", "JEOPARDY"),
    ]
    assert recorder.search_guide(guide(), "  ", NOW) == []
    # Finished programmes are not offered; one under way is, and says so.
    airing = recorder.search_guide(guide(), "general", NOW)
    assert airing[0]["airing"] is True


def test_a_series_rule_sets_each_airing_once_and_flags_clashes():
    schedule = recorder.empty_schedule()
    schedule["rules"].append({"id": "r1", "title": "Jeopardy",
                              "channel_number": None})

    added = recorder.expand_rules(schedule, guide(), CHANNELS, NOW)
    assert [(t["channel_number"], t["status"]) for t in added] == [
        ("7.1", "scheduled"), ("26.1", "conflict"),
    ]
    first = added[0]
    assert first["start"] == NOW + 1800 - recorder.GUIDE_PRE_PAD_SECONDS
    assert first["end"] == NOW + 3600 + recorder.GUIDE_POST_PAD_SECONDS
    assert first["program"]["start"] == NOW + 1800
    assert "one digital tuner" in added[1]["note"]

    # A second look at the same guide changes nothing.
    assert recorder.expand_rules(schedule, guide(), CHANNELS, NOW) == []


def test_a_series_rule_can_be_pinned_to_one_channel():
    schedule = recorder.empty_schedule()
    schedule["rules"].append({"id": "r1", "title": "jeopardy",
                              "channel_number": "26.1"})
    added = recorder.expand_rules(schedule, guide(), CHANNELS, NOW)
    assert [t["channel_number"] for t in added] == ["26.1"]
    assert added[0]["status"] == "scheduled"


def test_missed_and_due_timers():
    early = recorder.make_timer(CHANNELS[0], NOW - 600, NOW + 600, now=NOW)
    later = recorder.make_timer(CHANNELS[0], NOW + 900, NOW + 1200, now=NOW)
    gone = recorder.make_timer(CHANNELS[0], NOW - 900, NOW - 1, now=NOW)
    timers = [later, early, gone]
    assert recorder.due_timer(timers, NOW) is early
    assert recorder.expire_missed(timers, NOW) == [gone]
    assert gone["status"] == "missed"


def test_library_names_sort_by_title_and_never_collide(tmp_path):
    timer = recorder.make_timer(CHANNELS[0], NOW, NOW + 60, title="Jeopardy!/Tonight",
                                now=NOW)
    base = recorder.recording_basename(timer)
    assert base.startswith("Jeopardy! Tonight - 7.1 WJLA - ")
    assert "/" not in base

    first = recorder.unique_stem(tmp_path, base)
    recorder.recording_part(first).write_bytes(b"x")
    second = recorder.unique_stem(tmp_path, base)
    assert second != first and second.name.endswith("(2)")
    assert recorder.stem_of_part(recorder.recording_part(first)) == first


def test_erase_only_resolves_plain_library_files(tmp_path):
    (tmp_path / "Show.mp4").write_bytes(b"x")
    (tmp_path / "Show.ts.part").write_bytes(b"x")
    assert recorder.library_file(tmp_path, "Show.mp4") == tmp_path / "Show.mp4"
    for name in ("../Show.mp4", "Show.ts.part", ".hidden.mp4", "", None,
                 "sub/Show.mp4"):
        assert recorder.library_file(tmp_path, name) is None
    assert [e["name"] for e in recorder.list_library(tmp_path)] == ["Show.mp4"]


def test_an_ad_free_copy_reports_what_was_cut_from_its_original(tmp_path):
    (tmp_path / "Show.mp4").write_bytes(b"x")
    (tmp_path / "Show (no ads).mp4").write_bytes(b"x")
    (tmp_path / "Show (no ads).json").write_text(json.dumps({
        "file": "Show.mp4", "duration": 1978.3,
        "blocks": [{"start": 0, "end": 48, "label": "ad"},
                   {"start": 48, "end": 366, "label": "show"},
                   {"start": 366, "end": 581, "label": "ad"},
                   {"start": 581, "end": 1722, "label": "show"},
                   {"start": 1722, "end": 1978.3, "label": "ad"}],
        "kept": [[48.0, 366.3], [581.5, 1722.4]]}))
    (tmp_path / "Other (no ads).mp4").write_bytes(b"x")    # no report
    library = {e["name"]: e for e in recorder.list_library(tmp_path)}
    assert "ad_strip" not in library["Show.mp4"]
    assert library["Show (no ads).mp4"]["ad_strip"] == {
        "source": "Show.mp4", "recorded_seconds": 1978, "kept_seconds": 1459,
        "breaks": 1}
    assert library["Other (no ads).mp4"]["ad_strip"] is None
    assert "Show (no ads).json" not in library


def test_recorder_encodes_efficiently_and_stops_through_stdin():
    command = recorder.build_record_command("udp://127.0.0.1:1", "/x/a.ts.part")
    assert "-nostdin" not in command      # it is stopped with "q"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-preset") + 1] == "veryfast"
    assert "-crf" in command
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert "bwdif" in command[command.index("-vf") + 1]
    assert command[-3:] == ["-f", "mpegts", "/x/a.ts.part"]
    # Decoder, filter and x264 pools: not one thread per core (TasksMax).
    decoder = command.index("-threads")
    assert decoder < command.index("-i")
    assert command[decoder + 1] == recorder.RECORD_THREADS
    assert command[command.index("-filter_threads") + 1] == recorder.RECORD_THREADS
    encoder_options = command[command.index("-c:v"):]
    assert encoder_options[encoder_options.index("-threads") + 1] == recorder.RECORD_THREADS

    remux = recorder.build_remux_command("a.ts.part", "a.mp4.part")
    assert remux[remux.index("-c") + 1] == "copy"
    assert remux[remux.index("-movflags") + 1] == "+faststart"


def test_a_live_copy_only_copies_and_stops_through_stdin():
    command = recorder.build_live_copy_command("/s/digital.m3u8", "/x/a.ts.part")
    assert "-nostdin" not in command
    assert command[command.index("-i") + 1] == "/s/digital.m3u8"
    assert command[command.index("-live_start_index") + 1] == "0"
    assert command[command.index("-c") + 1] == "copy"
    assert "-vf" not in command and "-c:v" not in command
    assert command[-3:] == ["-f", "mpegts", "/x/a.ts.part"]


def test_broadcast_decoders_refuse_impossible_picture_sizes():
    # A corrupt MPEG-2 sequence header once claimed a ~16k x 16k picture and
    # the live packager allocated a 0.8 GB frame, tripping MemoryMax.
    for command in (
        recorder.build_record_command("udp://127.0.0.1:1", "/x/a.ts.part"),
        server.build_tv_packager_command("medium", {
            "codec": "libx264", "hardware": False, "encoder_args": []}),
    ):
        cap = command.index("-max_pixels")
        assert cap < command.index("-i")      # an input (decoder) option
        assert command[cap + 1] == str(1920 * 1088)


def test_dvb_source_accepts_repeatable_tap_ports():
    args = dvb_source.parse_args([
        "--frequency", "1", "--service-id", "1", "--video-pid", "1",
        "--audio-pid", "2", "--udp-port", "18320",
        "--tap-port", "18384", "--tap-port", "18385",
    ])
    assert args.tap_port == [18384, 18385]
    assert dvb_source.parse_args([
        "--frequency", "1", "--service-id", "1", "--video-pid", "1",
        "--audio-pid", "2", "--udp-port", "18320",
    ]).tap_port is None


# ---------------------------------------------------------------------------
# Server runtime, with simulated processes
# ---------------------------------------------------------------------------

class FakeRecorder:
    def __init__(self, cmd, **_kwargs):
        self.cmd = cmd
        self.returncode = None
        self.stdin = self
        self.stderr = iter(())
        self.written = ""
        Path(cmd[-1]).write_bytes(b"\x47" * 188 * 10)

    def write(self, text):
        self.written += text

    def flush(self):
        pass

    def close(self):
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def vcr(monkeypatch, tmp_path):
    library = tmp_path / "TV Recordings"
    library.mkdir()
    monkeypatch.setattr(server, "RECORDINGS_DIR", library)
    monkeypatch.setattr(server, "RECORDINGS_FILE", tmp_path / "recordings.json")
    monkeypatch.setattr(server, "recording_schedule", None)
    monkeypatch.setattr(server, "active_recording", None)
    monkeypatch.setattr(server, "roku_recording", None)
    monkeypatch.setattr(server, "roku", None)
    monkeypatch.setattr(server, "_recording_retry_at", {})
    monkeypatch.setattr(server, "_rules_expanded_for", None)
    monkeypatch.setattr(server, "tuner_locks", {})
    monkeypatch.setattr(server, "LOCK_FILE", tmp_path / "locks.json")
    monkeypatch.setattr(server, "lanes",
                        {lane: server.new_lane_state() for lane in server.ALL_LANES})
    monkeypatch.setattr(server, "scanning", False)
    monkeypatch.setattr(server, "guide_refreshing", False)
    monkeypatch.setattr(server, "preempt_guide_refresh", lambda: None)
    monkeypatch.setattr(server, "load_channels", lambda: CHANNELS)
    monkeypatch.setattr(server, "file_size_limit", lambda: None)
    monkeypatch.setattr(server.epg, "load_guide", lambda: guide(int(time.time())))
    monkeypatch.setattr(server, "RECORDING_MIN_FREE_BYTES", 0)
    monkeypatch.setattr(server, "RECORDING_STOP_FREE_BYTES", 0)

    tunes = []

    def tune(channel_id, quality="medium", token=None, override=False):
        channel = next(c for c in CHANNELS if c["id"] == channel_id)
        held = server.lock_holder(server.LANE_DIGITAL, token, override)
        if held:
            return server.lock_refusal(held, "tune")
        tunes.append((channel_id, server.recording_tap_ports(channel)))
        server.lanes["digital"]["selection"] = channel
        return {"ok": True}, 200

    monkeypatch.setattr(server, "perform_digital_tune", tune)
    monkeypatch.setattr(server.subprocess, "Popen", FakeRecorder)

    def finalize(parts, stem=None):
        parts = [part for part in parts if part.exists()]
        if not parts:
            return []
        stem = stem or recorder.stem_of_part(parts[0])
        final = stem.with_name(stem.name + ".mp4")
        final.write_bytes(b"".join(part.read_bytes() for part in parts))
        for part in parts:
            part.unlink()
        return [final]

    monkeypatch.setattr(server, "finalize_recording", finalize)

    class Inline:
        def __init__(self, target, daemon=None, args=(), name=None):
            self.target, self.args = target, args

        def is_alive(self):
            return False

        def join(self, timeout=None):
            pass

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(server.threading, "Thread", Inline)
    return {"library": library, "tunes": tunes}


def schedule_now(seconds=600, **extra):
    now = time.time()
    body = {"mode": "manual", "channel_id": "wjla_7_1", "start": now - 1,
            "end": now + seconds, **extra}
    payload, status = server.create_recording(body)
    assert status == 201, payload
    return payload["timer"]


def test_manual_timers_refuse_the_past_and_overlaps(vcr):
    now = time.time()
    _payload, status = server.create_recording(
        {"mode": "manual", "channel_id": "wjla_7_1", "start": now - 900,
         "end": now - 60})
    assert status == 400

    schedule_now(1800)
    payload, status = server.create_recording(
        {"mode": "manual", "channel_id": "weta_26_1", "start": now + 600,
         "end": now + 2400})
    assert status == 409
    assert payload["error"]["code"] == "conflict"

    _payload, status = server.create_recording(
        {"mode": "manual", "channel_id": "nope", "start": now, "end": now + 600})
    assert status == 404


def test_a_guide_timer_is_padded_and_titled(vcr):
    start = int(time.time()) + 1800
    payload, status = server.create_recording(
        {"mode": "guide", "channel_number": "7.1", "event_start": start})
    assert status == 201
    timer = payload["timer"]
    assert timer["title"] == "Jeopardy!"
    assert timer["start"] == start - recorder.GUIDE_PRE_PAD_SECONDS
    assert timer["kind"] == "guide"


def test_one_touch_records_to_the_end_of_the_programme(vcr):
    server.lanes["digital"]["selection"] = CHANNELS[0]
    payload, status = server.create_recording({"mode": "otr"})
    assert status == 201
    timer = payload["timer"]
    assert timer["title"] == "General Hospital"
    now = int(time.time())
    assert timer["end"] == now + 1800 + recorder.GUIDE_POST_PAD_SECONDS \
        or abs(timer["end"] - (now + 1800 + recorder.GUIDE_POST_PAD_SECONDS)) <= 2


def test_a_recording_tunes_with_a_tap_holds_the_lock_and_finishes(vcr):
    timer = schedule_now()
    server.recording_tick()

    # It tuned the lane with a tap attached, under its own lock.
    assert vcr["tunes"] and vcr["tunes"][0][0] == "wjla_7_1"
    assert vcr["tunes"][0][1] == [server.active_recording["tap_port"]]
    held = server.active_lock(server.LANE_DIGITAL)
    assert held["owner"] == "VCR"
    stored = json.loads(server.RECORDINGS_FILE.read_text())["timers"][0]
    assert stored["status"] == "recording"
    part = server.active_recording["part"]
    assert part.name.endswith(".ts.part")

    # A second tune on another channel, without the token, is refused.
    payload, status = server.perform_digital_tune("weta_26_1")
    assert status == 423

    # Past its end, the worker stops ffmpeg with "q" and files it.
    proc = server.active_recording["proc"]
    server.find_timer(timer["id"])["end"] = time.time() - 1
    server.recording_tick()
    assert proc.written == "q"
    assert server.active_recording is None
    assert server.active_lock(server.LANE_DIGITAL) is None
    done = server.find_timer(timer["id"])
    assert done["status"] == "done"
    assert done["file"].endswith(".mp4")
    assert (vcr["library"] / done["file"]).exists()
    assert "lock_token" not in done and "part" not in done


def test_a_recording_waits_out_someone_elses_lock(vcr):
    payload, _ = server.acquire_tuner_lock(
        {"lane": "digital", "owner": "vizelvision", "duration_seconds": 600})
    timer = schedule_now()
    server.recording_tick()
    assert server.active_recording is None
    assert "vizelvision" in server.find_timer(timer["id"])["note"]

    server.release_tuner_lock({"lane": "digital", "token": payload["token"]})
    server._recording_retry_at.clear()
    server.recording_tick()
    assert server.active_recording is not None


def test_losing_the_tuner_to_an_override_ends_the_tape_as_partial(vcr):
    timer = schedule_now()
    server.recording_tick()
    server.acquire_tuner_lock(
        {"lane": "digital", "owner": "web", "override": True})
    server.recording_tick()
    ended = server.find_timer(timer["id"])
    assert ended["status"] == "partial"
    assert "web" in ended["note"]


def test_stopping_early_keeps_what_was_taped(vcr):
    timer = schedule_now()
    server.recording_tick()
    payload, status = server.cancel_recording({"id": timer["id"]})
    assert status == 200
    server.recording_tick()
    stopped = server.find_timer(timer["id"])
    assert stopped["status"] == "done" and stopped["note"] == "stopped early"
    assert stopped["file"]


def test_a_crashed_recorder_resumes_into_one_file(vcr):
    timer = schedule_now()
    server.recording_tick()
    first = server.active_recording["part"]
    server.active_recording["proc"].returncode = 1
    server.recording_tick()
    resumed = server.find_timer(timer["id"])
    assert resumed["status"] == "scheduled"
    assert "files" not in resumed          # kept as a hidden piece
    assert first.exists() and first.name.endswith(".ts.part")

    server._recording_retry_at.clear()
    server.recording_tick()
    assert server.find_timer(timer["id"])["status"] == "recording"
    second = server.active_recording["part"]
    assert second.name.endswith(".2.ts.part") and "(2)" not in second.name
    assert server.recording_view(resumed)["bytes"] == 2 * 188 * 10

    server.cancel_recording({"id": timer["id"]})
    server.recording_tick()
    done = server.find_timer(timer["id"])
    assert done["files"] == [first.name.replace(".ts.part", ".mp4")]
    assert "pieces" not in done
    assert [p.name for p in vcr["library"].iterdir()] == done["files"]


def test_a_restart_resumes_under_the_lock_it_already_holds(vcr, monkeypatch):
    timer = schedule_now()
    server.recording_tick()
    # Simulate the process dying with the server: state on disk only.
    monkeypatch.setattr(server, "active_recording", None)
    monkeypatch.setattr(server, "recording_schedule", None)

    server.recover_recordings()
    recovered = server.find_timer(timer["id"])
    assert recovered["status"] == "scheduled"
    assert len(recovered["pieces"]) == 1       # held back to be joined
    assert "files" not in recovered

    server.recording_tick()
    assert server.find_timer(timer["id"])["status"] == "recording"

    server.cancel_recording({"id": timer["id"]})
    server.recording_tick()
    done = server.find_timer(timer["id"])
    assert done["status"] == "done" and len(done["files"]) == 1
    assert not list(vcr["library"].glob("*.part"))


def test_pieces_are_filed_when_the_window_closes_before_a_resume(vcr):
    timer = schedule_now()
    server.recording_tick()
    server.active_recording["proc"].returncode = 1
    server.recording_tick()
    assert server.find_timer(timer["id"])["pieces"]

    server.recording_tick(now=timer["end"] + 1)    # the tuner never came back
    missed = server.find_timer(timer["id"])
    assert missed["status"] == "partial" and len(missed["files"]) == 1
    assert "pieces" not in missed


def test_a_restart_while_finishing_files_the_pieces_again(vcr, monkeypatch):
    timer = schedule_now()
    server.recording_tick()
    part = server.active_recording["part"]
    with server.lock:
        server._finishing(server.find_timer(timer["id"]), part)
        server.save_recording_schedule()
    monkeypatch.setattr(server, "active_recording", None)
    monkeypatch.setattr(server, "recording_schedule", None)

    server.recover_recordings()
    done = server.find_timer(timer["id"])
    assert done["status"] == "partial" and len(done["files"]) == 1
    assert not part.exists()


def test_the_service_file_size_cap_blocks_recording(vcr, monkeypatch):
    monkeypatch.setattr(server, "file_size_limit", lambda: 16 * 1024 * 1024)
    status = server.recording_storage_status()
    assert not status["ready"]
    assert status["problems"][0]["code"] == "file_size_limit"
    assert "LimitFSIZE=16M:16G" in status["problems"][0]["fix"]

    timer = schedule_now()
    server.recording_tick()
    assert server.active_recording is None
    assert "LimitFSIZE" in server.find_timer(timer["id"])["note"]


def test_a_missing_library_directory_explains_the_fix(vcr, monkeypatch):
    monkeypatch.setattr(server, "RECORDINGS_DIR", vcr["library"] / "absent")
    problem = server.recording_storage_status()["problems"][0]
    assert problem["code"] == "missing_dir"
    assert "install -d" in problem["fix"] and "minidlna" in problem["fix"]


def test_series_rules_are_expanded_by_the_worker(vcr):
    payload, status = server.create_recording_rule({"title": "jeopardy"})
    assert status == 201
    assert len(payload["timers_added"]) == 2
    again, status = server.create_recording_rule({"title": "Jeopardy"})
    assert status == 200 and again["existing"]

    server.delete_recording_rule({"id": payload["rule"]["id"]})
    statuses = {t["status"] for t in server.recording_state()["timers"]}
    assert statuses == {"cancelled"}


def test_search_marks_what_is_already_scheduled(vcr):
    start = int(time.time()) + 1800
    server.create_recording({"mode": "guide", "channel_number": "7.1",
                             "event_start": start})
    results = server.recording_search_payload("jeopardy")["results"]
    assert results[0]["scheduled"] is True
    assert results[1]["conflict"]          # 26.1 airs at the same time


def test_status_documents_carry_the_recording(vcr):
    schedule_now()
    server.recording_tick()
    summary = server.recording_summary()
    assert summary["status"] == "recording"
    assert "lock_token" not in summary and "part" not in summary


def test_source_command_carries_taps():
    command = server.build_tv_source_command(CHANNELS[0], 18320, [18384])
    assert command[-2:] == ["--tap-port", "18384"]
    assert "--tap-port" not in server.build_tv_source_command(CHANNELS[0], 18320)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_finalize_remuxes_a_real_transport_stream_to_mp4(tmp_path):
    part = tmp_path / "Clip - 7.1 WJLA - 2026-09-23 2000.ts.part"
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-f", "mpegts", str(part),
    ], check=True, timeout=30)

    final = server.finalize_recording_file(part)
    assert final == tmp_path / "Clip - 7.1 WJLA - 2026-09-23 2000.mp4"
    assert final.stat().st_size > 0
    assert not part.exists()
    assert not list(tmp_path.glob("*.part"))


def make_clip(path, seconds, frequency=440):
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}",
        "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-f", "mpegts", str(path),
    ], check=True, timeout=30)


def test_pieces_are_named_under_one_stem(tmp_path):
    stem = tmp_path / "Jeopardy! - 7.1 WJLA - 2026-09-24 1930"
    assert recorder.piece_part(stem, 1) == recorder.recording_part(stem)
    assert recorder.piece_part(stem, 2).name == \
        "Jeopardy! - 7.1 WJLA - 2026-09-24 1930.2.ts.part"
    assert recorder.concat_list([Path("/t/It's.ts.part")]) == \
        "ffconcat version 1.0\nfile '/t/It'\\''s.ts.part'\n"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_finalize_joins_pieces_into_one_mp4(tmp_path):
    stem = tmp_path / "It's Jeopardy! - 7.1 WJLA - 2026-09-24 1930"
    pieces = [recorder.piece_part(stem, 1), recorder.piece_part(stem, 2)]
    make_clip(pieces[0], 2)
    make_clip(pieces[1], 3, frequency=880)

    files = server.finalize_recording(pieces, stem)
    assert files == [stem.with_name(stem.name + ".mp4")]
    assert server.media_duration(files[0]) == pytest.approx(5, abs=0.5)
    assert sorted(p.name for p in tmp_path.iterdir()) == [files[0].name]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_failed_join_files_every_piece(tmp_path, monkeypatch):
    stem = tmp_path / "Clip - 7.1 WJLA - 2026-09-24 1930"
    pieces = [recorder.piece_part(stem, 1), recorder.piece_part(stem, 2)]
    make_clip(pieces[0], 1)
    make_clip(pieces[1], 1)
    monkeypatch.setattr(server, "join_recording_pieces", lambda *_: None)

    files = server.finalize_recording(pieces, stem)
    assert [f.name for f in files] == [
        "Clip - 7.1 WJLA - 2026-09-24 1930.mp4",
        "Clip - 7.1 WJLA - 2026-09-24 1930 (2).mp4",
    ]
    assert not list(tmp_path.glob("*.part"))


def test_a_new_channel_never_listens_on_a_tap_the_old_source_still_feeds(vcr):
    first = schedule_now(120)
    server.recording_tick()
    old_port = server.active_recording["tap_port"]
    server.lanes["digital"]["taps"] = [old_port]
    server.find_timer(first["id"])["end"] = time.time() - 1
    server.recording_tick()

    # The lane is still on 7.1 feeding old_port; a 26.1 recording must not
    # listen there, or it would tape 7.1 until the retune lands.
    now = time.time()
    server.create_recording({"mode": "manual", "channel_id": "weta_26_1",
                             "start": now - 1, "end": now + 600})
    server.recording_tick()
    assert server.active_recording["channel_id"] == "weta_26_1"
    assert server.active_recording["tap_port"] != old_port


def test_back_to_back_timers_on_one_channel_reuse_the_tap(vcr):
    first = schedule_now(120)
    server.recording_tick()
    old_port = server.active_recording["tap_port"]
    server.lanes["digital"]["taps"] = [old_port]
    server.find_timer(first["id"])["end"] = time.time() - 1
    server.recording_tick()

    schedule_now(600)
    server.recording_tick()
    assert server.active_recording["tap_port"] == old_port


# ---------------------------------------------------------------------------
# Serving tapes to the browser
# ---------------------------------------------------------------------------

def test_byte_range_covers_what_a_seeking_player_sends():
    assert recorder.byte_range(None, 1000) is None
    assert recorder.byte_range("", 1000) is None
    assert recorder.byte_range("bytes=0-", 1000) == (0, 999)
    assert recorder.byte_range("bytes=100-199", 1000) == (100, 199)
    assert recorder.byte_range("bytes=900-5000", 1000) == (900, 999)
    assert recorder.byte_range("bytes=-100", 1000) == (900, 999)
    assert recorder.byte_range("bytes=-5000", 1000) == (0, 999)
    # Multiple ranges and other units are answered with the whole file.
    assert recorder.byte_range("bytes=0-1,5-6", 1000) is None
    assert recorder.byte_range("items=0-1", 1000) is None


def test_byte_range_past_the_end_is_unsatisfiable():
    import pytest
    for header in ("bytes=1000-", "bytes=2000-3000", "bytes=-0", "bytes=50-10"):
        with pytest.raises(ValueError):
            recorder.byte_range(header, 1000)


def test_download_name_keeps_the_title_but_stays_header_safe():
    inline = recorder.content_disposition('Jeopardy! - 7.1 "WJLA".mp4', False)
    saved = recorder.content_disposition("El Señor de los Cielos.mp4", True)
    assert inline.startswith("inline;")
    assert saved.startswith("attachment;")
    assert '"WJLA"' not in inline               # quotes cannot end the header value
    assert "filename*=UTF-8''El%20Se%C3%B1or" in saved
    assert "\r" not in saved and "\n" not in saved


@pytest.fixture
def live(vcr, monkeypatch):
    """Bring the digital lane's live stream up as one generation."""
    stream = {"ready": False}
    monkeypatch.setattr(server, "lane_stream_ready",
                        lambda lane: stream["ready"])

    def go(generation):
        server.lanes["digital"]["stream_id"] = generation
        stream["ready"] = generation is not None

    return go


def test_a_from_live_recording_copies_the_live_stream_without_a_tap(vcr, live):
    timer = schedule_now(from_live=True)
    assert timer["from_live"] is True
    server.recording_tick()

    # It tuned under its own lock but opened no tap, and copies nothing
    # until the live stream is ready.
    assert vcr["tunes"] == [("wjla_7_1", [])]
    assert server.active_lock(server.LANE_DIGITAL)["owner"] == "VCR"
    assert server.active_recording["proc"] is None
    assert server.find_timer(timer["id"])["status"] == "recording"

    live("gen-1")
    server.recording_tick()
    proc = server.active_recording["proc"]
    assert proc.cmd[proc.cmd.index("-c") + 1] == "copy"
    assert proc.cmd[proc.cmd.index("-i") + 1] \
        == str(server.lane_playlist_path(server.LANE_DIGITAL))
    assert proc.cmd[-1] == str(server.active_recording["part"])

    server.find_timer(timer["id"])["end"] = time.time() - 1
    server.recording_tick()
    assert proc.written == "q"
    done = server.find_timer(timer["id"])
    assert done["status"] == "done" and done["file"].endswith(".mp4")


def test_a_from_live_recording_follows_a_restarted_live_stream(vcr, live):
    timer = schedule_now(from_live=True)
    server.recording_tick()
    live("gen-1")
    server.recording_tick()
    first_proc = server.active_recording["proc"]
    first = server.active_recording["part"]

    # The live encoder dies: the copy stops and its piece is kept.
    live(None)
    server.recording_tick()
    assert first_proc.written == "q"
    assert server.active_recording["proc"] is None
    stored = server.find_timer(timer["id"])
    assert stored["pieces"] == [str(first)]

    # The keeper brings it back: the next piece starts on the new
    # generation, still under the same lock and without spending an attempt.
    live("gen-2")
    server.recording_tick()
    second = server.active_recording["part"]
    assert second.name.endswith(".2.ts.part") and "(2)" not in second.name
    assert server.active_recording["generation"] == "gen-2"
    assert stored["status"] == "recording" and stored["attempts"] == 1
    assert stored["part"] == str(second)

    server.cancel_recording({"id": timer["id"]})
    server.recording_tick()
    done = server.find_timer(timer["id"])
    assert done["files"] == [first.name.replace(".ts.part", ".mp4")]
    assert (vcr["library"] / done["file"]).stat().st_size == 2 * 188 * 10


def test_a_from_live_copy_that_dies_on_its_own_stream_resumes(vcr, live):
    timer = schedule_now(from_live=True)
    server.recording_tick()
    live("gen-1")
    server.recording_tick()
    server.active_recording["proc"].returncode = 1
    server.recording_tick()
    resumed = server.find_timer(timer["id"])
    assert resumed["status"] == "scheduled"
    assert "resuming" in resumed["note"]


# ---------------------------------------------------------------------------
# The Roku lane's VCR
# ---------------------------------------------------------------------------

@pytest.fixture
def roku_vcr(vcr, monkeypatch):
    from types import SimpleNamespace

    import roku_lane
    from test_roku_lane import GAME, NFL, Config, FakeController

    capture = {"running": True, "stream_id": "cap-1", "tap_url": "udp://127.0.0.1:1"}
    controller = FakeController(capture)
    lane = roku_lane.RokuLane(
        Config(), controller,
        baseball=SimpleNamespace(game=lambda pk, day: dict(GAME),
                                 games=lambda day: {"date": day, "games": [dict(GAME)]}),
        sports=SimpleNamespace(game=lambda *a: dict(NFL),
                               games=lambda league, day: {"date": day, "games": [dict(NFL)]}),
        capture_source=lambda: dict(capture), playing_seconds=.1, sleep=lambda _: None)
    monkeypatch.setattr(server, "roku", lane)
    # The fake config names no HD PVR; take it as plugged in.
    monkeypatch.setattr(server, "roku_sources_available", lambda: True)
    server.lanes["roku"]["stream_id"] = "cap-1"
    real_ready = server.lane_stream_ready
    monkeypatch.setattr(server, "lane_stream_ready",
                        lambda name: name == "roku" or real_ready(name))
    return {**vcr, "lane": lane, "controller": controller}


def schedule_roku(seconds=600, **extra):
    now = time.time()
    body = {"lane": "roku", "mode": "manual",
            "target": {"source_id": "youtube-tv", "channel_id": "espn"},
            "start": now - 1, "end": now + seconds, **extra}
    payload, status = server.create_recording(body)
    assert status == 201, payload
    return payload["timer"]


def test_roku_and_digital_timers_never_conflict(roku_vcr):
    schedule_now(1800)
    timer = schedule_roku(1800)
    assert timer["lane"] == "roku" and timer["from_live"] is True
    assert timer["target"] == {"source_id": "youtube-tv", "channel_id": "ESPN"}
    assert timer["channel_name"] == "YouTube TV ESPN"
    now = time.time()
    payload, status = server.create_recording({
        "lane": "roku", "mode": "manual", "start": now + 60, "end": now + 900,
        "target": {"source_id": "youtube-tv", "channel_id": "NBC"}})
    assert status == 409 and "one source Roku" in payload["error"]["message"]
    payload, status = server.create_recording({
        "lane": "roku", "mode": "manual", "start": now, "end": now + 900,
        "target": {"source_id": "youtube-tv", "channel_id": "HBO"}})
    assert status == 404


def test_a_youtube_tv_airing_in_the_guide_tapes_the_roku_from_early_enough(roku_vcr, monkeypatch):
    now = int(time.time())
    start = now + 3600
    monkeypatch.setattr(server.yttv_epg, "load_guide", lambda: {"channels": {"NBC": {
        "events": [{"start": start, "duration": 1800, "title": "NBC Nightly News"},
                   {"start": now - 7200, "duration": 600, "title": "Over"}]}}})
    payload, status = server.create_recording({
        "lane": "roku", "mode": "guide", "event_start": start,
        "target": {"source_id": "youtube-tv", "channel_id": "nbc"}})
    assert status == 201, payload
    timer = payload["timer"]
    assert timer["kind"] == "guide" and timer["title"] == "NBC Nightly News"
    assert timer["target"] == {"source_id": "youtube-tv", "channel_id": "NBC"}
    # The Roku needs time to reach the channel before the padded start.
    assert timer["start"] == start - recorder.GUIDE_PRE_PAD_SECONDS - roku_lane.NAVIGATION_SECONDS
    assert timer["end"] == start + 1800 + recorder.GUIDE_POST_PAD_SECONDS

    payload, status = server.create_recording({
        "lane": "roku", "mode": "guide", "event_start": now - 7200,
        "target": {"source_id": "youtube-tv", "channel_id": "NBC"}})
    assert status == 400 and payload["error"]["code"] == "in_the_past"
    payload, status = server.create_recording({
        "lane": "roku", "mode": "guide", "event_start": start + 1,
        "target": {"source_id": "youtube-tv", "channel_id": "NBC"}})
    assert status == 404 and payload["error"]["code"] == "unknown_event"
    payload, status = server.create_recording({
        "lane": "roku", "mode": "guide", "event_start": start,
        "target": {"source_id": "roku-manual"}})
    assert status == 400 and payload["error"]["code"] == "invalid_mode"


def test_a_roku_recording_navigates_then_copies_from_the_live_edge(roku_vcr):
    schedule_roku()
    server.recording_tick()
    held = server.active_lock(server.LANE_ROKU)
    assert held["owner"] == "VCR"
    assert roku_vcr["controller"].plays == ["ESPN"]
    assert server.roku_recording["signal"] in ("tuning", "ok")
    server.recording_tick()
    proc = server.roku_recording["proc"]
    assert proc is not None
    assert str(server.lane_playlist_path(server.LANE_ROKU)) in proc.cmd
    assert proc.cmd[proc.cmd.index("-live_start_index") + 1] == "-3"
    assert "copy" in proc.cmd and "libx264" not in proc.cmd
    # Nothing but the VCR may move the Roku now.
    _payload, status = server.perform_roku_tune({"source": "youtube-tv", "channel_id": "NBC"})
    assert status == 423


def test_a_roku_recording_ends_when_the_roku_is_moved_off_it(roku_vcr):
    schedule_roku()
    server.recording_tick()
    server.recording_tick()
    assert server.roku_recording["proc"] is not None
    # Someone overrides the lock and takes the remote.
    server.release_tuner_lock({"lane": "roku", "override": True})
    server.recording_tick()
    assert server.roku_recording is None
    timer = server.recordings_payload()["history"][0]
    assert timer["status"] == "partial" and "taken over" in timer["note"]


def test_the_vcr_shares_a_roku_someone_else_holds_on_its_target(roku_vcr):
    lane = roku_vcr["lane"]
    lane.tune({"source_id": "youtube-tv", "channel_id": "ESPN"})
    payload, _ = server.acquire_tuner_lock(
        {"lane": "roku", "owner": "VizelVision master", "duration_seconds": 600})
    schedule_roku()
    server.recording_tick()
    assert server.roku_recording["shared"] is True
    assert server.roku_recording["token"] is None
    assert server.active_lock(server.LANE_ROKU)["owner"] == "VizelVision master"
    assert roku_vcr["controller"].plays == ["ESPN"]  # It never moved the Roku.
    server.recording_tick()
    assert server.roku_recording["proc"] is not None
    # The holder moves on: the shared tape ends, the holder's lock stays.
    lane.tune({"source_id": "youtube-tv", "channel_id": "NBC", "token": payload["token"]})
    server.recording_tick()
    assert server.roku_recording is None
    assert server.active_lock(server.LANE_ROKU)["owner"] == "VizelVision master"


def test_the_vcr_waits_when_a_holder_shows_something_else(roku_vcr):
    server.acquire_tuner_lock({"lane": "roku", "owner": "Web remote", "duration_seconds": 600})
    timer = schedule_roku()
    server.recording_tick()
    assert server.roku_recording is None
    stored = next(t for t in server.recordings_payload()["timers"] if t["id"] == timer["id"])
    assert stored["status"] == "scheduled" and "Web remote" in stored["note"]
    assert roku_vcr["controller"].plays == []


def test_digital_and_roku_record_side_by_side(roku_vcr):
    schedule_now()
    schedule_roku()
    server.recording_tick()
    server.recording_tick()
    assert server.active_recording is not None and server.roku_recording is not None
    payload = server.recordings_payload()
    assert {t["lane"] for t in payload["active_recordings"]} == {"digital", "roku"}
    assert server.active_lock(server.LANE_DIGITAL)["owner"] == "VCR"
    assert server.active_lock(server.LANE_ROKU)["owner"] == "VCR"


def test_one_touch_tapes_what_the_roku_shows(roku_vcr):
    roku_vcr["lane"].tune({"source_id": "youtube-tv", "channel_id": "NBC"})
    payload, status = server.create_recording({"lane": "roku", "mode": "otr"})
    assert status == 201
    timer = payload["timer"]
    assert timer["target"] == {"source_id": "youtube-tv", "channel_id": "NBC"}
    assert timer["end"] - timer["start"] == pytest.approx(recorder.OTR_DEFAULT_SECONDS, abs=2)
    roku_vcr["lane"].keypress("Down")
    payload, status = server.create_recording({"lane": "roku", "mode": "otr",
                                               "end": time.time() + 3600})
    assert status == 409  # The first one-touch tape still has the Roku.


# ---------------------------------------------------------------------------
# The VCR's inputs: what its SOURCE menu offers
# ---------------------------------------------------------------------------

def test_without_a_roku_the_vcr_lists_the_digital_tuner_alone(vcr):
    inputs = server.recordings_payload()["inputs"]
    assert [entry["lane"] for entry in inputs] == ["digital"]
    digital = inputs[0]
    assert digital["modes"] == ["manual", "guide", "otr"]
    assert [source["items_path"] for source in digital["sources"]] == ["/api/v1/sources/digital"]


def test_the_vcr_lists_the_roku_and_its_feeds(roku_vcr):
    inputs = {entry["lane"]: entry for entry in server.recordings_payload()["inputs"]}
    assert list(inputs) == ["digital", "roku"]
    roku = inputs["roku"]
    assert roku["modes"] == ["manual", "otr"] and roku["available"] is True
    assert roku["shares_with_holder"] is True and roku["lead_seconds"] >= 60
    sources = {source["id"]: source for source in roku["sources"]}
    assert list(sources) == list(roku_lane.SOURCES)
    assert sources["youtube-tv"]["dated"] is False and sources["youtube-tv"]["typical_seconds"] is None
    assert sources["mlb"]["dated"] is True and 2 * 3600 < sources["mlb"]["typical_seconds"] < 4 * 3600
    assert sources["sunday-ticket"]["typical_seconds"] == sources["thursday-night-football"]["typical_seconds"]


def test_an_input_names_who_else_holds_it(roku_vcr):
    assert all(entry["held_by"] is None for entry in server.recordings_payload()["inputs"])
    server.acquire_tuner_lock({"lane": "roku", "owner": "VizelVision master", "duration_seconds": 600})
    inputs = {entry["lane"]: entry for entry in server.recordings_payload()["inputs"]}
    assert inputs["roku"]["held_by"] == "VizelVision master"
    assert inputs["digital"]["held_by"] is None
    # The VCR's own hold is not someone else's.
    server.release_tuner_lock({"lane": "roku", "override": True})
    schedule_roku()
    server.recording_tick()
    assert server.active_lock(server.LANE_ROKU)["owner"] == "VCR"
    inputs = {entry["lane"]: entry for entry in server.recordings_payload()["inputs"]}
    assert inputs["roku"]["held_by"] is None


def test_every_input_takes_the_vcr_pages_one_timer_shape(roku_vcr):
    """The page posts lane, source_id, channel_id and a game's date flat."""
    now = time.time()
    payload, status = server.create_recording({
        "lane": "digital", "mode": "manual", "source_id": "digital",
        "channel_id": "wjla_7_1", "start": now + 60, "end": now + 1800})
    assert status == 201, payload
    assert payload["timer"]["lane"] == "digital" and payload["timer"]["channel_id"] == "wjla_7_1"

    payload, status = server.create_recording({
        "lane": "roku", "mode": "manual", "source_id": "mlb", "channel_id": "823254",
        "date": "2026-10-06", "start": now + 60, "end": now + 3 * 3600})
    assert status == 201, payload
    timer = payload["timer"]
    assert timer["target"] == {"source_id": "mlb", "channel_id": "823254", "date": "2026-10-06"}
    assert timer["title"] == "Phillies at Braves"


def test_a_lane_that_cannot_record_is_refused(vcr):
    now = time.time()
    for lane in ("analog", "fm", "nope"):
        payload, status = server.create_recording({
            "lane": lane, "mode": "manual", "channel_id": "wjla_7_1",
            "start": now + 60, "end": now + 1800})
        assert status == 400 and payload["error"]["code"] == "invalid_lane", lane
    assert server.recordings_payload()["timers"] == []
