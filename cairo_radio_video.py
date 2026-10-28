#!/usr/bin/env python3
"""Cairo Radio's title card as video: one 1280x720 frame a second for its encoder.

web_basic.py runs it under build_cairo_radio_command, which lays the station's own
audio under these frames. The card is the Sports Guide's dusk (sports_guide_video.py)
with the station's name, the show on now and what is playing, as VizelVision's
off-air card words them (~/vizelVision/roku_stream/radio_info.py, describe), and the
time. It reads hazel's station -- its master control's /api/state, the show from
/api/log or the weekly /api/grid -- every five seconds. When the station has not
answered for 30 s, the card says only that it is on the air.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime

import sports_guide_video as guide   # sets numpy's thread counts before importing it

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

FPS = 1
LINE_LIMIT = 64
INTERVAL = 5
STALE_SECONDS = 30
GRID_SECONDS = 600
TEXT_WIDTH = 1080


def _fit(text):
    text = " ".join(str(text).split()).upper()
    return text if len(text) <= LINE_LIMIT else text[:LINE_LIMIT - 1].rstrip() + "…"


def describe(state, show):
    """``{"show", "playing"}`` for a station /api/state snapshot and the show on now, as
    VizelVision's off-air card words them; either may be None."""
    # Some clocks already end in SHOW ("MORNING SHOW"); don't say it twice.
    shown = _fit(show if show.upper().rstrip().endswith("SHOW") else show + " SHOW") if show else None
    now = state.get("now") or {}
    title, artist = (now.get("title") or "").strip(), (now.get("artist") or "").strip()
    kind = now.get("kind") or ""
    playing = None
    # Buffering placeholders ("SPOTIFY BUFFERING") are not program content.
    if title and "buffering" not in kind:
        if artist and kind in ("music", "spotify"):
            # Long credit lists would hide the title: keep the first credited artist.
            if len(f"{artist} — {title}") > LINE_LIMIT:
                artist = artist.split(",")[0].strip()
            title = f"{artist} — {title}"
        playing = _fit(title)
    return {"show": shown, "playing": playing}


class Station:
    """What the station plays, read on a thread so a slow station never holds up a frame."""

    def __init__(self, base_url, *, opener=urllib.request.urlopen, clock=time.monotonic,
                 local_now=datetime.now):
        self.base_url = base_url.rstrip("/")
        self.opener, self.clock, self.local_now = opener, clock, local_now
        self.lock = threading.Lock()
        self._info, self._at = {"show": None, "playing": None}, None
        self.shows = {}
        self.grid, self.grid_at = None, 0.0

    def _get(self, path):
        with self.opener(self.base_url + path, timeout=3) as response:
            return json.loads(response.read(4 * 1024 * 1024))

    def show(self, state):
        # The log records the clock each item was scheduled under, which follows
        # overruns; the weekly grid covers items outside the log.
        log_id = (state.get("now") or {}).get("log_id")
        if log_id is not None:
            if log_id not in self.shows:
                self.shows = {entry.get("id"): entry.get("source_slot")
                              for entry in self._get("/api/log").get("entries", [])}
                self.shows.setdefault(log_id, None)
            if self.shows[log_id]:
                return self.shows[log_id]
        if self.grid is None or self.clock() - self.grid_at > GRID_SECONDS:
            self.grid, self.grid_at = self._get("/api/grid"), self.clock()
        now = self.local_now()
        return next((cell.get("name") for cell in self.grid
                     if cell.get("dow") == now.weekday() and cell.get("hour") == now.hour), None)

    def refresh(self):
        state = self._get("/api/state")
        info = describe(state, self.show(state))
        with self.lock:
            self._info, self._at = info, self.clock()

    def info(self):
        with self.lock:
            fresh = self._at is not None and self.clock() - self._at < STALE_SECONDS
            return dict(self._info) if fresh else {"show": None, "playing": None}

    def run(self, stopping):
        while not stopping.is_set():
            try:
                self.refresh()
            except Exception as error:  # noqa: BLE001 - the card goes on without it
                print(f"[cairo-radio] station not answering: {error}", file=sys.stderr, flush=True)
            stopping.wait(INTERVAL)


def wrap(text, face, limit=TEXT_WIDTH, lines=2):
    """``text`` in at most ``lines`` lines of ``limit`` pixels, the last cut with an ellipsis."""
    words, out = text.split(), []
    while words and len(out) < lines:
        line = words.pop(0)
        while words and face.getlength(f"{line} {words[0]}") <= limit:
            line = f"{line} {words.pop(0)}"
        out.append(line)
    if words:
        last = out[-1]
        while last and face.getlength(last + "…") > limit:
            last = last[:-1].rstrip()
        out[-1] = last + "…"
    return out


def _centered(draw, y, text, face, color):
    # The guide's drop shadow, then the text, centred on the card.
    draw.text((guide.W / 2 + 3, y + 3), text, font=face, fill=guide.DROP, anchor="ms")
    draw.text((guide.W / 2, y), text, font=face, fill=color, anchor="ms")


_backdrop = None


def card(info, moment):
    """One frame: the station's name, the show, what is playing, and the time."""
    global _backdrop
    if _backdrop is None:
        _backdrop = guide.backdrop()
    image = Image.fromarray(_backdrop)
    draw = ImageDraw.Draw(image)
    _centered(draw, 200, "Cairo Radio", guide.font(104, bold=True), guide.CREAM)
    draw.line((guide.W / 2 - 300, 236, guide.W / 2 + 300, 236), fill=guide.GOLD, width=3)
    if info.get("show"):
        _centered(draw, 320, info["show"], guide.font(44, bold=True), guide.GOLD)
    playing = info.get("playing")
    if playing:
        face = guide.font(40)
        for index, line in enumerate(wrap(playing, face)):
            _centered(draw, 410 + index * 54, line, face, guide.CREAM)
    elif not info.get("show"):
        _centered(draw, 380, "ON THE AIR", guide.font(44, bold=True), guide.GOLD)
    clock = moment.strftime("%I:%M %p").lstrip("0")
    _centered(draw, 640, clock, guide.font(34), guide.SKY)
    return np.asarray(image, dtype=np.uint8)


def frames(station, clock=time.time):
    """Frames forever, drawn again only when the card would change."""
    shown, frame = None, None
    while True:
        moment = datetime.fromtimestamp(clock())
        key = (tuple(sorted(station.info().items(), key=lambda kv: kv[0])), moment.strftime("%H:%M"))
        if key != shown:
            shown, frame = key, card(dict(key[0]), moment)
        yield frame


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="the station's master control (its /api/...)")
    parser.add_argument("--fps", type=int, default=FPS)
    parser.add_argument("--lead", type=float, default=2.0,
                        help="seconds the frames may run ahead of the wall clock")
    parser.add_argument("--seconds", type=float, help="stop after this much video (tests)")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    parent = os.getppid()
    stopping = threading.Event()
    station = Station(args.url)
    threading.Thread(target=station.run, args=(stopping,), daemon=True).start()
    out = sys.stdout.buffer
    started = time.monotonic()
    try:
        for n, frame in enumerate(frames(station)):
            if args.seconds is not None and n >= args.seconds * args.fps:
                break
            ahead = started + n / args.fps - args.lead - time.monotonic()
            if ahead > 0:
                time.sleep(ahead)
            # An orphan would write frames to nobody; its encoder ends with it.
            if os.getppid() != parent:
                return 0
            out.write(memoryview(np.ascontiguousarray(frame)).cast("B"))
            out.flush()
    except BrokenPipeError:
        pass
    finally:
        stopping.set()
        try:
            out.flush()
        except BrokenPipeError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
