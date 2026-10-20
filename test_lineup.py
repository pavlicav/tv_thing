"""TV Thing channel numbers (lineup.py, lineup.json); no hardware, no server."""
import json
from pathlib import Path

import pytest

import lineup

ROOT = Path(__file__).parent

SECTIONS = [
    {"first": 0, "last": 0, "name": "House channel"},
    {"first": 1, "last": 20, "name": "Local stations"},
    {"first": 21, "last": 49, "name": "Cable", "new": ["youtube-tv"]},
    {"first": 50, "last": 52, "name": "Football", "games": ["sunday-ticket", "thursday-night-football"]},
    {"first": 70, "last": 70, "name": "House channel"},
    {"first": 90, "last": 99, "name": "Roku", "new": ["*"]},
    {"first": 250, "last": 251, "name": "New on the antenna", "new": ["digital"]},
    {"first": 300, "last": 999, "name": "More channels", "overflow": True},
]


def plan_document(channels=(), sections=SECTIONS):
    return {"sections": list(sections), "channels": [
        {"number": 0, "source": "vizelvision"}, {"number": 70, "source": "vizelvision"},
        {"number": 4, "source": "digital", "channel": "4.1"},
        {"number": 15, "source": "youtube-tv", "channel": "NBC", "name": "NBC Lansing"},
        {"number": 21, "source": "youtube-tv", "channel": "TBS"},
        *channels]}


@pytest.fixture
def make(tmp_path):
    path = tmp_path / "lineup.json"

    def build(document=None):
        path.write_text(json.dumps(document or plan_document()))
        return lineup.Lineup(path, tmp_path / "lineup_state.json", clock=lambda: 1_800_000_000)
    return build


def antenna(*numbers):
    return [(number, f"Station {number}") for number in numbers]


def numbers(views):
    return [view and view["tv_number"] for view in views]


# -- the plan -------------------------------------------------------------------

@pytest.mark.parametrize("change, message", [
    (lambda d: d["sections"].append({"first": 20, "last": 22, "name": "Clash"}), "overlap"),
    (lambda d: d["channels"].append({"number": 4, "source": "digital", "channel": "5.1"}), "twice"),
    (lambda d: d["channels"].append({"number": 51, "source": "digital", "channel": "5.1"}), "numbers itself"),
    (lambda d: d["channels"].append({"number": 5, "source": "digital", "channel": "4.1"}), "already has"),
    (lambda d: d["channels"].append({"number": -1, "source": "digital", "channel": "5.1"}), "whole number"),
    (lambda d: d["sections"].pop(), "overflow"),
    (lambda d: d["sections"].append({"first": 400, "last": 300, "name": "Backwards"}), "before first"),
])
def test_an_unusable_plan_is_refused(change, message):
    document = plan_document()
    change(document)
    with pytest.raises(lineup.LineupError, match=message):
        lineup.parse_plan(document)


def test_the_station_holds_two_numbers():
    plan = lineup.parse_plan(plan_document())
    assert plan.house == ((0, "VizelVision"), (70, "VizelVision"))
    assert plan.fixed["youtube-tv/NBC"] == (15, "NBC Lansing")


def test_the_station_can_hold_a_slide_channel():
    plan = lineup.parse_plan(plan_document([
        {"number": 1, "source": "vizelvision", "channel": "sports-guide"},
        {"number": 2, "source": "vizelvision", "channel": "sports-guide", "name": "Scores"}]))
    assert plan.house == ((0, "VizelVision"), (70, "VizelVision"))
    assert plan.slides == ((1, "VizelVision Sports Guide", "sports-guide"), (2, "Scores", "sports-guide"))
    assert plan.station_numbers() == {0, 1, 2, 70}
    with pytest.raises(lineup.LineupError, match="no 'weather' channel"):
        lineup.parse_plan(plan_document([{"number": 1, "source": "vizelvision", "channel": "weather"}]))
    with pytest.raises(lineup.LineupError, match="numbers itself"):
        lineup.parse_plan(plan_document([{"number": 51, "source": "vizelvision", "channel": "sports-guide"}]))


def test_a_slide_channel_is_numbered_and_never_given_out(make):
    numbering = make(plan_document([{"number": 49, "source": "vizelvision", "channel": "sports-guide"}]))
    assert numbering.slides() == [{"tv_number": 49, "tv_name": "VizelVision Sports Guide",
                                   "tv_section": "Cable", "channel_id": "sports-guide"}]
    assert [view["tv_number"] for view in numbering.house()] == [0, 70]
    # New YouTube TV rows fill Cable around TBS on 21 and the guide on 49, then overflow.
    taken = numbers(numbering.number("youtube-tv", [(f"Row {n}", f"Row {n}") for n in range(28)]))
    assert taken == [*range(22, 49), 300]


