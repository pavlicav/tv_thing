"""VizelVision's Sports Guide: today's games by sport, each where TV Thing shows it.

Channel 60 plays it as slides (static/sports-guide.html, from
/api/v1/sports-guide). A game is listed only when it can be watched here:

* on a national broadcaster in the lineup, by that channel's name and number:
  a network on its DC station over the antenna (ABC 7), else a cable channel
  from YouTube TV (TBS 31). A regional Sunday NFL game on CBS or FOX is on
  Washington's station only for a DC team, else on YouTube TV's Lansing
  station when its listings name it;
* else on one of the Roku's game sources, on its Live Sports number: any
  other Sunday-afternoon game on Sunday Ticket, Thursday Night Football on
  Prime Video, a baseball game on MLB.TV, an NHL game on ESPN Unlimited.

Streaming-only games (Peacock, Apple TV, ESPN+ outside hockey), regional
sports networks and games already over are left out. Sports go in a fixed
order, each sport's games by start time, then by channel number.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from roku_control.config import normalize_channel_name
from roku_control.networks import NETWORKS
from roku_control.sports import NETWORK_ALIASES, nhl_on_espn_unlimited

# Each sport and its leagues, as (league, tag), in the order the guide shows them.
# The pros and the colleges each get their own pages, as the papers listed them.
SPORTS = (
    ("NFL Football", (("nfl", "NFL"),)),
    ("College Football", (("ncaaf", "NCAA"),)),
    ("Baseball", (("mlb", "MLB"),)),
    ("Basketball", (("nba", "NBA"), ("wnba", "WNBA"), ("ncaam", "NCAA"), ("ncaaw", "NCAAW"))),
    ("Hockey", (("nhl", "NHL"),)),
    ("Soccer", (("epl", "EPL"), ("ucl", "UCL"), ("mls", "MLS"), ("nwsl", "NWSL"))),
)
# The leagues read from ESPN's scoreboards; baseball comes from the Roku's MLB catalog.
ESPN_LEAGUES = tuple(league for _, leagues in SPORTS for league, _ in leagues if league != "mlb")
# A network's national games are on its station over the antenna (networks.json's antenna
# market, Washington), by virtual channel number: no Roku needed. (YouTube TV's NBC and the
# rest are its own market's, Lansing's.) Keyed by every name the network goes by.
NETWORK_STATIONS = {normalize_channel_name(name): (station.network, station.antenna)
                    for station in NETWORKS.antenna.stations.values() if station.antenna
                    for name in station.names()}
# YouTube TV rows whose names are too long for the guide's network column.
SHORT_NAMES = {"Big Ten Network": "BTN", "NBC Sports Network": "NBCSN",
               "CBS Sports Network": "CBSSN", "The CW": "CW"}
# The game sources' Live Sports numbers, named for the service that streams the game.
SERVICES = {"sunday-ticket": "Sunday Ticket", "thursday-night-football": "Prime Video",
            "mlb": "MLB.TV", "nhl": "ESPN+"}


class Channels:
    """Where a broadcaster is in the lineup.

    ``antenna``: the antenna's channels with their TV Thing numbers;
    ``youtube_tv``: the YouTube TV catalog's items, numbered, with aliases.
    """

    def __init__(self, antenna, youtube_tv):
        self.antenna = {channel.get("number"): channel["tv_number"] for channel in antenna
                        if isinstance(channel.get("tv_number"), int)}
        self.rows = {}
        for item in youtube_tv:
            if not isinstance(item.get("tv_number"), int):
                continue
            for name in (item.get("id"), item.get("name"), *(item.get("aliases") or ())):
                if name:
                    self.rows.setdefault(normalize_channel_name(str(name)),
                                         (str(item["id"]), item["tv_number"]))

    def find(self, name):
        """``(label, tv_number)`` of the channel carrying a broadcaster, or None.

        MLB writes shared carriage as "TBS/HBO MAX": the first part carried here wins.
        """
        for part in str(name or "").split("/"):
            part = part.strip()
            # ESPN+ and Paramount+ stream; without the "+" they would read as ESPN and Paramount.
            if not part or "+" in part:
                continue
            found = self.station(part) or self.youtube_tv(part)
            if found:
                return found
        return None

    def station(self, name):
        """``(label, tv_number)`` of a network's DC station over the antenna, or None."""
        station = NETWORK_STATIONS.get(_key(name))
        return (station[0], self.antenna[station[1]]) if station and station[1] in self.antenna else None

    def youtube_tv(self, name):
        """``(label, tv_number)`` of YouTube TV's row for a broadcaster, or None. A network's
        row is its Lansing station."""
        found = self.rows.get(_key(name))
        return (SHORT_NAMES.get(found[0], found[0]), found[1]) if found else None


