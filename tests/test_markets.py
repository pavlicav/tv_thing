"""One dial across cities (Phase B): each box owns its market's plan and serves it; the dial
is the home market's channels then each other market's (server.antenna_dial); only a box in a
station's market plays it (tuner_pool.in_market)."""
import json
from pathlib import Path

import pytest

import host_config
import lineup
import server
import tuner_pool

DC = [
    {"id": "wrc_hd_4_1", "number": "4.1", "name": "WRC-HD", "rf_channel": 34, "tsid": 529, "service_id": 3},
    {"id": "wttg_5_1", "number": "5.1", "name": "WTTG", "rf_channel": 36, "tsid": 531, "service_id": 3},
]
# Lansing's own plan: WLNS on RF 36, the RF WTTG has in DC.
LANSING = [{"id": "wlns_6_1", "number": "6.1", "name": "WLNS", "rf_channel": 36, "tsid": 1234, "service_id": 1}]


class Peer:
    def __init__(self, plan, tuners=()):
        self._plan, self._tuners, self.sent = plan, list(tuners), []
        self.reachable, self.last_error = True, None
        self.listed = {}

    def plan(self):
        return self._plan

    def guide(self):
        return self.listed

    def streams(self):
        return {}

    def stream_guide(self):
        return {}

    def node(self):
        return self._plan["node"]

    def location(self):
        return None

    def tuners(self):
        return self._tuners

    def refresh(self):
        return True

    def tune(self, body):
        self.sent.append(body)
        stream = {"id": "g1", "active": True, "ready": True, "playlist_path": "/stream/peer/digital.m3u8"}
        # Its listing shows the tune it took (server.relayed_tune_dropped reads it).
        self._tuners = [dict(tuner, selection={"id": body["channel_id"]}, stream=stream)
                        if tuner["id"] == body.get("tuner") else tuner for tuner in self._tuners]
        return {"ok": True, "lane": "digital", "stream": stream}, 200


def served(rows, numbers):
    """Rows as their owner's /api/v1/plan gives them: keyed and numbered."""
    return [dict(row, key=row["number"], tv_number=number, tv_name=row["name"]) for row, number in zip(rows, numbers)]


@pytest.fixture
def numbering(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "channel_lineup", lineup.Lineup(server.LINEUP_FILE, tmp_path / "lineup_state.json"))


@pytest.fixture
def home(monkeypatch, numbering):
    """viviserv in DC, the primary; its peer in Lansing."""
    monkeypatch.setattr(server, "MARKET", "dc")
    monkeypatch.setattr(server, "load_channels", lambda: [dict(row) for row in DC])
    peer = Peer({"node": "viviserv2", "market": "lansing", "primary": False, "revision": "r",
                 "rows": served(LANSING + [DC[1]], [400, 401])})
    monkeypatch.setattr(server, "peer", peer)
    return peer


@pytest.fixture
def lansing(monkeypatch, tmp_path, numbering):
    """viviserv2 in Lansing, a second node; the primary in DC."""
    monkeypatch.setattr(server.HOST, "_cache", {**server.HOST._cache,
                                                "role": host_config.Resolved("second", "auto", "")})
    monkeypatch.setattr(server, "MARKET", "lansing")
    away = tmp_path / "channels.json"
    away.write_text(json.dumps(LANSING))
    monkeypatch.setattr(server, "AWAY_CHANNELS_FILE", away)
    # (Under the suite this box is feed.json's viviserv, so the primary goes by another name.)
    peer = Peer({"node": "dc-primary", "market": "dc", "primary": True, "revision": "r",
                 "rows": served(DC, [14, 15])})
    monkeypatch.setattr(server, "peer", peer)
    return away


def test_the_dial_is_the_home_markets_then_the_other_markets_own(home):
    dial = server.antenna_dial()
    assert [(source, key, row["id"]) for source, key, row in dial] == [
        ("digital", "4.1", "wrc_hd_4_1"), ("digital", "5.1", "wttg_5_1"),
        ("digital@lansing", "6.1", "lansing/wlns_6_1")]
    lansing_row = dial[2][2]
    assert lansing_row["market"] == "lansing" and lansing_row["node"] == "viviserv2"
    assert lansing_row["tv_number"] == 400      # its owner's number
    assert dial[0][2]["market"] == "dc" and "node" not in dial[0][2]


