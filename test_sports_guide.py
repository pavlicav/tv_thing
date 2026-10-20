"""VizelVision's Sports Guide listings (sports_guide.py); no network, no server."""
import sports_guide

ANTENNA = [{"number": "4.1", "tv_number": 4}, {"number": "5.1", "tv_number": 5},
           {"number": "7.1", "tv_number": 7}, {"number": "50.1", "tv_number": 8},
           {"number": "26.1", "tv_number": 2}, {"number": "99.9"}]
YOUTUBE_TV = [{"id": "NBC", "name": "NBC", "aliases": ["WRC"], "tv_number": 16},
              {"id": "FOX", "name": "FOX", "aliases": ["WTTG"], "tv_number": 17},
              {"id": "CBS", "name": "CBS", "aliases": ["WUSA"], "tv_number": 19},
              {"id": "TBS", "name": "TBS", "aliases": [], "tv_number": 31},
              {"id": "TNT", "name": "TNT", "aliases": [], "tv_number": 32},
              {"id": "ESPN", "name": "ESPN", "aliases": ["ESPN HD"], "tv_number": 33},
              {"id": "Big Ten Network", "name": "Big Ten Network", "aliases": ["BTN"], "tv_number": 41},
              {"id": "USA", "name": "USA", "aliases": ["USA Network"], "tv_number": 57},
              {"id": "Unnumbered", "name": "Unnumbered", "aliases": []}]
CHANNELS = sports_guide.Channels(ANTENNA, YOUTUBE_TV)


def team(name, short=None):
    return {"name": name, "nickname": name.split()[-1], "short": short}


def game(game_id, start, away, home, *broadcasts, state="pre", **extra):
    """A scoreboard game: broadcasts are names, or (name, market)."""
    return {"game_id": game_id, "start_time": start, "state": state,
            "finished": state == "post", "away": team(*away), "home": team(*home),
            "broadcasts": [{"name": b, "market": "national"} if isinstance(b, str)
                           else {"name": b[0], "market": b[1]} for b in broadcasts], **extra}


def test_a_broadcaster_is_found_on_its_channel_here():
    find = CHANNELS.find
    assert find("ESPN") == ("ESPN", 33) and find("ESPN HD") == ("ESPN", 33)
    # A network is on its DC station over the antenna, however ESPN spells it.
    assert find("ABC") == ("ABC", 7) and find("Fox") == ("FOX", 5) and find("CW") == ("CW", 8)
    # Without that station, YouTube TV's row carries it.
    assert find("CBS") == ("CBS", 19)
    assert find("USA Net") == ("USA", 57) and find("BTN") == ("BTN", 41)
    # MLB's shared carriage: the first part carried here.
    assert find("TBS/HBO MAX") == ("TBS", 31) and find("Peacock/NBC") == ("NBC", 4)
    # Streaming only, or not in the lineup: nowhere here.
    for name in ("ESPN+", "SECN+", "Paramount+", "Peacock", "Prime Video", "Apple TV", "NHL Network",
                 "Unnumbered", "", None):
        assert find(name) is None, name


def test_a_national_game_is_listed_on_its_broadcasters_channel():
    espn = {"nfl": [game(1, "2026-10-20T00:15Z", ("Washington Commanders", "Commanders"),
                         ("San Francisco 49ers", "49ers"), "ESPN", "ABC", state="in",
                         suggested_target={"source_id": "youtube-tv", "channel_id": "ESPN"})],
            "ncaaf": [game(2, "2026-10-24T16:00Z", ("LSU Tigers", "LSU"), ("Auburn Tigers", "Auburn"), "ABC"),
                      game(3, "2026-10-24T16:00Z", ("Rice Owls", "Rice"), ("FAU Owls", "FAU"), "ESPN+"),
                      game(4, "2026-10-24T16:00Z", ("Duke", "Duke"), ("UNC", "UNC"), ("ESPN", "home"))],
            "wnba": [game(5, "2026-10-20T00:00Z", ("Atlanta Dream", "Dream"), ("Golden State Valkyries", "Valkyries"),
                          "USA Net", "Peacock")]}
    rows = sports_guide.day_games(espn, {}, CHANNELS)
    assert [(row["sport"], row["tag"], row["title"], row["network"], row["tv_number"], row["via"], row["live"])
            for row in rows] == [
        ("Football", "NFL", "Commanders at 49ers", "ESPN", 33, "channel", True),
        ("Football", "NCAA", "LSU at Auburn", "ABC", 7, "channel", False),
        ("Basketball", "WNBA", "Dream at Valkyries", "USA", 57, "channel", False)]
    assert rows[0]["start"] == 1792455300 and rows[0]["start_time"] == "2026-10-20T00:15Z"


