"""The channel registry (channels.py), /api/v1/channels, networks.json and roku.json's live
channel list. Simulated sources only: nothing tunes or touches hardware."""
import json
import os
import shutil
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import channels
import roku_lane
import server
import sports_guide
from roku_control import networks, sports
from roku_control.config import load_config

ANTENNA = [{"name": "WRC-HD", "id": "wrc_hd_4_1", "number": "4.1", "rf_channel": 34},
           {"name": "NHK-WLD", "id": "nhk_wld_22_4", "number": "22.4", "rf_channel": 21}]
GAME = {"id": "823254", "name": "Phillies at Braves", "start_time": "2027-01-15T23:00Z",
        "state": "preview", "status": "Scheduled", "playable": True}
# /api/v1/channels' contract: every channel has these, and nothing else at the top.
VIEW = {"id", "source_id", "channel_id", "number", "name", "section", "kind", "lane", "node",
        "network", "market", "tune", "stream", "listings", "detail"}


@pytest.fixture
def lineup_sources(monkeypatch):
    """The antenna's WRC and NHK, YouTube TV's NBC and CNN, a baseball game on 2027-01-15, and
    an NHL schedule that does not answer. Internet streams and the station are the checkout's."""
    def catalog(source_id, requested_date=None):
        if source_id == "youtube-tv":
            return {"items": [{"id": name, "name": name, "number": None, "aliases": []}
                              for name in ("NBC", "CNN")]}
        if source_id == "nhl":
            raise RuntimeError("NHL schedule timed out")
        return {"date": requested_date or "2027-01-15",
                "items": [dict(GAME)] if source_id == "mlb" else []}

    monkeypatch.setattr(server, "roku", SimpleNamespace(
        catalog=catalog, source_list=lambda available: roku_lane.RokuLane.source_list(None, available),
        listed_source_list=lambda available: []))
    monkeypatch.setattr(server, "roku_sources_available", lambda: True)
    monkeypatch.setattr(server, "load_channels", lambda: [dict(channel) for channel in ANTENNA])


def test_every_kind_of_channel_is_one_shape_with_what_plays_it(lineup_sources):
    payload = server.api_v1_channels("2027-01-15")
    listed = payload["channels"]
    assert all(set(channel) == VIEW for channel in listed)
    by_id = {channel["id"]: channel for channel in listed}
    node = server.FEED_CONFIG.node

    assert by_id["digital/4.1"] | {"detail": None} == {
        "id": "digital/4.1", "source_id": "digital", "channel_id": "wrc_hd_4_1", "number": 4,
        "name": by_id["digital/4.1"]["name"], "section": "Local stations", "kind": "digital",
        "lane": "digital", "node": node, "network": "NBC", "market": "dc",
        "tune": {"source": "digital", "channel_id": "wrc_hd_4_1"}, "stream": "/stream/digital.m3u8",
        "listings": "4.1", "detail": None}
    assert by_id["digital/4.1"]["detail"] == {"source_name": "WRC-HD", "antenna_number": "4.1",
                                              "rf_channel": 34}
    assert by_id["digital/22.4"]["network"] is None

    nbc = by_id["youtube-tv/NBC"]
    assert (nbc["lane"], nbc["network"], nbc["market"], nbc["listings"]) == (
        "roku", "NBC", "lansing", "youtube-tv/NBC")
    assert nbc["tune"] == {"source": "youtube-tv", "channel_id": "NBC"}
    assert by_id["youtube-tv/CNN"]["network"] is None

    game = by_id["mlb/823254"]
    assert 61 <= game["number"] <= 69 and game["listings"] == f"games/{game['number']}"
    assert game["tune"] == {"source": "mlb", "channel_id": "823254", "date": "2027-01-15"}
    assert game["detail"]["start_time"] == "2027-01-15T23:00Z" and game["detail"]["playable"]

    puppies = by_id["internet/pentulive"]
    assert (puppies["kind"], puppies["lane"], puppies["tune"], puppies["node"]) == ("internet", None, None, node)
    assert puppies["stream"] == "/stream/internet/pentulive/index.m3u8"
    assert [c["number"] for c in listed if c["id"] == "vizelvision"] == [0, 70]
    guide = by_id["vizelvision/sports-guide"]
    assert (guide["kind"], guide["number"], guide["stream"]) == ("slides", 60, "/stream/sports-guide/index.m3u8")

    # The station holds two numbers under one id; nothing else repeats.
    ids = [channel["id"] for channel in listed if channel["id"] != "vizelvision"]
    assert len(ids) == len(set(ids))
    numbers = [channel["number"] for channel in listed]
    assert numbers == sorted(numbers)
    assert payload["date"] == "2027-01-15"
    assert payload["errors"] == [{"source_id": "nhl", "message": "NHL schedule timed out"}]
    # Every tune a channel offers is one the API takes.
    assert all(server.CHANNELS.tuner(c["tune"]["source"])[0] for c in listed if c["tune"])