def test_one_station_carried_by_both_markets_is_one_channel(home):
    """Lansing's plan listing WTTG (same TSID and service) adds no second WTTG."""
    assert [row["id"] for _, _, row in server.antenna_dial()].count("wttg_5_1") == 1
    assert "lansing/wttg_5_1" not in [row["id"] for _, _, row in server.antenna_dial()]


def test_the_lineup_lists_the_other_markets_channels(home):
    by_id = {channel.id: channel for channel in server.CHANNELS.channels()}
    wlns = by_id["digital@lansing/6.1"]
    assert (wlns.channel_id, wlns.number, wlns.market, wlns.node, wlns.stream, wlns.listings) == (
        "lansing/wlns_6_1", 400, "lansing", "viviserv2", None, "digital@lansing/6.1")
    assert by_id["digital/4.1"].listings == "4.1"       # this box's own antenna's guide
    assert wlns.detail["antenna_market"] == "lansing"


def test_only_a_tuner_in_the_stations_market_plays_it(home):
    here = {"id": "viviserv:digital", "kind": "digital", "local": True, "market": "dc", "available": True,
            "reachable": True, "rf_channels": None, "stream": {"active": True}}
    there = dict(here, id="viviserv2:digital", local=False, market="lansing", rf_channels=[36],
                 tsids={"36": 1234})
    wlns, wttg = server.dial_channel("lansing/wlns_6_1"), server.dial_channel("wttg_5_1")
    # The primary never scanned gets everything by RF, but not another city's station.
    assert tuner_pool.choose([here, there], wlns)[0] is there
    assert tuner_pool.choose([here, there], wttg)[0] is here
    assert tuner_pool.in_market({"id": "old"}, wlns)        # a box from before markets


def test_another_markets_channel_is_relayed_from_its_owner(home, monkeypatch):
    here = {"id": "viviserv:digital", "node": "viviserv", "lane": "digital", "kind": "digital", "local": True,
            "market": "dc", "available": True, "reachable": True, "rf_channels": None, "stream": {"active": False}}
    there = dict(here, id="viviserv2:digital", node="viviserv2", local=False, market="lansing")
    home._tuners = [there]
    monkeypatch.setattr(server, "local_tuners", lambda: [here])
    monkeypatch.setattr(server, "RELAY_CONFIRM_POLL_SECONDS", 0)
    payload, status = server.api_v1_digital_tune({"channel_id": "lansing/wlns_6_1"}, "low")
    assert status == 200 and payload["relayed"] and payload["tuner"] == "viviserv2:digital"
    assert home.sent[0]["channel_id"] == "lansing/wlns_6_1"


def test_the_digital_catalog_lists_this_boxs_own_then_the_other_markets_relayed(home):
    catalog = server.api_v1_digital_source()
    assert [(item["id"], item.get("relayed", False), item["market"]) for item in catalog["items"]] == [
        ("wrc_hd_4_1", False, "dc"), ("wttg_5_1", False, "dc"), ("lansing/wlns_6_1", True, "lansing")]
    wlns = catalog["items"][2]
    assert (wlns["node"], wlns["market_name"], wlns["tv_number"]) == ("viviserv2", "Lansing", 400)
    assert wlns["listings"] == "digital@lansing/6.1" and "listings" not in catalog["items"][0]
    assert catalog["source"]["count"] == 3


def test_away_the_catalog_keeps_this_boxs_own_ids_and_adds_the_primarys_relayed(lansing):
    items = server.api_v1_digital_source()["items"]
    # Its own as its plan has them (its recorder and guide know them so), then DC's.
    assert [(item["id"], item.get("relayed", False)) for item in items] == [
        ("wlns_6_1", False), ("wrc_hd_4_1", True), ("wttg_5_1", True)]
    assert (items[1]["market_name"], items[1]["node"], items[1]["tv_number"]) == ("Washington", "dc-primary", 14)


def test_away_with_no_box_in_the_home_market_its_channels_are_not_listed(lansing, monkeypatch):
    """The tracked plan stands in on the dial, but no tuner could play it."""
    monkeypatch.setattr(server, "peer", None)
    assert [item["id"] for item in server.api_v1_digital_source()["items"]] == ["wlns_6_1"]