def test_a_broken_lineup_numbers_nothing_and_writes_nothing(tmp_path):
    (tmp_path / "lineup.json").write_text("{not json")
    numbering = lineup.Lineup(tmp_path / "lineup.json", tmp_path / "state.json")
    assert numbering.number("digital", antenna("4.1", "7.5")) == [None, None]
    assert numbering.number_games({"mlb": [{"id": "1"}]}) == {}
    assert numbering.house() == [] and numbering.sections() == []
    assert not (tmp_path / "state.json").exists()
    with pytest.raises(lineup.LineupError):
        numbering.plan()


# -- fixed and new channels -------------------------------------------------------

def test_fixed_channels_take_their_numbers_and_names(make):
    numbering = make()
    views = numbering.number("youtube-tv", [("NBC", "NBC"), ("TBS", "TBS")])
    assert views == [{"tv_number": 15, "tv_name": "NBC Lansing", "tv_section": "Local stations"},
                     {"tv_number": 21, "tv_name": "TBS", "tv_section": "Cable"}]
    assert numbering.house()[1] == {"tv_number": 70, "tv_name": "VizelVision", "tv_section": "House channel"}


def test_a_surprise_antenna_channel_is_numbered_once_and_keeps_it(make, tmp_path):
    numbering = make()
    assert numbers(numbering.number("digital", antenna("4.1", "7.5"))) == [4, 250]
    # A later scan finds another, listed first: neither moves.
    assert numbers(numbering.number("digital", antenna("2.1", "4.1", "7.5"))) == [251, 4, 250]
    state = json.loads((tmp_path / "lineup_state.json").read_text())["assigned"]
    assert state["digital/7.5"] == {"number": 250, "name": "Station 7.5", "first_seen": 1_800_000_000}
    # A restart reads them back; a channel that drops out keeps its number.
    again = lineup.Lineup(tmp_path / "lineup.json", tmp_path / "lineup_state.json")
    assert numbers(again.number("digital", antenna("7.5"))) == [250]
    assert numbers(again.number("digital", antenna("2.1"))) == [251]


def test_a_full_section_spills_into_overflow(make):
    numbering = make()
    assert numbers(numbering.number("digital", antenna("2.1", "3.1", "6.1"))) == [250, 251, 300]


def test_new_feeds_land_in_their_own_section(make):
    numbering = make()
    assert numbers(numbering.number("youtube-tv", [("CNN", "CNN")])) == [22]
    assert numbers(numbering.number("prime-channels", [("x", "X")])) == [90]


def test_lineup_json_has_the_last_word(make, tmp_path):
    numbering = make()
    assert numbers(numbering.number("digital", antenna("7.5", "2.1"))) == [250, 251]
    # Listing 7.5 moves it; giving 251 to another channel moves 2.1 off it.
    make(plan_document([{"number": 7, "source": "digital", "channel": "7.5"},
                        {"number": 251, "source": "digital", "channel": "9.1"}]))
    assert numbers(numbering.number("digital", antenna("7.5", "2.1", "9.1"))) == [7, 250, 251]
    state = json.loads((tmp_path / "lineup_state.json").read_text())["assigned"]
    assert set(state) == {"digital/2.1"}


def test_antenna_keys_survive_a_rescan_and_tell_twins_apart():
    channels = [{"number": "4.1", "id": "wrc_hd_4_1", "rf_channel": 34},
                {"number": "4.1", "id": "far_4_1", "rf_channel": 10},
                {"id": "unnumbered"}]
    assert lineup.antenna_keys(channels) == ["4.1", "4.1@rf10", "id:unnumbered"]


# -- games ----------------------------------------------------------------------

def game(game_id, start):
    return {"id": game_id, "name": f"Game {game_id}", "start_time": start}


def test_a_days_games_share_a_block_by_kickoff(make):
    numbering = make()
    pools = {"sunday-ticket": [game("12", "2026-10-18T20:25Z"), game("10", "2026-10-18T17:00Z"),
                               game("11", "2026-10-18T17:00Z")],
             "thursday-night-football": [game("9", "2026-10-18T17:00Z")],
             "mlb": [game("7", "2026-10-18T18:00Z")]}
    views = numbering.number_games(pools)
    assert {key: view["tv_number"] for key, view in views.items()} == {
        ("thursday-night-football", "9"): 50, ("sunday-ticket", "10"): 51,
        ("sunday-ticket", "11"): 52,
        ("sunday-ticket", "12"): 300,      # The block holds three.
        ("mlb", "7"): 301}                 # No section of its own here.
    assert views[("sunday-ticket", "10")]["tv_section"] == "Football"
    # The numbers do not depend on the order the catalogs came in.
    shuffled = {"sunday-ticket": list(reversed(pools["sunday-ticket"])),
                "mlb": pools["mlb"], "thursday-night-football": pools["thursday-night-football"]}
    assert numbering.number_games(shuffled) == views


def test_game_overflow_steps_around_given_out_numbers(make):
    numbering = make()
    numbering.number("digital", antenna("2.1", "3.1", "6.1"))     # 6.1 has 300.
    views = numbering.number_games({"sunday-ticket": [game(str(n), "") for n in range(1, 5)]})
    assert sorted(view["tv_number"] for view in views.values()) == [50, 51, 52, 301]


