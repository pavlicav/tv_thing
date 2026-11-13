"""The antenna's listings from the web (ota_epg.py): Gracenote's over-the-air grid continues
each channel where the broadcast's own twelve hours of guide end."""
import pytest

import ota_epg
import server

HOUR = 3600
NOW = 1794254400            # Mon 2026-11-09 15:00 EST


def iso(moment):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def airing(number, call_sign, start, hours, title, **program):
    return {"callSign": call_sign, "channelNo": number, "startTime": iso(start),
            "endTime": iso(start + hours * HOUR), "flag": [], "tags": [], "rating": None,
            "program": {"title": title, "episodeTitle": None, "shortDesc": None,
                        "season": None, "episode": None, **program}}


def grid(*channels):
    """``channels``: (number, call sign, station id, [airing...])."""
    return {"channels": [{"channelNo": number, "callSign": call, "channelId": station, "events": airings}
                         for number, call, station, airings in channels]}


ANTENNA = [{"number": "5.1", "name": "WTTG"}, {"number": "44.1", "name": "WZDC"},
           {"number": "44.2", "name": "XITOS"}, {"number": "49.4", "name": "NTDTV"},
           {"number": "10.1", "name": "WDCO"}]


# -- reading the grid -------------------------------------------------------------------------
def test_an_airing_in_the_guides_shape():
    found = ota_epg.parse_grid(grid(("5.1", "WTTGDT", "20367", [
        airing("5.1", "WTTGDT", NOW, 1, "FOX 5 News", episodeTitle="Evening", season="3",
               shortDesc="The news."),
        dict(airing("5.1", "WTTGDT", NOW + HOUR, 1, "NFL"), tags=["Live"], flag=["New"]),
        dict(airing("5.1", "WTTGDT", NOW, 1, "x"), endTime="junk")])))
    events = found["5.1"]["20367"]["events"]
    assert events[0] == {"start": NOW, "duration": HOUR, "title": "FOX 5 News", "captions": False,
                         "episode_title": "Evening", "description": "The news.", "season": 3}
    assert events[1]["live"] and events[1]["new"] and len(events) == 2


def test_nothing_readable_is_nothing():
    assert ota_epg.parse_grid(None) == {} and ota_epg.parse_grid({"channels": "x"}) == {}


def test_a_shared_number_is_matched_by_call_letters():
    found = ota_epg.parse_grid(grid(
        ("44.1", "WRCDT3", "61342", [airing("44.1", "WRCDT3", NOW, 1, "TeleXitos")]),
        ("44.1", "WZDCCD", "76363", [airing("44.1", "WZDCCD", NOW, 1, "La mesa caliente")]),
        ("44.2", "WZDCCD2", "107282", [airing("44.2", "WZDCCD2", NOW, 1, "El final")]),
        ("44.2", "WRCDT4", "1", [airing("44.2", "WRCDT4", NOW, 1, "Other")]),
        ("5.1", "WTTGDT", "20367", [airing("5.1", "WTTGDT", NOW, 1, "News")])))
    matched = ota_epg.match(found, ANTENNA)
    assert matched["44.1"][0] == "76363"
    assert matched["44.2"][0] == "107282"          # by its .1's name, WZDC
    assert matched["5.1"][0] == "20367"
    assert "10.1" not in matched


def test_a_shared_number_nothing_tells_apart_is_left_unlisted():
    found = ota_epg.parse_grid(grid(("49.4", "AAAA", "1", []), ("49.4", "BBBB", "2", [])))
    assert ota_epg.match(found, ANTENNA) == {}


# -- a read -----------------------------------------------------------------------------------
SETTINGS = {"url": "http://gn.test/api/grid", "lineup_id": "USA-OTA20036", "postal_code": "20036"}


