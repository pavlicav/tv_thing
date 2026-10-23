#!/usr/bin/env python3
"""VizelVision's Sports Guide as video: channel 60's slides, frame by frame.

Writes raw 1280x720 RGB24 frames at FPS to stdout, for server.py's HLS
encoder (build_sports_guide_command), which makes the stream a Roku plays at
/stream/sports-guide/index.m3u8. It draws what static/sports-guide.html draws
in a browser -- "Today's Sports...", then each sport's games by start time,
in the manner of a mid-90s cable character generator -- from
GET /api/v1/sports-guide, read again at the start of every round.

Frames are paced to the wall clock, running ``--lead`` seconds ahead so the
first segments are written at once. A page that has finished building repeats
one cached frame. It exits when its reader goes away, or when its parent does
(checked every second: PR_SET_PDEATHSIG would fire when the *thread* that
started it ends, and the server starts it from a request's thread).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# One thread for numpy's libraries, which would otherwise size pools to the
# host's 28 threads under the unit's TasksMax. Set before numpy is imported.
for _name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

W, H = 1280, 720
FPS = 30
FONT_DIR = Path(__file__).parent / "static" / "fonts"
ROWS_PER_PAGE = 7
TITLE_SECONDS = 9.0
REFRESH_SECONDS = 60.0
TRANSITIONS = ("wipe", "iris", "blinds", "dissolve", "barn")
# Seconds each transition takes, and its easing (cubic-bezier control points).
TRANSITION_TIMING = {"wipe": (0.9, (.5, 0, .3, 1)), "iris": (1.0, (.4, 0, .3, 1)),
                     "blinds": (1.0, None), "dissolve": (0.9, None), "barn": (0.9, (.5, 0, .3, 1))}
EASE_OUT = (0, 0, .58, 1)
BLIND = 48

CREAM = (255, 243, 217)
GOLD = (244, 214, 143)
MINT = (195, 234, 217)
ROSE = (245, 193, 207)
CORAL = (255, 163, 146)
SKY = (180, 211, 244)
DROP = (13, 15, 42)
LETTERS = ((247, 169, 201), (247, 226, 148), (152, 223, 210), (249, 192, 154), (165, 210, 243))


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

_fonts = {}


def font(size, bold=False, italic=False):
    key = (size, bold, italic)
    if key not in _fonts:
        style = ("bold" if bold else "") + ("italic" if italic else "") or "regular"
        _fonts[key] = ImageFont.truetype(str(FONT_DIR / f"guide-serif-{style}.otf"), size)
    return _fonts[key]


def bezier(points):
    """CSS cubic-bezier(x1, y1, x2, y2) as a function of progress 0..1."""
    if points is None:
        return lambda p: p
    x1, y1, x2, y2 = points

    def curve(a, b, s):
        return 3 * a * s * (1 - s) ** 2 + 3 * b * s * s * (1 - s) + s ** 3

    def ease(p):
        if p <= 0 or p >= 1:
            return min(max(p, 0.0), 1.0)
        lo, hi = 0.0, 1.0
        for _ in range(24):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if curve(x1, x2, mid) < p else (lo, mid)
        return curve(y1, y2, (lo + hi) / 2)
    return ease


def scanned(rgb, top=0):
    """Every third screen line darkened, as the page's scan-line overlay does.
    The overlay is affine, so it can be applied to each layer (at the screen
    line ``top`` it is placed on) rather than to every frame."""
    rgb = rgb.astype(np.float32)
    first = (2 - top) % 3
    rgb[first::3] = rgb[first::3] * 0.93 + np.array([0, 0, 20 * 0.07], np.float32)
    return rgb


def backdrop():
    """The dusk: a vertical gradient, eight-bit banding, two soft glows."""
    y = np.linspace(0, 1, H, dtype=np.float32)[:, None]
    stops = [(0, (35, 46, 92)), (.28, (51, 66, 122)), (.58, (83, 91, 149)),
             (.84, (125, 113, 163)), (1, (155, 127, 159))]
    rgb = np.zeros((H, 1, 3), np.float32)
    for (y0, c0), (y1, c1) in zip(stops, stops[1:]):
        inside = (y >= y0) & (y <= y1)
        mix = np.clip((y - y0) / (y1 - y0), 0, 1)
        rgb = np.where(inside[..., None], np.array(c0) + (np.array(c1) - np.array(c0)) * mix[..., None], rgb)
    rgb = np.repeat(rgb, W, axis=1)
    band = (np.arange(H) % 28) < 14
    rgb[band] = rgb[band] * (1 - .028) + 255 * .028
    rgb[~band] = rgb[~band] * (1 - .028)
    xs, ys = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
    for cx, cy, rx, ry, color, alpha, reach in (
            (.92 * W, 1.0 * H, .60 * W, .40 * H, (255, 196, 206), .22, .75),
            (.16 * W, .04 * H, .70 * W, .55 * H, (196, 214, 255), .26, .70)):
        d = np.sqrt(((xs - cx) / rx) ** 2 + ((ys - cy) / ry) ** 2)
        a = (alpha * np.clip(1 - d / reach, 0, 1))[..., None]
        rgb = rgb * (1 - a) + np.array(color, np.float32) * a
    return np.clip(scanned(rgb), 0, 255).astype(np.uint8)


class Layer:
    """A transparent RGBA sheet to draw one element on, then place."""

    def __init__(self, width, height):
        self.image = Image.new("RGBA", (max(1, int(width)), max(1, int(height))), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)

    def text(self, x, y, text, face, color, alpha=1.0, shadow=3, spacing=0.0):
        """Draw at a baseline, with the character generator's hard drop shadow;
        returns the advance. ``spacing`` is letter spacing in em."""
        fill = (*color, round(255 * alpha))
        for dx, dy, ink in (((shadow, shadow, (*DROP, round(204 * alpha))),) if shadow else ()) + \
                ((0, 0, fill),):
            cursor = x
            if spacing:
                for char in text:
                    self.draw.text((cursor + dx, y + dy), char, font=face, fill=ink, anchor="ls")
                    cursor += face.getlength(char) + spacing * face.size
            else:
                self.draw.text((x + dx, y + dy), text, font=face, fill=ink, anchor="ls")
        return width(text, face, spacing)

    def cropped(self, x, y):
        """(rgb * alpha, alpha, x, y): the drawn part only, as floats, scanned
        for the screen lines it lands on."""
        box = self.image.getbbox()
        if box is None:
            return np.zeros((1, 1, 3), np.float32), np.zeros((1, 1, 1), np.float32), x, y
        x, y = int(x) + box[0], int(y) + box[1]
        rgba = np.asarray(self.image.crop(box), np.float32)
        alpha = rgba[..., 3:4] / 255
        return scanned(rgba[..., :3], y) * alpha, alpha, x, y

    def place(self, canvas, x, y):
        rgb, alpha, x, y = self.cropped(x, y)
        blend(canvas, rgb, alpha, x, y)


def width(text, face, spacing=0.0):
    if spacing:
        return sum(face.getlength(char) for char in text) + spacing * face.size * len(text)
    return face.getlength(text)


def small_caps(text, size):
    """Pieces of (text, face) for small caps without OpenType: capitals at 74%
    of the size for letters, figures at full size."""
    pieces = []
    for char in text:
        face = font(round(size * .74), bold=False) if char.isalpha() else font(size)
        shown = char.upper()
        if pieces and pieces[-1][1] is face:
            pieces[-1] = (pieces[-1][0] + shown, face)
        else:
            pieces.append((shown, face))
    return pieces


def caps_width(text, size):
    return sum(width(piece, face, .12 * size / face.size) for piece, face in small_caps(text, size))


def draw_caps(layer, x, y, text, size, color, alpha=1.0, shadow=3):
    for piece, face in small_caps(text, size):
        x += layer.text(x, y, piece, face, color, alpha, shadow, spacing=.12 * size / face.size)
    return x


def blend(frame, rgb, alpha, x, y, opacity=1.0):
    """Lay a premultiplied layer on a frame (float or uint8) at (x, y), clipped
    to it. Only the layer's own rectangle is touched."""
    h, w = alpha.shape[:2]
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
    if x0 >= x1 or y0 >= y1:
        return
    a = alpha[y0 - y:y1 - y, x0 - x:x1 - x] * opacity
    mixed = frame[y0:y1, x0:x1] * (1 - a) + rgb[y0 - y:y1 - y, x0 - x:x1 - x] * opacity
    if frame.dtype == np.uint8:
        mixed = np.clip(mixed + .5, 0, 255)
    frame[y0:y1, x0:x1] = mixed


