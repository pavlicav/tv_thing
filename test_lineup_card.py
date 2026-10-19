"""The printable lineup card (lineup_card.py, lineup_card.tex); no server."""
import datetime
import shutil
import subprocess
from pathlib import Path

import pytest

import lineup_card

ROOT = Path(__file__).parent

SECTIONS = [
    {"first": 0, "last": 0, "name": "House channel"},
    {"first": 1, "last": 29, "name": "Local stations"},
    {"first": 60, "last": 69, "name": "Football", "games": ["sunday-ticket", "thursday-night-football"]},
    {"first": 70, "last": 70, "name": "House channel"},
    {"first": 71, "last": 79, "name": "Football", "games": ["sunday-ticket", "thursday-night-football"]},
    {"first": 80, "last": 99, "name": "Baseball", "games": ["mlb"]},
    {"first": 270, "last": 279, "name": "Shopping & more"},
    {"first": 300, "last": 349, "name": "New on the antenna", "new": ["digital"]},
]

CHANNELS = [
    {"tv_number": 70, "tv_name": "VizelVision", "kind": "vizelvision", "name": "VizelVision"},
    {"tv_number": 0, "tv_name": "VizelVision", "kind": "vizelvision", "name": "VizelVision"},
    {"tv_number": 4, "tv_name": "WRC-HD", "kind": "digital", "name": "WRC-HD", "antenna_number": "4.1"},
    {"tv_number": 16, "tv_name": "WILX", "kind": "roku", "source_id": "youtube-tv", "name": "NBC"},
    {"tv_number": 17, "tv_name": "FOX SOUL", "kind": "roku", "source_id": "youtube-tv", "name": "SOUL"},
    {"tv_number": 60, "tv_name": "Bears at Lions", "kind": "roku", "source_id": "sunday-ticket"},
    {"tv_number": 80, "tv_name": "Guardians at Rays", "kind": "roku", "source_id": "mlb"},
    {"tv_number": 271, "tv_name": "QVC2", "kind": "digital", "name": "QVC2", "antenna_number": "49.3"},
    {"tv_number": 270, "tv_name": "Q & A_100%", "kind": "digital", "name": "Q & A_100%", "antenna_number": "9.5"},
    {"tv_number": None, "tv_name": None, "kind": "digital", "name": "Unnumbered"},
]

PAYLOAD = {"sections": SECTIONS, "channels": CHANNELS}


def blocks_by_name():
    return {block["name"]: block for block in lineup_card.card_blocks(PAYLOAD)}


def test_sections_sharing_a_name_print_as_one_block_in_number_order():
    blocks = lineup_card.card_blocks(PAYLOAD)
    assert [block["name"] for block in blocks] == [
        "House channel", "Local stations", "Football", "Baseball", "Shopping & more"]
    house, football = blocks[0], blocks[2]
    assert house["ranges"] == [(0, 0), (70, 70)]
    assert [row[0] for row in house["rows"]] == [0, 70]
    assert football["ranges"] == [(60, 69), (71, 79)]


def test_games_are_left_off_and_empty_sections_dropped():
    blocks = blocks_by_name()
    assert blocks["Football"]["rows"] == [] and blocks["Baseball"]["rows"] == []
    assert "New on the antenna" not in blocks
    listed = [row[1] for block in blocks.values() for row in block["rows"]]
    assert "Bears at Lions" not in listed and "Unnumbered" not in listed


def test_a_listing_carries_its_network_and_antenna_channel():
    rows = blocks_by_name()["Local stations"]["rows"]
    assert rows == [(4, "WRC-HD", "", "4.1"),
                    (16, "WILX", "NBC", ""),
                    (17, "FOX SOUL", "", "")]     # SOUL is already in the name


def test_channels_tex_escapes_names_and_dates_the_card():
    tex = lineup_card.channels_tex(PAYLOAD, datetime.date(2026, 10, 19))
    assert r"\newcommand\EffectiveDay{OCT 19}" in tex
    assert r"\newcommand\EffectiveYear{2026}" in tex
    assert r"\cardstockfalse" not in tex
    assert r"\block{House channel}{0 · 70}" in tex
    assert r"\block{Football}{60–69 · 71–79}" in tex
    assert r"\block{Shopping \& more}{270–279}" in tex
    assert r"\chan{270}{Q \& A\_100\%}{}{9.5}" in tex
    assert r"\chan{16}{WILX}{NBC}{}" in tex
    assert ("\\games{NFL Sunday Ticket and Thursday Night Football games, "
            "numbered each morning by start time.}") in tex
    # The section name is not repeated back as a source.
    assert "\\games{The day's games, numbered each morning by start time.}" in tex


def test_the_print_version_is_on_white_paper():
    tex = lineup_card.channels_tex(PAYLOAD, datetime.date(2026, 10, 19), cardstock=False)
    assert tex.startswith("\\cardstockfalse\n")


def test_tex_escape_covers_every_special_character():
    assert lineup_card.tex_escape(r"a\b&c%d$e#f_g{h}i~j^k") == (
        r"a\textbackslash{}b\&c\%d\$e\#f\_g\{h\}i\textasciitilde{}j\textasciicircum{}k")


def test_about_page_offers_both_cards_it_ships():
    about = (ROOT / "static" / "about.html").read_text()
    for name in lineup_card.CARDS.values():
        assert f'href="{name}" download' in about
        assert (ROOT / "static" / name).read_bytes().startswith(b"%PDF-")


@pytest.mark.skipif(lineup_card.find_tectonic() is None or shutil.which("pdfinfo") is None,
                    reason="needs tectonic and pdfinfo")
@pytest.mark.parametrize("cardstock", [True, False])
def test_the_card_typesets_onto_one_page(tmp_path, cardstock):
    out = lineup_card.build(PAYLOAD, tmp_path / "card.pdf", datetime.date(2026, 10, 19),
                            lineup_card.find_tectonic(), cardstock)
    info = subprocess.run(["pdfinfo", str(out)], capture_output=True, text=True, check=True).stdout
    assert "Pages:           1" in info