def _key(name):
    name = str(name or "")
    return normalize_channel_name(NETWORK_ALIASES.get(name.upper(), name))


def _start(value):
    try:
        start = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(start.timestamp()) if start.tzinfo else None


def _team(team):
    team = team or {}
    return team.get("short") or team.get("nickname") or team.get("name") or "TBA"


def _channel(found):
    return found and {"network": found[0], "tv_number": found[1], "via": "channel"}


def _service(source_id, item):
    number = (item or {}).get("tv_number")
    return {"network": SERVICES[source_id], "tv_number": number, "via": source_id} \
        if isinstance(number, int) else None


def _national(game, channels):
    for broadcast in game.get("broadcasts") or ():
        if broadcast.get("market") in (None, "national"):
            found = channels.find(broadcast.get("name"))
            if found:
                return _channel(found)
    return None


def _where(league, game, numbered, channels):
    """Where a game from ESPN's scoreboard can be watched here, or None."""
    game_id = str(game.get("game_id"))
    if league == "nfl":
        # A Sunday-afternoon CBS or FOX game is regional: on Washington's station
        # over the antenna for a DC team, else on Lansing's in YouTube TV when its
        # listings say so, else on Sunday Ticket. Thursday's is on Prime Video.
        target = game.get("suggested_target") or {}
        source, markets = target.get("source_id"), game.get("markets") or {}
        regional = markets.get("network")
        station = channels.station(regional) if regional and markets.get("antenna") else None
        if station is None and regional and source == "youtube-tv":
            station = channels.youtube_tv(target.get("channel_id"))
        if station:
            return _channel(station)
        if source in ("sunday-ticket", "thursday-night-football"):
            return _service(source, numbered.get((source, game_id)))
    found = _national(game, channels)
    if found is None and league == "nhl" and nhl_on_espn_unlimited(game):
        found = _service("nhl", numbered.get(("nhl", game_id)))
    return found


def _baseball_where(item, channels):
    """Where a game from the MLB catalog can be watched: its route, as the Roku would go."""
    route = item.get("route") or {}
    if route.get("provider") == "youtube_tv":
        return _channel(channels.find(route.get("channel")))
    if route.get("provider") == "mlb":
        return _service("mlb", item)
    return None


def _row(sport, league, tag, game, where, start_time, live):
    away, home = _team(game.get("away")), _team(game.get("home"))
    return {"sport": sport, "league": league, "tag": tag,
            "start": _start(start_time), "start_time": start_time,
            "time_tbd": game.get("start_time_tbd") is True, "live": live,
            "title": f"{away} at {home}", "away": away, "home": home, **where}


def day_games(espn, catalogs, channels, *, live_only=False):
    """One day's watchable games, as the guide's rows.

    ``espn``: ``{league: games}`` from roku_control's scoreboard (the NFL's
    with their suggested targets); ``catalogs``: ``{source_id: listing}``,
    the Roku's numbered game catalogs, baseball's among them; ``channels``: a
    Channels. ``live_only`` keeps just the games being played (last night's,
    after the day turns at 3 AM).
    """
    numbered = {(source_id, str(item.get("id"))): item
                for source_id, listing in catalogs.items() for item in (listing or {}).get("items", ())}
    rows = []
    for sport, leagues in SPORTS:
        for league, tag in leagues:
            if league == "mlb":
                for item in (catalogs.get("mlb") or {}).get("items", ()):
                    state = str(item.get("state") or "").lower()
                    where = _baseball_where(item, channels)
                    if state == "final" or (live_only and state != "live") or where is None:
                        continue
                    rows.append(_row(sport, league, tag, item.get("game") or {}, where,
                                     item.get("start_time"), state == "live"))
                continue
            for game in espn.get(league) or ():
                state = str(game.get("state") or "").lower()
                if game.get("finished") is True or state == "post" or (live_only and state != "in"):
                    continue
                where = _where(league, game, numbered, channels)
                if where is not None:
                    rows.append(_row(sport, league, tag, game, where, game.get("start_time"), state == "in"))
    return rows


def _number(row):
    number = row.get("tv_number")
    return number if isinstance(number, int) else float("inf")