# ---------------------------------------------------------------------------
# Words, as the page has them
# ---------------------------------------------------------------------------

def clock_parts(moment):
    return f"{moment.hour % 12 or 12}:{moment.minute:02d}", "am" if moment.hour < 12 else "pm"


def start_text(game):
    if game.get("time_tbd") or not game.get("start"):
        return "TBA"
    start = datetime.fromtimestamp(game["start"])
    if start.minute == 0 and start.hour % 12 == 0:
        return "Noon" if start.hour else "Midnight"
    return " ".join(clock_parts(start))


def today_text(payload):
    try:
        day = datetime.strptime(payload["date"], "%Y-%m-%d")
    except (TypeError, KeyError, ValueError):
        day = datetime.now()
    return f"{day:%A}, {day:%B} {day.day}"


def deck(payload):
    """The round: the title page, then each sport on a page or a few."""
    pages = [{"kind": "title"}]
    for sport in (payload or {}).get("sports") or ():
        games = sport.get("games") or []
        if not games:
            continue
        count = math.ceil(len(games) / ROWS_PER_PAGE)
        per = math.ceil(len(games) / count)
        tags = len({game.get("tag") for game in games}) > 1
        for page in range(count):
            pages.append({"kind": "sport", "sport": sport.get("sport", ""), "tags": tags,
                          "page": page, "count": count, "games": games[page * per:(page + 1) * per]})
    return pages