def test_a_game_on_a_roku_source_is_on_its_live_sports_number():
    sunday = {"source_id": "sunday-ticket", "channel_id": "10", "date": "2026-10-25"}
    espn = {"nfl": [game(10, "2026-10-25T17:00Z", ("Chicago Bears",), ("Atlanta Falcons",), "FOX",
                         suggested_target=sunday),
                    game(11, "2026-10-30T00:15Z", ("Miami Dolphins",), ("Baltimore Ravens",), "Prime Video",
                         suggested_target={"source_id": "thursday-night-football", "channel_id": "11"}),
                    # The Sunday Ticket catalog did not answer for this one: not listed.
                    game(12, "2026-10-25T17:00Z", ("Detroit Lions",), ("Green Bay Packers",), "CBS",
                         suggested_target={**sunday, "channel_id": "12"})],
            "nhl": [game(20, "2026-10-24T23:00Z", ("Sharks",), ("Bruins",), "ESPN+", ("NESN", "home")),
                    game(21, "2026-10-25T23:00Z", ("Canadiens",), ("Jets",), "TNT"),
                    game(22, "2026-10-25T23:00Z", ("Kings",), ("Islanders",), "NHL Network")]}
    catalogs = {"sunday-ticket": {"items": [{"id": "10", "tv_number": 63}, {"id": "12"}]},
                "thursday-night-football": {"items": [{"id": "11", "tv_number": 61}]},
                "nhl": {"items": [{"id": "20", "tv_number": 65}]},
                "mlb": {"items": [
                    {"id": "823254", "state": "live", "start_time": "2026-10-20T00:08:00Z", "tv_number": 62,
                     "route": {"provider": "youtube_tv", "channel": "TBS"},
                     "game": {"away": {"nickname": "Guardians"}, "home": {"nickname": "Rays"}}},
                    {"id": "823255", "state": "preview", "start_time": "2026-10-20T23:00:00Z", "tv_number": 64,
                     "route": {"provider": "mlb"},
                     "game": {"away": {"nickname": "Mets"}, "home": {"nickname": "Cubs"}}},
                    {"id": "823256", "state": "preview", "start_time": "2026-10-20T23:00:00Z", "tv_number": 66,
                     "route": None, "game": {}},
                    {"id": "823257", "state": "final", "start_time": "2026-10-20T17:00:00Z", "tv_number": 67,
                     "route": {"provider": "mlb"}, "game": {}}]}}
    rows = sports_guide.day_games(espn, catalogs, CHANNELS)
    assert [(row["title"], row["network"], row["tv_number"], row["via"]) for row in rows] == [
        ("Bears at Falcons", "Sunday Ticket", 63, "sunday-ticket"),
        ("Dolphins at Ravens", "Prime Video", 61, "thursday-night-football"),
        ("Guardians at Rays", "TBS", 31, "channel"),
        ("Mets at Cubs", "MLB.TV", 64, "mlb"),
        ("Sharks at Bruins", "ESPN+", 65, "nhl"),
        ("Canadiens at Jets", "TNT", 32, "channel")]
    assert rows[2]["live"] is True and rows[3]["live"] is False


