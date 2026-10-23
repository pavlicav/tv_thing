"""VizelVision's Sports Guide: today's games by sport, each where TV Thing shows it.

Channel 60 plays it as slides (static/sports-guide.html, from
/api/v1/sports-guide). A game is listed only when it can be watched here:

* on a national broadcaster in the lineup, by that channel's name and number:
  a network on its DC station over the antenna (ABC 7), else a cable channel
  from YouTube TV (TBS 31);
* else on one of the Roku's game sources, on its Live Sports number: an
  out-of-market Sunday game on Sunday Ticket, Thursday Night Football on
  Prime Video, a baseball game on MLB.TV, an NHL game on ESPN Unlimited.

Streaming-only games (Peacock, Apple TV, ESPN+ outside hockey), regional
sports networks and games already over are left out. Sports go in a fixed
order, each sport's games by start time.
"""
from __future__ import annotations

from datetime import datetime

from roku_control.config import normalize_channel_name
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
# A network's national games are on its DC station, by virtual channel number:
# over the air, no Roku needed. (YouTube TV's NBC and the rest are Lansing's.)
NETWORK_STATIONS = {"abc": ("ABC", "7.1"), "cbs": ("CBS", "9.1"), "nbc": ("NBC", "4.1"),
                    "fox": ("FOX", "5.1"), "cw": ("CW", "50.1"), "thecw": ("CW", "50.1"),
                    "ion": ("ION", "66.1"), "univision": ("Univision", "14.1"),
                    "telemundo": ("Telemundo", "44.1")}
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
            part = NETWORK_ALIASES.get(part.upper(), part)
            key = normalize_channel_name(part)
            station = NETWORK_STATIONS.get(key)
            if station and station[1] in self.antenna:
                return station[0], self.antenna[station[1]]
            if key in self.rows:
                row, number = self.rows[key]
                return SHORT_NAMES.get(row, row), number
        return None


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
        # A Sunday-afternoon game out of market is on Sunday Ticket, not on the
        # CBS or FOX ESPN calls national; Thursday's is on Prime Video.
        source = (game.get("suggested_target") or {}).get("source_id")
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
    Channels. ``live_only`` keeps just the games being played (yesterday's,
    after midnight).
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


def by_sport(rows):
    """``[{sport, games}]`` in SPORTS order, each sport's games by start time."""
    order = [sport for sport, _ in SPORTS]
    grouped = {}
    for row in rows:
        grouped.setdefault(row["sport"], []).append(row)
    return [{"sport": sport, "games": sorted(
                grouped[sport], key=lambda row: (row["time_tbd"] or row["start"] is None,
                                                 row["start"] or 0, row["title"]))}
            for sport in order if sport in grouped]
