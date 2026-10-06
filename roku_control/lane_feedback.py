"""Verification frames from TV Thing's Roku lane.

The lane's capture process is the only reader of the HD PVR. It copies the
transport stream to HLS and to a private loopback UDP tap. This module decodes
that tap into PPM frames for screen recognition.

The decoder runs only while something needs frames -- a navigation, a
calibration, the still-playing check, a preview -- and stops after
``idle_seconds``. An idle Roku therefore costs one stream copy, not a
continuous 720p60 decode (about 65% of a core in vizelVision's always-on
reader).
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .capture_stream import CaptureStream
from .config import FeedbackConfig
from .errors import NavigationError
from .feedback import PpmFrame, VisualFeedback


class LaneCaptureFeedback(VisualFeedback):
    """Fresh frames decoded from the Roku lane's TS tap."""

    IDLE_SECONDS = 60.0

    def __init__(self, config: FeedbackConfig, capture_source: Callable[[], dict],
                 *, idle_seconds: float | None = None,
                 stream_factory: Callable[[list[str]], CaptureStream] = CaptureStream) -> None:
        super().__init__(config)
        self.device = Path(config.device) if config.device else None
        # () -> {"running": bool, "stream_id": str | None, "tap_url": str}
        self.capture_source = capture_source
        self.stream_factory = stream_factory
        self.idle_seconds = self.IDLE_SECONDS if idle_seconds is None else idle_seconds
        self._capture_lock = threading.Lock()
        self._stream: CaptureStream | None = None
        self._stream_capture_id: str | None = None
        self._sequence = 0
        self._last_used = time.monotonic()
        self.last_stream_failure: dict | None = None
        self._signal_lock = threading.Lock()
        self._signal_cache: dict | None = None
        self._signal_checked = 0.0
        self._stopping = threading.Event()
        self._reaper: threading.Thread | None = None

    # -- decoder lifetime -------------------------------------------------
    # Frames handed to recognition per second. The controller waits a second
    # or more between keys, so 15 fps keeps an observation 200 ms from its
    # request while converting and piping a quarter of the 60 fps the HD PVR
    # sends (166 MB/s of RGB into Python at the full rate).
    FRAMES_PER_SECOND = 15

    def decoder_command(self, tap_url: str) -> list[str]:
        # The tap is already a clean transport stream from a running reader,
        # so the probe can be far smaller than a cold HD PVR open needs.
        # Two threads and no input buffering: a decoder that keeps up adds no
        # frames of delay, and one that cannot is not left seconds behind.
        return [self.config.ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-threads", "2", "-fflags", "nobuffer", "-flags", "low_delay",
                "-probesize", "1000000", "-analyzeduration", "1500000",
                "-f", "mpegts", "-i", tap_url, "-an",
                "-vf", f"bwdif=mode=send_frame:parity=auto:deint=interlaced,fps={self.FRAMES_PER_SECOND}",
                "-f", "image2pipe", "-vcodec", "ppm", "-flush_packets", "1", "pipe:1"]

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
            self._stream_capture_id = None

    def _ensure_reaper(self) -> None:
        if self._reaper is None or not self._reaper.is_alive():
            self._reaper = threading.Thread(target=self._reap_idle, name="roku-verifier-idle", daemon=True)
            self._reaper.start()

    def _reap_idle(self) -> None:
        while not self._stopping.wait(min(2.0, max(self.idle_seconds / 4, .05))):
            if self._stream is None:
                continue
            if time.monotonic() - self._last_used < self.idle_seconds:
                continue
            # Never pull the decoder from under an observation in progress.
            if self._capture_lock.acquire(blocking=False):
                try:
                    if self._stream is not None and time.monotonic() - self._last_used >= self.idle_seconds:
                        self._close_stream()
                finally:
                    self._capture_lock.release()

    def close(self) -> None:
        self._stopping.set()
        with self._capture_lock:
            self._close_stream()
        if self._reaper is not None:
            self._reaper.join(timeout=3)

    @property
    def decoding(self) -> bool:
        return self._stream is not None

    # -- observations -----------------------------------------------------
    def _capture(self, *, record: bool) -> PpmFrame:
        deadline = time.monotonic() + self.config.capture_timeout_seconds
        while not self._capture_lock.acquire(timeout=.25):
            self.wait(0)
            if time.monotonic() >= deadline:
                raise NavigationError("The Roku capture is busy with another observation")
        try:
            source = self.capture_source()
            capture_id = source.get("stream_id")
            if not source.get("running") or not capture_id:
                self._close_stream()
                raise NavigationError("The Roku capture is not running",
                                      details={"capture": source.get("error")})
            if self._stream is not None and (self._stream_capture_id != capture_id
                                             or self._stream.process.poll() is not None):
                self._close_stream()
            if self._stream is None:
                self._stream = self.stream_factory(self.decoder_command(source["tap_url"]))
                self._stream_capture_id = capture_id
                self._ensure_reaper()
            self._last_used = time.monotonic()
            barrier = datetime.now(timezone.utc).isoformat()
            try:
                frame = self._stream.next_frame(self.config.capture_timeout_seconds, self.wait)
            except NavigationError as exc:
                self.last_stream_failure = {"at": datetime.now(timezone.utc).isoformat(),
                                            "stream_id": capture_id, "error": str(exc),
                                            "capture": self._stream.status()}
                self._close_stream()
                raise
            if self.capture_source().get("stream_id") != capture_id:
                # The lane restarted its reader while this frame was decoded.
                self._close_stream()
                raise NavigationError("The Roku capture restarted during observation")
            self._last_used = time.monotonic()
            if record:
                self._sequence += 1
                self.last_frame = frame
                self.last_capture = {"backend": "lane", "device": str(self.device) if self.device else None,
                                     "fresh": True, "stream_id": capture_id,
                                     "sequence": self._sequence, "barrier_at": barrier,
                                     "observed_at": datetime.now(timezone.utc).isoformat()}
                if self.on_capture:
                    self.on_capture(dict(self.last_capture))
            return frame
        finally:
            self._capture_lock.release()

    def capture(self) -> PpmFrame:
        return self._capture(record=True)

    def observe(self) -> PpmFrame:
        """A fresh frame that never replaces the evidence of a cue."""
        return self._capture(record=False)

    # -- readiness and diagnostics ----------------------------------------
    def _device_ready(self) -> bool:
        if self.device is None:
            return False
        try:
            info = self.device.stat()
            return stat.S_ISCHR(info.st_mode) and os.access(self.device, os.R_OK | os.W_OK)
        except OSError:
            return False

    def status(self) -> dict[str, object]:
        source = self.capture_source()
        stream = self._stream
        return {"backend": "lane", "device": str(self.device) if self.device else None,
                "ready": self._device_ready() and bool(source.get("running")),
                "stream_id": source.get("stream_id"), "last_capture": self.last_capture,
                "decoding": stream is not None,
                "capture": stream.status() if stream else None,
                "last_stream_failure": self.last_stream_failure}

    def assert_ready(self) -> dict[str, object]:
        status = self.status()
        if not status["ready"]:
            raise NavigationError("The Roku capture device is missing, inaccessible or not running",
                                  details=status)
        return status

    def source_catalog(self) -> dict[str, object]:
        status = self.status()
        key = f"lane:{self.device}"
        return {"sources": [{"key": key, "source": "lane", "source_id": key,
                             "name": self.config.source_name, "detail": str(self.device),
                             "available": status["ready"]}],
                "selected": key, "required": key, "ready": status["ready"]}

    def select_source(self, key: str) -> dict[str, object]:
        if key != f"lane:{self.device}":
            raise NavigationError("Only the Roku lane's capture input is available")
        self.capture()
        return self.source_catalog()

    def input_signal(self) -> dict[str, object]:
        """Query the incoming signal, not the driver's last configured size."""
        # Several calibration tabs must not queue concurrent USB timing queries.
        with self._signal_lock:
            if self._signal_cache is not None and time.monotonic() - self._signal_checked < .25:
                return dict(self._signal_cache)
            result = self._query_input_signal()
            result["observed_at"] = datetime.now(timezone.utc).isoformat()
            self._signal_cache = result
            self._signal_checked = time.monotonic()
            return dict(result)

    def _query_input_signal(self) -> dict[str, object]:
        if not self._device_ready():
            return {"state": "unavailable", "label": "Capture device unavailable"}
        try:
            probe = subprocess.run(
                ["/usr/bin/v4l2-ctl", "--device", str(self.device), "--query-dv-timings"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2,
            )
            return self._parse_input_signal(probe.returncode, probe.stdout + probe.stderr)
        except subprocess.TimeoutExpired:
            return {"state": "unknown", "label": "Input timing query timed out"}
        except OSError as exc:
            return {"state": "unknown", "label": "Input resolution unavailable", "error": str(exc)}

    @staticmethod
    def _parse_input_signal(returncode: int, output: str) -> dict[str, object]:
        if returncode:
            if "No locks available" in output or "Link has been severed" in output:
                return {"state": "no_signal", "label": "No component signal"}
            if "No such device" in output or "No such file" in output:
                return {"state": "unavailable", "label": "Capture device disconnected"}
            if "Device or resource busy" in output:
                return {"state": "busy", "label": "Capture device busy"}
            return {"state": "unknown", "label": "Input resolution unavailable", "error": output.strip()[-1000:]}
        width = re.search(r"Active width:\s*(\d+)", output)
        height = re.search(r"Active height:\s*(\d+)", output)
        scan = re.search(r"Frame format:\s*(interlaced|progressive)", output)
        rate = re.search(r"\(([\d.]+) (fields|frames) per second\)", output)
        if not width or not height or not scan or not int(width[1]) or not int(height[1]):
            return {"state": "unknown", "label": "Input resolution unavailable"}
        label = f"{width[1]} × {height[1]} · {scan[1]}"
        if rate:
            label += f" · {rate[1]} {rate[2]}/s"
        return {"state": "locked", "width": int(width[1]), "height": int(height[1]),
                "scan": scan[1], "rate": float(rate[1]) if rate else None, "label": label}

    # -- preview ----------------------------------------------------------
    def _run(self, command: list[str], *, input_data: bytes | None = None) -> bytes:
        """Bound a helper process's lifetime and reap it even when cancelled."""
        input_file = None
        try:
            if input_data is not None:
                # A complete, seekable frame: cancellation polling cannot
                # strand the encoder waiting for the rest of a large PPM.
                input_file = tempfile.TemporaryFile()
                input_file.write(input_data)
                input_file.seek(0)
            process = subprocess.Popen(command, stdin=input_file if input_file is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise NavigationError(f"Cannot start the preview encoder: {exc}") from exc
        finally:
            if input_file is not None:
                input_file.close()
        deadline = time.monotonic() + self.config.capture_timeout_seconds
        try:
            while True:
                self.wait(0)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NavigationError("The preview encoder timed out")
                try:
                    stdout, stderr = process.communicate(timeout=min(.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode or not stdout:
                raise NavigationError("The preview encoder failed",
                                      details={"ffmpeg": stderr.decode(errors="replace")[-1000:]})
            return stdout
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def preview_frames(self) -> Iterator[bytes]:
        """JPEG stills of what the verifier sees, twice a second. Never cue evidence."""
        while True:
            frame = self.observe()
            payload = f"P6\n{frame.width} {frame.height}\n255\n".encode() + frame.rgb
            yield self._run([self.config.ffmpeg_path, "-hide_banner", "-loglevel", "error",
                             "-f", "image2pipe", "-vcodec", "ppm", "-i", "pipe:0",
                             "-frames:v", "1", "-q:v", "5", "-f", "mjpeg", "pipe:1"], input_data=payload)
            time.sleep(.5)