def test_a_read_asks_six_hours_at_a_time_and_keeps_two_days():
    asked = []

    def fetch(settings, at):
        asked.append(at)
        return ota_epg.parse_grid(grid(("5.1", "WTTGDT", "20367", [
            airing("5.1", "WTTGDT", at + h * HOUR, 1, f"show {at + h * HOUR}") for h in range(6)])))
    guide = ota_epg.refresh(SETTINGS, ANTENNA, now=NOW + 600, fetch=fetch, sleep=lambda s: None)
    assert asked[0] == NOW and all(b - a == 6 * HOUR for a, b in zip(asked, asked[1:]))
    assert asked[-1] < NOW + 600 + 48 * HOUR <= asked[-1] + 6 * HOUR
    events = guide["channels"]["5.1"]["events"]
    assert events[0]["start"] == NOW and events[-1]["start"] < NOW + 600 + 48 * HOUR
    assert guide["channels"]["5.1"]["call_sign"] == "WTTGDT"
    assert guide["unlisted"] == ["10.1", "44.1", "44.2", "49.4"] and guide["failed_spans"] == []


def test_a_span_that_fails_leaves_a_gap_and_none_answering_fails():
    calls = []

    def flaky(settings, at):
        calls.append(at)
        if len(calls) == 2:
            raise OSError("timed out")
        return ota_epg.parse_grid(grid(("5.1", "WTTGDT", "20367", [airing("5.1", "WTTGDT", at, 6, "x")])))
    guide = ota_epg.refresh(SETTINGS, ANTENNA, now=NOW, fetch=flaky, sleep=lambda s: None)
    assert len(guide["failed_spans"]) == 1 and guide["channels"]["5.1"]["events"]

    def dead(settings, at):
        raise OSError("refused")
    with pytest.raises(RuntimeError, match="no span answered"):
        ota_epg.refresh(SETTINGS, ANTENNA, now=NOW, fetch=dead, sleep=lambda s: None)


def test_each_market_reads_its_own_zip_code():
    listings = ota_epg.load_listings()
    assert ota_epg.market_settings(listings, "dc")["lineup_id"] == "USA-OTA20036"
    assert ota_epg.market_settings(listings, "lansing")["postal_code"] == "48933"
    assert ota_epg.market_settings(listings, "nowhere") is None


def test_another_markets_listing_is_not_carried_over():
    stored = {"lineup_id": "USA-OTA48933", "channels": {"6.1": {"events": [
        {"start": NOW, "duration": HOUR, "title": "x"}]}}}
    fresh = {"lineup_id": "USA-OTA20036", "generated_at": NOW, "channels": {}}
    assert ota_epg.merge_guides(stored, fresh, NOW) is fresh


# -- the guide its readers see ----------------------------------------------------------------
def event(start, hours, title):
    return {"start": start, "duration": hours * HOUR, "title": title}


BROADCAST = {"generated_at": NOW - 12 * HOUR, "coverage_hours": 12.0, "horizon": NOW - HOUR,
             "channels": {
                 "5.1": {"number": "5.1", "name": "WTTG", "events": [event(NOW - 2 * HOUR, 1, "Own")]},
                 "49.4": {"number": "49.4", "name": "NTDTV", "events": []},
                 "7.1": {"number": "7.1", "name": "WJLA", "stale": True,
                         "events": [event(NOW, 2, "Own 7")]}}}
WEB = {"generated_at": NOW - HOUR, "lineup_id": "USA-OTA20036", "horizon": NOW + 48 * HOUR,
       "channels_with_events": 3, "unlisted": ["10.1"], "failed_spans": [],
       "channels": {
           "5.1": {"source": "gracenote", "station": "20367", "call_sign": "WTTGDT", "events": [
               event(NOW - 2 * HOUR, 1, "Web"), event(NOW - HOUR, 1, "Next"), event(NOW, 1, "Later")]},
           "49.4": {"source": "gracenote", "station": "1", "call_sign": "WWTDLD4",
                    "events": [event(NOW, 1, "NTD")]},
           "7.1": {"source": "gracenote", "events": [event(NOW + HOUR, 1, "Overlaps"),
                                                     event(NOW + 2 * HOUR, 1, "After")]},
           "4.1": {"source": "gracenote", "events": [event(NOW, 1, "Not in the broadcast")]}}}


