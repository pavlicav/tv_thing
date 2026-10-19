"""YouTube TV listings from the web: parsers, the refresh and the map. No network."""
import gzip
import json

import pytest

import yttv_epg

NOW = 1_800_000_000


def gracenote_answer(*airings):
    """An api/sslgrid answer: airings grouped by day, as the endpoint sends them."""
    return {"2027-01-15": [dict(airing) for airing in airings]}


def airing(start, minutes, title, **extra):
    return {"startTime": start, "endTime": start + minutes * 60,
            "tag": extra.pop("tag", []), "flag": extra.pop("flag", []), "rating": extra.pop("rating", None),
            "program": {"title": title, "episodeTitle": extra.get("episode_title"),
                        "shortDesc": extra.get("description"), "season": extra.get("season"),
                        "episode": extra.get("episode")}}


XMLTV = b"""<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="abc123"><display-name>Stories by AMC</display-name></channel>
  <channel id="zzz"><display-name>Somebody Else</display-name></channel>
  <programme channel="abc123" start="20270115080000 +0000" stop="20270115090000 +0000">
    <title>Creepshow</title><sub-title>The Companion</sub-title><desc>A boy hides.</desc>
    <episode-num system="xmltv_ns">1.3.</episode-num><new/>
  </programme>
  <programme channel="zzz" start="20270115080000 +0000" stop="20270115090000 +0000">
    <title>Not wanted</title>
  </programme>
  <programme channel="abc123" start="20270115090000 +0000" stop="20270115093000 +0000">
    <title>Into the Badlands</title>
  </programme>
</tv>"""


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def test_gracenote_airings_become_events_with_what_a_box_guide_shows():
    events = yttv_epg.parse_gracenote(gracenote_answer(
        airing(NOW, 30, "NFL Football", episode_title="Cowboys at Packers",
               description="Week 6.", tag=["CC", "Live"], flag=["New"], rating="TV-PG"),
        airing(NOW + 1800, 60, "Seinfeld", season="4", episode="11")))
    assert events[0] == {"start": NOW, "duration": 1800, "title": "NFL Football",
                         "captions": True, "episode_title": "Cowboys at Packers",
                         "description": "Week 6.", "new": True, "live": True, "rating": "TV-PG"}
    # Empty fields are left out of the stored guide.
    assert events[1] == {"start": NOW + 1800, "duration": 3600, "title": "Seinfeld",
                         "captions": False, "season": 4, "episode": 11}


def test_gracenote_drops_broken_airings_and_keeps_the_order():
    events = yttv_epg.parse_gracenote(gracenote_answer(
        airing(NOW + 3600, 30, "Later"),
        {"startTime": NOW, "endTime": NOW, "program": {"title": "Zero length"}},
        {"startTime": "soon", "endTime": NOW + 60, "program": {}},
        airing(NOW, 60, "First")))
    assert [e["title"] for e in events] == ["First", "Later"]
    assert yttv_epg.parse_gracenote({"error": "Bad request"}) == []
    assert yttv_epg.parse_gracenote(None) == []


def test_xmltv_keeps_only_the_wanted_channels():
    found = yttv_epg.parse_xmltv(XMLTV, {"Stories by AMC", "Missing Channel"})
    assert set(found) == {"Stories by AMC", "Missing Channel"}
    assert found["Missing Channel"] == []
    first, second = found["Stories by AMC"]
    # NOW is 2027-01-15 08:00 UTC, when the sample's first programme starts.
    assert first == {"start": NOW, "duration": 3600,
                     "title": "Creepshow", "captions": False, "episode_title": "The Companion",
                     "description": "A boy hides.", "season": 2, "episode": 4, "new": True}
    assert second["title"] == "Into the Badlands" and second["duration"] == 1800
    assert second["start"] == NOW + 3600


def test_xmltv_times_carry_their_offset():
    assert yttv_epg._xmltv_time("20270115080000 +0000") == yttv_epg._xmltv_time("20270115030000 -0500")
    assert yttv_epg._xmltv_time("20270115080000") == yttv_epg._xmltv_time("20270115080000 +0000")
    assert yttv_epg._xmltv_time("tomorrow") is None


