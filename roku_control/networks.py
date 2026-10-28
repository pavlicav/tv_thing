"""Which station carries each broadcast network, in the markets TV Thing gets television from.

``networks.json``, at the checkout's root, lists them: the antenna's market
(Washington's stations, by virtual channel) and the Roku's YouTube TV market
(Lansing's, whose stations are YouTube TV's network rows). The sports code
(sports.py, sports_guide.py) and the channel list (channels.py) read them here
rather than each keeping its own table.

It is read once, at import: a change takes a control restart. A broken file
costs the sports their station placement, never control: the markets are
empty and the error is printed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import normalize_channel_name

NETWORKS_FILE = Path(__file__).resolve().parent.parent / "networks.json"


class NetworksError(ValueError):
    """networks.json is missing or does not describe the markets."""


@dataclass(frozen=True)
class Station:
    network: str
    call_sign: str
    label: str
    antenna: str | None = None      # its virtual channel, when the antenna gets it
    aliases: tuple = ()

    def names(self):
        return (self.network, *self.aliases)


@dataclass(frozen=True)
class Market:
    id: str
    name: str
    label: str
    stations: dict = field(default_factory=dict)     # network -> Station
    affiliates: dict = field(default_factory=dict)   # antenna channel -> network
    nfl_teams: tuple = ()
    nhl_teams: tuple = ()
    simulcasts: dict = field(default_factory=dict)   # team -> {cable channel: call sign}

    def network_named(self, name):
        """The Station whose network a name (``NBC``, ``The CW``) is, or None."""
        key = normalize_channel_name(str(name or ""))
        return next((station for station in self.stations.values()
                     if key in {normalize_channel_name(n) for n in station.names()}), None)


@dataclass(frozen=True)
class Networks:
    markets: dict
    antenna: Market
    youtube_tv: Market

    def antenna_channel(self, number):
        """``(network, market id or None)`` of the antenna's channel ``number``, or None."""
        station = next((s for s in self.antenna.stations.values() if s.antenna == number), None)
        if station:
            return station.network, self.antenna.id
        network = self.antenna.affiliates.get(number)
        return (network, None) if network else None

    def youtube_tv_row(self, names):
        """``(network, market id)`` of the YouTube TV row with one of ``names``, or None."""
        station = next(filter(None, map(self.youtube_tv.network_named, names)), None)
        return (station.network, self.youtube_tv.id) if station else None


def _strings(value, where):
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise NetworksError(f"{where} must be a list of names")
    return tuple(value)


def _market(market_id, raw):
    where = f"markets.{market_id}"
    if not isinstance(raw, dict) or not raw.get("name") or not raw.get("label"):
        raise NetworksError(f"{where} needs a name and a label")
    stations = {}
    for network, entry in (raw.get("stations") or {}).items():
        if not isinstance(entry, dict) or not entry.get("call_sign"):
            raise NetworksError(f"{where}.stations.{network} needs a call_sign")
        stations[network] = Station(network, entry["call_sign"], entry.get("label") or entry["call_sign"],
                                    entry.get("antenna"), _strings(entry.get("aliases", []),
                                                                   f"{where}.stations.{network}.aliases"))
    affiliates = raw.get("affiliates") or {}
    simulcasts = raw.get("simulcasts") or {}
    if not isinstance(affiliates, dict) or not isinstance(simulcasts, dict):
        raise NetworksError(f"{where}: affiliates and simulcasts are objects")
    return Market(market_id, raw["name"], raw["label"], stations, dict(affiliates),
                  _strings(raw.get("nfl_teams", []), f"{where}.nfl_teams"),
                  _strings(raw.get("nhl_teams", []), f"{where}.nhl_teams"),
                  {team: dict(channels) for team, channels in simulcasts.items()})


def parse(document):
    """Validate a networks.json document and return its Networks."""
    if not isinstance(document, dict) or not isinstance(document.get("markets"), dict):
        raise NetworksError("networks.json needs its markets")
    markets = {market_id: _market(market_id, raw) for market_id, raw in document["markets"].items()}
    for role in ("antenna_market", "youtube_tv_market"):
        if document.get(role) not in markets:
            raise NetworksError(f"{role} must name one of the markets ({', '.join(markets)})")
    return Networks(markets, markets[document["antenna_market"]], markets[document["youtube_tv_market"]])


def load(path=NETWORKS_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return parse(json.load(f))
    except (OSError, ValueError) as error:
        raise error if isinstance(error, NetworksError) else NetworksError(f"{path}: {error}") from None


def _load_or_empty():
    try:
        return load()
    except NetworksError as error:
        print(f"[Networks] {error}; no market has stations until it is fixed", flush=True)
        empty = Market("none", "No market", "nowhere")
        return Networks({}, empty, empty)


NETWORKS = _load_or_empty()
