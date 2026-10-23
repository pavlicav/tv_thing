"""The real-stream gate: lane locks (nothing else moves while it measures),
and the CPU it charges the server's process tree."""
import io
import json
import unittest
import urllib.error
from unittest import mock

import stream_sanity_check as gate


class FakeServer:
    def __init__(self, lanes, held=None, roku_busy=0):
        self.lanes, self.held = lanes, held or {}
        self.locked, self.released, self.cancelled = {}, [], []
        self.roku_busy = roku_busy   # /api/v1/roku polls that still show a navigation


    def http_json(self, base_url, path, method="GET", payload=None, timeout=5):
        if path == "/api/v1/status":
            return {"lanes": self.lanes}
        if path == "/api/v1/sources/digital":
            return {"items": [{"id": "a", "number": "7.1", "name": "A", "frequency": 1, "rf_channel": 7},
                              {"id": "b", "number": "7.2", "name": "B", "frequency": 1, "rf_channel": 7}]}
        if path == "/api/v1/tuner/lock":
            lane = payload["lane"]
            if lane in self.held:
                body = json.dumps({"lock": {"owner": self.held[lane]}}).encode()
                raise urllib.error.HTTPError(path, 423, "Locked", {}, io.BytesIO(body))
            self.locked[lane] = f"token-{lane}"
            return {"ok": True, "token": self.locked[lane]}
        if path == "/api/v1/roku":
            busy = self.roku_busy > 0
            self.roku_busy -= 1
            return {"lane": {"roku": {"operation": {"active": busy}}},
                    "calibration": {"capture": {"decoding": busy}}}
        if path == "/api/v1/roku/cancel":
            self.cancelled.append(payload["token"])
            return {"ok": True}
        if path == "/api/v1/tuner/lock/release":
            self.released.append((payload["lane"], payload["token"]))
            return {"ok": True}
        raise AssertionError(path)


class GateLockTests(unittest.TestCase):
    LANES = {"digital": {}, "analog": {}, "roku": {"available": False}}

    def run_gate(self, server, tune):
        with mock.patch.object(gate, "http_json", server.http_json), \
                mock.patch.object(gate, "tune_and_observe", tune), \
                mock.patch.object(gate.signal, "signal"), \
                mock.patch.object(gate.time, "sleep"), \
                mock.patch("builtins.print") as printed:
            result = gate.main(["--url", "http://gate.invalid", "--server-pid", "1", "--retunes", "2"])
        self.output = "\n".join(" ".join(map(str, call.args)) for call in printed.call_args_list)
        return result

    def test_digital_gate_locks_every_available_lane_and_tunes_with_its_token(self):
        server = FakeServer(self.LANES)
        tokens = []

        def tune(url, channel, args, seen, samples, failures, token=None):
            tokens.append(token)
            raise RuntimeError("stop after the first round")

        self.run_gate(server, tune)
        self.assertEqual(server.locked, {"digital": "token-digital", "analog": "token-analog"})
        self.assertEqual(tokens, ["token-digital"])
        self.assertEqual(sorted(server.released), [("analog", "token-analog"), ("digital", "token-digital")])

    def test_a_lane_someone_holds_fails_the_gate_without_tuning_and_releases_the_rest(self):
        server = FakeServer(self.LANES, held={"analog": "Web remote"})
        tune = mock.Mock()
        result = self.run_gate(server, tune)
        tune.assert_not_called()
        self.assertEqual(server.released, [("digital", "token-digital")])
        self.assertNotEqual(result, 0)
        self.assertIn("could not lock the analog lane: Web remote holds it", self.output)

    def test_a_navigation_begun_before_the_gate_is_cancelled_and_waited_out(self):
        server = FakeServer({"digital": {}, "roku": {}}, roku_busy=3)
        tokens = []

        def tune(url, channel, args, seen, samples, failures, token=None):
            tokens.append(token)
            raise RuntimeError("stop after the first round")

        self.run_gate(server, tune)
        self.assertEqual(server.cancelled, ["token-roku"])
        self.assertLess(server.roku_busy, 0)   # polled until idle before tuning
        self.assertEqual(tokens, ["token-digital"])
        self.assertEqual(sorted(server.released), [("digital", "token-digital"), ("roku", "token-roku")])