def test_tidy_drops_overlaps_and_duplicates():
    events = yttv_epg.tidy([
        {"start": NOW + 1800, "duration": 1800, "title": "B"},
        {"start": NOW, "duration": 1800, "title": "A"},
        {"start": NOW, "duration": 600, "title": "A again, shorter"},
        {"start": NOW + 1900, "duration": 600, "title": "inside B"},
    ])
    assert [e["title"] for e in events] == ["A", "B"]


def test_window_keeps_what_is_on_and_the_hours_ahead():
    events = [{"start": NOW - 7200, "duration": 3600}, {"start": NOW - 600, "duration": 1800},
              {"start": NOW + 47 * 3600, "duration": 3600}, {"start": NOW + 49 * 3600, "duration": 60}]
    assert [e["start"] for e in yttv_epg.window(events, NOW, 48)] == [NOW - 600, NOW + 47 * 3600]


# ---------------------------------------------------------------------------
# The refresh
# ---------------------------------------------------------------------------

LISTINGS = {
    "gracenote": {"url": "https://example.invalid/api/sslgrid", "lineup_id": "L",
                  "headend_id": "H", "postal_code": "00000"},
    "xmltv": {"url": "https://example.invalid/{feed}.xml.gz"},
    "channels": {
        "NBC": {"gracenote": 35543, "call_sign": "WILXDT"},
        "CNN": {"gracenote": 10142},
        "Stories by AMC": {"xmltv": "Plex/us", "channel": "Stories by AMC"},
        "Local Now": None,
    },
}


def test_refresh_reads_each_row_from_its_source_a_second_apart():
    asked, slept = [], []

    def gracenote(settings, station, now):
        asked.append(station)
        return [{"start": NOW - 60, "duration": 1800, "title": f"On {station}"},
                {"start": NOW - 7200, "duration": 600, "title": "Long gone"}]

    def xmltv(settings, feed):
        assert settings["url"].endswith("{feed}.xml.gz") and feed == "Plex/us"
        return XMLTV

    guide = yttv_epg.refresh(LISTINGS, ["NBC", "CNN", "Stories by AMC", "Local Now"], NOW,
                             gracenote=gracenote, xmltv=xmltv, sleep=slept.append)
    assert asked == [35543, 10142] and slept == [yttv_epg.REQUEST_GAP_SECONDS]
    assert guide["channels"]["NBC"]["station"] == 35543
    assert guide["channels"]["NBC"]["call_sign"] == "WILXDT"
    assert [e["title"] for e in guide["channels"]["NBC"]["events"]] == ["On 35543"]
    assert guide["channels"]["Stories by AMC"]["feed"] == "Plex/us"
    assert guide["unmapped"] == ["Local Now"] and guide["requested"] == 3
    assert guide["errors"] == {}
    assert guide["channels_with_events"] == 3


def test_one_failing_source_does_not_sink_the_rest():
    def gracenote(settings, station, now):
        if station == 10142:
            raise OSError("timed out")
        return [{"start": NOW, "duration": 1800, "title": "News"}]

    def xmltv(settings, feed):
        raise OSError("HTTP Error 503")

    guide = yttv_epg.refresh(LISTINGS, ["NBC", "CNN", "Stories by AMC"], NOW,
                             gracenote=gracenote, xmltv=xmltv, sleep=lambda _: None)
    assert set(guide["channels"]) == {"NBC"}
    assert "timed out" in guide["errors"]["CNN"]
    assert "503" in guide["errors"]["Stories by AMC"]


def test_a_feed_that_no_longer_lists_a_channel_is_an_error_not_an_empty_row():
    guide = yttv_epg.refresh(LISTINGS, ["Stories by AMC"], NOW,
                             xmltv=lambda settings, feed: XMLTV.replace(b"Stories by AMC", b"Renamed"))
    assert guide["channels"] == {}
    assert "lists nothing" in guide["errors"]["Stories by AMC"]