def test_the_web_continues_each_channel_where_the_broadcast_ends():
    combined = ota_epg.combine(BROADCAST, WEB)
    wttg = combined["channels"]["5.1"]
    assert [e["title"] for e in wttg["events"]] == ["Own", "Next", "Later"]
    assert wttg["web_from"] == NOW - HOUR and wttg["listing"]["call_sign"] == "WTTGDT"
    assert [e["title"] for e in combined["channels"]["49.4"]["events"]] == ["NTD"]
    # Its own events stand; the web picks up after them, and it is not stale any more.
    wjla = combined["channels"]["7.1"]
    assert [e["title"] for e in wjla["events"]] == ["Own 7", "After"] and "stale" not in wjla
    assert combined["channels"]["4.1"]["number"] == "4.1"
    # Read time and coverage at the read stay the broadcast's; the horizon is the whole's.
    assert (combined["generated_at"], combined["coverage_hours"]) == (NOW - 12 * HOUR, 12.0)
    assert combined["horizon"] == NOW + 3 * HOUR and combined["web"]["generated_at"] == NOW - HOUR
    assert BROADCAST["channels"]["5.1"]["events"] == [event(NOW - 2 * HOUR, 1, "Own")]   # unchanged


def test_either_alone():
    assert ota_epg.combine(None, None) is None
    alone = ota_epg.combine(BROADCAST, None)
    assert alone["channels"]["5.1"]["events"] == BROADCAST["channels"]["5.1"]["events"] and alone["web"] is None
    web_only = ota_epg.combine(None, WEB)
    assert web_only["generated_at"] == NOW - HOUR and web_only["channels"]["5.1"]["events"][0]["title"] == "Web"


# -- in control -------------------------------------------------------------------------------
@pytest.fixture
def guides(monkeypatch):
    monkeypatch.setattr(server.epg, "load_guide", lambda: BROADCAST)
    monkeypatch.setattr(server.ota_epg, "load_guide", lambda: WEB)
    monkeypatch.setattr(server, "OTA_GUIDE_ENABLED", True)


def test_every_reader_sees_the_web_continuation(guides, monkeypatch):
    assert server.epg.upcoming(server.antenna_guide(), "49.4", at=NOW + 60)["title"] == "NTD"
    monkeypatch.setattr(server, "OTA_GUIDE_ENABLED", False)
    assert server.epg.upcoming(server.antenna_guide(), "49.4", at=NOW + 60) is None


def test_the_guide_page_shows_the_web_rows_and_summary(guides, monkeypatch):
    monkeypatch.setattr(server, "name_antenna_games", lambda rows: rows)
    payload = server.guide_payload(now=NOW)
    assert payload["channels"]["49.4"]["events"][0]["title"] == "NTD"
    assert payload["web"]["available"] and payload["web"]["unlisted"] == ["10.1"]
    assert payload["coverage_hours"] == 3.0


def test_a_read_stores_the_markets_listing(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "MARKET", "dc")
    monkeypatch.setattr(server, "load_channels", lambda: ANTENNA)
    monkeypatch.setattr(server.ota_epg, "fetch_grid", None)
    asked = []

    def refresh(settings, channels, **kw):
        asked.append(settings["lineup_id"])
        return dict(WEB, elapsed_seconds=1.0)
    monkeypatch.setattr(server.ota_epg, "refresh", refresh)
    assert server.run_ota_guide_refresh("test") is True
    assert asked == ["USA-OTA20036"] and server.ota_epg.load_guide()["channels"]["49.4"]
    monkeypatch.setattr(server, "MARKET", "nowhere")
    assert server.run_ota_guide_refresh("test") is False
    assert "no ZIP code" in server.ota_guide_status["last_error"]
