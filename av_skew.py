"""Whether a digital lane's stream keeps its sound with its picture: the A/V skew.

On 2026-11-08 viviserv's WTTG (5.1, RF 36) encode, started while its reception was poor,
carried its audio 12.4 s behind its video, and the gap grew. A damaged broadcast timestamp
can do that: offline, one flipped bit in a video PTS threw the encoder's video clock forward
for good (ffmpeg patches what then goes backwards, but a jump forward runs on smoothly).
Nothing in the encoder's health saw it, and the pool kept handing that stream out: anyone
watching heard the station 12 s late, and VizelVision's master refused it outright ("Input
audio/video is outside the master clock window", a 1 s window).

So control reads each running digital lane's newest HLS segment: the first video and first
audio presentation timestamps in it (``segment_skew``; plain MPEG-TS parsing, a few KB
read, no ffprobe). A stream whose last SAMPLES readings all put the two more than
LIMIT_SECONDS apart is *skewed*: it no longer plays its channel healthily
(server.lane_plays, tuner_pool.plays), so the next tune for that channel starts a fresh
encode, a fresh timestamp domain, rather than handing the skewed one out.

Reading a segment is read-only and never disturbs the stream.
"""
from __future__ import annotations

import threading
from collections import deque
from pathlib import Path

# A healthy encode keeps its tracks well under half a second apart (viviserv2's WTTG: 0.35 s;
# a clean offline run: 0.02 s). VizelVision's master takes at most 1 s.
LIMIT_SECONDS = 1.0
SAMPLES = 3                     # consecutive readings, one per signal sample (5 s)
PACKET = 188
READ_BYTES = 512 * 1024         # a 0.6 s segment is ~200-400 KB; both PTS come early
PTS_WRAP = 1 << 33
VIDEO_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24}        # MPEG-1/2, MPEG-4, H.264, HEVC
AUDIO_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x81, 0x87}  # MPEG audio, AAC, LATM, AC-3, E-AC-3


def _payload(packet):
    """(pid, payload_unit_start, payload) of one TS packet, or None."""
    if len(packet) != PACKET or packet[0] != 0x47:
        return None
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    start = bool(packet[1] & 0x40)
    control = (packet[3] >> 4) & 0x3
    offset = 4
    if control & 0x2:
        offset += 1 + packet[4]
    if not control & 0x1 or offset >= PACKET:
        return pid, start, b""
    return pid, start, packet[offset:]


def _section(payload):
    """A PSI section from a unit-start payload (its pointer field skipped)."""
    pointer = payload[0] if payload else 0
    return payload[1 + pointer:]


def _pts(payload):
    """The PTS (90 kHz) at the head of a PES packet, or None."""
    if len(payload) < 14 or payload[:3] != b"\x00\x00\x01" or not payload[7] & 0x80:
        return None
    p = payload[9:14]
    return (((p[0] >> 1) & 0x07) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1)


def segment_skew(path):
    """Video's first PTS minus audio's, in seconds, in one MPEG-TS segment; or None when the
    segment lacks one of them (or cannot be read). Positive: the sound runs behind."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(READ_BYTES)
    except OSError:
        return None
    pmt_pids, video_pid, audio_pid = set(), None, None
    first = {}
    for at in range(0, len(data) - PACKET + 1, PACKET):
        parsed = _payload(data[at:at + PACKET])
        if parsed is None:
            continue
        pid, start, payload = parsed
        if not start or not payload:
            continue
        if pid == 0:
            section = _section(payload)
            if len(section) >= 8:
                length = ((section[1] & 0x0F) << 8) | section[2]
                for entry in range(8, min(3 + length - 4, len(section)) - 3, 4):
                    program = (section[entry] << 8) | section[entry + 1]
                    if program:
                        pmt_pids.add(((section[entry + 2] & 0x1F) << 8) | section[entry + 3])
        elif pid in pmt_pids and (video_pid is None or audio_pid is None):
            section = _section(payload)
            if len(section) < 12:
                continue
            length = ((section[1] & 0x0F) << 8) | section[2]
            end = min(3 + length - 4, len(section))
            entry = 12 + (((section[10] & 0x0F) << 8) | section[11])
            while entry + 5 <= end:
                kind = section[entry]
                stream_pid = ((section[entry + 1] & 0x1F) << 8) | section[entry + 2]
                if kind in VIDEO_TYPES and video_pid is None:
                    video_pid = stream_pid
                elif kind in AUDIO_TYPES and audio_pid is None:
                    audio_pid = stream_pid
                entry += 5 + (((section[entry + 3] & 0x0F) << 8) | section[entry + 4])
        elif pid in (video_pid, audio_pid) and pid not in first:
            pts = _pts(payload)
            if pts is not None:
                first[pid] = pts
                if len(first) == 2:
                    break
    if video_pid is None or audio_pid is None or len(first) < 2:
        return None
    delta = (first[video_pid] - first[audio_pid]) % PTS_WRAP
    if delta >= PTS_WRAP // 2:
        delta -= PTS_WRAP
    return round(delta / 90000, 3)


def newest_segment(streams_dir, prefix, stream_id):
    """The newest finished segment of one stream generation, or None. (ffmpeg's temp_file
    flag writes a segment as .tmp and renames it whole, so every .ts is finished.)"""
    if not stream_id:
        return None
    segments = sorted(Path(streams_dir).glob(f"{prefix}_{stream_id}_*.ts"))
    return segments[-1] if segments else None


class AvSkew:
    """Each lane's recent skew readings, for the stream generation they were read from."""

    def __init__(self, limit=LIMIT_SECONDS, samples=SAMPLES):
        self.limit, self.samples = limit, samples
        self._lock = threading.Lock()
        self._readings = {}         # lane -> (stream_id, deque of seconds)

    def record(self, lane, stream_id, seconds):
        """One reading; True when it makes the stream newly skewed (worth a log line)."""
        if seconds is None:
            return False
        with self._lock:
            was = self._skewed_locked(lane, stream_id)
            held = self._readings.get(lane)
            if held is None or held[0] != stream_id:
                held = (stream_id, deque(maxlen=self.samples))
                self._readings[lane] = held
            held[1].append(seconds)
            return not was and self._skewed_locked(lane, stream_id)

    def forget(self, lane):
        with self._lock:
            self._readings.pop(lane, None)

    def _skewed_locked(self, lane, stream_id):
        held = self._readings.get(lane)
        return (held is not None and held[0] == stream_id and len(held[1]) == self.samples
                and all(abs(seconds) > self.limit for seconds in held[1]))

    def skewed(self, lane, stream_id):
        """Whether this lane's stream ``stream_id`` has read out of sync SAMPLES times running."""
        with self._lock:
            return self._skewed_locked(lane, stream_id)

    def view(self, lane, stream_id):
        """``(last reading or None, skewed)`` for this lane's stream ``stream_id``."""
        with self._lock:
            held = self._readings.get(lane)
            if held is None or held[0] != stream_id or not held[1]:
                return None, False
            return held[1][-1], self._skewed_locked(lane, stream_id)
