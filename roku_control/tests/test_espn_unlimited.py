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


def open_game(frames, **kwargs):
    page, events = Page(frames), []
    result = espn_unlimited.open_game(controller(page), GAME, events, **kwargs)
    return result, page.keys, events


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
        with self.assertRaisesRegex(NavigationError, 'not on the ESPN page'):
            open_game(['menu-live', 'on-now-first-card', 'on-now-countdown', 'on-now-countdown',
                       'on-now-countdown'])

    def test_a_page_that_never_shows_its_menu_is_not_browsed(self):
        with self.assertRaisesRegex(NavigationError, 'Live section'):
            open_game(['page-opened', 'page-opened'])


if __name__ == '__main__':
    unittest.main()
