"""How well each of this node's tuners plays the station it is on: the pool's signal grade.

Two antennas can both lock a station and still play it very differently: on 2026-11-01 WTTG
(5.1, RF 36) was choppy on viviserv and clean on viviserv2. SNR does not tell them apart (the
two drivers scale it differently: viviserv read 26 dB and stuttered, viviserv2 22 dB and
played cleanly). Lock and uncorrected blocks do: viviserv's frontend reported 50-416
uncorrected blocks on 14 of 15 reads and lost sync on 3, viviserv2's none.

So a sample of a playing tuner's frontend is *errored* when it is not locked or reports
uncorrected blocks, and the share of errored samples over the last WINDOW gives the verdict:

* ``good``: at most GOOD_SHARE errored;
* ``poor``: at least POOR_SHARE errored;
* ``fair``: between.

Fewer than MIN_SAMPLES samples give no verdict yet. Each RF channel's last verdict is kept in
``state/signal_health.json`` (one antenna per node, so per node and RF channel) and is
forgotten after MAX_AGE_SECONDS: reception changes with the weather and the leaves.

Reading the frontend is read-only and never disturbs the stream (feed: devices/<id>/signal).
Control is the only writer of the file.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

WINDOW = 24                     # samples: two minutes at SAMPLE_SECONDS
MIN_SAMPLES = 6
SAMPLE_SECONDS = 5.0
GOOD_SHARE = 0.10
POOR_SHARE = 0.30
MAX_AGE_SECONDS = 14 * 86400
# A driver without the counter answers all ones (viviserv2's lgdt3306a, for BER).
NOT_SUPPORTED = 0xFFFFFFFF
VERDICTS = ("good", "fair", "poor")


def errored(sample):
    """Whether one frontend sample (atsc_scan.read_signal's shape) shows trouble."""
    if not sample or not sample.get("locked"):
        return True
    blocks = sample.get("uncorrected_blocks")
    return blocks is not None and blocks != NOT_SUPPORTED and blocks > 0


def verdict(share):
    if share <= GOOD_SHARE:
        return "good"
    return "poor" if share >= POOR_SHARE else "fair"


class SignalHealth:
    """Each lane's recent samples, and each RF channel's last verdict, thread-safe."""

    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._samples = {}          # lane -> (rf, deque of bools)
        self._stored = self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            return {int(rf): dict(entry) for rf, entry in (data.get("rf_channels") or {}).items()
                    if entry.get("verdict") in VERDICTS}
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"rf_channels": {str(rf): self._stored[rf] for rf in sorted(self._stored)}},
                                   indent=2) + "\n")
        os.replace(temp, self.path)

    def record(self, lane, rf, sample):
        """One sample of ``lane``'s frontend while it plays RF ``rf``. A new RF channel starts
        the lane's window over. Every full window's verdict is stored for ``rf``."""
        if rf is None:
            return
        with self._lock:
            held = self._samples.get(lane)
            if held is None or held[0] != rf:
                held = (rf, deque(maxlen=WINDOW))
                self._samples[lane] = held
            window = held[1]
            window.append(errored(sample))
            if len(window) >= MIN_SAMPLES and (len(window) == WINDOW or len(window) % MIN_SAMPLES == 0):
                share = sum(window) / len(window)
                self._stored[rf] = {"verdict": verdict(share), "errored": round(share, 3),
                                    "samples": len(window), "measured_at": round(self.clock())}
                try:
                    self._save()
                except OSError as error:
                    print(f"Signal health: could not save {self.path}: {error}")

    def forget(self, lane):
        """``lane`` stopped or changed channel: its window starts over."""
        with self._lock:
            self._samples.pop(lane, None)

    def live(self, lane, rf):
        """``{rf, verdict, errored, samples}`` for what ``lane`` plays now; verdict None while
        there are fewer than MIN_SAMPLES samples of ``rf``."""
        with self._lock:
            held = self._samples.get(lane)
            window = list(held[1]) if held and held[0] == rf else []
        if rf is None:
            return None
        share = sum(window) / len(window) if window else None
        return {"rf": rf, "verdict": verdict(share) if len(window) >= MIN_SAMPLES else None,
                "errored": None if share is None else round(share, 3), "samples": len(window)}

    def stored(self):
        """``{"<rf>": verdict}``: each RF channel's last verdict here, the stale ones left out."""
        now = self.clock()
        with self._lock:
            return {str(rf): entry["verdict"] for rf, entry in sorted(self._stored.items())
                    if now - entry.get("measured_at", 0) <= MAX_AGE_SECONDS}
