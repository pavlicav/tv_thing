"""VizelVision's schedule as guide listings: the projection, the programme on air, outages."""
import io
import json
from urllib.error import URLError

import pytest

import station_listings
from station_listings import StationListings, parse_listings

NOW = "2026-11-05T01:00:00+00:00"


def segment(start, end, title, kind="block", **fields):
    return {"start": f"2026-11-05T{start}:00+00:00", "end": f"2026-11-05T{end}:00+00:00", "title": title,
            "kind": kind, "target": {"source_id": "youtube-tv", "channel_id": "NBC"}, "game": None, "notes": [],
            "estimated": False, "item": None, **fields}


OFF = {"source_id": "off-air", "channel_id": "off-air"}
GAME = {"game_date": "2026-11-04", "game_pk": 401909922, "league": "nba", "title": "Philadelphia 76ers at Detroit Pistons"}


def at(clock):
    return int(station_listings._seconds(f"2026-11-05T{clock}:00+00:00"))


def test_a_game_held_to_its_final_says_its_league_source_and_that_the_end_is_a_guess():
    event = parse_listings({"segments": [segment("01:00", "02:40", "76ers at Pistons", "game", game=GAME, estimated=True,
                                                 target={"source_id": "youtube-tv", "channel_id": "ESPN",
                                                         "resolved_source": {"label": "ESPN HD"}})]})[0]
    assert event == {"start": at("01:00"), "duration": 100 * 60, "kind": "game", "title": "76ers at Pistons",
                     "live": True, "estimated": True, "episode_title": "NBA · YouTube TV · ESPN HD",
                     "description": "On until its final, and that time is an estimate."}


@pytest.mark.parametrize("source,origin", [
    ("nhl", "NHL · ESPN Unlimited"), ("sunday-ticket", "NHL · NFL Sunday Ticket"),
    ("thursday-night-football", "NHL · Prime Video"), ("mlb", "NHL")])
def test_a_games_channel_is_its_id_so_only_its_source_is_named(source, origin):
    event = parse_listings({"segments": [segment("01:00", "02:00", "Game", "game", game={"league": "nhl"},
                                                 target={"source_id": source, "channel_id": "401892649"})]})[0]
    assert event["episode_title"] == origin


def test_a_block_names_the_channel_it_takes_by_its_lineup_name():
    names = {("tv-thing-digital", "wjla_7_1"): "WJLA"}
    event = parse_listings({"segments": [segment("16:00", "17:00", "The View", target={
        "source_id": "tv-thing-digital", "channel_id": "wjla_7_1"})]},
        channel_name=lambda source, channel: names.get((source, channel)))[0]
    assert event["title"] == "The View"
    assert event["episode_title"] == "Antenna · WJLA"
    assert event["description"] == "From the weekly schedule."
    assert "live" not in event and "estimated" not in event


def test_the_stations_notes_reach_the_description():
    event = parse_listings({"segments": [segment("01:00", "02:00", "Tape", "recording", notes=["Instead of OFF AIR"],
                                                 target={"source_id": "library", "channel_id": "abc"})]})[0]
    assert event["description"] == "A recording from Up next. Instead of OFF AIR."
    assert "episode_title" not in event


def test_an_untitled_entry_goes_by_where_it_comes_from():
    event = parse_listings({"segments": [segment("01:00", "02:00", "", "manual")]})[0]
    assert event["title"] == "YouTube TV · NBC"


def test_a_block_whose_source_is_the_test_card_is_off_the_air_and_runs_into_what_follows():
    """The station's own projection names 11 hours "Jimmy Kimmel": the 30 minutes painted so,
    on the test card, ran together with the off-air hours after them."""
    events = parse_listings({"segments": [
        segment("04:00", "04:30", "News 10"),
        segment("04:30", "16:00", "Jimmy Kimmel", target=OFF),
        segment("16:00", "17:00", "OFF AIR", "off_air", target=OFF, source="queue"),
        segment("17:00", "18:00", "OFF AIR", "off_air", target=OFF)]})
    assert [(e["title"], e["kind"], e["start"], e["duration"]) for e in events] == [
        ("News 10", "block", at("04:00"), 1800),
        ("OFF AIR", "off_air", at("04:30"), int(13.5 * 3600))]
    assert events[1]["description"] == "The test card."


def test_a_segment_with_no_time_in_it_is_left_out():
    assert parse_listings({"segments": [segment("01:00", "01:00", "Nothing")]}) == []


def test_the_programme_on_air_starts_when_its_entry_went_on_not_at_the_projections_now():
    playback = {"segments": [segment("01:00", "02:40", "Game", "game", item="aired", estimated=True, game=GAME),
                             segment("02:40", "03:00", "Next", item="later")]}
    queue = {"items": [{"id": "later", "status": "queued", "completed_at": None},
                       {"id": "aired", "status": "succeeded", "completed_at": "2026-11-05T00:00:29+00:00"}]}
    game, following = parse_listings(playback, queue)
    assert (game["start"], game["start"] + game["duration"]) == (at("00:00") + 29, at("02:40"))
    assert following["start"] == at("02:40")