def test_away_the_home_markets_channel_is_relayed_from_the_primary(lansing, monkeypatch):
    here = {"id": "viviserv2:digital", "node": "viviserv2", "lane": "digital", "kind": "digital", "local": True,
            "market": "lansing", "available": True, "reachable": True, "rf_channels": [36],
            "tsids": {"36": 1234}, "stream": {"active": False}}
    there = dict(here, id="dc-primary:digital", node="dc-primary", local=False, market="dc", rf_channels=None)
    server.peer._tuners = [there]
    monkeypatch.setattr(server, "local_tuners", lambda: [here])
    monkeypatch.setattr(server, "RELAY_CONFIRM_POLL_SECONDS", 0)
    # WTTG is on RF 36 here too, but this box's 36 is WLNS.
    payload, status = server.api_v1_digital_tune({"channel_id": "wttg_5_1"}, "low")
    assert status == 200 and payload["relayed"] and payload["tuner"] == "dc-primary:digital"
    assert payload["stream"]["playlist_path"] == "/stream/peer/digital.m3u8"


NOW = 1_800_000_000


def airing(title, start=NOW - 600, duration=1800):
    return {"start": start, "duration": duration, "title": title}


def guide_rows(rows):
    """A box's own antenna guide (epg.json's shape), keyed by antenna number."""
    return {"channels": {number: {"number": number, "name": name, "id": channel_id, "events": events}
                         for number, (name, channel_id, events) in rows.items()}, "generated_at": NOW}


@pytest.fixture
def guides(monkeypatch):
    """viviserv's own guide, and the Lansing box's as its peer read it: both have a 5.1."""
    monkeypatch.setattr(server, "antenna_guide", lambda: guide_rows({
        "4.1": ("WRC-HD", "wrc_hd_4_1", [airing("Sunday Night Football")]),
        "5.1": ("WTTG", "wttg_5_1", [airing("Animal Control")])}))
    monkeypatch.setattr(server, "name_antenna_games", lambda rows: rows)
    monkeypatch.setattr(server, "OTA_GUIDE_ENABLED", False)


def test_the_other_markets_channels_are_listed_from_the_guide_of_the_box_there(home, guides):
    home.listed = guide_rows({"6.1": ("WLNS-TV", "wlns_6_1", [airing("Marshals"), airing("News", NOW + 1200)]),
                              "5.1": ("WSYM", "wsym_5_1", [airing("Not Washington's")])})["channels"]
    rows = server.guide_payload(now=NOW)["channels"]
    assert [key for key in rows if rows[key]["kind"] == "digital"] == ["4.1", "5.1", "digital@lansing/6.1"]
    wlns = rows["digital@lansing/6.1"]
    assert (wlns["id"], wlns["market"], wlns["relayed"], wlns["tv_number"], wlns["key"]) == (
        "lansing/wlns_6_1", "lansing", True, 400, "digital@lansing/6.1")
    assert [event["title"] for event in wlns["events"]] == ["Marshals", "News"]
    # This box's own 5.1 stays Washington's: Lansing's 5.1 is not on the dial (WTTG is one station).
    assert rows["5.1"]["events"][0]["title"] == "Animal Control"
    now = server.guide_now_payload(now=NOW)["channels"]
    assert now["digital@lansing/6.1"]["now"]["title"] == "Marshals"
    assert now["digital@lansing/6.1"]["next"]["title"] == "News"
    assert now["4.1"]["now"]["title"] == "Sunday Night Football"
    one = server.guide_payload(number="digital@lansing/6.1", now=NOW)["channels"]
    assert list(one) == ["digital@lansing/6.1"]


def test_a_peer_reads_this_boxs_own_antenna_guide_only(home, guides):
    home.listed = guide_rows({"6.1": ("WLNS-TV", "wlns_6_1", [airing("Marshals")])})["channels"]
    payload = server.guide_payload(now=NOW, local=True)
    assert list(payload["channels"]) == ["4.1", "5.1"] and payload["available"]


def test_with_no_guide_of_its_own_the_other_markets_is_still_listed(home, guides, monkeypatch):
    monkeypatch.setattr(server, "antenna_guide", lambda: None)
    home.listed = guide_rows({"6.1": ("WLNS-TV", "wlns_6_1", [airing("Marshals")])})["channels"]
    payload = server.guide_payload(now=NOW)
    assert "digital@lansing/6.1" in payload["channels"] and not payload["available"]


