import sys
import time
import unittest
from unittest.mock import Mock, patch

from roku_control.config import FeedbackConfig
from roku_control.errors import CueCancelledError, NavigationError
from roku_control.feedback import PpmFrame
from roku_control.lane_feedback import LaneCaptureFeedback

TAP = 'udp://127.0.0.1:18360?fifo_size=65536&overrun_nonfatal=1'


class LaneFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.source = {'running': True, 'stream_id': 'capture-1', 'tap_url': TAP}
        self.streams = []

    def stream(self, *frames):
        stream = Mock()
        stream.process.poll.return_value = None
        stream.error = None
        stream.next_frame.side_effect = list(frames) if len(frames) > 1 else None
        if len(frames) == 1:
            stream.next_frame.return_value = frames[0]
        self.streams.append(stream)
        return stream

    def feedback(self, *streams, idle_seconds=60):
        factory = Mock(side_effect=list(streams))
        feedback = LaneCaptureFeedback(
            FeedbackConfig(backend='lane', device='/dev/video1', ocr_enabled=False,
                           source_name='Hauppauge HD PVR — Component'),
            lambda: dict(self.source), idle_seconds=idle_seconds, stream_factory=factory)
        self.addCleanup(feedback.close)
        return feedback, factory

    def test_decoder_reads_the_lane_tap_and_never_the_device(self):
        feedback, factory = self.feedback(self.stream(PpmFrame(1, 1, b'abc')))
        with patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected HTTP')):
            self.assertEqual(feedback.capture().rgb, b'abc')
        command = factory.call_args.args[0]
        self.assertEqual(command[command.index('-i') + 1], TAP)
        self.assertNotIn('/dev/video1', command)
        self.assertEqual(command[command.index('-f') + 1], 'mpegts')
        self.assertEqual(command[-1], 'pipe:1')
        # One filter thread per core made this decoder 58 threads (TasksMax).
        self.assertEqual(command[command.index('-filter_threads') + 1], '2')

    def test_evidence_is_fresh_and_tied_to_the_lane_capture_generation(self):
        feedback, factory = self.feedback(self.stream(PpmFrame(1, 1, b'abc')))
        first = feedback.capture()
        evidence = dict(feedback.last_capture)
        self.assertTrue(evidence['fresh'])
        self.assertEqual(evidence['stream_id'], 'capture-1')
        feedback.observe()  # A preview or still-playing check is not cue evidence.
        self.assertIs(feedback.last_frame, first)
        self.assertEqual(feedback.last_capture, evidence)
        feedback.capture()
        self.assertEqual(feedback.last_capture['sequence'], evidence['sequence'] + 1)
        factory.assert_called_once()

    def test_restarted_lane_capture_reopens_the_decoder(self):
        first, second = self.stream(PpmFrame(1, 1, b'abc')), self.stream(PpmFrame(1, 1, b'def'))
        feedback, factory = self.feedback(first, second)
        feedback.capture()
        self.source['stream_id'] = 'capture-2'
        self.assertEqual(feedback.capture().rgb, b'def')
        first.close.assert_called_once()
        self.assertEqual(feedback.last_capture['stream_id'], 'capture-2')
        self.assertEqual(factory.call_count, 2)

    def test_capture_restart_during_an_observation_is_not_evidence(self):
        stream = self.stream(PpmFrame(1, 1, b'abc'))
        feedback, _ = self.feedback(stream)

        def restart(*args):
            self.source['stream_id'] = 'capture-2'
            return PpmFrame(1, 1, b'abc')
        stream.next_frame.side_effect = restart
        with self.assertRaisesRegex(NavigationError, 'restarted during observation'):
            feedback.capture()
        self.assertIsNone(feedback.last_capture)
        stream.close.assert_called_once()

    def test_stopped_lane_refuses_and_closes_the_decoder(self):
        stream = self.stream(PpmFrame(1, 1, b'abc'))
        feedback, _ = self.feedback(stream)
        feedback.capture()
        self.source.update(running=False, stream_id=None)
        with self.assertRaisesRegex(NavigationError, 'not running'):
            feedback.capture()
        stream.close.assert_called_once()

    def test_failed_decoder_is_recorded_and_replaced_on_the_next_request(self):
        first = self.stream(PpmFrame(1, 1, b'abc'), NavigationError('stopped'))
        second = self.stream(PpmFrame(1, 1, b'def'))
        feedback, _ = self.feedback(first, second)
        feedback.capture()
        with self.assertRaises(NavigationError):
            feedback.capture()
        first.close.assert_called_once()
        self.assertEqual(feedback.last_stream_failure['error'], 'stopped')
        self.assertEqual(feedback.capture().rgb, b'def')

    def failing_stream(self, message, *, error=None, returncode=None):
        stream = self.stream()
        stream.next_frame.side_effect = NavigationError(message)
        stream.error = error
        stream.process.poll.return_value = returncode
        return stream

    def test_decoder_that_exits_before_a_frame_is_started_once_more(self):
        # 2026-10-13: the unit's task cap refused one of ffmpeg's threads and
        # the decoder exited; three Roku tunes failed on their first frame.
        ended = 'Capture stream ended or returned an invalid image'
        first = self.failing_stream(ended, error=ended)
        second = self.stream(PpmFrame(1, 1, b'def'))
        feedback, factory = self.feedback(first, second)
        feedback.DECODER_RETRY_SECONDS = 0
        self.assertEqual(feedback.capture().rgb, b'def')
        first.close.assert_called_once()
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(feedback.last_stream_failure['attempt'], 1)
        self.assertEqual(feedback.last_capture['stream_id'], 'capture-1')

    def test_decoder_that_exits_twice_fails_the_observation(self):
        first = self.failing_stream('Capture stream stopped', returncode=1)
        second = self.failing_stream('Capture stream stopped', returncode=1)
        feedback, factory = self.feedback(first, second)
        feedback.DECODER_RETRY_SECONDS = 0
        with self.assertRaisesRegex(NavigationError, 'stopped'):
            feedback.capture()
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(feedback.last_stream_failure['attempt'], 2)
        self.assertIsNone(feedback.last_capture)

    def test_decoder_that_timed_out_is_not_restarted_within_the_observation(self):
        stream = self.failing_stream('timed out waiting for a fresh frame')
        feedback, factory = self.feedback(stream)
        with self.assertRaisesRegex(NavigationError, 'timed out'):
            feedback.capture()
        factory.assert_called_once()
        stream.close.assert_called_once()

    def test_stopped_lane_during_the_retry_is_not_retried(self):
        ended = 'Capture stream ended or returned an invalid image'
        stream = self.failing_stream(ended, error=ended)
        feedback, factory = self.feedback(stream)
        feedback.wait = lambda _seconds: self.source.update(running=False, stream_id=None)
        with self.assertRaisesRegex(NavigationError, 'not running'):
            feedback.capture()
        factory.assert_called_once()

    def test_cancelled_observation_keeps_the_decoder(self):
        stream = self.stream(PpmFrame(1, 1, b'abc'))
        stream.next_frame.side_effect = CueCancelledError('stop')
        feedback, _ = self.feedback(stream)
        with self.assertRaises(CueCancelledError):
            feedback.capture()
        stream.close.assert_not_called()
        self.assertIsNone(feedback.last_capture)

    def test_idle_decoder_stops_and_restarts_on_demand(self):
        first, second = self.stream(PpmFrame(1, 1, b'abc')), self.stream(PpmFrame(1, 1, b'def'))
        feedback, factory = self.feedback(first, second, idle_seconds=.1)
        feedback.capture()
        self.assertTrue(feedback.decoding)
        deadline = time.monotonic() + 3
        while feedback.decoding and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(feedback.decoding)
        first.close.assert_called_once()
        self.assertEqual(feedback.capture().rgb, b'def')
        self.assertEqual(factory.call_count, 2)

    def test_slow_encoder_receives_entire_frame_after_polling_timeout(self):
        feedback, _ = self.feedback()
        payload = b'frame' * 300_000
        result = feedback._run([
            sys.executable, '-c',
            'import sys,time; time.sleep(.4); print(len(sys.stdin.buffer.read()))',
        ], input_data=payload)
        self.assertEqual(int(result), len(payload))

    def test_input_resolution_uses_detected_timings_and_rejects_stale_size(self):
        output = ('Active width: 1920\nActive height: 1080\nFrame format: interlaced\n'
                  'Pixelclock: 74250000 Hz (60.00 fields per second)\n')
        signal = LaneCaptureFeedback._parse_input_signal(0, output)
        self.assertEqual(signal['label'], '1920 × 1080 · interlaced · 60.00 fields/s')
        missing = LaneCaptureFeedback._parse_input_signal(
            1, output + 'VIDIOC_QUERY_DV_TIMINGS: failed: No locks available')
        self.assertEqual(missing['state'], 'no_signal')
        self.assertNotIn('width', missing)

    def test_single_source_catalog(self):
        feedback, _ = self.feedback()
        with patch.object(LaneCaptureFeedback, '_device_ready', return_value=True):
            catalog = feedback.source_catalog()
        self.assertEqual(catalog['sources'][0]['name'], 'Hauppauge HD PVR — Component')
        self.assertTrue(catalog['ready'])


if __name__ == '__main__':
    unittest.main()