def dwell(page):
    return TITLE_SECONDS if page["kind"] == "title" else min(15.0, 5.2 + len(page["games"]) * 1.15)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

_ease_out = bezier(EASE_OUT)


class Item:
    """One element of a page: placed at (x, y), building in from ``delay``
    over ``duration`` (fade, sliding ``dx`` pixels), flashing if asked."""

    def __init__(self, layer, x, y, delay=None, duration=.55, dx=-26, flash=False):
        self.rgb, self.alpha, self.x, self.y = layer.cropped(x, y)
        self.delay, self.duration, self.dx, self.flash = delay, duration, dx, flash

    def state(self, t):
        """(opacity, x offset) at page time t."""
        opacity, offset = 1.0, 0
        if self.delay is not None:
            if t < self.delay:
                return 0.0, 0
            p = _ease_out(min(1.0, (t - self.delay) / self.duration))
            opacity, offset = p, round(self.dx * (1 - p))
        if self.flash and (t % 1.6) >= 1.12:
            opacity *= .25
        return opacity, offset

    def settled(self, t):
        return self.delay is None or t >= self.delay + self.duration


class Page:
    def __init__(self, slide, payload, channel, background):
        self.slide, self.payload, self.channel = slide, payload, channel
        self.items = []
        canvas = background.astype(np.float32)
        self.title(canvas) if slide["kind"] == "title" else self.sport(canvas)
        # Everything but the clock and the items, kept as bytes: a page is a
        # few MB, not tens.
        self.plain = np.clip(canvas + .5, 0, 255).astype(np.uint8)
        del canvas
        self.minute = None
        self.static = {}
        self.base = None
        self.settle = max((item.delay + item.duration for item in self.items if item.delay is not None),
                          default=0.0)

    # -- the parts every page has ---------------------------------------------
    def footer(self, canvas):
        band = Layer(W, 58)
        alpha = np.linspace(0, .55 * 255, 58, dtype=np.float32)[:, None].repeat(W, 1)
        band.image = Image.fromarray(np.dstack([np.full((58, W), c, np.float32) for c in (16, 18, 50)]
                                               + [alpha]).astype(np.uint8), "RGBA")
        band.place(canvas, 0, H - 58)
        line = Layer(W, 1)
        line.draw.rectangle((0, 0, W - 1, 0), fill=(*GOLD, 71))
        line.place(canvas, 0, H - 58)
        layer = Layer(W, 58)
        face = font(21)
        layer.text(64, 36, f"Channel {self.channel}", face, CREAM, .82, shadow=0)
        fine = font(21, italic=True)
        note = "Listings subject to change"
        layer.text(640 - width(note, fine, .02) / 2, 36, note, fine, CREAM, .82, shadow=0, spacing=.02)
        layer.place(canvas, 0, H - 58)

    def masthead(self, canvas):
        layer = Layer(W, 120)
        self.wordmark(layer, 64, 62, 34, 21)
        date = today_text(self.payload)
        face = font(27, italic=True)
        layer.text(1216 - width(date, face), 62, date, face, SKY)
        layer.place(canvas, 0, 0)
        rule(canvas, 64, 1216, 104)

    @staticmethod
    def wordmark(layer, x, y, size, small, center=False):
        face = font(size, bold=True, italic=True)
        tail = font(small, bold=True)
        total = width("VizelVision", face) + 14 + width("SPORTS GUIDE", tail, .32)
        if center:
            x -= total / 2
        for letter, color in zip("Vizel", LETTERS):
            x += layer.text(x, y, letter, face, color)
        x += layer.text(x, y, "Vision", face, CREAM)
        layer.text(x + 14, y, "SPORTS GUIDE", tail, GOLD, spacing=.32)

    # -- the title page ---------------------------------------------------------
    def title(self, canvas):
        sports = (self.payload or {}).get("sports") or []
        layer = Layer(W, 90)
        self.wordmark(layer, 640, 70, 46, 25, center=True)
        self.items.append(Item(layer, 0, 127, delay=.2))
        head = font(128, bold=True, italic=True)
        words = "Today’s Sports"
        left = 640 - width(words + "...", head) / 2
        layer = Layer(W, 200)
        layer.text(left, 160, words, head, CREAM, shadow=5)
        layer.place(canvas, 0, 170)
        x = left + width(words, head)
        for index, at in enumerate((1.3, 1.75, 2.2)):
            dot = Layer(60, 200)
            dot.text(0, 160, ".", head, CREAM, shadow=5)
            item = Item(dot, x, 170, delay=at, duration=1 / FPS, dx=0)
            self.items.append(item)
            x += width(".", head)
        date = today_text(self.payload)
        face = font(42, italic=True)
        layer = Layer(W, 70)
        layer.text(640 - width(date, face) / 2, 50, date, face, SKY)
        self.items.append(Item(layer, 0, 355, delay=.6))
        line = Layer(560, 8)
        line.draw.rectangle((0, 0, 559, 1), fill=(*GOLD, 179))
        line.draw.rectangle((0, 7, 559, 7), fill=(*GOLD, 179))
        self.items.append(Item(line, 360, 444, delay=2.4))
        layer, lines = Layer(W, 104), 1
        if not self.payload:
            message = "Gathering today’s listings…" if self.payload is None else \
                "The sports desk is not answering. Listings will return shortly."
            face = font(25, italic=True)
            layer.text(640 - width(message, face) / 2, 40, message, face, ROSE, shadow=0)
        elif not sports:
            message = "No more live games on the dial today."
            face = font(30)
            layer.text(640 - width(message, face) / 2, 40, message, face, MINT, shadow=0)
        else:
            lines = self.summary(layer, sports)
        self.items.append(Item(layer, 0, 462, delay=2.6))
        if self.payload and self.payload.get("errors"):
            layer = Layer(W, 50)
            note = "Some listings are not in yet."
            face = font(25, italic=True)
            layer.text(640 - width(note, face) / 2, 34, note, face, ROSE, shadow=0)
            self.items.append(Item(layer, 0, 516 + 44 * (lines - 1), delay=3.0))
        self.footer(canvas)

    @staticmethod
    def summary(layer, sports):
        """Each sport and its count, diamonds between; on two lines when one would run
        wider than the page's 1100 px (as the page's own text wraps). Returns the lines."""
        name, number = font(30), font(30, bold=True)

        def parts_of(group):
            parts = []
            for index, sport in enumerate(group):
                if index:
                    parts.append(("sep", None, 14 + 22 + 14))
                label = f"{sport.get('sport', '')} "
                count = str(len(sport.get("games") or []))
                parts.append((label, name, width(label, name)))
                parts.append((count, number, width(count, number)))
            return parts

        rows = [parts_of(sports)]
        if sum(part[2] for part in rows[0]) > 1100 and len(sports) > 1:
            half = math.ceil(len(sports) / 2)
            rows = [parts_of(sports[:half]), parts_of(sports[half:])]
        for line, parts in enumerate(rows):
            x, base = 640 - sum(part[2] for part in parts) / 2, 40 + 44 * line
            for text, face, advance in parts:
                if text == "sep":
                    cx, cy = x + 14 + 11, base - 10
                    layer.draw.polygon([(cx, cy - 10), (cx + 10, cy), (cx, cy + 10), (cx - 10, cy)],
                                       fill=(*ROSE, 255))
                else:
                    layer.text(x, base, text, face, GOLD if face is number else MINT, shadow=0)
                x += advance
        return len(rows)

    # -- a sport's page -------------------------------------------------------
    def sport(self, canvas):
        slide = self.slide
        self.masthead(canvas)
        layer = Layer(W, 100)
        name = font(70, bold=True, italic=True)
        advance = layer.text(64, 76, slide["sport"], name, CREAM)
        if slide["page"] > 0:
            layer.text(64 + advance + 18, 76, "continued", font(30, italic=True), ROSE, shadow=0)
        self.items.append(Item(layer, 0, 100, delay=.35))
        if slide["count"] > 1:
            words = f"page {slide['page'] + 1} of {slide['count']}"
            caps = Layer(W, 100)
            draw_caps(caps, 1216 - caps_width(words, 24), 76, words, 24, SKY)
            caps.place(canvas, 0, 100)
        games = slide["games"]
        panel(canvas, 52, 212, 1228, 212 + 28 + 56 * len(games), len(games))
        for position, game in enumerate(games):
            self.row(position, game, games, slide["tags"])
        self.footer(canvas)

    def row(self, position, game, games, tags):
        top = 212 + 14 + 56 * position
        layer, now = Layer(W, 56), None
        baseline = 45
        text = start_text(game)
        same = position > 0 and not game.get("live") and not games[position - 1].get("live") \
            and start_text(games[position - 1]) == text
        if game.get("live"):
            now = Layer(W, 56)
            face = font(34, bold=True, italic=True)
            now.text(232 - width("Now", face), baseline, "Now", face, CORAL)
        elif not same:
            if text == "TBA":
                face = font(34, italic=True)
                layer.text(232 - width(text, face), baseline, text, face, GOLD, .9)
            elif text[0].isalpha():
                face = font(34, bold=True, italic=True)
                layer.text(232 - width(text, face), baseline, text, face, GOLD, .9)
            else:
                clock, half = text.split(" ")
                big, small = font(34, bold=True), font(24)
                x = 232 - width(clock, big) - 5 - width(half, small)
                x += layer.text(x, baseline, clock, big, GOLD)
                layer.text(x + 5, baseline, half, small, GOLD, .85, shadow=0)
        x = 262
        if tags:
            draw_caps(layer, 258, baseline, str(game.get("tag") or ""), 25, SKY, shadow=0)
            x = 388
        number_face = font(38, bold=True)
        number = str(game.get("tv_number", ""))
        layer.text(1198 - width(number, number_face), baseline, number, number_face, GOLD)
        service = game.get("via") not in (None, "channel")
        network_face = font(34, italic=True) if service else font(34, bold=True)
        network = str(game.get("network") or "")
        network_x = 1198 - 74 - width(network, network_face)
        layer.text(network_x, baseline, network, network_face, ROSE if service else MINT)
        regular, italic = font(34), font(34, italic=True)
        title = str(game.get("title") or "")
        away, _, home = title.rpartition(" at ")
        pieces = [(away, regular, CREAM, 1.0), (" at ", italic, CREAM, .78), (home, regular, CREAM, 1.0)] \
            if away else [(title, regular, CREAM, 1.0)]
        room = network_x - 16 - 30 - 16 - x
        if sum(width(text, face) for text, face, _, _ in pieces) > room:
            flat = "".join(text for text, _, _, _ in pieces)
            while flat and width(flat + "…", regular) > room:
                flat = flat[:-1]
            pieces = [(flat.rstrip() + "…", regular, CREAM, 1.0)]
        for text, face, color, alpha in pieces:
            x += layer.text(x, baseline, text, face, color, alpha)
        dot = x + 16 + 6
        while dot <= network_x - 16 - 2:
            layer.draw.ellipse((dot - 1.7, baseline - 4.7, dot + 1.7, baseline - 1.3), fill=(*CREAM, 140))
            dot += 13
        delay = .9 + position * .28
        self.items.append(Item(layer, 0, top, delay=delay, duration=.45))
        if now is not None:
            self.items.append(Item(now, 0, top, delay=delay, duration=.45, flash=True))

    # -- frames ---------------------------------------------------------------
    def with_clock(self):
        moment = datetime.now()
        if moment.minute != self.minute or self.base is None:
            self.minute = moment.minute
            self.static = {}
            canvas = self.plain.copy()
            clock, half = clock_parts(moment)
            big, small = font(21, bold=True), font(17, bold=True)
            layer = Layer(400, 58)
            x = 400 - 64 - width(clock, big) - width(half, small)
            x += layer.text(x, 36, clock, big, GOLD, shadow=0)
            layer.text(x, 36, half, small, GOLD, shadow=0)
            layer.place(canvas, W - 400, H - 58)
            self.base = canvas
        return self.base

    def frame(self, t):
        """The page at page time t, as uint8 RGB."""
        base = self.with_clock()
        if t >= self.settle:
            dim = any(item.flash for item in self.items) and (t % 1.6) >= 1.12
            if dim not in self.static:
                canvas = base.copy()
                for item in self.items:
                    opacity, offset = item.state(t)
                    blend(canvas, item.rgb, item.alpha, item.x + offset, item.y, opacity)
                self.static[dim] = canvas
            return self.static[dim]
        canvas = base.copy()
        for item in self.items:
            opacity, offset = item.state(t)
            if opacity > 0:
                blend(canvas, item.rgb, item.alpha, item.x + offset, item.y, opacity)
        return canvas