def test_away_the_home_markets_channels_are_listed_from_the_primarys_guide(lansing, guides, monkeypatch):
    """viviserv2: its own guide is Lansing's, by number; DC's comes from the primary's."""
    monkeypatch.setattr(server, "antenna_guide", lambda: guide_rows({
        "6.1": ("WLNS-TV", "wlns_6_1", [airing("Marshals")]),
        "4.1": ("WLNS-LD", "wlns_ld_4_1", [airing("Lansing's 4.1")])}))
    server.peer.listed = guide_rows({"4.1": ("WRC-HD", "wrc_hd_4_1", [airing("Sunday Night Football")]),
                                     "5.1": ("WTTG", "wttg_5_1", [airing("Animal Control")])})["channels"]
    by_id = {channel.id: channel for channel in server.CHANNELS.channels()}
    assert (by_id["digital/4.1"].listings, by_id["digital@lansing/6.1"].listings) == ("digital/4.1", "6.1")
    rows = server.guide_payload(now=NOW)["channels"]
    assert rows["digital/4.1"]["events"][0]["title"] == "Sunday Night Football"
    assert rows["digital/4.1"]["id"] == "wrc_hd_4_1" and rows["digital/4.1"]["tv_number"] == 14
    assert rows["4.1"]["events"][0]["title"] == "Lansing's 4.1"
    items = {item["id"]: item for item in server.api_v1_digital_source()["items"]}
    assert items["wrc_hd_4_1"]["listings"] == "digital/4.1"


def test_away_with_no_box_in_the_home_market_its_channels_have_no_listings(lansing, monkeypatch):
    monkeypatch.setattr(server, "peer", None)
    assert {channel.id: channel.listings for channel in server.CHANNELS.channels()
            if channel.kind == "digital"}["digital/4.1"] is None


def test_a_box_learns_the_home_market_from_the_primarys_plan(lansing, monkeypatch):
    assert server.home_market() == "dc" and server.away()
    assert server.own_plan_file() == lansing
    monkeypatch.setattr(server, "peer", None)
    assert server.home_market() == host_config.DEFAULT_MARKET


def test_away_the_dial_has_the_primarys_channels_and_this_boxs_own(lansing):
    dial = server.antenna_dial()
    assert [(source, key, row["id"]) for source, key, row in dial] == [
        ("digital", "4.1", "wrc_hd_4_1"), ("digital", "5.1", "wttg_5_1"),
        ("digital@lansing", "6.1", "lansing/wlns_6_1")]
    assert dial[0][2]["node"] == "dc-primary" and dial[0][2]["tv_number"] == 14
    # Numbered here, in the Michigan block (lineup.json's digital@lansing/6.1).
    assert dial[2][2]["tv_number"] == 26 and "node" not in dial[2][2]


def test_away_this_boxs_tuners_play_only_its_own_markets_channels(lansing):
    assert server.own_channel("lansing/wlns_6_1")["id"] == "lansing/wlns_6_1"
    assert server.own_channel("wlns_6_1")["id"] == "wlns_6_1"
    assert server.own_channel("wrc_hd_4_1") is None


def test_away_a_scan_saves_this_boxs_own_plan_not_the_tracked_one(lansing):
    # The home plan file, this box's own (untracked: none in a fresh checkout), is left as it was.
    home_plan = lambda: server.CHANNELS_FILE.read_text() if server.CHANNELS_FILE.exists() else None  # noqa: E731
    tracked = home_plan()
    server.save_channels(LANSING + [{"id": "wilx_10_1", "number": "10.1"}])
    assert [row["id"] for row in json.loads(lansing.read_text())] == ["wlns_6_1", "wilx_10_1"]
    assert home_plan() == tracked


def test_the_plan_a_box_serves(lansing):
    plan = server.api_v1_plan()
    assert (plan["node"], plan["market"], plan["primary"]) == (server.FEED_CONFIG.node, "lansing", False)
    assert [(row["key"], row["id"], row["tv_number"]) for row in plan["rows"]] == [("6.1", "wlns_6_1", 26)]
    assert plan["revision"].startswith("lansing:")


def test_a_market_is_a_short_name():
    assert host_config.check("market", "lansing") == "lansing"
    assert host_config.check("market", "") is None
    for bad in ("Lansing", "new york", "-x", "a" * 25):
        with pytest.raises(ValueError):
            host_config.check("market", bad)


