"""Internet streams across the two boxes: each box's plan publishes the streams it relays itself
(server.published_streams), and the other lists those it does not carry, played from that box's
relay through /stream/peer/ at the number it gives them (server.internet_views), with the
listings from its guide (server.internet_guide_channels)."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import channels
import lineup
import peers
import server

NOW = 1_800_000_000
OWLS = {"id": "owls4", "name": "Owls on 4", "note": "The owl cam, all night", "tv_number": 4,
        "tv_name": "Owls on 4", "tv_section": "Our channels", "guide": True}


class Peer:
    """The other box as server.py reads it: its plan's streams and its guide's rows for them."""

    def __init__(self, streams=(), listings=None, node="dc-primary"):
        self._plan = {"node": node, "market": "dc", "primary": True, "revision": "r", "rows": [],
                      "internet": list(streams)}
        self.listings = listings or {}
        self.reachable, self.last_error = True, None

    def plan(self):
        return self._plan

    def streams(self):
        return peers.published_streams(self._plan)

    def stream_guide(self):
        return self.listings

    def guide(self):
        return {}

    def node(self):
        return self._plan["node"]

    def location(self):
        return None

    def tuners(self):
        return []


def airing(title, start=NOW - 300, duration=3600):
    return {"start": start, "duration": duration, "title": title}


@pytest.fixture(autouse=True)
def numbering(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "channel_lineup", lineup.Lineup(server.LINEUP_FILE, tmp_path / "lineup_state.json"))


@pytest.fixture
def second(monkeypatch):
    """viviserv2: a second node whose peer is the primary."""
    monkeypatch.setattr(server, "HOST", SimpleNamespace(primary=False))
    monkeypatch.setattr(server, "PEER_URL", "http://100.68.34.34:8080")

    def peer(*streams, listings=None):
        found = Peer(streams, listings)
        monkeypatch.setattr(server, "peer", found)
        return found
    return peer


def views():
    return {view["channel_id"]: view for view in server.internet_views()}


def test_a_stream_the_peer_relays_reaches_this_dial_by_itself(second):
    second(OWLS, listings={"internet/owls4": {"events": [airing("Barn Owls"), airing("Snowy Owls", NOW + 3300)]}})
    owls = views()["owls4"]
    assert (owls["tv_number"], owls["tv_name"], owls["tv_section"], owls["name"], owls["note"]) == (
        4, "Owls on 4", "Our channels", "Owls on 4", "The owl cam, all night")
    assert owls["stream_path"] == "/stream/peer/internet/owls4/index.m3u8"
    assert owls["relayed"] and owls["listed"]
    channel = next(found for found in channels.InternetChannels(server).channels(None, None)
                   if found.id == "internet/owls4")
    assert (channel.number, channel.node, channel.stream, channel.listings) == (
        4, "dc-primary", "/stream/peer/internet/owls4/index.m3u8", "internet/owls4")
    # Its airings are the peer's guide's.
    row = server.internet_guide_channels()["internet/owls4"]
    assert [event["title"] for event in row["events"]] == ["Barn Owls", "Snowy Owls"]
    assert row["stream_path"] == "/stream/peer/internet/owls4/index.m3u8"
    guide, _ = channels.InternetChannels(server).guide(channels.GuideWindow(NOW, NOW - 3600, NOW + 7200))
    assert [event["title"] for event in guide["internet/owls4"]["events"]] == ["Barn Owls", "Snowy Owls"]


def test_one_without_listings_yet_has_an_empty_row_and_one_without_any_none(second):
    second(OWLS, dict(OWLS, id="wrens", name="Wrens", tv_number=None, guide=False))
    assert server.internet_guide_channels()["internet/owls4"]["events"] == []
    assert "internet/wrens" not in server.internet_guide_channels()
    assert not views()["wrens"]["listed"]


def test_a_stream_this_box_relays_itself_is_never_taken_from_the_peer(second):
    second(dict(OWLS, id="pentulive", name="Puppies", tv_number=7))
    pentulive = views()["pentulive"]
    assert (pentulive["tv_number"], pentulive["name"], pentulive["stream_path"], pentulive["relayed"]) == (
        2, "Pentulive", "/stream/internet/pentulive/index.m3u8", False)


def test_a_number_taken_here_gives_way(second):
    # 6 is the Sports Guide's here, and 10 NHK's: each new stream takes the next free new number.
    second(dict(OWLS, tv_number=6), dict(OWLS, id="wrens", name="Wrens", tv_number=10))
    found = views()
    assert (found["owls4"]["tv_number"], found["wrens"]["tv_number"]) == (370, 371)
    assert found["owls4"]["tv_section"] == "New internet streams"


def test_lineup_json_here_hides_or_numbers_it(second, monkeypatch, tmp_path):
    second(OWLS, dict(OWLS, id="wrens", name="Wrens", tv_number=9))
    document = json.loads(Path(lineup.local_or_default(server.LINEUP_FILE)).read_text())
    document["hidden"] = [*document.get("hidden", []), {"source": "internet", "channel": "owls4"}]
    document["channels"].append({"number": 7, "source": "internet", "channel": "wrens", "name": "Wrens Here"})
    (tmp_path / "lineup.json").write_text(json.dumps(document))
    monkeypatch.setattr(server, "channel_lineup", lineup.Lineup(tmp_path / "lineup.json", tmp_path / "state.json"))
    found = views()
    assert "owls4" not in found
    assert (found["wrens"]["tv_number"], found["wrens"]["tv_name"]) == (7, "Wrens Here")


def test_a_stream_on_the_primarys_loopback_takes_the_primarys_name_number_and_listings(second):
    """Trains is in this box's internet_streams.json too, but only the primary can reach it."""
    second(dict(OWLS, id="cabride", name="Cab Ride", note="Trains, all day", tv_number=1, tv_name="Trains"),
           listings={"internet/cabride": {"events": [airing("Aizu Railway")]}})
    trains = views()["cabride"]
    assert (trains["tv_number"], trains["tv_name"], trains["note"], trains["stream_path"]) == (
        1, "Trains", "Trains, all day", "/stream/peer/internet/cabride/index.m3u8")
    assert [event["title"] for event in server.internet_guide_channels()["internet/cabride"]["events"]] == [
        "Aizu Railway"]