def rule(canvas, x0, x1, y):
    """The masthead's double rule: 2 px over 1 px, gold, with a drop below."""
    layer = Layer(x1 - x0, 11)
    layer.draw.rectangle((0, 0, x1 - x0 - 1, 1), fill=(*GOLD, 179))
    layer.draw.rectangle((0, 8, x1 - x0 - 1, 8), fill=(*GOLD, 179))
    layer.draw.rectangle((0, 9, x1 - x0 - 1, 10), fill=(*DROP, 128))
    layer.place(canvas, x0, y)


def panel(canvas, x0, y0, x1, y1, rows):
    """The bevelled box the listings sit in: a deep translucent fill, a gold
    edge, a lit top, a shaded foot, a drop below, and a line between rows."""
    w, h = x1 - x0, y1 - y0

    def shape(box):
        mask = Image.new("L", (w, h + 5), 0)
        ImageDraw.Draw(mask).rounded_rectangle(box, 6, fill=255)
        return np.asarray(mask, np.float32) / 255

    inside = shape((0, 0, w - 1, h - 1))
    drop = shape((0, 5, w - 1, h + 4)) * (1 - inside) * .35
    ramp = np.clip(np.arange(h + 5, dtype=np.float32) / max(1, h - 1), 0, 1)[:, None]
    fill = inside * (.5 - .18 * ramp)
    alpha = drop + fill
    rgb = (np.array((10, 12, 36), np.float32) * drop[..., None]
           + np.array((18, 24, 66), np.float32) * fill[..., None]) / np.maximum(alpha, 1e-6)[..., None]
    sheet = Layer(w, h + 5)
    sheet.image = Image.fromarray(np.dstack([rgb, alpha[..., None] * 255]).clip(0, 255).astype(np.uint8), "RGBA")
    lines = Layer(w, h + 5)
    lines.draw.rounded_rectangle((0, 0, w - 1, h - 1), 6, outline=(*GOLD, 140), width=2)
    lines.draw.rectangle((3, 2, w - 4, 3), fill=(255, 255, 255, 33))
    lines.draw.rectangle((3, h - 4, w - 4, h - 3), fill=(0, 0, 0, 46))
    for index in range(1, rows):
        lines.draw.line((30, 14 + 56 * index, w - 31, 14 + 56 * index), fill=(*CREAM, 23))
    sheet.image = Image.alpha_composite(sheet.image, lines.image)
    sheet.place(canvas, x0, y0)