def test_another_markets_new_channels_take_their_own_section_then_the_other_cities():
    plan = lineup.parse_plan(json.loads(Path(lineup.local_or_default(server.LINEUP_FILE)).read_text()))
    assert plan.new_section("digital@lansing").name == "Michigan extras"
    assert [s.name for s in plan.new_sections("digital@lansing")] == ["Michigan extras", "Other cities' antennas"]
    assert plan.new_section("digital@toledo").name == "Other cities' antennas"
    assert plan.new_section("digital").name == "New on the antenna"
    assert [s.name for s in plan.new_sections("digital")] == ["New on the antenna"]


# -- a box that moves: the market from its own scan (host_config.detect_market) ----------------
# viviserv2's first scan in mid-Michigan (2026-11-21): RF -> TSID and the PSIP names heard.
MICHIGAN_RECEPTION = {
    "rf_channels": [12, 14, 16, 30, 33, 36],
    "tsids": {"12": 1499, "14": 1527, "16": 1503, "30": 1469, "33": 1495, "36": 1545},
    "names": {"12": ["WJRT-HD", "WJRT-D2"], "14": ["WLAJ-DT", "WLAJ-DT", "WLNS-TV"],
              "16": ["FOX", "NBC", "Comet", "Antenna"], "30": ["WNEM-D2", "WNEM-HD"],
              "33": ["WKAR HD", "WKARWLD"], "36": ["WAQP HD", "SBN"]},
}
CALLS = host_config.market_call_signs()


def test_a_call_sign_is_read_from_a_psip_name():
    match = host_config.call_sign_matches
    assert match("WLNS-TV", "WLNS") and match("WKAR HD", "WKAR") and match("WKARWLD", "WKAR")
    assert match("WLAJ-DT", "WLAJ-DT2")             # the station, not its subchannel's suffix
    assert match("WRC-HD", "WRC") and not match("WRCB", "WRC")
    assert not match("FOX", "WSMH") and not match("", "WLNS")


def test_a_scan_that_hears_the_home_markets_stations_is_home():
    home = {row["tsid"] for row in DC}
    assert host_config.detect_market(None, home, CALLS) is None                      # never scanned
    assert host_config.detect_market({"tsids": {"36": 531}}, home | {531}, CALLS) is None


def test_a_scan_that_hears_another_citys_call_signs_is_there():
    assert host_config.detect_market(MICHIGAN_RECEPTION, {529, 531}, CALLS) == "lansing"
    # A city networks.json does not know: the caller names it.
    unknown = {"tsids": {"20": 9999}, "names": {"20": ["KXYZ-HD"]}}
    assert host_config.detect_market(unknown, {529, 531}, CALLS) == ""
    assert host_config.market_name("viviserv2") == "viviserv2"
    assert host_config.market_name("Box 3!") == "box-3" and host_config.market_name("2") == "away"


def _second_node(tmp_path, reception, primary_plan=None):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    if reception is not None:
        (state / "reception.json").write_text(json.dumps(reception))
    if primary_plan is not None:
        (state / "peer_plan.json").write_text(json.dumps(primary_plan))
    environ = {"TV_THING_STATE_DIR": str(state), "TV_THING_NODE_ROLE": "second"}
    return host_config.Host(environ=environ, path=tmp_path / "host.json", hostname="viviserv2")


def test_a_second_node_with_no_market_set_finds_it_from_its_scan(tmp_path):
    primary = {"node": "viviserv", "market": "dc", "primary": True, "rows": served(DC, [14, 15])}
    host = _second_node(tmp_path, MICHIGAN_RECEPTION, primary)
    assert (host.value("market"), host.get("market").source) == ("lansing", "auto")


def test_a_second_node_that_hears_the_primarys_stations_is_in_its_market(tmp_path):
    primary = {"node": "viviserv", "market": "dc", "primary": True, "rows": served(DC, [14, 15])}
    host = _second_node(tmp_path, {"rf_channels": [36], "tsids": {"36": 531}}, primary)
    assert host.value("market") == "dc"
    assert _second_node(tmp_path / "x", None).value("market") == host_config.DEFAULT_MARKET


def test_a_second_node_in_a_city_no_market_lists_is_named_after_itself(tmp_path):
    host = _second_node(tmp_path, {"rf_channels": [20], "tsids": {"20": 9999}, "names": {"20": ["KXYZ"]}})
    assert host.value("market") == "viviserv2"