def test_the_lineup_is_the_same_channels_each_in_its_kinds_row(lineup_sources):
    rows = server.api_v1_lineup("2027-01-15")["channels"]
    listed = server.api_v1_channels("2027-01-15")["channels"]
    assert [row["tv_number"] for row in rows] == [channel["number"] for channel in listed]
    wrc = next(row for row in rows if row.get("channel_id") == "wrc_hd_4_1")
    assert wrc == {"tv_number": 4, "tv_name": wrc["tv_name"], "tv_section": "Local stations",
                   "kind": "digital", "source_id": "digital", "channel_id": "wrc_hd_4_1",
                   "name": "WRC-HD", "antenna_number": "4.1", "rf_channel": 34}
    game = next(row for row in rows if row.get("channel_id") == "823254")
    assert game["kind"] == "roku" and game["date"] == "2027-01-15" and game["source_id"] == "mlb"


def test_the_channel_list_is_served_indexed_and_linked(lineup_sources):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.TVHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}{path}", timeout=5) as response:
            return response.status, json.load(response)
    try:
        status, payload = get("/api/v1/channels?date=2027-01-15")
        assert status == 200 and payload["date"] == "2027-01-15"
        assert any(channel["id"] == "mlb/823254" for channel in payload["channels"])
        assert get("/api/v1")[1]["endpoints"]["channels"]["path"] == "/api/v1/channels"
    finally:
        httpd.shutdown()
        httpd.server_close()
    catalog = server.api_v1_source_catalog()
    assert catalog["channels_path"] == "/api/v1/channels"
    assert [source["id"] for source in catalog["sources"]][:2] == ["digital", "cable"]
    assert all({"lane", "kind"} <= set(source) for source in catalog["sources"])
    assert set(catalog["lineup"]) == {"path", "sections", "house", "slides", "internet"}


@pytest.mark.parametrize("source, performer, lane", [
    ("digital", "perform_digital_tune", "digital"), ("tv", "perform_digital_tune", "digital"),
    ("cable", "perform_cable_tune", "analog"), ("NTSC", "perform_cable_tune", "analog")])
def test_a_tune_goes_to_the_adapter_that_tunes_its_source(monkeypatch, source, performer, lane):
    asked = []
    monkeypatch.setattr(server, performer, lambda *args, **kwargs: (
        asked.append((args, kwargs)), ({"error": "Tuner busy"}, 409))[1])
    payload, status = server.api_v1_tune({"source": source, "channel_id": "wrc_hd_4_1", "channel": 7,
                                          "token": "t", "override": True, "quality": "high"})
    assert status == 409 and payload["error"]["message"] == "Tuner busy"
    (args, kwargs), = asked
    assert args == ("wrc_hd_4_1" if lane == "digital" else 7, "high")
    assert kwargs == {"token": "t", "override": True}