def test_the_test_card_on_air_with_no_entry_named_starts_at_the_last_entry_if_that_put_it_on():
    playback = {"segments": [segment("01:00", "02:00", "OFF AIR", "off_air", target=OFF)]}
    earlier = {"status": "succeeded", "completed_at": "2026-11-04T20:00:00+00:00", "target": OFF}
    latest = {"status": "succeeded", "completed_at": "2026-11-05T00:30:00+00:00", "target": OFF}
    assert parse_listings(playback, {"items": [earlier, latest]})[0]["start"] == at("00:30")
    # Something else aired last, so whatever put the test card on since is not in the queue.
    other = {"status": "succeeded", "completed_at": "2026-11-05T00:45:00+00:00",
             "target": {"source_id": "youtube-tv", "channel_id": "NBC"}}
    assert parse_listings(playback, {"items": [latest, other]})[0]["start"] == at("01:00")


def test_only_the_first_segment_can_be_on_air():
    playback = {"segments": [segment("01:00", "02:00", "First"), segment("02:00", "03:00", "Second", item="old")]}
    queue = {"items": [{"id": "old", "status": "succeeded", "completed_at": "2026-11-04T23:00:00+00:00"}]}
    assert [e["start"] for e in parse_listings(playback, queue)] == [at("01:00"), at("02:00")]


# ---------- reading it ----------

PLAYBACK = {"now": NOW, "paused": True, "pause_reason": "Restarting", "segments": [segment("01:00", "02:00", "News")]}


class Station:
    """A fake station: answers from ``answers``, or raises what it holds."""

    def __init__(self):
        self.answers = {"/v1/playback": PLAYBACK, "/v1/queue": {"items": []}}
        self.seen = []
        self.now = 1_800_000_000

    def open(self, request, timeout):
        path = request.full_url.removeprefix("http://station:6970")
        self.seen.append((path, timeout))
        answer = self.answers[path.split("?")[0]]
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer if isinstance(answer, bytes) else json.dumps(answer).encode())

    def listings(self):
        return StationListings("http://station:6970/", opener=self.open, clock=lambda: self.now)


def test_a_read_asks_a_week_ahead_with_a_short_timeout_and_says_how_it_went():
    station = Station()
    listing = station.listings().read()
    assert [e["title"] for e in listing["events"]] == ["News"]
    assert (listing["available"], listing["paused"], listing["pause_reason"]) == (True, True, "Restarting")
    assert (listing["generated_at"], listing["last_error"]) == (station.now, None)
    assert station.seen == [("/v1/playback?until=2027-01-22T08:00:00Z", 3), ("/v1/queue", 3)]


def test_reads_are_kept_for_a_while_so_the_guide_never_hammers_the_station():
    station, seen = Station(), 0
    listings = station.listings()
    listings.read()
    seen = len(station.seen)
    station.now += station_listings.CACHE_SECONDS - 1
    listings.read()
    assert len(station.seen) == seen
    station.now += 1
    listings.read()
    assert len(station.seen) == 2 * seen


@pytest.mark.parametrize("failure", [URLError("refused"), TimeoutError(), OSError("reset"), b"not json", b'{"segments": 1}'])
def test_an_outage_keeps_the_last_listing_a_while_and_is_not_retried_at_once(failure):
    station = Station()
    listings = station.listings()
    assert listings.read()["available"]
    station.answers["/v1/playback"] = failure
    station.now += station_listings.CACHE_SECONDS

    listing = listings.read()
    asked = len(station.seen)
    assert listing["available"] and [e["title"] for e in listing["events"]] == ["News"]
    assert "did not answer" in listing["last_error"]
    # A hung station would otherwise cost every guide its timeout.
    listings.read()
    assert len(station.seen) == asked

    station.now += station_listings.STALE_SECONDS
    gone = listings.read()
    assert (gone["available"], gone["events"], gone["paused"], gone["generated_at"]) == (False, [], False, None)
    assert "did not answer" in gone["last_error"]

    station.answers["/v1/playback"] = PLAYBACK
    station.now += station_listings.CACHE_SECONDS
    back = listings.read()
    assert back["available"] and back["last_error"] is None


def test_never_having_heard_from_the_station_lists_nothing():
    station = Station()
    station.answers["/v1/playback"] = URLError("refused")
    listing = station.listings().read()
    assert (listing["available"], listing["events"]) == (False, [])


def test_the_queue_not_answering_costs_only_when_the_programme_on_air_began():
    station = Station()
    station.answers["/v1/queue"] = URLError("refused")
    listing = station.listings().read()
    assert listing["available"] and listing["last_error"] is None
    assert listing["events"][0]["start"] == at("01:00")


def test_an_oversized_answer_is_refused(monkeypatch):
    monkeypatch.setattr(station_listings, "MAX_BYTES", 10)
    station = Station()
    assert not station.listings().read()["available"]