def test_a_market_that_is_set_wins_over_the_scan(tmp_path):
    host = _second_node(tmp_path, MICHIGAN_RECEPTION)
    (tmp_path / "host.json").write_text(json.dumps({"market": "flint"}))
    host = host_config.Host(environ=host.environ, path=tmp_path / "host.json", hostname="viviserv2")
    assert (host.value("market"), host.get("market").source) == ("flint", "file")


# -- the Michigan block: viviserv2 numbers its own antenna's channels (lineup.json) -------------
def _michigan_rows():
    """viviserv2's plan from that scan: (rf, tsid, numbers and names)."""
    found = [
        (12, 1499, [("12.1", "WJRT-HD")] + [(f"12.{n}", f"WJRT-D{n}") for n in range(2, 8)]),
        (14, 1527, [("53.1", "WLAJ-DT"), ("53.2", "WLAJ-DT"), ("6.1", "WLNS-TV")]),
        (16, 1503, [("66.1", "FOX"), ("66.2", "NBC"), ("66.3", "Comet"), ("46.3", "Antenna")]),
        (30, 1469, [("5.1", "WNEM-HD")] + [(f"5.{n}", f"WNEM-D{n}") for n in range(2, 7)]),
        (33, 1495, [("23.1", "WKAR HD"), ("23.2", "WKARWLD"), ("23.3", "WKARCRT"), ("23.4", "WKARKID"),
                    ("23.5", "WKARMLC")]),
        (36, 1545, [("49.1", "WAQP HD")] + [(f"49.{n}", f"WAQP-{n}") for n in range(2, 12)]),
    ]
    rows = [{"id": f"{name.lower().replace('-', '_').replace(' ', '_')}_{number.replace('.', '_')}",
             "number": number, "name": name, "rf_channel": rf, "tsid": tsid, "service_id": index + 1}
            for rf, tsid, services in found for index, (number, name) in enumerate(services)]
    return sorted(rows, key=server.channel_sort_key)      # as a scan saves them (channels_from_scan)


def test_the_michigan_stations_take_the_planned_numbers(lansing):
    lansing.write_text(json.dumps(_michigan_rows()))
    numbered = {row["number"]: (row["tv_number"], row["tv_name"], row["tv_section"])
                for row in server.numbered_channels(server.load_channels())}
    assert {number: numbered[number][0] for number in
            ("5.1", "6.1", "12.1", "23.1", "66.2", "53.1", "53.2", "66.1", "49.1")} == {
        "5.1": 25, "6.1": 26, "12.1": 28, "23.1": 30, "66.2": 31, "53.1": 34, "53.2": 35, "66.1": 36,
        "49.1": 37}
    assert numbered["66.2"][1:] == ("NBC 25", "Michigan") and numbered["66.1"][1] == "FOX 66"
    # Every subchannel of that scan is in Michigan extras; none needed a number of its own.
    extras = {number: tv for number, (tv, _, section) in numbered.items() if section == "Michigan extras"}
    assert len(extras) == 27 and min(extras.values()) == 380 and max(extras.values()) == 406
    assert server.channel_lineup._assigned() == {}


def test_a_dx_find_takes_the_next_free_michigan_extra_then_the_other_cities(tmp_path):
    document = {"sections": [
        {"first": 25, "last": 26, "name": "Michigan"},
        {"first": 380, "last": 381, "name": "Michigan extras", "new": ["digital@lansing"]},
        {"first": 430, "last": 431, "name": "Other cities' antennas", "new": ["digital@*"]},
        {"first": 450, "last": 999, "name": "More channels", "overflow": True}],
        "channels": [{"number": 25, "source": "digital@lansing", "channel": "5.1"},
                     {"number": 380, "source": "digital@lansing", "channel": "5.2"}]}
    path = tmp_path / "lineup.json"
    path.write_text(json.dumps(document))
    numbers = lineup.Lineup(path, tmp_path / "state.json")
    views = numbers.number("digital@lansing", [("5.1", "WNEM-HD"), ("5.2", "WNEM-D2"), ("4.1", "WDIV-HD"),
                                               ("7.1", "WXYZ-HD"), ("2.1", "WJBK"), ("9.1", "CBET")])
    assert [view["tv_number"] for view in views] == [25, 380, 381, 430, 431, 450]