# ---------------------------------------------------------------------------
# Transitions and the loop
# ---------------------------------------------------------------------------

_iris = None


def transition(kind, p, old, new):
    """The frame part-way (p, eased) from the old page to the new."""
    global _iris
    if kind == "dissolve":
        k = int(p * 256)
        return ((old.astype(np.uint16) * (256 - k) + new.astype(np.uint16) * k) >> 8).astype(np.uint8)
    out = old.copy()
    if kind == "wipe":
        edge = int(round(p * W))
        out[:, :edge] = new[:, :edge]
    elif kind == "barn":
        half = int(round(p * W / 2))
        out[:, W // 2 - half:W // 2 + half] = new[:, W // 2 - half:W // 2 + half]
    elif kind == "blinds":
        rows = (np.arange(H) % BLIND) < p * BLIND
        out[rows] = new[rows]
    elif kind == "iris":
        if _iris is None:
            ys, xs = np.ogrid[:H, :W]
            _iris = np.sqrt((xs - W / 2) ** 2 + (ys - H / 2) ** 2).astype(np.float32)
        inside = _iris < p * .75 * math.hypot(W, H) / math.sqrt(2)
        out[inside] = new[inside]
    return out


class Listings:
    """The latest /api/v1/sports-guide, read in the background so a slow
    schedule never stalls a frame. None until the first read; False after a
    failed first read."""

    def __init__(self, url, refresh=REFRESH_SECONDS):
        self.url, self.refresh = url, refresh
        self.payload, self.read_at = None, 0.0
        self._wake = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                with urllib.request.urlopen(self.url, timeout=30) as response:
                    self.payload = json.loads(response.read())
            except (OSError, ValueError) as error:
                print(f"[Sports Guide video] listings unavailable: {error}", file=sys.stderr, flush=True)
                if self.payload is None:
                    self.payload = False
            self.read_at = time.monotonic()
            self._wake.wait(self.refresh)
            self._wake.clear()

    def latest(self):
        return self.payload


def channel_of(payload, default=60):
    view = (payload or {}).get("channel") or {}
    return view.get("tv_number") if isinstance(view.get("tv_number"), int) else default


def tune_in(slides, now, fps=FPS):
    """Where a channel that never stopped would be at ``now`` (epoch seconds), with
    its rounds laid end to end since the epoch: ``(page index, frames into it)``."""
    lengths = [int(round(dwell(slide) * fps)) for slide in slides]
    at = int(now * fps) % sum(lengths)
    for index, length in enumerate(lengths):
        if at < length:
            return index, at
        at -= length
    return 0, 0


def frames(listings, fps=FPS, clock=time.time):
    """Every frame, forever: rounds of the deck, each page building in over the last.

    Tuning in lands mid-round, as on cable: the first round with listings starts
    where the wall clock says (tune_in), part-way through a page if need be. Until
    the listings are in, the title page waits for them, and they cut in at once."""
    background = backdrop()
    turns, last, tuned = 0, None, False
    while True:
        payload = listings.latest()
        slides = deck(payload or None)
        first, skip = 0, 0
        if payload and not tuned:
            (first, skip), tuned = tune_in(slides, clock(), fps), True
        for index in range(first, len(slides)):
            slide = slides[index]
            page = Page(slide, payload, channel_of(payload), background)
            kind = TRANSITIONS[turns % len(TRANSITIONS)] if last is not None else None
            turns += last is not None
            span, curve = TRANSITION_TIMING.get(kind, (0, None))
            ease = bezier(curve)
            start = skip if index == first else 0
            shown = last
            for n in range(start, int(round(dwell(slide) * fps))):
                if not tuned and listings.latest():
                    break           # the listings are in: tune in from here
                t, into = n / fps, (n - start) / fps
                frame = page.frame(t)
                if kind and into < span:
                    frame = transition(kind, ease(into / span), last, frame)
                yield frame
                shown = frame
            else:
                last = page.frame(dwell(slide))
                continue
            last = shown
            break


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="the server's /api/v1/sports-guide")
    parser.add_argument("--fps", type=int, default=FPS)
    parser.add_argument("--lead", type=float, default=2.0,
                        help="seconds the frames may run ahead of the wall clock")
    parser.add_argument("--seconds", type=float, help="stop after this much video (tests)")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    parent = os.getppid()
    listings = Listings(args.url)
    out = sys.stdout.buffer
    started = time.monotonic()
    try:
        for n, frame in enumerate(frames(listings, args.fps)):
            if args.seconds is not None and n >= args.seconds * args.fps:
                break
            ahead = started + n / args.fps - args.lead - time.monotonic()
            if ahead > 0:
                time.sleep(ahead)
            # An orphan would write frames to nobody; its encoder ends with it.
            if n % args.fps == 0 and os.getppid() != parent:
                return 0
            out.write(memoryview(np.ascontiguousarray(frame)).cast("B"))
    except BrokenPipeError:
        pass
    finally:
        try:
            out.flush()
        except BrokenPipeError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