class CpuAccountingTests(unittest.TestCase):
    HZ = gate.CLOCK_TICKS
    # pid -> (start tick, CPU ticks including reaped children)
    SERVER, ENCODER = 10, 20

    def test_long_running_processes_are_charged_what_they_used_in_between(self):
        before = {self.SERVER: (100, 50), self.ENCODER: (200, 9000)}
        after = {self.SERVER: (100, 52), self.ENCODER: (200, 9180)}
        self.assertEqual(gate.cpu_ticks_used(before, after, listed_before=50.0), 182)

    def test_reaping_a_killed_encoder_does_not_count_its_lifetime_twice(self):
        # The encoder used 30 more ticks before the kill; reaping it moves its
        # final 9030 into the server's count, beside the server's own 2.
        before = {self.SERVER: (100, 50), self.ENCODER: (200, 9000)}
        after = {self.SERVER: (100, 50 + 2 + 9030)}
        self.assertEqual(gate.cpu_ticks_used(before, after, listed_before=50.0), 32)

    def test_a_process_started_since_the_last_sample_is_charged_all_it_used(self):
        before = {self.SERVER: (100, 50)}
        listed = 50.0
        after = {self.SERVER: (100, 50), 30: (int(listed * self.HZ) + 20, 40)}
        self.assertEqual(gate.cpu_ticks_used(before, after, listed), 40)

    def test_a_reused_pid_is_a_new_process(self):
        before = {self.SERVER: (100, 50), self.ENCODER: (200, 9000)}
        listed = 50.0
        after = {self.SERVER: (100, 50 + 9000), self.ENCODER: (int(listed * self.HZ) + 5, 7)}
        self.assertEqual(gate.cpu_ticks_used(before, after, listed), 7)

    def test_an_old_process_missed_once_is_not_charged_its_whole_life(self):
        # A ps listing that dropped the encoder must not make the next sample
        # charge its whole lifetime as if it had just started.
        before = {self.SERVER: (100, 50)}
        after = {self.SERVER: (100, 51), self.ENCODER: (200, 9000)}
        self.assertEqual(gate.cpu_ticks_used(before, after, listed_before=50.0), 1)

    def sample(self, uptimes, tick_reads):
        """One process_group_sample per tick read; uptimes are (listed, read) pairs.

        ps lists the server and, as its children, every other pid in the read.
        """
        current = {}

        def ps(*_args, **_kwargs):
            lines = [f"{self.SERVER} 1 1024 python3 server.py"]
            lines += [f"{pid} {self.SERVER} 2048 ffmpeg -i udp://x"
                      for pid in current if pid != self.SERVER]
            return mock.Mock(stdout="\n".join(lines))

        def read(pid):
            if pid not in current:
                raise FileNotFoundError(pid)
            return current[pid]

        with mock.patch.object(gate.subprocess, "run", side_effect=ps), \
                mock.patch.object(gate, "system_uptime", side_effect=uptimes), \
                mock.patch.object(gate, "process_cpu_ticks", side_effect=read), \
                mock.patch.dict(gate._cpu_baselines, clear=True):
            results = []
            for ticks in tick_reads:
                current.clear()
                current.update(ticks)
                results.append(gate.process_group_sample(self.SERVER))
            return results

    def test_samples_report_the_rate_since_the_previous_one(self):
        first = {self.SERVER: (100, 50), self.ENCODER: (200, 9000)}
        second = {self.SERVER: (100, 50), self.ENCODER: (200, 9000 + int(1.8 * self.HZ))}
        results = self.sample([99.0, 100.0, 100.9, 101.0], [first, second])
        self.assertIsNone(results[0]["cpu_percent"])    # a baseline only
        self.assertAlmostEqual(results[1]["cpu_percent"], 180.0)
        self.assertEqual(results[1]["ffmpeg_count"], 1)
        self.assertEqual(results[1]["rss_mib"], 3.0)

    def test_a_fresh_encoder_reads_what_it_used_not_its_age_ratio(self):
        # 2026-10-17: ps read a 0.08 s old ffmpeg at 116%. Charged by use, its
        # 3 ticks in the second since the last sample are 3%.
        first = {self.SERVER: (100, 50)}
        second = {self.SERVER: (100, 50), 30: (int(100.92 * self.HZ), 3)}
        results = self.sample([99.0, 100.0, 100.9, 101.0], [first, second])
        self.assertAlmostEqual(results[1]["cpu_percent"], 3.0)

    def test_a_sample_too_soon_after_the_last_keeps_measuring_from_it(self):
        reads = [{self.SERVER: (100, 50), self.ENCODER: (200, 9000)},
                 {self.SERVER: (100, 50), self.ENCODER: (200, 9010)},
                 {self.SERVER: (100, 50), self.ENCODER: (200, 9100)}]
        results = self.sample([99.0, 100.0, 100.1, 100.2, 100.9, 101.0], reads)
        self.assertEqual([r["cpu_percent"] for r in results[:2]], [None, None])
        self.assertAlmostEqual(results[2]["cpu_percent"], 100.0)   # 100 ticks over 1 s

    def test_the_summary_skips_samples_without_a_rate(self):
        def tune(url, channel, args, seen, samples, failures, token=None):
            samples["process"] += [
                {"cpu_percent": None, "rss_mib": 1, "ffmpeg_count": 1, "source_count": 1},
                {"cpu_percent": 120.0, "rss_mib": 1, "ffmpeg_count": 1, "source_count": 1},
            ]
            raise RuntimeError("stop after the first round")

        with mock.patch.object(gate, "http_json", FakeServer({"digital": {}}).http_json), \
                mock.patch.object(gate, "tune_and_observe", tune), \
                mock.patch.object(gate.signal, "signal"), \
                mock.patch.object(gate.time, "sleep"), \
                mock.patch("builtins.print") as printed:
            gate.main(["--url", "http://gate.invalid", "--server-pid", "1", "--retunes", "1"])
        output = "\n".join(" ".join(map(str, call.args)) for call in printed.call_args_list)
        self.assertIn("process CPU average 120.0%", output)
        self.assertIn("process CPU peak 120.0%", output)


