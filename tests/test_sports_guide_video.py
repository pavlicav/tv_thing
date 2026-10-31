"""The Sports Guide drawn as video (sports_guide_video.py); no encoder, no server."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

import sports_guide_video as video

ROOT = Path(__file__).resolve().parent.parent


def game(title, start=None, live=False, tag="NFL", network="ESPN", number=33, via="channel", **extra):
    return {"title": title, "start": start, "live": live, "tag": tag, "network": network,
            "tv_number": number, "via": via, **extra}


def at(hour, minute=0):
    return int(datetime(2026, 10, 24, hour, minute).timestamp())


PAYLOAD = {"date": "2026-10-24", "channel": {"tv_number": 60}, "errors": [], "sports": [
    {"sport": "Football", "games": [game(f"Team{n} at Club{n}", at(12), tag="NCAA" if n else "NFL")
                                   for n in range(8)]},
    {"sport": "Hockey", "games": [game("Kings at Devils", at(15, 30), live=True, tag="NHL",
                                       network="ESPN+", number=61, via="nhl")]}]}


def test_the_deck_is_the_title_then_each_sport_on_even_pages():
    pages = video.deck(PAYLOAD)
    assert [(p["kind"], p.get("sport"), p.get("page"), len(p.get("games", ()))) for p in pages] == [
        ("title", None, None, 0), ("sport", "Football", 0, 4), ("sport", "Football", 1, 4),
        ("sport", "Hockey", 0, 1)]
    assert pages[1]["tags"] is True and pages[3]["tags"] is False
    assert video.deck(None) == [{"kind": "title"}]
    assert video.dwell(pages[0]) == 9 and video.dwell(pages[3]) == pytest.approx(6.35)


def test_times_read_as_a_listing_prints_them():
    assert video.start_text({"start": at(12)}) == "Noon"
    assert video.start_text({"start": at(0)}) == "Midnight"
    assert video.start_text({"start": at(19, 30)}) == "7:30 pm"
    assert video.start_text({"start": at(9, 5)}) == "9:05 am"
    assert video.start_text({"start": at(19), "time_tbd": True}) == "TBA"
    assert video.start_text({}) == "TBA"
    assert video.today_text(PAYLOAD) == "Saturday, October 24"


def test_scan_lines_land_on_the_same_screen_lines_wherever_a_layer_sits():
    flat = np.full((6, 2, 3), 100, np.float32)
    whole = video.scanned(flat, 0)
    assert [row[0, 0] < 100 for row in whole] == [False, False, True, False, False, True]
    shifted = video.scanned(flat, 1)            # a layer placed on screen line 1
    assert [row[0, 0] < 100 for row in shifted] == [False, True, False, False, True, False]


def test_each_transition_brings_the_new_page_in_its_own_way():
    old = np.zeros((video.H, video.W, 3), np.uint8)
    new = np.full((video.H, video.W, 3), 200, np.uint8)
    wipe = video.transition("wipe", .5, old, new)
    assert wipe[10, 10, 0] == 200 and wipe[10, video.W - 10, 0] == 0
    barn = video.transition("barn", .5, old, new)
    assert barn[10, video.W // 2, 0] == 200 and barn[10, 5, 0] == 0
    iris = video.transition("iris", .3, old, new)
    assert iris[video.H // 2, video.W // 2, 0] == 200 and iris[0, 0, 0] == 0
    blinds = video.transition("blinds", .5, old, new)
    assert blinds[0, 0, 0] == 200 and blinds[30, 0, 0] == 0 and blinds[48, 0, 0] == 200
    dissolve = video.transition("dissolve", .5, old, new)
    assert 95 <= dissolve[0, 0, 0] <= 105
    for kind in video.TRANSITIONS:
        assert np.array_equal(video.transition(kind, 1.0, old, new), new), kind


def test_a_page_builds_in_then_holds_one_frame():
    background = video.backdrop()
    page = video.Page(video.deck(PAYLOAD)[1], PAYLOAD, 60, background)
    start, settled = page.frame(0.0), page.frame(page.settle + .1)
    assert start.shape == (video.H, video.W, 3) and start.dtype == np.uint8
    # The rows are not in yet at the start, and are once it has built.
    row = (slice(226, 282), slice(250, 900))
    assert not np.array_equal(start[row], settled[row])
    assert page.frame(page.settle + .2) is settled
    assert page.settle == pytest.approx(.9 + 3 * .28 + .45)


def test_a_live_game_flashes_now():
    background = video.backdrop()
    page = video.Page(video.deck(PAYLOAD)[3], PAYLOAD, 60, background)
    # The cycle is 1.6 s, bright for its first 70%: 16.05 s is 0.05 s into one.
    bright, dim = page.frame(16.05), page.frame(16.05 + 1.3)
    assert not np.array_equal(bright, dim)
    assert page.frame(16.05 + 1.6) is bright


def test_the_title_page_says_what_is_on_or_why_not():
    background = video.backdrop()
    for payload in (PAYLOAD, None, False, {"date": "2026-10-24", "sports": []}):
        page = video.Page({"kind": "title"}, payload, 60, background)
        assert page.frame(5.0).shape == (video.H, video.W, 3)


def test_tuning_in_lands_where_a_channel_that_never_stopped_would_be():
    pages = video.deck(PAYLOAD)
    fps = 10
    lengths = [int(round(video.dwell(page) * fps)) for page in pages]     # 90, 98, 98, 64 frames
    assert video.tune_in(pages, 0, fps) == (0, 0)
    assert video.tune_in(pages, 9.5, fps) == (1, 5)                       # half a second into page 1
    round_seconds = sum(lengths) / fps
    assert video.tune_in(pages, round_seconds * 1000 + 9.5, fps) == (1, 5)   # every round the same
    assert video.tune_in(pages, (sum(lengths) - 1) / fps, fps) == (3, lengths[3] - 1)


class _Listings:
    def __init__(self, payload):
        self.payload = payload

    def latest(self):
        return self.payload


def _first(generator, count):
    return [next(generator) for _ in range(count)]


def test_the_first_frames_are_mid_round_and_a_round_later_it_starts_over():
    fps = 10
    clock = lambda: 9.5   # noqa: E731 - half a second into page 1 (see above)
    tuned = _first(video.frames(_Listings(PAYLOAD), fps, clock=clock), 3)
    background = video.backdrop()
    page = video.Page(video.deck(PAYLOAD)[1], PAYLOAD, 60, background)
    assert np.array_equal(tuned[0], page.frame(0.5))       # no transition into the first frame
    # From the very start of a round, the title page as before.
    title = _first(video.frames(_Listings(PAYLOAD), fps, clock=lambda: 0), 1)[0]
    assert np.array_equal(title, video.Page({"kind": "title"}, PAYLOAD, 60, background).frame(0))


def test_until_the_listings_come_the_title_page_waits_then_they_cut_in():
    listings = _Listings(None)
    frames = video.frames(listings, 10, clock=lambda: 9.5)
    waiting = _first(frames, 4)
    background = video.backdrop()
    gathering = video.Page({"kind": "title"}, None, 60, background)
    assert np.array_equal(waiting[0], gathering.frame(0))
    listings.payload = PAYLOAD
    after = _first(frames, 15)
    # The page the clock says wipes in over the waiting one, from half a second in.
    tuned = video.Page(video.deck(PAYLOAD)[1], PAYLOAD, 60, background)
    assert np.array_equal(after[0], waiting[-1])
    assert np.array_equal(after[-1], tuned.frame(0.5 + 1.4))


def test_the_title_pages_summary_takes_two_lines_when_one_would_not_fit():
    background = video.backdrop()
    many = {"date": "2026-10-24", "channel": {"tv_number": 60}, "errors": [], "sports": [
        {"sport": name, "games": [game("A at B", at(12))] * 12} for name in
        ("NFL Football", "College Football", "Baseball", "Basketball", "Hockey", "Soccer")]}
    one, two = video.Layer(video.W, 104), video.Layer(video.W, 104)
    assert video.Page.summary(one, PAYLOAD["sports"]) == 1
    assert video.Page.summary(two, many["sports"]) == 2
    box = two.image.getbbox()
    assert box[0] >= 80 and box[2] <= video.W - 80 and box[3] > 60   # within the page, a second line
    assert video.Page({"kind": "title"}, many, 60, background).frame(8.0).shape == (video.H, video.W, 3)


def test_it_writes_whole_frames_to_stdout(tmp_path):
    listings = tmp_path / "sports-guide.json"
    listings.write_text(json.dumps(PAYLOAD))
    result = subprocess.run(
        [sys.executable, str(ROOT / "sports_guide_video.py"), "--url", listings.as_uri(),
         "--fps", "10", "--lead", "1000", "--seconds", "1"],
        capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode()[-400:]
    assert len(result.stdout) == 10 * video.W * video.H * 3
