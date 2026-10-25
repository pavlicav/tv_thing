from dataclasses import replace
from datetime import datetime, timezone
import gzip
from pathlib import Path
import tempfile
import unittest

from roku_control.controller import RokuStreamController
from roku_control.errors import NavigationError, SundayTicketBlackoutError
from roku_control.feedback import PpmFrame, VisualFeedback
from roku_control.states import AppState

from roku_control.tests import test_controller
from roku_control.tests.test_controller import FakeEcp

BILLS = {'game_id': 401872953, 'title': 'Los Angeles Chargers at Buffalo Bills',
         'away': {'name': 'Los Angeles Chargers', 'nickname': 'Chargers'},
         'home': {'name': 'Buffalo Bills', 'nickname': 'Bills'}}
# Text read from the live 720p guide on September 23 (OCR renders "NFL ST" as "NEL ST").
BILLS_HEADER = ('6:27 PM\nLos Angeles Chargers at Buffalo\nBills\nIn 3 days\n'
                'Sun, Sep 27, 1:00 PM « NEL ST - FOX')


def row(title, *, channel=None, live=True, ticket_logo=True):
    header = f'{title}\n' + ('' if live else 'In 3 days\n') + ('Sun · NEL ST · FOX' if not channel else 'FOX')
    return {'header': header, 'row': ('' if live else 'Upcoming: ') + title, 'channel': channel,
            'logo': ticket_logo and not channel}


class GuideFeedback:
    """A guide whose focus moves with the fake Roku's Down key."""
    ocr = object()

    def __init__(self, ecp, rows, *, logo_template=True):
        self.ecp, self.rows, self.logo_template = ecp, rows, logo_template

    def _row(self):
        index = self.ecp.keys.count('Down')
        return self.rows[min(index, len(self.rows) - 1)]

    def capture(self):
        return self._row()

    def read_guide_header(self, frame):
        text = frame['header']
        return {'text': text, 'sunday_ticket': 'NEL ST' in text, 'upcoming': 'In 3 days' in text}

    def read_guide_row(self, frame):
        return {'text': frame['row'], 'upcoming': frame['row'].startswith('Upcoming')}

    def identify_selected(self, channels, frame=None):
        if not frame['channel']:
            raise NavigationError('not a calibrated row')
        return {'channel': frame['channel']}

    def sunday_ticket_logo(self, frame):
        return {'matched': frame['logo'], 'difference': 1.0 if frame['logo'] else 40.0} if self.logo_template else None

    match_nfl_game = staticmethod(VisualFeedback.match_nfl_game)


# The local stations and their guide order on viviserv.
CHANNELS = [{'name': 'NBC', 'guide_index': 0}, {'name': 'FOX', 'guide_index': 1}, {'name': 'CBS', 'guide_index': 3}]


