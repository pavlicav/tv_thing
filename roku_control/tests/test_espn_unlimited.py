"""Finding an ESPN Unlimited game on YouTube TV's ESPN page, on the frames
captured from the Roku on 2026-10-19 and read by the local OCR."""
import gzip
import unittest
from pathlib import Path
from types import SimpleNamespace

from roku_control import espn_unlimited
from roku_control.errors import NavigationError
from roku_control.feedback import PpmFrame
from roku_control.ocr import LocalOcr

FIXTURES = Path(__file__).parent / 'fixtures'
GAME = {'league': 'nhl', 'game_id': 401802741, 'title': 'Colorado Avalanche at Philadelphia Flyers',
        'away': {'name': 'Colorado Avalanche', 'nickname': 'Avalanche', 'abbreviation': 'COL'},
        'home': {'name': 'Philadelphia Flyers', 'nickname': 'Flyers', 'abbreviation': 'PHI'}}
_frames = {}


def frame(name):
    if name not in _frames:
        _frames[name] = PpmFrame.parse(gzip.decompress((FIXTURES / f'espn-{name}.ppm.gz').read_bytes()))
    return _frames[name]


class Page:
    """The Roku on the ESPN page: each key shows the next scripted frame."""

    def __init__(self, frames):
        self.frames, self.keys = list(frames), []
        self.shown = frame('page-opened')
        self.ocr = LocalOcr()

    # The ECP remote.
    def keypress(self, key):
        self.keys.append(key)
        if key not in ('Select',):
            self.shown = frame(self.frames.pop(0))

    # The visual feedback.
    def capture(self):
        return self.shown

    def verify_network_page(self, channel, aliases):
        assert channel == 'ESPN'
        return {'matched': True, 'channel': 'ESPN'}


def controller(page):
    nav = SimpleNamespace(key_delay_seconds=0)
    fb = SimpleNamespace(network_page_settle_seconds=0, verify_attempts=2, verify_retry_seconds=0)
    return SimpleNamespace(feedback=page, ecp=page, config=SimpleNamespace(navigation=nav, feedback=fb),
                           _sleep=lambda seconds: None, _publish_operation=lambda **kwargs: None,
                           _focus_network_tile=lambda events: {'focused': 'network_tile'})


def open_game(frames, game=GAME, **kwargs):
    page, events = Page(frames), []
    result = espn_unlimited.open_game(controller(page), game, events, **kwargs)
    return result, page.keys, events


def team(name, nickname, abbreviation):
    return {'name': name, 'nickname': nickname, 'abbreviation': abbreviation}


# 2026-10-22: Live in rows by sport. Red Wings at Canucks was blacked out in
# the Roku's (Lansing) market; Mammoth at Kraken, an exclusive, was not.
UTAH_SEATTLE = {'league': 'nhl', 'game_id': 401892562, 'title': 'Utah Mammoth at Seattle Kraken',
                'away': team('Utah Mammoth', 'Mammoth', 'UTA'), 'home': team('Seattle Kraken', 'Kraken', 'SEA')}
DETROIT_VANCOUVER = {'league': 'nhl', 'game_id': 401892563, 'title': 'Detroit Red Wings at Vancouver Canucks',
                     'away': team('Detroit Red Wings', 'Red Wings', 'DET'),
                     'home': team('Vancouver Canucks', 'Canucks', 'VAN')}