def test_a_block_split_around_a_pinned_channel_fills_in_order(make):
    sections = [s for s in SECTIONS if s["name"] != "Football"] + [
        {"first": 60, "last": 61, "name": "Football", "games": ["sunday-ticket"]},
        {"first": 63, "last": 64, "name": "Football", "games": ["sunday-ticket"]}]
    numbering = make(plan_document([{"number": 62, "source": "vizelvision"}], sections=sections))
    views = numbering.number_games({"sunday-ticket": [game(str(n), "") for n in range(1, 6)]})
    assert [views[("sunday-ticket", str(n))]["tv_number"] for n in range(1, 6)] == [60, 61, 63, 64, 300]
    assert [view["tv_number"] for view in numbering.house()] == [0, 62, 70]


def test_a_split_block_lists_the_same_sources():
    document = plan_document()
    document["sections"].append({"first": 53, "last": 55, "name": "More football", "games": ["sunday-ticket"]})
    with pytest.raises(lineup.LineupError, match="share some game"):
        lineup.parse_plan(document)


def test_game_partners_name_the_shared_block(make):
    numbering = make()
    assert numbering.game_partners("sunday-ticket") == ("sunday-ticket", "thursday-night-football")
    assert numbering.game_partners("mlb") == ("mlb",)


# -- the real lineup card ---------------------------------------------------------

def real_plan():
    return lineup.parse_plan(json.loads((ROOT / "lineup.json").read_text()))


def test_every_channel_today_has_a_place_on_the_card():
    plan = real_plan()
    channels = json.loads((ROOT / "channels.json").read_text())
    listed = {key for key in plan.fixed if key.startswith("digital/")}
    assert {f"digital/{key}" for key in lineup.antenna_keys(channels)} == listed
    roku_rows = [row["name"] for row in json.loads((ROOT / "roku.json").read_text())["channels"]]
    assert {f"youtube-tv/{name}" for name in roku_rows} == {
        key for key in plan.fixed if key.startswith("youtube-tv/")}
    assert "roku-manual/manual" in plan.fixed


def test_the_card_reads_like_a_cable_lineup():
    plan = real_plan()
    by_number = {number: key for key, (number, _) in plan.fixed.items()}
    assert [number for number, _ in plan.house] == [0, 70]
    assert plan.slides == ((60, "VizelVision Sports Guide", "sports-guide"),)
    # Locals through 29, NHK and Baltimore's CW among them, the Roku's Lansing
    # stations together, and 22-29 held for a Lansing / Flint receiver.
    assert plan.section_of(29).name == "Local stations" and plan.section_of(30).name == "Cable"
    locals_ = [by_number[n] for n in range(1, 30) if n in by_number]
    assert "digital/22.4" in locals_ and "digital/54.1" in locals_
    assert [by_number[n] for n in range(16, 22)] == [
        "youtube-tv/NBC", "youtube-tv/FOX", "youtube-tv/ABC", "youtube-tv/CBS",
        "youtube-tv/PBS", "youtube-tv/The CW"]
    assert not any(n in by_number for n in range(22, 30))
    # DC's network stations keep their own dial positions.
    assert [by_number[n] for n in (4, 5, 7, 9)] == [
        "digital/4.1", "digital/5.1", "digital/7.1", "digital/9.1"]
    # Cable 30-59, every one from YouTube TV, the classics among them.
    assert all(key.startswith("youtube-tv/") for number, key in by_number.items() if 30 <= number <= 59)
    assert {"youtube-tv/CNN", "youtube-tv/MS NOW", "youtube-tv/Discovery Channel",
            "youtube-tv/HGTV"} <= {by_number[n] for n in range(30, 60)}
    # Live Sports: the Sports Guide on 60, then the games from 61 around
    # VizelVision on 70, football, baseball and hockey in one block, numbered together.
    assert plan.section_of(60).name == "Live Sports" and not plan.section_of(60).games
    for source in ("sunday-ticket", "thursday-night-football", "mlb", "nhl"):
        assert [(s.first, s.last, s.name) for s in plan.games_sections(source)] == [
            (61, 69, "Live Sports"), (71, 99, "Live Sports")]
    assert by_number[109] == "roku-manual/manual"
    assert plan.new_section("digital").first >= 300 and plan.new_section("youtube-tv").first >= 300
    assert all(number >= 110 for number, key in by_number.items()
               if key.startswith("digital/") and key not in locals_)


def test_a_state_file_that_cannot_be_written_still_numbers(tmp_path):
    (tmp_path / "lineup.json").write_text(json.dumps(plan_document()))
    numbering = lineup.Lineup(tmp_path / "lineup.json", tmp_path / "missing-dir" / "state.json")
    assert numbers(numbering.number("digital", antenna("7.5"))) == [250]
    assert numbers(numbering.number("digital", antenna("2.1", "7.5"))) == [251, 250]