def test_away_the_always_on_channel_is_one_this_box_receives(lansing, monkeypatch, tmp_path):
    rows = _michigan_rows()
    lansing.write_text(json.dumps(rows))
    playback = tmp_path / "playback_state.json"
    monkeypatch.setattr(server, "PLAYBACK_STATE_FILE", playback)
    # Saved before the move: DC's WORLD 26.4, which no tuner here receives.
    playback.write_text(json.dumps({"channel_id": "world_26_4", "quality": "low"}))
    server.RECEPTION_FILE.write_text(json.dumps(MICHIGAN_RECEPTION))
    channel, quality = server.load_playback_state()
    assert (channel["id"], channel["number"], quality) == ("lansing/wnem_hd_5_1", "5.1", "low")
    # A tune here saves the dial's id; it comes back as the same channel.
    playback.write_text(json.dumps({"channel_id": "lansing/wlns_tv_6_1", "quality": "medium"}))
    assert server.load_playback_state()[0]["id"] == "lansing/wlns_tv_6_1"


def test_the_always_on_channel_is_never_one_the_last_scan_lost(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "load_channels", lambda: [dict(row) for row in DC])
    monkeypatch.setattr(server, "PLAYBACK_STATE_FILE", tmp_path / "playback_state.json")
    (tmp_path / "playback_state.json").write_text(json.dumps({"channel_id": "wrc_hd_4_1"}))
    server.RECEPTION_FILE.write_text(json.dumps({"rf_channels": [36], "tsids": {"36": 531}}))
    assert server.load_playback_state()[0]["id"] == "wttg_5_1"       # RF 34 no longer received
    server.RECEPTION_FILE.write_text(json.dumps({"rf_channels": [12], "tsids": {"12": 1499}}))
    assert server.load_playback_state() == (None, "medium")


def test_away_with_no_roku_lane_the_sports_guide_lists_the_games_on_the_dial(lansing, monkeypatch):
    # viviserv2: no Roku lane, and a 5.1 of its own (WNEM, a CBS station) beside DC's WTTG.
    lansing.write_text(json.dumps(LANSING + [{"id": "wnem_hd_5_1", "number": "5.1", "name": "WNEM-HD",
                                              "rf_channel": 30, "tsid": 4321, "service_id": 1}]))
    monkeypatch.setattr(server, "roku", None)
    monkeypatch.setattr(server, "internet_guide_channels", lambda local=False: {})
    asked = []

    def nfl(game_id, start, away, home, *networks):
        return {"league": "nfl", "game_id": game_id, "start_time": start, "state": "pre", "finished": False,
                "away": {"short": away[0], "abbreviation": away[1]},
                "home": {"short": home[0], "abbreviation": home[1]},
                "broadcasts": [{"name": name, "market": "national"} for name in networks]}
    boards = {"nfl": [
        nfl(1, "2027-01-17T18:00Z", ("Commanders", "WSH"), ("Eagles", "PHI"), "FOX"),
        nfl(2, "2027-01-17T18:00Z", ("Colts", "IND"), ("Vikings", "MIN"), "FOX"),
        nfl(3, "2027-01-18T01:20Z", ("Chiefs", "KC"), ("Seahawks", "SEA"), "NBC"),
        nfl(4, "2027-01-17T18:00Z", ("Bills", "BUF"), ("Jets", "NYJ"), "ESPN")],
        "nhl": [{"league": "nhl", "game_id": 9, "start_time": "2027-01-17T18:00Z", "state": "pre",
                 "away": {"short": "Flyers"}, "home": {"short": "Kings"},
                 "broadcasts": [{"name": "ESPN+", "market": "national"}]}]}

    class Scoreboard:
        def games(self, league, day, power_five=True):
            asked.append((league, day, power_five))
            return boards.get(league, [])
    monkeypatch.setattr(server, "SPORTS_SCOREBOARD", Scoreboard())
    payload = server.api_v1_sports_guide(now=1_800_205_200)     # Sunday 2027-01-17, noon Eastern
    assert (payload["available"], payload["errors"]) == (True, [])
    assert {(day, power_five) for _, day, power_five in asked} == {("2027-01-17", False)}
    # On the DC stations the dial has from the primary (FOX 15, NBC 14), never Lansing's 5.1.
    # Colts at Vikings is another market's FOX game; ESPN and ESPN+ need the Roku's YouTube TV.
    assert [(game["title"], game["network"], game["tv_number"])
            for sport in payload["sports"] for game in sport["games"]] == [
        ("Commanders at Eagles", "FOX", 15), ("Chiefs at Seahawks", "NBC", 14)]
