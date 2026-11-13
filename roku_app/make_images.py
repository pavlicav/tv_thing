"""Draws the Roku app's artwork in images/: the cable box's cells, ruler,
panel and backdrops in static/guide.html's --stb colours, the info banner
the Watch page shows on a channel change, and the home-screen icons and
splash. Cells are 9-patches, so the app stretches them to any width.

Needs Pillow: /home/vivian/miniconda3/bin/python3 roku_app/make_images.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

IMAGES = Path(__file__).parent / "images"
BOLD = Path(__file__).parent / "fonts" / "DejaVuSans-Bold.ttf"
SCALE = 4  # icons are drawn large, then reduced, for smooth edges


def rgb(value):
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


# static/guide.html's set-top-box palette.
BG_HI, BG, BG_LO, LINE = rgb("#13306f"), rgb("#0a1d4a"), rgb("#050f2b"), rgb("#030a1f")
CELL_HI, CELL, CELL_LO = rgb("#2c56b0"), rgb("#1d3f8c"), rgb("#16326f")
CHAN_HI, CHAN, CHAN_LO = rgb("#56688f"), rgb("#36476d"), rgb("#26344f")
GOLD_HI, GOLD, GOLD_LO = rgb("#fff1a8"), rgb("#ffd23f"), rgb("#e5a312")
TEXT, BEVEL = rgb("#ffffff"), rgb("#8aa4dc")


def mix(a, b, f):
    return tuple(round(x + (y - x) * f) for x, y in zip(a, b))


def colour_at(stops, t):
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if p0 <= t <= p1:
            return mix(c0, c1, (t - p0) / (p1 - p0) if p1 > p0 else 0)
    return stops[-1][1]


def vertical(width, height, stops):
    """A top-to-bottom gradient; stops are (0-1, RGB or RGBA)."""
    image = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        colour = colour_at(stops, y / max(1, height - 1))
        draw.line([(0, y), (width - 1, y)], fill=colour if len(colour) == 4 else colour + (255,))
    return image


def highlight(image, alpha, y=0, colour=(255, 255, 255)):
    """CSS inset 0 1px 0: a light line along the top."""
    line = Image.new("RGBA", (image.width, 1), colour + (round(alpha * 255),))
    image.alpha_composite(line, (0, y))
    return image


def nine_patch(core, stretch_x, stretch_y):
    """Adds the 1 px border whose black marks say which middle stretches."""
    patch = Image.new("RGBA", (core.width + 2, core.height + 2), (0, 0, 0, 0))
    patch.paste(core, (1, 1))
    for x in range(*stretch_x):
        patch.putpixel((x + 1, 0), (0, 0, 0, 255))
    for y in range(*stretch_y):
        patch.putpixel((0, y + 1), (0, 0, 0, 255))
    return patch


def cell(stops, top_alpha, border=None):
    core = highlight(vertical(16, 40, stops), top_alpha)
    if border:
        ImageDraw.Draw(core).rectangle([0, 0, 15, 39], outline=border + (255,))
        highlight(core, top_alpha, y=1)
    return nine_patch(core, (4, 12), (4, 36))


def rounded(width, height, radius, stops, border, top_alpha):
    """A rounded box: gradient fill, 1 px border, a light top edge."""
    big = Image.new("RGBA", (width * SCALE, height * SCALE), (0, 0, 0, 0))
    fill = vertical(width * SCALE, height * SCALE, stops)
    mask = Image.new("L", big.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big.width - 1, big.height - 1], radius=radius * SCALE, fill=255)
    big.paste(fill, (0, 0), mask)
    draw = ImageDraw.Draw(big)
    if top_alpha:
        draw.line([(radius * SCALE, SCALE), (big.width - radius * SCALE, SCALE)],
                  fill=(255, 255, 255, round(top_alpha * 255)), width=SCALE)
    draw.rounded_rectangle([0, 0, big.width - 1, big.height - 1], radius=radius * SCALE,
                           outline=border, width=SCALE)
    return big.resize((width, height), Image.LANCZOS)


def radial(width, height, base_stops, centre, radii, hi, reach):
    """CSS radial-gradient(rx ry at cx cy, hi 0%, transparent reach) over a
    vertical gradient: the lit top of the guide's screen."""
    base = vertical(width, height, base_stops).convert("RGB")
    px = base.load()
    cx, cy = centre[0] * width, centre[1] * height
    rx, ry = radii[0] * width, radii[1] * height
    for y in range(height):
        for x in range(width):
            d = math.hypot((x - cx) / rx, (y - cy) / ry)
            if d < reach:
                px[x, y] = mix(px[x, y], hi, 1 - d / reach)
    return base


def screen(width, height):
    return radial(width, height, [(0, BG), (1, BG_LO)], (0.5, -0.1), (1.2, 0.7), BG_HI, 0.6)