class EspnPageTests(unittest.TestCase):
    def test_the_focused_card_names_the_game(self):
        card = espn_unlimited.read_card(SimpleNamespace(ocr=LocalOcr()), frame('on-now-avalanche-flyers'), GAME)
        self.assertTrue(card['focused'] and card['matched'] and card['espn_plus'])
        self.assertEqual(card['caption'], 'ESPN+ Colorado Avalanche vs. Philadelphia Flyers')
        other = espn_unlimited.read_card(SimpleNamespace(ocr=LocalOcr()), frame('on-now-first-card'), GAME)
        self.assertTrue(other['focused'])
        self.assertFalse(other['matched'])

    def test_with_the_menu_open_no_card_is_focused(self):
        self.assertFalse(espn_unlimited.card_focused(frame('menu-live')))
        ocr = SimpleNamespace(ocr=LocalOcr())
        self.assertTrue(espn_unlimited.menu_live_focused(ocr, frame('menu-live')))
        # "Upcoming" has read "Jpcoming"; the other sections still name the menu.
        self.assertTrue(espn_unlimited.menu_live_focused(ocr, frame('menu-live-misread')))
        self.assertFalse(espn_unlimited.menu_live_focused(ocr, frame('page-opened')))

    def test_the_home_stream_is_the_games_second_card(self):
        result, keys, events = open_game(['menu-live', 'on-now-first-card', 'on-now-avalanche-flyers',
                                          'on-now-avalanche-flyers'])
        self.assertEqual(keys, ['Select', 'Down', 'Right', 'Right', 'Right'])
        self.assertEqual((result['matched'], result['stream'], result['streams'], result['position']),
                         (True, 'home', 2, 2))
        self.assertEqual(result['surface'], 'espn_on_now_card')

    def test_the_away_stream_is_back_one(self):
        result, keys, _ = open_game(['menu-live', 'on-now-avalanche-flyers', 'on-now-avalanche-flyers',
                                     'on-now-avalanche-flyers'], feed='away')
        self.assertEqual(keys, ['Select', 'Down', 'Right', 'Right', 'Left'])
        self.assertEqual((result['stream'], result['position']), ('away', 0))

    def test_a_game_with_one_card_plays_that_one(self):
        result, keys, _ = open_game(['menu-live', 'on-now-avalanche-flyers', 'on-now-countdown',
                                     'on-now-avalanche-flyers'])
        self.assertEqual(keys[-2:], ['Right', 'Left'])
        self.assertEqual((result['stream'], result['streams']), (None, 1))

    def test_the_end_of_the_row_without_the_game_fails(self):
        with self.assertRaisesRegex(NavigationError, 'not among the ESPN page'):
            open_game(['menu-live', 'on-now-first-card', 'on-now-countdown', 'on-now-countdown',
                       'on-now-countdown'])

    def test_a_page_that_never_shows_its_menu_is_not_browsed(self):
        with self.assertRaisesRegex(NavigationError, 'Live section'):
            open_game(['page-opened', 'page-opened'])


class SportRowTests(unittest.TestCase):
    """The ESPN page as it was on 2026-10-22."""

    def test_row_labels_and_blocked_cards(self):
        ocr = SimpleNamespace(ocr=LocalOcr())
        self.assertEqual(espn_unlimited.read_row_label(ocr, frame('nhl-mammoth-kraken')), 'NHL')
        self.assertEqual(espn_unlimited.read_row_label(ocr, frame('row-ncaaw-soccer')), 'NCAAW Soccer')
        self.assertEqual(espn_unlimited.read_row_label(ocr, frame('on-now-first-card')), 'On Now')
        for name, blocked in (('nhl-red-wings-blocked', True), ('nhl-red-wings-blocked-home', True),
                              ('nhl-mammoth-kraken', False), ('nhl-hurricanes-oilers', False),
                              ('row-ncaaw-soccer', False), ('on-now-avalanche-flyers', False)):
            with self.subTest(name=name):
                self.assertIs(espn_unlimited.card_blocked(frame(name)), blocked)

    def test_other_sports_rows_are_passed_over_down_to_nhl(self):
        result, keys, events = open_game(['menu-live', 'row-ncaaw-soccer', 'nhl-red-wings-blocked',
                                          'nhl-red-wings-blocked-home', 'nhl-mammoth-kraken',
                                          'nhl-hurricanes-oilers', 'nhl-mammoth-kraken'], game=UTAH_SEATTLE)
        self.assertEqual(keys, ['Select', 'Down', 'Right', 'Down', 'Right', 'Right', 'Right', 'Left'])
        self.assertEqual([e['label'] for e in events if e['event'] == 'espn_row'], ['NCAAW Soccer', 'NHL'])
        self.assertEqual((result['stream'], result['streams'], result['position']), (None, 1, 2))

    def test_a_game_blacked_out_on_both_streams_is_never_selected(self):
        with self.assertRaises(NavigationError) as error:
            open_game(['menu-live', 'nhl-red-wings-blocked', 'nhl-red-wings-blocked-home'],
                      game=DETROIT_VANCOUVER)
        self.assertEqual(error.exception.details['code'], 'espn_game_blacked_out')

    def test_no_nhl_row_fails_without_walking_other_sports(self):
        with self.assertRaises(NavigationError) as error:
            open_game(['menu-live', 'row-ncaaw-soccer', 'row-ncaaw-soccer'], game=UTAH_SEATTLE)
        self.assertEqual(error.exception.details['code'], 'espn_game_not_listed')

    def test_the_blocked_in_your_area_card_is_a_blackout(self):
        from roku_control.config import FeedbackConfig
        from roku_control.feedback import VisualFeedback
        feedback = VisualFeedback(FeedbackConfig())
        self.assertEqual(feedback.blackout_card(frame('program-blocked'))['kind'], 'blocked_in_area')
        self.assertIsNone(feedback.blackout_card(frame('playing-away-stream')))


if __name__ == '__main__':
    unittest.main()