def by_sport(rows):
    """``[{sport, games}]`` in SPORTS order, each sport's games by start time,
    those at the same time by channel number."""
    order = [sport for sport, _ in SPORTS]
    grouped = {}
    for row in rows:
        grouped.setdefault(row["sport"], []).append(row)
    return [{"sport": sport, "games": sorted(
                grouped[sport], key=lambda row: (row["time_tbd"] or row["start"] is None,
                                                 row["start"] or 0, _number(row), row["title"]))}
            for sport in order if sport in grouped]


# The antenna's listings title a game by its sport alone ("College Football"):
# the station sends no teams. The guide names them from ESPN's scoreboard,
# by these titles' leagues (static/listing-title.js gives the same titles
# their brackets).
LISTING_LEAGUES = (
    (re.compile(r"College Football"), "ncaaf"),
    (re.compile(r"(Men's )?College Basketball"), "ncaam"),
    (re.compile(r"Women's College Basketball"), "ncaaw"),
    (re.compile(r"NFL Football"), "nfl"),
    (re.compile(r"MLB Baseball|\d{4} (World Series|(American|National) League (Championship|Division) Series"
                r"|MLB Wild Card Series)"), "mlb"),
    (re.compile(r"NBA Basketball|\d{4} NBA (Finals|Playoffs)"), "nba"),
    (re.compile(r"WNBA Basketball|\d{4} WNBA (Finals|Playoffs)"), "wnba"),
    (re.compile(r"NHL Hockey|\d{4} Stanley Cup (Final|Playoffs)"), "nhl"),
    (re.compile(r"Premier League Soccer"), "epl"),
    (re.compile(r"MLS Soccer"), "mls"),
    (re.compile(r"NWSL Soccer"), "nwsl"),
)
# The antenna's affiliates other than NETWORK_STATIONS' own, by network (WNUV, Baltimore's CW).
AFFILIATES = NETWORKS.antenna.affiliates
# How far a game's start may be from its airing's: ESPN gives the broadcast
# window's start, as the listing does.
MATCH_SECONDS = 15 * 60
_EASTERN = ZoneInfo("America/New_York")


def _league(title):
    return next((league for pattern, league in LISTING_LEAGUES if pattern.fullmatch(title or "")), None)


def _network(name):
    """The network NETWORK_STATIONS names for an ESPN broadcast, or None."""
    name = str(name or "")
    station = NETWORK_STATIONS.get(normalize_channel_name(NETWORK_ALIASES.get(name.upper(), name)))
    return station and station[0]


def _station_networks():
    networks = {number: label for label, number in NETWORK_STATIONS.values()}
    networks.update(AFFILIATES)
    return networks


def _unnamed(channels):
    """The antenna's game airings with no teams: ``(number, index, league, day, network)``."""
    networks = _station_networks()
    for number, channel in channels.items():
        network = networks.get(number)
        if network is None:
            continue
        for index, event in enumerate(channel.get("events") or ()):
            league = None if event.get("episode_title") else _league(event.get("title"))
            if league:
                day = datetime.fromtimestamp(event["start"], _EASTERN).date().isoformat()
                yield number, index, league, day, network


def scoreboards_wanted(channels):
    """The ``(league, day)`` scoreboards name_games needs for these channels."""
    return {(league, day) for _, _, league, day, _ in _unnamed(channels)}


def name_games(channels, scoreboards):
    """The antenna's channels, their games' airings named by their teams.

    ``channels``: guide channels by antenna number; ``scoreboards``:
    ``{(league, day): games}`` from roku_control's scoreboard, every division
    game. An airing titled by its sport alone gets ``episode_title`` "LSU at
    Auburn" (and ``teams_from`` "ESPN") when exactly one game ESPN lists
    nationally on its station's network starts within MATCH_SECONDS of it.
    With none, or two (a network's regional window), it keeps its title alone.
    The channels given are not changed.
    """
    named = {}
    for number, index, league, day, network in _unnamed(channels):
        event = channels[number]["events"][index]
        found = [game for game in scoreboards.get((league, day)) or ()
                 if game.get("start_time_tbd") is not True
                 and (start := _start(game.get("start_time"))) is not None
                 and abs(start - event["start"]) <= MATCH_SECONDS
                 and any(broadcast.get("market") in (None, "national") and _network(broadcast.get("name")) == network
                         for broadcast in game.get("broadcasts") or ())]
        if len(found) == 1:
            title = f"{_team(found[0].get('away'))} at {_team(found[0].get('home'))}"
            named.setdefault(number, {})[index] = dict(event, episode_title=title, teams_from="ESPN")
    return {number: dict(channel, events=[named.get(number, {}).get(index, event)
                                          for index, event in enumerate(channel.get("events") or ())])
            if number in named else channel
            for number, channel in channels.items()}
