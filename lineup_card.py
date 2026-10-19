"""Print TV Thing's channel lineup as a vintage cable lineup card (a PDF).

Reads the numbered lineup from a running server (``GET /api/v1/lineup``) or a
saved copy of that response, typesets it with ``lineup_card.tex`` and writes
``static/lineup-card.pdf``, which the About page offers for download. The
day's games are left off -- their numbers change every morning -- and each
games block prints a note in their place.

Needs Tectonic (https://tectonic-typesetting.github.io), a self-contained
XeTeX that fetches the packages and fonts it needs on first use.

    python3 lineup_card.py                          # from http://127.0.0.1:8080
    python3 lineup_card.py --json lineup.json --out /tmp/card.pdf
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import roku_lane

BASE_DIR = Path(__file__).parent
TEMPLATE = BASE_DIR / "lineup_card.tex"
DEFAULT_OUT = BASE_DIR / "static" / "lineup-card.pdf"
DEFAULT_URL = "http://127.0.0.1:8080"
TECTONIC_TIMEOUT = 600      # the first build downloads TeX Live's pieces

_TEX_SPECIALS = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def tex_escape(text):
    return "".join(_TEX_SPECIALS.get(char, char) for char in str(text))


def _numbers(ranges):
    """60-69 and 71-79 as the card prints them."""
    return " · ".join(str(first) if first == last else f"{first}–{last}"
                      for first, last in ranges)


def card_blocks(payload):
    """The card's blocks, in number order, from an /api/v1/lineup response.

    Sections that share a name print as one block (the station on 0 and 70,
    football split around it). A games section lists the sources it numbers
    rather than the day's games; a section with no channels is left off.
    """
    blocks, by_name = [], {}
    for section in sorted(payload.get("sections") or (), key=lambda s: s["first"]):
        block = by_name.get(section["name"])
        if block is None:
            block = by_name[section["name"]] = {
                "name": section["name"], "ranges": [], "games": (), "rows": []}
            blocks.append(block)
        block["ranges"].append((section["first"], section["last"]))
        block["games"] = tuple(dict.fromkeys(block["games"] + tuple(section.get("games") or ())))

    def block_of(number):
        return next((block for block in blocks
                     if any(first <= number <= last for first, last in block["ranges"])), None)

    for channel in payload.get("channels") or ():
        number = channel.get("tv_number")
        block = None if number is None else block_of(number)
        if block is None or block["games"]:
            continue
        name = channel.get("tv_name") or channel.get("name") or ""
        # A YouTube TV local goes by its call sign; the network goes beside it.
        network = channel.get("name") or ""
        note = network if network and network.lower() not in name.lower() else ""
        antenna = channel.get("antenna_number") if channel.get("kind") == "digital" else None
        block["rows"].append((number, name, note, antenna or ""))
    for block in blocks:
        block["rows"].sort()
    return [block for block in blocks if block["rows"] or block["games"]]


def _games_note(sources, section_name):
    labels = [roku_lane.SOURCES.get(source, (source,))[0] for source in sources]
    labels = [label for label in labels if label.lower() != section_name.lower()]
    which = f"{' and '.join(labels)} games" if labels else "The day's games"
    return f"{which}, numbered each morning by start time."


def channels_tex(payload, effective):
    """channels.tex: the effective date and the blocks, for lineup_card.tex."""
    lines = [
        f"\\newcommand\\EffectiveDay{{{effective.strftime('%b').upper()} {effective.day}}}",
        f"\\newcommand\\EffectiveYear{{{effective.year}}}",
        "\\newcommand\\Channels{%",
    ]
    for block in card_blocks(payload):
        lines.append(f"\\block{{{tex_escape(block['name'])}}}{{{_numbers(block['ranges'])}}}")
        if block["games"]:
            lines.append(f"\\games{{{tex_escape(_games_note(block['games'], block['name']))}}}")
            continue
        rows = block["rows"]
        for index, (number, name, note, antenna) in enumerate(rows):
            lines.append(f"\\chan{{{number}}}{{{tex_escape(name)}}}"
                         f"{{{tex_escape(note)}}}{{{tex_escape(antenna)}}}")
            # Keep a heading with two listings, and never strand the last one.
            if index == 0 or index == len(rows) - 2:
                lines.append("\\nobreak")
    lines.append("}")
    return "\n".join(lines) + "\n"


def find_tectonic():
    found = shutil.which("tectonic")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "tectonic"
    return str(local) if local.exists() else None


def build(payload, out_path, effective, tectonic):
    """Typeset the card and put the PDF at out_path (replaced atomically)."""
    out_path = Path(out_path)
    with tempfile.TemporaryDirectory(prefix="lineup-card-") as work:
        work = Path(work)
        shutil.copy(TEMPLATE, work / "card.tex")
        (work / "channels.tex").write_text(channels_tex(payload, effective))
        result = subprocess.run(
            [tectonic, "--chatter", "minimal", "card.tex"], cwd=work,
            capture_output=True, text=True, timeout=TECTONIC_TIMEOUT)
        if result.returncode != 0:
            raise RuntimeError(f"tectonic failed:\n{result.stdout}{result.stderr}")
        staged = out_path.with_name(f".{out_path.name}.tmp")
        shutil.copy(work / "card.pdf", staged)
        os.replace(staged, out_path)
    return out_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url", default=DEFAULT_URL,
                        help="TV Thing server to read /api/v1/lineup from (default %(default)s)")
    source.add_argument("--json", type=Path, help="a saved /api/v1/lineup response instead")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="where to write the PDF (default static/lineup-card.pdf)")
    parser.add_argument("--date", type=datetime.date.fromisoformat,
                        help="the effective date printed on the card (default today)")
    args = parser.parse_args(argv)

    if args.json:
        payload = json.loads(args.json.read_text())
    else:
        with urllib.request.urlopen(f"{args.url.rstrip('/')}/api/v1/lineup", timeout=60) as response:
            payload = json.load(response)
    if payload.get("lineup_error"):
        parser.error(f"the server's lineup is broken: {payload['lineup_error']}")
    tectonic = find_tectonic()
    if tectonic is None:
        parser.error("tectonic is not installed (https://tectonic-typesetting.github.io)")
    out = build(payload, args.out, args.date or datetime.date.today(), tectonic)
    print(out)


if __name__ == "__main__":
    sys.exit(main())