def test_the_plan_publishes_only_the_streams_this_box_relays_itself(second, monkeypatch):
    monkeypatch.setattr(server, "load_channels", lambda: [])
    second(OWLS)
    published = {entry["id"]: entry for entry in server.api_v1_plan()["internet"]}
    # Not Trains or Ads 5 (the primary's loopback), nor the owls (the primary's own).
    assert list(published) == ["pentulive"]
    assert published["pentulive"] == {"id": "pentulive", "name": "Pentulive", "note": server.web_streams.streams()[
        "pentulive"].note, "tv_number": 2, "tv_name": "pen2live", "tv_section": "Our channels", "guide": False}


def test_the_primary_publishes_every_stream_it_relays_with_its_numbers(monkeypatch):
    monkeypatch.setattr(server, "load_channels", lambda: [])
    published = {entry["id"]: entry for entry in server.api_v1_plan()["internet"]}
    assert {"cabride", "pentulive", "ads5", "cflplus", "birds8"} <= set(published)
    assert (published["birds8"]["tv_number"], published["birds8"]["guide"]) == (8, True)
    assert (published["cabride"]["tv_name"], published["pentulive"]["guide"]) == ("Trains", False)
    # And peers.published_streams reads it back as it was given.
    assert peers.published_streams(server.api_v1_plan())["birds8"]["tv_number"] == 8


def test_the_guide_a_peer_reads_has_this_boxs_own_streams_and_not_those_it_relays(second, monkeypatch):
    second(OWLS, listings={"internet/owls4": {"events": [airing("Barn Owls")]}})
    monkeypatch.setattr(server.web_streams, "listings", lambda stream_id: [airing(f"{stream_id} now")])
    assert "internet/owls4" not in server.internet_guide_channels(local=True)
    local = channels.GuideWindow(NOW, NOW - 3600, local=True)
    assert "internet/owls4" not in channels.InternetChannels(server).guide(local)[0]
    # The primary's own streams go in its local guide, Birds on 8's among them.
    monkeypatch.setattr(server, "HOST", SimpleNamespace(primary=True))
    rows = server.internet_guide_channels(local=True)
    assert rows["internet/birds8"]["events"][0]["title"] == "birds8 now"
    assert "internet/owls4" not in rows


def test_the_registry_gives_a_peer_its_antennas_and_streams_rows_only(monkeypatch):
    asked = []

    class Part:
        def __init__(self, kind):
            self.kind = kind

        def guide(self, window):
            asked.append(self.kind)
            return {}, {}
    kinds = (channels.AntennaChannels.kind, channels.InternetChannels.kind, "roku", "vizelvision")
    channels.Registry([Part(kind) for kind in kinds]).guide(channels.GuideWindow(NOW, NOW - 3600, local=True))
    assert asked == ["digital", "internet"]


def test_the_plan_is_read_again_when_its_streams_change(monkeypatch, tmp_path):
    streams = tmp_path / "internet_streams.json"
    streams.write_text('{"streams": []}')
    monkeypatch.setattr(server, "INTERNET_STREAMS_FILE", streams)
    before = server.plan_revision()
    streams.write_text('{"streams": [{"id": "owls4", "name": "Owls", "resolver": "url", "url": "http://x/"}]}')
    assert server.plan_revision() != before
    assert before.startswith(f"{server.MARKET}:")


def test_the_inputs_page_says_the_peer_makes_it(second):
    second(OWLS)
    owls = next(entry for entry in server.web_inputs() if entry["id"] == "internet/owls4")
    assert (owls["service"], owls["upstream"], owls["origin"], owls["listings"]) == (
        "peer", "100.68.34.34:8080", "internal", True)
    assert owls["made_by"] == "the primary's relay, through /stream/peer/"
    assert owls["stream"] == "/stream/peer/internet/owls4/index.m3u8"
