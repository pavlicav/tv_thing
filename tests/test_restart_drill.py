"""The restart drill's judgement (scripts/restart_drill.py): playlists, generations, recordings
and VizelVision, from fixture data. Nothing here touches a server."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("restart_drill", Path(__file__).resolve().parent.parent / "scripts" / "restart_drill.py")
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:{seq}
#EXTINF:4.000,
digitalseg_1_{seq}.ts
#EXTINF:4.000,
digitalseg_1_{next}.ts
"""

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
0.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000
1.m3u8
"""


def status(**lanes):
    out = {"lanes": {}, "locks": {}}
    for name, (generation, selection) in lanes.items():
        out["lanes"][name] = {"selection": {"id": selection} if name != "roku" else {"channel_id": selection},
                              "stream": {"active": generation is not None, "generation": generation,
                                         "playback": [{"protocol": "hls", "url": f"stream/{name}.m3u8"}]}}
    return out


class PlaylistTests(unittest.TestCase):
    def test_media_playlist(self):
        self.assertEqual(drill.parse_playlist(PLAYLIST.format(seq=41, next=42)), (41, 4.0, 2))

    def test_not_a_playlist(self):
        for text in ("", "<html>busy</html>", "#EXTM3U\n#EXT-X-TARGETDURATION:4\n"):
            self.assertIsNone(drill.parse_playlist(text))

    def test_master_playlist_variant(self):
        self.assertEqual(drill.first_variant(MASTER), "0.m3u8")
        self.assertIsNone(drill.first_variant(PLAYLIST.format(seq=1, next=2)))


class AdvanceTests(unittest.TestCase):
    def test_stalls_past_target_plus_slack(self):
        tracker = drill.Advance(0)
        self.assertFalse(tracker.update(0, (10, 4.0, 5)))
        self.assertFalse(tracker.update(5.9, (10, 4.0, 5)))       # 4 + 2 s slack
        self.assertTrue(tracker.update(6.1, (10, 4.0, 5)))
        self.assertFalse(tracker.update(7, (11, 4.0, 5)))         # moving again
        self.assertAlmostEqual(tracker.worst, 6.1)

    def test_a_new_streams_growing_list_is_advancing(self):
        # A fresh encoder's playlist stays at sequence 0 while it fills to its list size.
        tracker = drill.Advance(0)
        for second, segments in enumerate(range(3, 20)):
            self.assertFalse(tracker.update(second, (0, 1.0, segments)))
        self.assertFalse(tracker.update(17, (1, 1.0, 20)))         # full: the sequence moves
        self.assertLess(tracker.worst, 1.5)
        self.assertTrue(tracker.update(20.1, (1, 1.0, 20)))        # then nothing new: stalled

    def test_an_unreadable_playlist_counts_as_still(self):
        tracker = drill.Advance(0)
        tracker.update(0, (10, 4.0, 5))
        self.assertFalse(tracker.update(3, None))
        self.assertTrue(tracker.update(6.5, None))


class LaneTests(unittest.TestCase):
    def test_running_lanes(self):
        lanes = drill.running_lanes(status(digital=("g1", "wzdc_44_1"), analog=(None, None), roku=("c1.2", "FOX")))
        self.assertEqual(set(lanes), {"digital", "roku"})
        self.assertEqual(lanes["roku"]["selection"], "FOX")
        self.assertEqual(lanes["digital"]["playlist"], "stream/digital.m3u8")

    def test_generation_and_selection_changes_fail(self):
        before = drill.running_lanes(status(digital=("g1", "a"), roku=("c1.2", "FOX")))
        verdict = drill.Verdict()
        drill.compare_lanes(before, drill.running_lanes(status(digital=("g1", "a"), roku=("c1.2", "FOX"))), verdict, expect_change=False)
        self.assertEqual(verdict.failures, [])
        drill.compare_lanes(before, drill.running_lanes(status(digital=("g2", "a"), roku=("c1.3", "FOX"))), verdict, expect_change=False)
        self.assertEqual([f["invariants"] for f in verdict.failures], [[1], [1, 5]])
        verdict = drill.Verdict()
        drill.compare_lanes(before, drill.running_lanes(status(digital=("g1", "b"))), verdict, expect_change=False)
        self.assertEqual([f["message"] for f in verdict.failures],
                         ["the digital lane's selection changed (a → b)", "the roku lane stopped running"])

    def test_expected_changes_pass(self):
        before = drill.running_lanes(status(digital=("g1", "a")))
        verdict = drill.Verdict()
        drill.compare_lanes(before, drill.running_lanes(status(digital=("g9", "a"))), verdict, expect_change=True)
        self.assertEqual(verdict.failures, [])


class RecordingTests(unittest.TestCase):
    def test_growth(self):
        tracker = drill.Growth(0, 1000)
        self.assertFalse(tracker.update(10, 1000))
        self.assertFalse(tracker.update(15, 2000))
        self.assertFalse(tracker.update(35, 2000))
        self.assertTrue(tracker.update(35.5, None))

    def test_active_recordings(self):
        payload = {"active_recordings": [{"id": "t1", "bytes": 5000, "end": 1800}, {"id": "t2"}, "junk"]}
        self.assertEqual(drill.active_recordings(payload), {"t1": (5000, 1800), "t2": (None, None)})
        self.assertEqual(drill.active_recordings(None), {})

    def test_gone_before_or_at_its_end(self):
        verdict = drill.Verdict()
        drill.recording_gone("t1", 1800, 1797, verdict)
        self.assertEqual(verdict.failures, [])
        drill.recording_gone("t2", 1800, 1000, verdict)
        drill.recording_gone("t3", None, 1000, verdict)
        self.assertEqual([f["invariants"] for f in verdict.failures], [[1, 4], [1, 4]])


class VizelVisionTests(unittest.TestCase):
    def master(self, programme="p1", failure_at=None):
        return {"on_air": {"id": programme, "source_id": "internet", "channel_id": "cabride"} if programme else None,
                "last_input_failure": {"at": failure_at} if failure_at else None}

    def test_steady(self):
        verdict = drill.Verdict()
        base = drill.on_air(self.master(failure_at=100.0))
        drill.compare_vizelvision(base, drill.on_air(self.master(failure_at=100.0)), verdict, expect_change=False)
        self.assertEqual(verdict.failures, [])
        self.assertEqual(drill.internet_on_air(self.master()), "cabride")

    def test_off_air_put_back_and_new_failures(self):
        base = drill.on_air(self.master())
        for current, message in ((self.master(None), "off air"), (self.master("p2"), "changed"),
                                 (self.master(failure_at=200.0), "new input failure")):
            verdict = drill.Verdict()
            drill.compare_vizelvision(base, drill.on_air(current), verdict, expect_change=False)
            self.assertRegex(verdict.failures[0]["message"], message)

    def test_expected_change_must_be_back_on_air_at_the_end(self):
        base = drill.on_air(self.master())
        verdict = drill.Verdict()
        drill.compare_vizelvision(base, drill.on_air(self.master(None)), verdict, expect_change=True)
        self.assertEqual(verdict.failures, [])
        drill.compare_vizelvision(base, drill.on_air(self.master(None)), verdict, expect_change=True, final=True)
        self.assertEqual(len(verdict.failures), 1)
        verdict = drill.Verdict()
        drill.compare_vizelvision(base, drill.on_air(self.master("p2")), verdict, expect_change=True, final=True)
        self.assertEqual(verdict.failures, [])

    def test_nothing_on_air_at_the_start_is_not_judged(self):
        verdict = drill.Verdict()
        drill.compare_vizelvision(drill.on_air(self.master(None)), drill.on_air(self.master(None)), verdict, expect_change=False)
        self.assertEqual(verdict.failures, [])


if __name__ == "__main__":
    unittest.main()
