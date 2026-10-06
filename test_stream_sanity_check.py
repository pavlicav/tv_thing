"""The real-stream gate's lane locks: it measures with nothing else moving."""
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


if __name__ == "__main__":
    unittest.main()
