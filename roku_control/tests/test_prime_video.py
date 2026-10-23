import copy
import io
import json
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from roku_control.config import FeedbackConfig, PrimeVideoConfig, RokuConfig, NavigationConfig
from roku_control.ecp import EcpClient, RokuApp
from roku_control.errors import NavigationError
from roku_control.feedback import PpmFrame, VisualFeedback
from roku_control.prime_video import PrimeVideoCatalog, observe, start_game


GAME = {'league': 'nfl', 'game_id': 401872964, 'title': 'Pittsburgh Steelers at Cleveland Browns',
        'away': {'name': 'Pittsburgh Steelers', 'nickname': 'Steelers'},
        'home': {'name': 'Cleveland Browns', 'nickname': 'Browns'},
        'broadcasts': [{'name': 'Prime Video', 'market': 'national'}], 'state': 'in',
        'start_time': '2026-10-02T00:15Z', 'status': 'In Progress', 'finished': False}
EVENT = {'content_id': 'B0GZ57JWXN', 'title': 'Steelers vs. Browns', 'date': '2026-10-01',
         'live': True, 'url': 'https://www.primevideo.com/detail/B0GZ57JWXN'}


def page(body):
    return ('<script type="application/json" id="dv-web-page-hydration-data">' +
            json.dumps({'init': {'preparations': {'body': body}}}) + '</script>').encode()


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.entity = {'titleID': EVENT['content_id'], 'entityType': 'EVENT', 'title': EVENT['title']}
        self.header = {'asin': EVENT['content_id'], 'entityType': 'EVENT', 'title': EVENT['title'],
                       'pageDateTimeBadge': 'Oct 1, 2026 7 PM EDT', 'liveState': {'isLive': True}}
        self.requests = []

    def catalog(self, request, timeout):
        self.requests.append(request.full_url)
        return io.BytesIO(page({'containers': [{'entities': [self.entity, self.entity]}]})
                          if 'tournament/' in request.full_url else
                          page({'atf': {'state': {'detail': {'headerDetail': {'event': self.header}}}}}))

    def resolve(self, game=None):
        return PrimeVideoCatalog(opener=self.catalog).resolve(game or GAME)

    def test_resolves_eastern_date_and_deduplicates_hero_and_card(self):
        self.assertEqual(self.resolve(), EVENT)
        self.assertEqual(len(self.requests), 2)

    def test_wrong_year_or_date_or_team_never_matches(self):
        for change in ({'pageDateTimeBadge': 'Oct 1, 2025 7 PM EDT'},
                       {'pageDateTimeBadge': 'Oct 11, 2026 7 PM EDT'}, {'title': 'Steelers vs. Bengals'}):
            with self.subTest(change=change):
                original = self.header.copy()
                self.header.update(change)
                with self.assertRaisesRegex(NavigationError, 'uniquely match'):
                    self.resolve()
                self.header = original

    def test_not_yet_live_and_replays_fail(self):
        self.header['liveState']['isLive'] = False
        with self.assertRaisesRegex(NavigationError, 'not started'):
            self.resolve()
        with self.assertRaisesRegex(NavigationError, 'ended'):
            self.resolve({**GAME, 'state': 'post'})

    def test_non_prime_and_schema_changes_fail_without_guessing(self):
        with self.assertRaisesRegex(NavigationError, 'not listed'):
            self.resolve({**GAME, 'broadcasts': [{'name': 'NBC'}]})
        self.assertEqual(self.requests, [])
        with self.assertRaisesRegex(NavigationError, 'metadata changed'):
            PrimeVideoCatalog(opener=lambda *a, **k: io.BytesIO(b'<html>Sign in</html>')).resolve(GAME)

    def test_deep_link_query_is_encoded(self):
        client = EcpClient('http://roku')
        with patch.object(client, '_request') as request:
            client.launch('13', content_id='a&b', media_type='live')
        request.assert_called_once_with('POST', 'launch/13?contentID=a%26b&mediaType=live')


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.ecp = Mock()
        self.ecp.device_info.return_value = {'ecp-setting-mode': 'enabled'}
        self.ecp.apps.return_value = [RokuApp('13', 'Prime Video')]
        self.ecp.active_app.return_value = RokuApp('13', 'Prime Video')
        self.player = {'state': 'play', 'plugin': {'id': '13'}, 'format': {'video': 'hevc_b', 'audio': 'aac_adts'}}
        self.ecp.media_player.return_value = self.player
        self.controller = Mock(ecp=self.ecp, feedback=Mock(assert_ready=Mock()), config=SimpleNamespace(
            roku=RokuConfig(), navigation=NavigationConfig(), prime_video=PrimeVideoConfig()))
        self.controller.feedback.capture.return_value = SimpleNamespace(rgb=bytes(range(256)) * 24)

    def run_game(self, observations):
        with patch('roku_control.prime_video.observe', side_effect=observations):
            return start_game(self.controller, GAME, EVENT, [])

    def test_profile_then_two_verified_live_overlays(self):
        result = self.run_game([{'state': 'profile', 'focused_profile': True, 'matched': False},
                                *[{'state': 'game_overlay', 'matched': True}] * 2])
        self.assertTrue(result['selected_prime_event']['matched'])
        self.ecp.launch.assert_called_once_with('13', content_id=EVENT['content_id'], media_type='live')
        self.assertEqual([c.args[0] for c in self.ecp.keypress.call_args_list], ['Home', 'Select'])

    def test_profile_then_event_page_watch_then_live_overlays(self):
        self.run_game([{'state': 'profile', 'focused_profile': True, 'matched': False},
                       *[{'state': 'event_page', 'matched': False}] * 2,
                       *[{'state': 'game_overlay', 'matched': True}] * 2])
        self.assertEqual([c.args[0] for c in self.ecp.keypress.call_args_list], ['Home', 'Select', 'Select'])

    def test_event_page_then_watch_live_menu(self):
        """2026-10-22: Watch opened a menu with Watch Live / Main Broadcast focused."""
        self.run_game([{'state': 'event_page', 'matched': False}, {'state': 'watch_menu', 'matched': False},
                       *[{'state': 'game_overlay', 'matched': True}] * 2])
        self.assertEqual([c.args[0] for c in self.ecp.keypress.call_args_list], ['Home', 'Select', 'Select'])

    def test_overlay_is_revealed_with_down_and_may_fade_between_matches(self):
        """2026-10-22: the title shows for a few seconds after Down; Up opened
        Prime's Stats panel, which stayed on screen."""
        self.run_game([{'state': 'unknown', 'matched': False}, {'state': 'game_overlay', 'matched': True},
                       {'state': 'unknown', 'matched': False}, {'state': 'game_overlay', 'matched': True}])
        keys = [c.args[0] for c in self.ecp.keypress.call_args_list]
        self.assertEqual(keys, ['Home', 'Down', 'Down'])

    def test_event_page_that_stays_gets_two_watch_selects_at_most(self):
        with self.assertRaisesRegex(NavigationError, 'could not be visually verified'):
            self.run_game([{'state': 'event_page', 'matched': False}] * 24)
        self.assertEqual([c.args[0] for c in self.ecp.keypress.call_args_list], ['Home', 'Select', 'Select'])

    def test_sign_in_subscription_and_wrong_profile_are_actionable(self):
        for state in ('sign_in', 'subscription', 'profile'):
            with self.subTest(state=state):
                self.ecp.reset_mock()
                with self.assertRaises(NavigationError) as error:
                    self.run_game([{'state': state, 'matched': False}])
                self.assertIn('action_required', error.exception.details)
                self.assertEqual([c.args[0] for c in self.ecp.keypress.call_args_list], ['Home'])

    def test_existing_player_with_wrong_game_never_becomes_ready(self):
        with self.assertRaisesRegex(NavigationError, 'could not be visually verified'):
            self.run_game([{'state': 'unknown', 'matched': False}] * 24)
        keys = [c.args[0] for c in self.ecp.keypress.call_args_list]
        self.assertNotIn('Select', keys)
        self.assertNotIn('Up', keys)
        self.assertLessEqual(keys.count('Down'), 6)

    def test_audio_only_and_wrong_plugin_are_rejected(self):
        for change in ({'format': {'audio': 'aac', 'video': 'none'}}, {'plugin': {'id': '195316'}}):
            with self.subTest(change=change):
                self.ecp.media_player.return_value = {**self.player, **change}
                with self.assertRaises(NavigationError):
                    self.run_game([{'state': 'game_overlay', 'matched': True}] * 24)

    def test_another_screen_between_matches_starts_over(self):
        observations = [{'state': 'game_overlay', 'matched': True}, {'state': 'loading', 'matched': False}]*12
        with self.assertRaises(NavigationError):
            self.run_game(observations)

    def test_playback_stopping_between_matches_starts_over(self):
        states = iter(['play', 'buffer'] * 12)
        self.ecp.media_player.side_effect = lambda: {**self.player, 'state': next(states)}
        observations = [{'state': 'game_overlay', 'matched': True}, {'state': 'unknown', 'matched': False}]*12
        with self.assertRaises(NavigationError):
            self.run_game(observations)


class ObservedFrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feedback = VisualFeedback(FeedbackConfig())
        cls.frames = {}
        for name in ('prime-profile', 'prime-profile-alone', 'prime-splash', 'prime-event-page',
                     'prime-event-page-two-lines', 'prime-watch-menu', 'prime-live-overlay',
                     'prime-live-overlay-lwe', 'prime-stats-panel'):
            raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(Path(__file__).parent / 'fixtures' / (name+'.jpg')),
                                  '-f', 'image2pipe', '-vcodec', 'ppm', '-'], check=True, capture_output=True).stdout
            cls.frames[name] = PpmFrame.parse(raw)

    def test_actual_focused_profile_and_other_name(self):
        result = observe(self.feedback, self.frames['prime-profile'], GAME, 'Vivian')
        self.assertEqual(result['state'], 'profile')
        self.assertTrue(result['focused_profile'])
        self.assertFalse(observe(self.feedback, self.frames['prime-profile'], GAME, 'Someone else')['focused_profile'])

    def test_chooser_without_a_kids_profile(self):
        """2026-10-15 and 10-22: only the profile and a dim "New" were left; the
        chooser went unrecognized until every attempt ran out."""
        result = observe(self.feedback, self.frames['prime-profile-alone'], GAME, 'Vivian')
        self.assertEqual(result, {'state': 'profile', 'matched': False, 'focused_profile': True})
        self.assertEqual(observe(self.feedback, self.frames['prime-profile-alone'], GAME, 'Someone else')['state'],
                         'unknown')

    def test_event_page_with_watch_focused_only_for_this_game(self):
        """2026-10-15: the deep link stopped on the event's page, Watch focused."""
        seahawks_broncos = {**GAME, 'away': {'name': 'Seattle Seahawks', 'nickname': 'Seahawks'},
                            'home': {'name': 'Denver Broncos', 'nickname': 'Broncos'}}
        self.assertEqual(observe(self.feedback, self.frames['prime-event-page'], seahawks_broncos, 'Vivian')['state'],
                         'event_page')
        self.assertEqual(observe(self.feedback, self.frames['prime-event-page'], GAME, 'Vivian')['state'], 'unknown')
        # 2026-10-22: a two-line description put Watch 22 px lower.
        patriots_bears = {**GAME, 'away': {'name': 'New England Patriots', 'nickname': 'Patriots'},
                          'home': {'name': 'Chicago Bears', 'nickname': 'Bears'}}
        self.assertEqual(observe(self.feedback, self.frames['prime-event-page-two-lines'], patriots_bears,
                                 'Vivian')['state'], 'event_page')

    def test_watch_live_menu_for_this_game_only(self):
        patriots_bears = {**GAME, 'away': {'name': 'New England Patriots', 'nickname': 'Patriots'},
                          'home': {'name': 'Chicago Bears', 'nickname': 'Bears'}}
        self.assertEqual(observe(self.feedback, self.frames['prime-watch-menu'], patriots_bears, 'Vivian')['state'],
                         'watch_menu')
        self.assertEqual(observe(self.feedback, self.frames['prime-watch-menu'], GAME, 'Vivian')['state'], 'unknown')

    def test_overlay_whose_live_badge_reads_lwe(self):
        """2026-10-22 (Prime 16.0.383): OCR read the red LIVE badge as "LWE"."""
        patriots_bears = {**GAME, 'away': {'name': 'New England Patriots', 'nickname': 'Patriots'},
                          'home': {'name': 'Chicago Bears', 'nickname': 'Bears'}}
        self.assertTrue(observe(self.feedback, self.frames['prime-live-overlay-lwe'], patriots_bears, '')['matched'])
        self.assertFalse(observe(self.feedback, self.frames['prime-live-overlay-lwe'], GAME, '')['matched'])
        # What Up opens instead: Stats, with no title to verify.
        self.assertFalse(observe(self.feedback, self.frames['prime-stats-panel'], patriots_bears, '')['matched'])

    def test_launch_splash_is_loading_without_ocr(self):
        # Full-frame OCR of this splash ran past two minutes.
        with patch.object(self.feedback.ocr, 'read', side_effect=AssertionError('OCR ran')):
            self.assertEqual(observe(self.feedback, self.frames['prime-splash'], GAME, 'Vivian')['state'], 'loading')

    def test_actual_live_overlay_identifies_both_teams(self):
        self.assertTrue(observe(self.feedback, self.frames['prime-live-overlay'], GAME, '')['matched'])
        other = copy.deepcopy(GAME)
        other['home'] = {'nickname': 'Bengals', 'name': 'Cincinnati Bengals'}
        self.assertFalse(observe(self.feedback, self.frames['prime-live-overlay'], other, '')['matched'])


if __name__ == '__main__':
    unittest.main()