class SundayTicketScanTests(unittest.TestCase):
    def scan(self, rows, game=BILLS, channels=None, **options):
        self.ecp = FakeEcp()
        with tempfile.TemporaryDirectory() as directory:
            controller = RokuStreamController(test_controller.ControllerTests.config(None, Path(directory), channels),
                                              ecp=self.ecp, feedback=GuideFeedback(self.ecp, rows, **options),
                                              sleeper=lambda _: None)
            controller._open_channel_row = lambda *args, **kwargs: {'matched': True, 'channel': 'NBC'}
            anchor = min(controller.config.channels, key=lambda channel: channel.guide_index)
            self.events = []
            return controller._open_sunday_ticket_row(game, anchor, self.events)

    def test_finds_the_live_game_after_two_fresh_observations(self):
        result = self.scan([row('NBC', channel='NBC'), row('Carolina Panthers at Cleveland Browns'),
                            row('Los Angeles Chargers at Buffalo Bills'), row('Jets at Lions')])
        self.assertEqual(result['row_offset'], 2)
        self.assertEqual(self.ecp.keys, ['Down', 'Down'])  # No Select: the caller decides.
        self.assertEqual([o['teams'] for o in result['observations']],
                         [{'away': 'Los Angeles Chargers', 'home': 'Buffalo Bills'}] * 2)

    def test_upcoming_game_is_not_selected(self):
        with self.assertRaisesRegex(NavigationError, 'has not started'):
            self.scan([row('NBC', channel='NBC'), row('Los Angeles Chargers at Buffalo Bills', live=False)])
        self.assertNotIn('Select', self.ecp.keys)

    def test_live_ocr_text_from_the_device_matches(self):
        frame = {'header': BILLS_HEADER, 'row': 'Upcoming: Los Angeles Chargers at Buffalo Bills',
                 'channel': None, 'logo': True}
        with self.assertRaisesRegex(NavigationError, 'has not started'):
            self.scan([row('NBC', channel='NBC'), frame])

    def test_a_local_station_showing_the_game_is_where_it_plays(self):
        # Sunday Ticket blacks out a game a local station carries: take the local row.
        result = self.scan([row('NBC', channel='NBC'), row('Los Angeles Chargers at Buffalo Bills', channel='FOX'),
                            row('Carolina Panthers at Cleveland Browns'), row('Los Angeles Chargers at Buffalo Bills')])
        self.assertEqual((result['surface'], result['local_station'], result['row_offset']),
                         ('local_station_guide_row', 'FOX', 1))
        self.assertEqual(self.ecp.keys, ['Down'])

    def test_only_the_games_own_network_counts_as_its_local_station(self):
        game = {**BILLS, 'broadcasts': [{'name': 'CBS', 'market': 'national'}]}
        result = self.scan([row('NBC', channel='NBC'), row('Los Angeles Chargers at Buffalo Bills', channel='FOX'),
                            row('Los Angeles Chargers at Buffalo Bills')], game=game, channels=CHANNELS)
        self.assertEqual((result['surface'], result['row_offset']), ('sunday_ticket_guide_row', 2))

    def test_a_calibrated_row_after_the_sunday_ticket_block_is_not_taken(self):
        with self.assertRaisesRegex(NavigationError, 'not found'):
            self.scan([row('NBC', channel='NBC'), row('Carolina Panthers at Cleveland Browns'),
                       row('Los Angeles Chargers at Buffalo Bills', channel='FOX'), row('ESPN', channel='ESPN')])

    def test_scan_stops_after_the_sunday_ticket_block(self):
        rows = [row('NBC', channel='NBC'), row('Carolina Panthers at Cleveland Browns'),
                row('New York Jets at Detroit Lions'), row('FOX', channel='FOX')] + [row('x', channel='ESPN')] * 80
        with self.assertRaisesRegex(NavigationError, 'not found') as raised:
            self.scan(rows)
        self.assertEqual(raised.exception.details['rows_read'], 3)
        self.assertTrue(raised.exception.details['sunday_ticket_rows_seen'])

    def test_one_team_is_not_enough(self):
        with self.assertRaisesRegex(NavigationError, 'not found'):
            self.scan([row('NBC', channel='NBC'), row('Los Angeles Chargers at Kansas City Chiefs'),
                       row('ESPN', channel='ESPN')])

    def test_without_a_logo_template_uncalibrated_rows_still_qualify(self):
        result = self.scan([row('NBC', channel='NBC'), row('Los Angeles Chargers at Buffalo Bills')],
                           logo_template=False)
        self.assertEqual(result['row_offset'], 1)


BEARS = {'game_id': 401872999, 'title': 'Chicago Bears at Atlanta Falcons',
         'away': {'name': 'Chicago Bears', 'nickname': 'Bears'},
         'home': {'name': 'Atlanta Falcons', 'nickname': 'Falcons'},
         'broadcasts': [{'name': 'Fox', 'market': 'national'}]}  # ESPN's spelling on October 18.


def fixture(name):
    return PpmFrame.parse(gzip.decompress((Path(__file__).parent / 'fixtures' / f'{name}.ppm.gz').read_bytes()))