def test_a_roku_tune_goes_to_the_roku_with_its_source(monkeypatch):
    asked = []
    monkeypatch.setattr(server, "perform_roku_tune", lambda body: (asked.append(body), ({"ok": True}, 202))[1])
    assert server.api_v1_tune({"source": "YouTube-TV", "channel_id": "NBC"}) == ({"ok": True}, 202)
    assert asked == [{"source": "youtube-tv", "channel_id": "NBC"}]


def test_a_tune_names_every_source_it_could_have_been():
    payload, status = server.api_v1_tune({"source": "betamax"})
    assert status == 400 and payload["error"]["code"] == "invalid_source"
    assert payload["error"]["message"] == ("source must be digital, cable, or a Roku source ("
                                           + ", ".join(roku_lane.SOURCES) + ")")
    payload, status = server.api_v1_tune({"source": "digital", "quality": "8k"})
    assert (status, payload["error"]["code"]) == (400, "invalid_quality")


def test_the_registry_merges_its_adapters_in_order():
    class One(channels.Adapter):
        kind, tunes, tune_label = "one", ("one",), "one"

        def catalog(self):
            return {"sources": [{"id": "one"}], "lineup": {"ones": [1]}}

        def guide(self, window):
            return {"one/1": {"events": []}}, {"available": True}

    class Two(channels.Adapter):
        kind, tunes, aliases, tune_label = "two", ("two",), {"deux": "two"}, "two"

        def guide(self, window):
            return {"two/1": {"events": []}}, {"two": None}

    registry = channels.Registry([One(None), Two(None)])
    assert registry.catalog() == {"sources": [{"id": "one"}], "listed": [], "lineup": {"ones": [1]}}
    assert registry.guide(channels.GuideWindow(0, 0)) == (
        {"one/1": {"events": []}, "two/1": {"events": []}}, {"available": True, "two": None})
    assert registry.tuner("deux")[1] == "two" and registry.tuner("three") == (None, "three")
    assert registry.tune_choices() == "one, or two"


def test_a_guide_window_keeps_what_overlaps_it_and_asks_only_for_its_row():
    window = channels.GuideWindow(now=1000, floor=0, ceiling=2000, number="games/61")
    events = [{"start": -100, "duration": 50}, {"start": -10, "duration": 20},
              {"start": 1999, "duration": 5}, {"start": 2000, "duration": 5}]
    assert window.current(events) == events[1:3]
    assert window.wants("games/61") and not window.wants("4.1")
    assert window.may_want("games/") and not window.may_want("internet/")
    assert channels.GuideWindow(1000, 0).may_want("internet/")


# -- networks.json --------------------------------------------------------------

def test_networks_json_places_each_market_and_its_stations():
    found = networks.load()
    assert (found.antenna.id, found.youtube_tv.id) == ("dc", "lansing")
    assert found.antenna_channel("4.1") == ("NBC", "dc")
    assert found.antenna_channel("54.1") == ("CW", None)     # Baltimore's CW: an affiliate
    assert found.antenna_channel("22.4") is None
    assert found.youtube_tv_row(("The CW",)) == ("CW", "lansing")
    assert found.youtube_tv_row(("CNN",)) is None


def test_the_sports_tables_come_from_networks_json_as_they_were_written():
    # The values the sports code had as constants before networks.json held them.
    assert sports_guide.NETWORK_STATIONS == {
        "abc": ("ABC", "7.1"), "cbs": ("CBS", "9.1"), "nbc": ("NBC", "4.1"), "fox": ("FOX", "5.1"),
        "cw": ("CW", "50.1"), "thecw": ("CW", "50.1"), "ion": ("ION", "66.1"),
        "univision": ("Univision", "14.1"), "telemundo": ("Telemundo", "44.1")}
    assert sports_guide.AFFILIATES == {"54.1": "CW"}
    assert (sports.LOCAL_NFL_TEAMS, sports.YOUTUBE_TV_NFL_TEAMS) == ({"WSH", "BAL"}, {"DET"})
    assert sports.ANTENNA_STATIONS == {"CBS": "WUSA", "FOX": "FOX 5"}
    assert sports.LOCAL_SIMULCASTS == {"WSH": {"NFL Network": "WTTG"}}
    assert sports.NHL_LOCAL_TEAMS == {"WSH": "DC", "DET": "Lansing"}
    assert (sports.ANTENNA_MARKET.name, sports.YOUTUBE_TV_MARKET.name) == ("Washington", "Lansing")


