"""On-demand video for the station's slide channels (lineup.SLIDES).

A slide channel plays in a browser from its own page; for players that only
take streams (a Roku), it is also video: a renderer (sports_guide_video.py)
writes raw frames into one ffmpeg, which writes HLS into the channel's own
directory. The video is made only while someone watches. The first request
for the playlist starts the pair and waits for its first segments; once
nothing has asked for the playlist or a segment for ``idle_seconds``, both
stop and the directory is emptied. A pair that dies or stalls while watched
is restarted, ``max_restarts`` times a ``restart_window`` at most; after that
it rests for a window before trying again.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

PLAYLIST = "index.m3u8"


def _kill(process, timeout, killpg=os.killpg):
    """Stop a process and its session, then reap it."""
    if process is None:
        return
    for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5.0)):
        if process.poll() is not None:
            break
        try:
            killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue


class SlideStream:
    def __init__(self, name, directory, renderer_command, encoder_command, *,
                 idle_seconds=60.0, ready_segments=2, stall_seconds=10.0,
                 startup_seconds=15.0, stop_timeout=2.0, max_restarts=3,
                 restart_window=60.0, env=None, popen=subprocess.Popen,
                 killpg=os.killpg, clock=time.monotonic, sleep=time.sleep):
        self.name = name
        self.directory = Path(directory)
        self.renderer_command = renderer_command        # () -> argv
        self.encoder_command = encoder_command          # (generation, directory) -> argv
        self.idle_seconds = idle_seconds
        self.ready_segments = ready_segments
        self.stall_seconds = stall_seconds
        self.startup_seconds = startup_seconds
        self.stop_timeout = stop_timeout
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self.env = env
        self.popen, self.killpg, self.clock, self.sleep = popen, killpg, clock, sleep
        self._lock = threading.Lock()
        self.renderer = self.encoder = None
        self.generation = None
        self.started_at = None
        self.last_demand = None
        self.failures = deque()
        self.resting_until = 0.0
        self.starts = 0
        self.last_stop = None
        self.last_error = None

    # -- files ------------------------------------------------------------------
    @property
    def playlist(self):
        return self.directory / PLAYLIST

    def clear(self):
        if not self.directory.is_dir():
            return
        for path in self.directory.iterdir():
            if path.suffix in (".m3u8", ".ts", ".tmp") or path.name.endswith(".m3u8.tmp"):
                try:
                    path.unlink()
                except OSError:
                    pass   # temp_file segments come and go under us

    def segment_count(self):
        try:
            return self.playlist.read_text().count("#EXTINF:")
        except OSError:
            return 0

    def ready(self):
        return self._alive() and self.segment_count() >= self.ready_segments

    # -- processes ----------------------------------------------------------------
    def _alive(self):
        return (self.renderer is not None and self.renderer.poll() is None
                and self.encoder is not None and self.encoder.poll() is None)

    def _start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clear()
        generation = f"{int(time.time())}{uuid.uuid4().hex[:6]}"
        renderer = self.popen(self.renderer_command(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, env=self.env, start_new_session=True)
        try:
            encoder = self.popen(self.encoder_command(generation, self.directory),
                                 stdin=renderer.stdout, stdout=subprocess.DEVNULL,
                                 start_new_session=True)
        except OSError:
            _kill(renderer, self.stop_timeout, self.killpg)
            raise
        finally:
            # The encoder holds the pipe now: when it goes, the renderer's
            # next write fails and it exits.
            if renderer.stdout is not None:
                renderer.stdout.close()
        self.renderer, self.encoder, self.generation = renderer, encoder, generation
        self.started_at = self.clock()
        self.starts += 1
        print(f"[{self.name}] started generation {generation} "
              f"(renderer {renderer.pid}, encoder {encoder.pid})", flush=True)

    def _stop(self, reason):
        if self.renderer is None and self.encoder is None:
            return
        # The encoder first, so the renderer's write fails rather than blocks.
        _kill(self.encoder, self.stop_timeout, self.killpg)
        _kill(self.renderer, self.stop_timeout, self.killpg)
        print(f"[{self.name}] stopped generation {self.generation}: {reason}", flush=True)
        self.renderer = self.encoder = None
        self.started_at = None
        self.last_stop = reason
        self.clear()

    def _may_restart(self, now):
        while self.failures and now - self.failures[0] > self.restart_window:
            self.failures.popleft()
        if len(self.failures) >= self.max_restarts:
            self.resting_until = now + self.restart_window
            self.failures.clear()
            return False
        return now >= self.resting_until

    # -- what callers use -----------------------------------------------------
    def demand(self, wait=0.0):
        """Someone asked for the playlist: start the video if it is not running,
        and with ``wait`` block until its first segments are written. Returns
        whether it is ready."""
        with self._lock:
            now = self.clock()
            self.last_demand = now
            if not self._alive():
                if self.renderer is not None or self.encoder is not None:
                    self._failed("died", now)
                if not self._may_restart(now):
                    return False
                try:
                    self._start()
                except OSError as error:
                    self.last_error = f"could not start: {error}"
                    self.failures.append(now)
                    return False
        deadline = self.clock() + wait
        while not self.ready():
            if self.clock() >= deadline or not self._alive():
                return self.ready()
            self.sleep(0.1)
        return True

    def touch(self):
        """A segment was fetched: someone is still watching."""
        self.last_demand = self.clock()

    def _failed(self, reason, now):
        self.last_error = f"generation {self.generation} {reason}"
        self.failures.append(now)
        self._stop(reason)

    def check(self):
        """The supervisor's tick: stop when nobody watches, restart a pair
        that died or stalled while someone does."""
        with self._lock:
            if self.renderer is None and self.encoder is None:
                return
            now = self.clock()
            if self.last_demand is None or now - self.last_demand >= self.idle_seconds:
                self._stop("idle")
                return
            if not self._alive():
                self._failed("died", now)
            elif now - self.started_at > self.startup_seconds and self._stalled():
                self._failed("stalled", now)
            else:
                return
            if self._may_restart(now):
                try:
                    self._start()
                except OSError as error:
                    self.last_error = f"could not start: {error}"

    def _stalled(self):
        try:
            return time.time() - self.playlist.stat().st_mtime > self.stall_seconds
        except OSError:
            return True

    def stop(self, reason="stopped"):
        with self._lock:
            self._stop(reason)

    def status(self):
        now = self.clock()
        running = self._alive()
        return {
            "running": running,
            "ready": running and self.segment_count() >= self.ready_segments,
            "generation": self.generation if running else None,
            "playlist_path": f"/stream/{self.name}/{PLAYLIST}",
            "idle_seconds": self.idle_seconds,
            "seconds_since_demand": None if self.last_demand is None
            else round(now - self.last_demand, 1),
            "starts": self.starts,
            "resting": now < self.resting_until,
            "last_stop": self.last_stop,
            "last_error": self.last_error,
        }