class BlackoutCardTests(unittest.TestCase):
    """The 720p card captured on October 18, read by the local OCR."""

    def detect(self, frame):
        with tempfile.TemporaryDirectory() as directory:
            config = test_controller.ControllerTests().config(Path(directory))
            feedback = VisualFeedback(replace(config.feedback, ocr_enabled=True))
            return feedback.detect_youtube_tv_state(frame, config.channels,
                                                    {'state': 'play', 'plugin': {'id': '195316'}})

    def test_the_blackout_card_is_not_a_playing_game(self):
        observation = self.detect(fixture('sunday-ticket-blackout'))
        self.assertEqual(observation['state'], AppState.SUNDAY_TICKET_BLACKOUT)
        self.assertIn('airing on your local station', observation['ocr']['text'])

    def test_a_program_blocked_in_the_area_is_not_a_playing_game(self):
        """2026-10-22: a blacked-out ESPN Unlimited game, Red Wings at Canucks."""
        observation = self.detect(fixture('espn-program-blocked'))
        self.assertEqual(observation['state'], AppState.PROGRAM_BLOCKED)
        self.assertIn('blocked in your area', observation['ocr']['text'])

    def test_a_playing_program_is_still_a_full_program(self):
        self.assertEqual(self.detect(fixture('mlb-playing-video'))['state'], AppState.FULL_PROGRAM)


class PlayerFeedback(GuideFeedback):
    """The guide until Select, then the player screens in `after_select`.
    `station` is the row shown once the controller opens a channel's row."""

    def __init__(self, ecp, rows, after_select):
        super().__init__(ecp, rows)
        self.after_select = list(after_select)
        self.station = None

    def capture(self):
        return self.station or super().capture()

    def assert_ready(self):
        return {'ready': True}

    def detect_youtube_tv_state(self, frame, channels, media_player):
        assert 'Select' in self.ecp.keys
        state = self.after_select.pop(0) if len(self.after_select) > 1 else self.after_select[0]
        return {'state': state, 'evidence': 'test'}