if __name__ == "__main__":
    unittest.main()


class SlideGateTests(unittest.TestCase):
    """--slides: a slide channel's on-demand video, started cold each round."""

    SEGMENT = b"\x47" + b"\x00" * 187

    def fake(self, linger=False, leak=False):
        # Like the server: the video runs while its playlist is asked for, and
        # idles out a few status polls after the last request.
        state = {"running": False, "generation": None, "starts": 0, "polls_since_request": 0,
                 "requested": []}

        def http_json(base_url, path, method="GET", payload=None, timeout=5):
            if path == "/api/v1/status":
                if state["running"] and not linger:
                    state["polls_since_request"] += 1
                    if state["polls_since_request"] > 3:
                        state["running"] = False
                return {"lanes": {"digital": {}, "roku": {"available": False}},
                        "slides": {"sports-guide": {"running": state["running"], "idle_seconds": 0.01,
                                                    "generation": state["generation"] if state["running"] else None}}}
            if path == "/api/v1/tuner/lock":
                return {"ok": True, "token": "t"}
            if path == "/api/v1/tuner/lock/release":
                return {"ok": True}
            raise AssertionError(path)

        def http_bytes(base_url, path, method="GET", payload=None, timeout=5):
            path = path.split("?")[0]
            state["requested"].append(path)
            if path == "/stream/sports-guide/index.m3u8":
                if not state["running"]:
                    state["starts"] += 1
                    state.update(running=True, generation=f"g{state['starts']}")
                state["polls_since_request"] = 0
                gen = state["generation"]
                return "".join(f"#EXTINF:2.0,\nsportsguide_{gen}_{n:05d}.ts\n" for n in range(4)).encode()
            return self.SEGMENT

        def sample(pid):
            return {"cpu_percent": 40.0, "rss_mib": 200.0, "ffmpeg_count": 1, "source_count": 0,
                    "slide_processes": ["sports_guide_video.py --url x"] if leak and not state["running"] else []}

        return state, http_json, http_bytes, sample

    def run_gate(self, linger=False, leak=False):
        state, http_json, http_bytes, sample = self.fake(linger, leak)
        ticks = iter(range(10**9))
        with mock.patch.object(gate.time, "monotonic", lambda: next(ticks) * 0.25), \
                mock.patch.object(gate, "http_json", http_json), \
                mock.patch.object(gate, "http_bytes", http_bytes), \
                mock.patch.object(gate, "process_group_sample", sample), \
                mock.patch.object(gate, "gpu_sample", lambda: None), \
                mock.patch.object(gate, "decode_generation", lambda *a: (True, "")), \
                mock.patch.object(gate.signal, "signal"), \
                mock.patch.object(gate.time, "sleep"), \
                mock.patch("builtins.print") as printed:
            result = gate.main(["--url", "http://gate.invalid", "--server-pid", "1", "--slides",
                                "sports-guide", "--retunes", "3", "--dwell-seconds", "3"])
        self.output = "\n".join(" ".join(map(str, call.args)) for call in printed.call_args_list)
        return result, state

    def test_each_round_starts_the_video_cold_and_sees_it_stop(self):
        result, state = self.run_gate()
        self.assertEqual(state["starts"], 3)
        self.assertIn("/stream/sports-guide/sportsguide_g1_00000.ts", state["requested"])
        self.assertIn("PASS  fresh stream generations 3/3", self.output)
        self.assertIn("concurrent ffmpeg processes max 1 (limit 3)", self.output)
        self.assertEqual(result, 0, self.output)

    def test_video_that_outlives_its_last_viewer_fails_the_gate(self):
        result, state = self.run_gate(linger=True)
        self.assertNotEqual(result, 0)
        self.assertIn("video still running", self.output + json.dumps(state))

    def test_a_renderer_left_behind_fails_the_gate(self):
        result, _ = self.run_gate(leak=True)
        self.assertNotEqual(result, 0)
        self.assertIn("outlived its stream", self.output)