def test_finished_games_go_and_after_midnight_only_the_ones_still_on_stay():
    espn = {"nhl": [game(1, "2026-10-19T23:00Z", ("A",), ("B",), "TNT", state="post"),
                    game(2, "2026-10-20T02:00Z", ("C",), ("D",), "TNT", state="in"),
                    game(3, "2026-10-20T02:30Z", ("E",), ("F",), "TNT")]}
    assert [row["title"] for row in sports_guide.day_games(espn, {}, CHANNELS)] == ["C at D", "E at F"]
    assert [row["title"] for row in sports_guide.day_games(espn, {}, CHANNELS, live_only=True)] == ["C at D"]
    postponed = game(4, "2026-10-20T02:30Z", ("G",), ("H",), "TNT", state="post")
    assert sports_guide.day_games({"nhl": [postponed]}, {}, CHANNELS) == []


def test_the_guide_goes_by_sport_then_time():
    rows = sports_guide.day_games({
        "nhl": [game(1, "2026-10-24T23:00Z", ("Kings",), ("Devils",), "TNT")],
        "nba": [game(2, "2026-10-24T23:30Z", ("Bucks",), ("76ers",), "ESPN")],
        "ncaaf": [game(3, "2026-10-24T19:30Z", ("Iowa",), ("Minnesota",), "BTN"),
                  game(4, "2026-10-24T16:00Z", ("Pitt",), ("Miami",), "ESPN"),
                  game(5, None, ("Late",), ("Kick",), "ESPN", start_time_tbd=True),
                  game(6, "2026-10-24T16:00Z", ("Indiana",), ("Michigan",), "Fox")],
        "nfl": [game(7, "2026-10-24T20:00Z", ("Saints",), ("Steelers",), "CBS",
                     suggested_target={"source_id": "youtube-tv"})],
    }, {}, CHANNELS)
    guide = sports_guide.by_sport(rows)
    assert [sport["sport"] for sport in guide] == ["Football", "Basketball", "Hockey"]
    assert [row["title"] for row in guide[0]["games"]] == [
        "Indiana at Michigan", "Pitt at Miami", "Iowa at Minnesota", "Saints at Steelers", "Late at Kick"]
    assert guide[0]["games"][-1]["time_tbd"] is True and guide[0]["games"][-1]["start"] is None
    assert sports_guide.by_sport([]) == []


def test_the_scoreboard_reads_every_fbs_and_division_one_game_for_the_guide():
    import io
    import json
    from roku_control.sports import EspnScoreboard

    def event(event_id, conference, short):
        team = {"id": event_id, "displayName": f"{short} Team", "shortDisplayName": short,
                "name": "Team", "abbreviation": short[:4].upper(), "conferenceId": conference}
        return {"id": event_id, "date": "2026-10-24T16:00Z", "name": f"{short} game",
                "status": {"type": {"state": "pre"}},
                "competitions": [{"competitors": [{"homeAway": "home", "team": team},
                                                  {"homeAway": "away", "team": dict(team, id="9")}],
                                  "broadcasts": [{"market": "national", "names": ["ESPN2"]}]}]}

    urls = []

    def opener(request, timeout):
        urls.append(request.full_url)
        return io.BytesIO(json.dumps({"events": [event("1", "8", "Georgia"), event("2", "15", "Toledo")]}).encode())

    board = EspnScoreboard(opener=opener)
    assert [g["home"]["short"] for g in board.games("ncaaf", "2026-10-24")] == ["Georgia"]
    every = board.games("ncaaf", "2026-10-24", power_five=False)
    assert [g["home"]["short"] for g in every] == ["Georgia", "Toledo"]
    assert len(urls) == 2 and all("groups=80" in url for url in urls)
    assert board.games("ncaaf", "2026-10-24", power_five=False) is every and len(urls) == 2
    board.games("ncaam", "2026-11-07")
    assert "basketball/mens-college-basketball/scoreboard?dates=20261107&groups=50" in urls[-1]