def test_a_failed_row_keeps_its_last_listing_marked_stale():
    stored = {"generated_at": NOW - 86400, "channels": {
        "CNN": {"name": "CNN", "source": "gracenote", "station": 10142,
                "events": [{"start": NOW - 600, "duration": 3600, "title": "Still on"},
                           {"start": NOW - 9000, "duration": 600, "title": "Over"}]}}}
    fresh = {"generated_at": NOW, "channels": {
        "NBC": {"name": "NBC", "events": [{"start": NOW, "duration": 1800, "title": "New"}]}}}
    merged = yttv_epg.merge_guides(stored, fresh, NOW)
    assert merged["channels"]["CNN"]["stale"] is True
    assert [e["title"] for e in merged["channels"]["CNN"]["events"]] == ["Still on"]
    assert "stale" not in merged["channels"]["NBC"]
    assert merged["channels_with_events"] == 2


def test_fetch_xmltv_unpacks_a_gzipped_feed(monkeypatch):
    monkeypatch.setattr(yttv_epg, "_read", lambda request: gzip.compress(XMLTV))
    assert yttv_epg.fetch_xmltv(LISTINGS["xmltv"], "Plex/us") == XMLTV


def test_fetch_gracenote_asks_for_the_station_over_the_full_span(monkeypatch):
    sent = {}

    def read(request):
        sent.update(url=request.full_url, body=json.loads(request.data))
        return json.dumps(gracenote_answer(airing(NOW, 30, "SportsCenter"))).encode()

    monkeypatch.setattr(yttv_epg, "_read", read)
    events = yttv_epg.fetch_gracenote(LISTINGS["gracenote"], 10179, NOW)
    assert events[0]["title"] == "SportsCenter"
    assert sent["url"] == "https://example.invalid/api/sslgrid"
    assert sent["body"]["prgsvcid"] == "10179" and sent["body"]["timespan"] == "336"
    assert sent["body"]["lineupId"] == "L" and sent["body"]["timestamp"] == str(NOW)


# ---------------------------------------------------------------------------
# Storage and the map
# ---------------------------------------------------------------------------

def test_the_stored_guide_round_trips_and_is_reread_only_when_it_changes(tmp_path):
    path = tmp_path / "yttv_epg.json"
    assert yttv_epg.load_guide(path) is None
    yttv_epg.save_guide({"generated_at": NOW, "channels": {}}, path)
    first = yttv_epg.load_guide(path)
    assert first == {"generated_at": NOW, "channels": {}}
    assert yttv_epg.load_guide(path) is first
    yttv_epg.save_guide({"generated_at": NOW + 1, "channels": {}}, path)
    assert yttv_epg.load_guide(path)["generated_at"] == NOW + 1


def test_listings_validation_names_the_problem(tmp_path):
    path = tmp_path / "listings.json"

    def check(document):
        path.write_text(json.dumps(document))
        with pytest.raises(yttv_epg.ListingsError) as raised:
            yttv_epg.load_listings(path)
        return str(raised.value)

    assert "channels" in check({"gracenote": LISTINGS["gracenote"]})
    assert "whole number" in check({**LISTINGS, "channels": {"NBC": {"gracenote": "35543"}}})
    assert "xmltv feed" in check({**LISTINGS, "channels": {"X": {"xmltv": "Plex/us"}}})
    assert "postal_code" in check({**LISTINGS, "gracenote": {"url": "u", "lineup_id": "l",
                                                             "headend_id": "h"}})
    assert "{feed}" in check({**LISTINGS, "xmltv": {"url": "https://example.invalid/x.xml"}})
    path.write_text("{")
    with pytest.raises(yttv_epg.ListingsError):
        yttv_epg.load_listings(path)


def test_every_youtube_tv_row_has_a_listing_or_says_it_has_none():
    """A row added to roku.json needs a decision here: a source, or null."""
    listings = yttv_epg.load_listings()
    rows = yttv_epg.roku_channel_names()
    assert set(rows) == set(listings["channels"]), (
        "roku.json and yttv_listings.json list different YouTube TV rows")
    stations = [entry["gracenote"] for entry in listings["channels"].values()
                if entry and "gracenote" in entry]
    assert len(stations) == len(set(stations)), "two rows read the same Gracenote station"