class BlackoutPlaybackTests(unittest.TestCase):
    # The guide on October 18, with OCR missing "Falcons" on local FOX's row, so
    # the scan reaches the Sunday Ticket row that YouTube TV blacked out.
    ROWS = [row('NBC', channel='NBC'), row('Chicago Bears at Atlanta', channel='FOX'),
            row('Carolina Panthers at Philadelphia Eagles'), row('Chicago Bears at Atlanta Falcons')]
    LOCAL_FOX = row('Chicago Bears at Atlanta Falcons', channel='FOX')

    def play(self, after_select, *, rows=ROWS, fox=LOCAL_FOX):
        self.ecp = FakeEcp()
        feedback = PlayerFeedback(self.ecp, rows, after_select)

        def open_row(channel, events, **kwargs):
            if channel.name == 'FOX':
                feedback.station = fox
            return {'matched': True, 'channel': channel.name}

        with tempfile.TemporaryDirectory() as directory:
            controller = RokuStreamController(test_controller.ControllerTests.config(None, Path(directory), CHANNELS),
                                              ecp=self.ecp, feedback=feedback, sleeper=lambda _: None)
            controller._drive_to_live_guide = lambda app_id, events: {'state': AppState.LIVE_GUIDE}
            controller._open_channel_row = open_row
            try:
                return controller.play_sunday_ticket(BEARS)
            finally:
                self.operation = controller.operation_status()

    def test_a_blackout_card_plays_the_game_on_the_local_station(self):
        result = self.play([AppState.SUNDAY_TICKET_BLACKOUT, AppState.FULL_PROGRAM])
        self.assertTrue(result['ok'])
        self.assertEqual((result['provider'], result['source'], result['local_station']),
                         ('youtube_tv', 'FOX (local station)', 'FOX'))
        self.assertEqual(result['verification']['visible_channel_template']['local_station'], 'FOX')
        self.assertEqual(self.ecp.keys.count('Select'), 2)
        self.assertIn('on FOX, the local station', self.operation['message'])

    def test_without_a_station_showing_the_game_the_tune_fails(self):
        with self.assertRaisesRegex(SundayTicketBlackoutError, 'blacked out on Sunday Ticket, and FOX did not show it'):
            self.play([AppState.SUNDAY_TICKET_BLACKOUT], fox=row('FOX NFL Kickoff', channel='FOX'))
        self.assertEqual(self.operation['phase'], 'error')
        self.assertEqual(self.operation['error']['code'], 'sunday_ticket_blackout')
        self.assertEqual(self.operation['error']['details']['local_stations_tried'], ['FOX'])
        self.assertEqual(self.ecp.keys.count('Select'), 1)  # Never Select on an unverified row.

    def test_a_card_that_follows_the_first_playing_frame_is_still_caught(self):
        result = self.play([AppState.FULL_PROGRAM, AppState.SUNDAY_TICKET_BLACKOUT, AppState.FULL_PROGRAM])
        self.assertEqual(result['local_station'], 'FOX')

    def test_a_playing_game_needs_two_frames(self):
        result = self.play([AppState.FULL_PROGRAM])
        self.assertEqual((result['game'], result['source'], result['local_station']),
                         ('Chicago Bears at Atlanta Falcons', 'NFL Sunday Ticket', None))
        self.assertEqual(self.ecp.keys.count('Select'), 1)
        detections = [e for e in result['events'] if e['event'] == 'playback_state_detected']
        self.assertEqual(len(detections), 2)

    def test_the_local_row_seen_on_the_way_plays_without_trying_sunday_ticket(self):
        rows = [row('NBC', channel='NBC'), self.LOCAL_FOX, row('Chicago Bears at Atlanta Falcons')]
        result = self.play([AppState.FULL_PROGRAM], rows=rows)
        self.assertEqual(result['local_station'], 'FOX')
        self.assertEqual(self.ecp.keys, ['Down', 'Select'])

    def test_a_new_tune_leaves_the_blackout_card_with_back(self):
        ecp = FakeEcp()
        feedback = test_controller.FakeFeedback()
        feedback.states = [AppState.SUNDAY_TICKET_BLACKOUT, AppState.LIVE_GUIDE]
        with tempfile.TemporaryDirectory() as directory:
            controller = RokuStreamController(test_controller.ControllerTests.config(None, Path(directory)),
                                              ecp=ecp, feedback=feedback, sleeper=lambda _: None)
            observation = controller._drive_to_live_guide('195316', [])
        self.assertEqual(observation['state'], AppState.LIVE_GUIDE)
        self.assertEqual(ecp.keys, ['Back'])


class SundayTicketListingTests(unittest.TestCase):
    def test_a_fox_game_spelled_fox_is_on_sunday_ticket(self):
        from roku_control.sports import SportsCatalog
        game = {**BEARS, 'league': 'nfl', 'start_time': '2026-10-18T17:00Z',
                'away': {**BEARS['away'], 'abbreviation': 'CHI'}, 'home': {**BEARS['home'], 'abbreviation': 'ATL'}}
        with tempfile.TemporaryDirectory() as directory:
            catalog = SportsCatalog(test_controller.ControllerTests.config(None, Path(directory), CHANNELS),
                                    None, scoreboard=object())
            suggested = catalog._suggest(game)
        self.assertTrue(suggested['on_sunday_ticket'])
        self.assertEqual(suggested['channels'], ['FOX'])


def nfl_game(game_id, kickoff, away, home, network):
    """A game from ESPN's scoreboard; away and home are (name, abbreviation)."""
    def side(name, abbreviation):
        return {'name': name, 'nickname': name.split()[-1], 'abbreviation': abbreviation}
    return {'league': 'nfl', 'game_id': game_id, 'title': f'{away[0]} at {home[0]}', 'start_time': kickoff,
            'state': 'pre', 'status': 'Scheduled', 'finished': False, 'away': side(*away), 'home': side(*home),
            'broadcasts': [{'name': network, 'market': 'national'}]}


def listing(start, minutes, title, episode=None):
    begin = int(datetime.fromisoformat(start).timestamp())
    return {'start': begin, 'duration': minutes * 60, 'title': title,
            **({'episode_title': episode} if episode else {})}


