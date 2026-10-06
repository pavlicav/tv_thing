import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from roku_control.calibration import calibration_status, device_diagnostics
from roku_control.config import FeedbackConfig
from roku_control.lane_feedback import LaneCaptureFeedback


class CalibrationTests(unittest.TestCase):
    def feedback(self):
        feedback = LaneCaptureFeedback(FeedbackConfig(
            backend='lane', device='/dev/v4l/by-id/usb-AMBA_Hauppauge_HD_PVR_SERIAL-video-index0'),
            lambda: {'running': True, 'stream_id': 'capture-1', 'tap_url': 'udp://127.0.0.1:1'})
        self.addCleanup(feedback.close)
        return feedback

    def test_target_requires_detected_progressive_720_and_60_fps(self):
        feedback = self.feedback()
        good = dict(state='locked', width=1280, height=720, scan='progressive', rate=60)
        for difference, matches in [({}, True), ({'rate': 59.94}, True), ({'rate': 30}, False),
                                    ({'rate': None}, False), ({'scan': 'interlaced'}, False),
                                    ({'width': 1920, 'height': 1080}, False),
                                    ({'state': 'no_signal'}, False)]:
            with self.subTest(difference=difference):
                feedback.input_signal = Mock(return_value={**good, **difference})
                with patch('roku_control.calibration.device_diagnostics', return_value={}):
                    self.assertEqual(calibration_status(feedback)['target']['matches'], matches)

    def test_missing_node_distinguishes_matching_usb_and_unbound_driver(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            device = root / 'usb-AMBA_Hauppauge_HD_PVR_SERIAL-video-index0'
            usb = root / 'usb'
            unit = usb / '2-4'
            unit.mkdir(parents=True)
            (unit / 'serial').write_text('DIFFERENT')
            (unit / 'product').write_text('Hauppauge HD PVR')
            self.assertEqual(device_diagnostics(device, usb)['state'], 'missing')
            (unit / 'serial').write_text('SERIAL')
            interface = unit / '2-4:1.0'
            interface.mkdir()
            result = device_diagnostics(device, usb)
            self.assertEqual(result['state'], 'driver_missing')
            self.assertEqual(result['usb']['port'], '2-4')
            driver = root / 'hdpvr'
            driver.mkdir()
            (interface / 'driver').symlink_to(driver)
            result = device_diagnostics(device, usb)
            self.assertEqual(result['state'], 'node_missing')
            self.assertEqual(result['usb']['drivers'], ['hdpvr'])

    def test_permissions_and_invalid_paths_are_separate_from_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'ordinary-file'
            path.touch()
            self.assertEqual(device_diagnostics(path, root)['state'], 'invalid')
            with patch('roku_control.calibration.os.access', return_value=False):
                self.assertEqual(device_diagnostics(Path('/dev/null'), root)['state'], 'permission_denied')
            self.assertEqual(device_diagnostics(Path('/dev/null'), root)['state'], 'ready')

    def test_fast_queries_are_coalesced_and_expire_without_capture_lock(self):
        feedback = self.feedback()
        feedback._query_input_signal = Mock(side_effect=[
            {'state': 'locked', 'width': 1280}, {'state': 'no_signal'}])
        # A navigation capture can be waiting on a frame while diagnostics proceed.
        with feedback._capture_lock, patch('roku_control.lane_feedback.time.monotonic', side_effect=[10, 10.1, 10.3, 10.3]):
            first = feedback.input_signal()
            first['state'] = 'changed by caller'
            second = feedback.input_signal()
            third = feedback.input_signal()
        self.assertEqual(second['state'], 'locked')
        self.assertEqual(third['state'], 'no_signal')
        self.assertIn('observed_at', third)
        self.assertEqual(feedback._query_input_signal.call_count, 2)

    def test_timeout_clears_old_resolution(self):
        import subprocess
        feedback = self.feedback()
        feedback._device_ready = Mock(return_value=True)
        with patch('roku_control.lane_feedback.subprocess.run', side_effect=subprocess.TimeoutExpired('v4l2', 2)):
            signal = feedback.input_signal()
        self.assertEqual(signal['state'], 'unknown')
        self.assertNotIn('width', signal)
        self.assertIn('timed out', signal['label'])

if __name__ == '__main__':
    unittest.main()
