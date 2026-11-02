"""One dial across cities (Phase B): each box owns its market's plan and serves it; the dial
is the home market's channels then each other market's (server.antenna_dial); only a box in a
station's market plays it (tuner_pool.in_market)."""
import json

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

    def plan(self):
        return self._plan

    def node(self):
        return self._plan["node"]

    def location(self):
        return None

    def tuners(self):
        return self._tuners

    def tune(self, body):
        self.sent.append(body)
        return {"ok": True, "lane": "digital", "stream": {"id": "g1", "playlist_path": "/stream/peer/digital.m3u8"}}, 200


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
        "lansing/wlns_6_1", 400, "lansing", "viviserv2", None, None)
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
    payload, status = server.api_v1_digital_tune({"channel_id": "lansing/wlns_6_1"}, "low")
    assert status == 200 and payload["relayed"] and payload["tuner"] == "viviserv2:digital"
    assert home.sent[0]["channel_id"] == "lansing/wlns_6_1"


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
    # Numbered here, in the other cities' section.
    assert dial[2][2]["tv_number"] == 400 and "node" not in dial[2][2]


def test_away_this_boxs_tuners_play_only_its_own_markets_channels(lansing):
    assert server.own_channel("lansing/wlns_6_1")["id"] == "lansing/wlns_6_1"
    assert server.own_channel("wlns_6_1")["id"] == "wlns_6_1"
    assert server.own_channel("wrc_hd_4_1") is None


def test_away_a_scan_saves_this_boxs_own_plan_not_the_tracked_one(lansing):
    tracked = server.CHANNELS_FILE.read_text()
    server.save_channels(LANSING + [{"id": "wilx_10_1", "number": "10.1"}])
    assert [row["id"] for row in json.loads(lansing.read_text())] == ["wlns_6_1", "wilx_10_1"]
    assert server.CHANNELS_FILE.read_text() == tracked


def test_the_plan_a_box_serves(lansing):
    plan = server.api_v1_plan()
    assert (plan["node"], plan["market"], plan["primary"]) == (server.FEED_CONFIG.node, "lansing", False)
    assert [(row["key"], row["id"], row["tv_number"]) for row in plan["rows"]] == [("6.1", "wlns_6_1", 400)]
    assert plan["revision"].startswith("lansing:")


def test_a_market_is_a_short_name():
    assert host_config.check("market", "lansing") == "lansing"
    assert host_config.check("market", "") is None
    for bad in ("Lansing", "new york", "-x", "a" * 25):
        with pytest.raises(ValueError):
            host_config.check("market", bad)


def test_another_markets_new_channels_take_the_other_cities_section():
    plan = lineup.parse_plan(json.loads(server.LINEUP_FILE.read_text()))
    assert plan.new_section("digital@lansing").name == "Other cities' antennas"
    assert plan.new_section("digital").name == "New on the antenna"