# Sunday, October 25, 2026, as ESPN listed it: the CBS and FOX games are regional.
SLATE = [nfl_game(401873014, '2026-10-25T17:00Z', ('Indianapolis Colts', 'IND'), ('Minnesota Vikings', 'MIN'), 'CBS'),
         nfl_game(401873012, '2026-10-25T17:00Z', ('Cincinnati Bengals', 'CIN'), ('Baltimore Ravens', 'BAL'), 'CBS'),
         nfl_game(401873017, '2026-10-25T17:00Z', ('San Francisco 49ers', 'SF'), ('Atlanta Falcons', 'ATL'), 'Fox'),
         nfl_game(401873018, '2026-10-25T17:00Z', ('Tampa Bay Buccaneers', 'TB'), ('Carolina Panthers', 'CAR'), 'Fox'),
         nfl_game(401873019, '2026-10-25T20:05Z', ('Denver Broncos', 'DEN'), ('Arizona Cardinals', 'ARI'), 'CBS'),
         nfl_game(401873020, '2026-10-25T20:25Z', ('Green Bay Packers', 'GB'), ('Detroit Lions', 'DET'), 'Fox'),
         nfl_game(401873022, '2026-10-26T00:20Z', ('Kansas City Chiefs', 'KC'), ('Seattle Seahawks', 'SEA'), 'NBC')]
# YouTube TV's CBS and FOX that afternoon (yttv_epg.json): Lansing's WLNS and WSYM.
LANSING = {'CBS': {'events': [listing('2026-10-25T12:00-04:00', 60, 'The NFL Today'),
                              listing('2026-10-25T13:00-04:00', 180, 'NFL Football',
                                      'Indianapolis Colts at Minnesota Vikings'),
                              listing('2026-10-25T16:00-04:00', 30, 'NFL Today Postgame'),
                              listing('2026-10-25T16:30-04:00', 30, 'NFL Today Postgame')]},
           'FOX': {'events': [listing('2026-10-25T12:00-04:00', 60, 'FOX NFL Sunday'),
                              listing('2026-10-25T13:00-04:00', 205, 'NFL Football',
                                      'San Francisco 49ers at Atlanta Falcons'),
                              listing('2026-10-25T16:25-04:00', 185, 'NFL Football',
                                      'Green Bay Packers at Detroit Lions')]},
           'NBC': {'events': [listing('2026-10-25T20:20-04:00', 200, 'NFL Football',
                                      'Kansas City Chiefs at Seattle Seahawks')]}}
STATIONS = [{'name': 'NBC', 'guide_index': 0}, {'name': 'FOX', 'guide_index': 1, 'aliases': ['WTTG']},
            {'name': 'CBS', 'guide_index': 3, 'aliases': ['WUSA']}]