def banner():
    """The Watch page's info banner, 1280 x 154."""
    image = radial(1280, 154, [(0, BG), (1, rgb("#040a1c"))], (0.5, 0), (1.2, 1.4), BG_HI, 0.7)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 1279, 1], fill=LINE)
    draw.line([(0, 2), (1279, 2)], fill=mix(BG, BEVEL, 0.35))
    return image


def card(width, height):
    """Icons and splash: the cable box's screen, the favicon's set in gold,
    and TV THING."""
    image = screen(width, height).convert("RGBA")
    layer = Image.new("RGBA", (width * SCALE, height * SCALE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    w, h = layer.size
    sw = int(h * 0.40)
    sh = sw * 2 // 3
    sx, sy = (w - sw) // 2, int(h * 0.15)
    line = max(SCALE, sw // 20)
    draw.rounded_rectangle([sx, sy, sx + sw, sy + sh], radius=sw // 12, fill=rgb("#0b1c4d") + (255,), outline=GOLD + (255,), width=line)
    cx, cy, size = sx + sw / 2, sy + sh / 2, sh * 0.42
    draw.polygon([(cx - size * 0.38, cy - size / 2), (cx - size * 0.38, cy + size / 2), (cx + size * 0.5, cy)], fill=GOLD + (255,))
    stand = sy + sh + sw // 10
    draw.rounded_rectangle([cx - sw / 4, stand, cx + sw / 4, stand + line], radius=line // 2, fill=GOLD + (255,))
    font = ImageFont.truetype(str(BOLD), int(h * 0.15))
    word = "TV THING"
    tracking = font.size * 0.12
    advances = [font.getlength(ch) for ch in word]
    x = (w - (sum(advances) + tracking * (len(word) - 1))) / 2
    top = stand + int(h * 0.10)
    shadow = max(SCALE, font.size // 14)
    for ch, advance in zip(word, advances):
        draw.text((x + shadow, top + shadow), ch, font=font, fill=(0, 0, 0, 255))
        draw.text((x, top), ch, font=font, fill=TEXT + (255,))
        x += advance + tracking
    image.alpha_composite(layer.resize((width, height), Image.LANCZOS))
    return image.convert("RGB")


def main():
    IMAGES.mkdir(exist_ok=True)
    # Guide cells (static/guide.html .prog, .prog[aria-current], .gap, .chan, .ruler).
    cell([(0, CELL_HI), (0.45, CELL), (1, CELL_LO)], 0.16).save(IMAGES / "cell.9.png")
    cell([(0, GOLD_HI), (0.45, GOLD), (1, GOLD_LO)], 0.7, border=rgb("#8a6208")).save(IMAGES / "gold.9.png")
    cell([(0, rgb("#16295a")), (1, rgb("#102149"))], 0).save(IMAGES / "gap.9.png")
    cell([(0, CHAN_HI), (0.5, CHAN), (1, CHAN_LO)], 0.15).save(IMAGES / "chan.9.png")
    cell([(0, rgb("#7488b3")), (0.5, rgb("#4f6391")), (1, rgb("#3a4b70"))], 0.15).save(IMAGES / "chanfocus.9.png")
    ruler = highlight(vertical(16, 30, [(0, rgb("#7d9bdc")), (0.48, rgb("#4466b4")), (0.52, rgb("#2f4f99")), (1, rgb("#3a5ba8"))]), 0.4)
    nine_patch(ruler, (4, 12), (4, 26)).save(IMAGES / "ruler.9.png")
    # The info panel: a faint lit box with a bevel-blue edge.
    panel = rounded(32, 32, 6, [(0, (255, 255, 255, 18)), (1, (255, 255, 255, 5))], BEVEL + (89,), 0.08)
    nine_patch(panel, (8, 24), (8, 24)).save(IMAGES / "panel.9.png")
    # A remote key in the legend (.stb-btn) and the PIP's caption shade.
    pill = rounded(24, 22, 9, [(0, rgb("#4a6fc4")), (0.5, rgb("#2a4d9e")), (0.51, rgb("#1f3c80")), (1, rgb("#2a4d9e"))],
                   rgb("#0b1a44") + (255,), 0.35)
    nine_patch(pill, (10, 14), (10, 12)).save(IMAGES / "pill.9.png")
    nine_patch(vertical(8, 40, [(0, (0, 0, 0, 0)), (1, (0, 0, 0, 199))]), (2, 6), (1, 39)).save(IMAGES / "shade.9.png")
    # Backdrops.
    screen(1280, 720).save(IMAGES / "screen_hd.jpg", quality=90)
    banner().save(IMAGES / "banner_hd.jpg", quality=92)
    # Home screen and splash.
    card(336, 210).save(IMAGES / "icon_focus_hd.png")
    card(246, 140).save(IMAGES / "icon_focus_sd.png")
    card(108, 69).save(IMAGES / "icon_side_hd.png")
    card(1280, 720).save(IMAGES / "splash_hd.jpg", quality=92)


if __name__ == "__main__":
    main()
