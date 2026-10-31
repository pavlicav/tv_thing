"""On-demand slide channel video (slide_stream.py), with fake processes: it
starts on a playlist request, stops when nobody watches, restarts what dies,
and never runs away."""
import subprocess

import pytest

import slide_stream


class FakePipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, argv, pid, **kwargs):
        self.argv, self.pid, self.kwargs = argv, pid, kwargs
        self.stdout = FakePipe() if kwargs.get("stdout") is subprocess.PIPE else None
        self.returncode = None
        self.signals = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = -sig


class Rig:
    """A SlideStream on fake processes, a fake clock, and a directory where a
    test plays the encoder by writing the playlist."""

    def __init__(self, tmp_path, **options):
        self.now = 1000.0
        self.processes = []
        self.directory = tmp_path / "sports-guide"
        self.segments_after = options.pop("segments_after", 3)   # sleeps until the encoder writes
        self.fail_encoder = options.pop("fail_encoder", False)
        self.sleeps = 0

        def popen(argv, **kwargs):
            if self.fail_encoder and argv[0] == "encoder":
                raise FileNotFoundError("ffmpeg")
            process = FakeProcess(argv, 100 + len(self.processes), **kwargs)
            self.processes.append(process)
            return process

        def killpg(pid, sig):
            next(p for p in self.processes if p.pid == pid).send_signal(sig)

        def sleep(seconds):
            self.sleeps += 1
            self.now += seconds
            if self.sleeps >= self.segments_after:
                self.write_segments(2)

        self.stream = slide_stream.SlideStream(
            "sports-guide", self.directory, lambda: ["renderer", "--url", "x"],
            lambda generation, directory: ["encoder", generation, str(directory)],
            popen=popen, killpg=killpg, clock=lambda: self.now, sleep=sleep,
            env={"OMP_NUM_THREADS": "1"}, **options)

    def write_segments(self, count):
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "index.m3u8").write_text(
            "#EXTM3U\n" + "".join(f"#EXTINF:2.0,\nseg_{n}.ts\n" for n in range(count)))

    def live(self):
        return [p for p in self.processes if p.returncode is None]


def test_the_first_request_starts_the_pair_and_waits_for_two_segments(tmp_path):
    rig = Rig(tmp_path)
    assert rig.stream.demand(wait=8) is True
    renderer, encoder = rig.processes
    assert renderer.argv == ["renderer", "--url", "x"] and encoder.argv[0] == "encoder"
    # Frames go straight from the renderer into the encoder, each in its own session.
    assert encoder.kwargs["stdin"] is renderer.stdout and renderer.stdout.closed
    assert renderer.kwargs["start_new_session"] and encoder.kwargs["start_new_session"]
    assert renderer.kwargs["env"] == {"OMP_NUM_THREADS": "1"}
    assert encoder.argv[2] == str(rig.directory)
    status = rig.stream.status()
    assert status["running"] and status["ready"] and status["generation"] == encoder.argv[1]
    # Someone else asking meanwhile shares it.
    assert rig.stream.demand(wait=8) is True and len(rig.processes) == 2


def test_a_playlist_not_ready_in_time_is_reported_not_waited_on_forever(tmp_path):
    rig = Rig(tmp_path, segments_after=10_000)
    assert rig.stream.demand(wait=2) is False
    assert 2 <= rig.now - 1000 < 2.5
    assert rig.stream.status()["running"]     # still starting: the next request may find it


def test_nobody_watching_stops_both_and_empties_the_directory(tmp_path):
    rig = Rig(tmp_path, idle_seconds=60)
    rig.stream.demand(wait=8)
    (rig.directory / "seg_old.ts").write_bytes(b"x")
    rig.now += 59
    rig.stream.touch()                         # a segment fetched: still watched
    rig.now += 59
    rig.stream.check()
    assert len(rig.live()) == 2
    rig.now += 61
    rig.stream.check()
    assert rig.live() == [] and list(rig.directory.iterdir()) == []
    status = rig.stream.status()
    assert (status["running"], status["generation"], status["last_stop"]) == (False, None, "idle")
    # The encoder is stopped before the renderer, so the renderer's write fails rather than blocks.
    encoder, renderer = rig.processes[1], rig.processes[0]
    assert encoder.signals and renderer.signals


def test_a_pair_that_dies_while_watched_is_restarted_a_few_times_then_rests(tmp_path):
    rig = Rig(tmp_path, max_restarts=3, restart_window=60)
    rig.stream.demand(wait=8)
    for death in range(3):
        rig.processes[-1].returncode = 1            # the encoder exits
        rig.now += 1
        rig.stream.touch()
        rig.stream.check()
        if death < 2:
            assert len(rig.live()) == 2, death      # a fresh pair
    # Three deaths in a minute: it rests rather than spin.
    assert rig.live() == [] and rig.stream.status()["resting"]
    assert "died" in rig.stream.status()["last_error"]
    assert rig.stream.demand(wait=1) is False
    rig.now += 61
    assert rig.stream.demand(wait=8) is True


def test_a_stalled_playlist_restarts_the_pair(tmp_path):
    import os
    rig = Rig(tmp_path, stall_seconds=10, startup_seconds=15)
    rig.stream.demand(wait=8)
    first = rig.stream.generation
    playlist = rig.directory / "index.m3u8"
    os.utime(playlist, (playlist.stat().st_mtime - 30,) * 2)
    rig.now += 5
    rig.stream.touch()
    rig.stream.check()                 # still within its startup: left alone
    assert rig.stream.generation == first
    rig.now += 20
    rig.stream.touch()
    rig.stream.check()
    assert rig.stream.generation != first and "stalled" in rig.stream.status()["last_error"]
    assert len(rig.live()) == 2


def test_an_encoder_that_cannot_start_takes_the_renderer_with_it(tmp_path):
    rig = Rig(tmp_path, fail_encoder=True)
    assert rig.stream.demand(wait=8) is False
    assert rig.live() == [] and "could not start" in rig.stream.status()["last_error"]


def test_stop_ends_it_at_once(tmp_path):
    rig = Rig(tmp_path)
    rig.stream.demand(wait=8)
    rig.stream.stop("server shutting down")
    assert rig.live() == [] and rig.stream.status()["last_stop"] == "server shutting down"
    rig.stream.stop()                  # nothing running: nothing to do


@pytest.mark.parametrize("stubborn", [False, True])
def test_kill_escalates_to_sigkill_and_reaps(stubborn):
    import signal

    class Stubborn(FakeProcess):
        def send_signal(self, sig):
            self.signals.append(sig)
            if sig == signal.SIGKILL or not stubborn:
                self.returncode = -sig

    process = Stubborn(["x"], 7)
    slide_stream._kill(process, 0.1, killpg=lambda pid, sig: process.send_signal(sig))
    assert process.returncode is not None
    assert process.signals == ([signal.SIGTERM, signal.SIGKILL] if stubborn else [signal.SIGTERM])