class RegionalMarketTests(unittest.TestCase):
    """The Roku's YouTube TV carries Lansing's CBS and FOX; the antenna Washington's."""

    def suggest(self, listings, games=SLATE):
        from roku_control.sports import SportsCatalog
        with tempfile.TemporaryDirectory() as directory:
            catalog = SportsCatalog(test_controller.ControllerTests.config(None, Path(directory), STATIONS),
                                    None, scoreboard=object(), listings=listings)
            return {game['game_id'] % 100: catalog._suggest(game) for game in games}

    @staticmethod
    def where(suggested):
        return {key: (game['suggested_target']['source_id'], game['suggested_target']['channel_id'],
                      game['on_sunday_ticket']) for key, game in suggested.items()}

    def test_youtube_tv_shows_the_games_its_listings_name_and_sunday_ticket_the_rest(self):
        suggested = self.suggest(lambda: LANSING)
        self.assertEqual(self.where(suggested), {
            14: ('youtube-tv', 'CBS', False),            # Lansing's CBS
            12: ('sunday-ticket', '401873012', True),    # Washington's CBS, not Lansing's
            17: ('youtube-tv', 'FOX', False),
            18: ('sunday-ticket', '401873018', True),
            19: ('sunday-ticket', '401873019', True),    # Lansing's CBS had its postgame show
            20: ('youtube-tv', 'FOX', False),
            22: ('youtube-tv', 'NBC', False)})           # national, at night
        self.assertEqual(suggested[14]['markets'], {'youtube_tv': True, 'antenna': False, 'network': 'CBS', 'listed': True})
        self.assertEqual(suggested[12]['markets'], {'youtube_tv': False, 'antenna': True, 'network': 'CBS', 'listed': True})
        self.assertIn("Washington's WUSA has it", suggested[12]['note'])
        self.assertEqual(suggested[22]['markets'], {'youtube_tv': True, 'antenna': True, 'network': None, 'listed': False})

    def test_past_the_listings_youtube_tv_has_the_lions(self):
        def unreachable():
            raise OSError('no guide')
        for listings in (None, lambda: None, unreachable, lambda: {'CBS': {'events': []}}):
            suggested = self.suggest(listings)
            self.assertEqual(self.where(suggested), {
                14: ('sunday-ticket', '401873014', True), 12: ('sunday-ticket', '401873012', True),
                17: ('sunday-ticket', '401873017', True), 18: ('sunday-ticket', '401873018', True),
                19: ('sunday-ticket', '401873019', True), 20: ('youtube-tv', 'FOX', False),
                22: ('youtube-tv', 'NBC', False)}, listings)
            self.assertFalse(suggested[20]['markets']['listed'])

    def test_the_listings_are_worked_out_again_only_when_they_change(self):
        from roku_control.sports import SportsCatalog
        guide = [LANSING]
        catalog = SportsCatalog(None, None, scoreboard=object(), listings=lambda: guide[0])
        first = catalog._youtube_tv_listings()
        self.assertEqual(sorted(first), ['CBS', 'FOX'])
        self.assertIs(catalog._youtube_tv_listings(), first)
        guide[0] = {'CBS': {'events': []}}
        self.assertEqual(catalog._youtube_tv_listings(), {})

    def test_a_station_carries_its_own_markets_game(self):
        from roku_control.sports import SportsCatalog
        scoreboard = type('Scoreboard', (), {'games': lambda self, league, day:
                                             SLATE if (league, day) == ('nfl', '2026-10-25') else []})()
        baseball = type('Baseball', (), {'games': lambda self, day: {'games': []}})()
        now = datetime(2026, 10, 25, 17, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            catalog = SportsCatalog(test_controller.ControllerTests.config(None, Path(directory), STATIONS),
                                    baseball, scoreboard=scoreboard, listings=lambda: LANSING)
            youtube_tv, _ = catalog.games_on({'source_id': 'youtube-tv', 'channel_id': 'CBS'}, now=now)
            antenna, _ = catalog.games_on({'source_id': 'tv-thing-digital', 'channel_id': '9.1'}, 'WUSA-HD', now=now)
        self.assertEqual([game['title'] for game in youtube_tv], ['Indianapolis Colts at Minnesota Vikings'])
        self.assertEqual([game['title'] for game in antenna], ['Cincinnati Bengals at Baltimore Ravens'])


class HeaderTextTests(unittest.TestCase):
    def header(self, text):
        feedback = object.__new__(VisualFeedback)
        feedback.ocr = type('Ocr', (), {'read': lambda self, *a, **k: type('O', (), {'text': text})()})()
        from roku_control.feedback import PpmFrame
        return feedback.read_guide_header(PpmFrame(100, 100, bytes(30000)))

    def test_header_flags(self):
        self.assertEqual(self.header(BILLS_HEADER), {'text': BILLS_HEADER, 'sunday_ticket': True, 'upcoming': True})
        live = self.header('Los Angeles Chargers at Buffalo Bills\nSun, Sep 27, 1:00 PM • NFL ST • FOX')
        self.assertEqual((live['sunday_ticket'], live['upcoming']), (True, False))
        other = self.header('The front office discussed personnel status\nStarts in 5 min')
        self.assertEqual((other['sunday_ticket'], other['upcoming']), (False, True))


if __name__ == '__main__':
    unittest.main()