@pytest.mark.parametrize("document, complaint", [
    ({}, "needs its markets"),
    ({"markets": {"dc": {"name": "Washington", "label": "DC"}}, "antenna_market": "dc",
      "youtube_tv_market": "detroit"}, "youtube_tv_market must name one of the markets (dc)"),
    ({"markets": {"dc": {"name": "Washington", "label": "DC", "stations": {"NBC": {}}}}},
     "markets.dc.stations.NBC needs a call_sign"),
    ({"markets": {"dc": {"name": "Washington"}}}, "markets.dc needs a name and a label"),
])
def test_a_networks_file_that_does_not_describe_the_markets_says_why(document, complaint):
    with pytest.raises(networks.NetworksError, match=complaint.replace("(", r"\(").replace(")", r"\)")):
        networks.parse(document)


# -- roku.json's channel list, live ------------------------------------------------

@pytest.fixture
def roku_file(tmp_path):
    """A Roku lane built from a copy of the checkout's roku.json, which reloads it."""
    path = tmp_path / "roku.json"
    shutil.copy(Path(server.BASE_DIR) / "roku.json", path)
    config = load_config(path)
    holder = lambda: SimpleNamespace(config=config)   # noqa: E731
    controller = SimpleNamespace(config=config, feedback=None, ecp=None)
    lane = roku_lane.RokuLane(config, controller, baseball=holder(), sports=holder(),
                              capture_source=lambda: {"stream_id": "cap-1"}, config_path=path)
    return lane, path


def _add_row(path, name):
    document = json.loads(path.read_text())
    last = max(row["guide_index"] for row in document["channels"])
    document["channels"].append({"name": name, "aliases": [], "guide_index": last + 1})
    path.write_text(json.dumps(document))
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def test_a_row_added_to_roku_json_is_taken_up_without_a_restart(roku_file):
    lane, path = roku_file
    assert "Wedding Channel" not in [item["id"] for item in lane.catalog("youtube-tv")["items"]]
    _add_row(path, "Wedding Channel")
    assert [item["id"] for item in lane.catalog("youtube-tv")["items"]][-1] == "Wedding Channel"
    for holder in (lane, lane.controller, lane.baseball, lane.sports):
        assert holder.config.resolve_channel("wedding channel")[1].name == "Wedding Channel"
    assert lane.normalize({"source": "youtube-tv", "channel_id": "Wedding Channel"})[0] == {
        "source_id": "youtube-tv", "channel_id": "Wedding Channel"}
    assert lane.reload_channels() is False      # nothing new since


def test_the_channel_list_waits_for_a_navigation_to_end(roku_file):
    lane, path = roku_file
    before = lane.config.channels
    lane.operation = {"state": "running"}
    _add_row(path, "Wedding Channel")
    assert lane.reload_channels() is False and lane.config.channels is before
    lane.operation = {"state": "verified"}
    assert lane.reload_channels() is True and lane.config.channels[-1].name == "Wedding Channel"


def test_a_roku_json_that_does_not_load_keeps_the_list_there_is(roku_file, capsys):
    lane, path = roku_file
    before = lane.config.channels
    path.write_text("{ not json")
    assert lane.reload_channels() is False and lane.config.channels is before
    assert "does not load" in capsys.readouterr().out
    assert lane.reload_channels() is False and capsys.readouterr().out == ""   # said once
