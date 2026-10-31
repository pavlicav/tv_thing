"""The tuner pool: which of both nodes' digital tuners a live tune takes (tuner_pool.py), and
the tune and lock calls that go to the peer (server.api_v1_digital_tune, pool_lock_request)."""
import json
from pathlib import Path

import lineup
import peers
import server
import tuner_pool

ROOT = Path(__file__).resolve().parent.parent
WUSA = {"id": "wusa_hd_9_1", "number": "9.1", "rf_channel": 9}


def tuner(node, local, *, lock=None, active=True, ready=True, playing=None, reachable=True,
          rf=None, lane="digital"):
    return {
        "id": f"{node}:{lane}", "node": node, "lane": lane, "kind": "digital", "local": local,
        "reachable": reachable, "lock": lock, "rf_channels": rf,
        "available": reachable and lock is None,
        "selection": {"id": playing} if playing else None, "quality": "low",
        "stream": {"id": f"{node}-1", "active": active, "ready": ready, "error": None,
                   "playlist_path": "/stream/peer/digital.m3u8" if not local else "/stream/digital.m3u8"},
    }


LOCK = {"lane": "digital", "owner": "VizelVision", "seconds_remaining": 600}


# -- choosing ---------------------------------------------------------------------------------
def test_this_nodes_unlocked_tuner_is_retuned_as_before_the_pool():
    here, there = tuner("viviserv", True), tuner("viviserv2", False, active=False)
    assert tuner_pool.choose([here, there], WUSA) == (here, "retune")


def test_a_lock_here_sends_the_tune_to_the_peers_tuner():
    here, there = tuner("viviserv", True, lock=LOCK), tuner("viviserv2", False)
    assert tuner_pool.choose([here, there], WUSA) == (there, "retune")


def test_an_idle_tuner_is_taken_before_a_running_one_of_the_same_node():
    a = tuner("viviserv2", True)
    b = tuner("viviserv2", True, lane="digital-b", active=False, ready=False)
    assert tuner_pool.choose([a, b], WUSA) == (b, "idle")


def test_watching_along_a_locked_tuner_wins_over_retuning_a_free_one():
    here = tuner("viviserv", True)
    there = tuner("viviserv2", False, lock=LOCK, playing="wusa_hd_9_1")
    assert tuner_pool.choose([here, there], WUSA) == (there, "playing")


def test_a_tuner_whose_stream_is_not_ready_is_not_watched_along():
    here = tuner("viviserv", True, lock=LOCK, playing="wusa_hd_9_1", ready=False)
    there = tuner("viviserv2", False)
    assert tuner_pool.choose([here, there], WUSA) == (there, "retune")


def test_an_unknown_peer_is_never_free():
    here = tuner("viviserv", True, lock=LOCK)
    there = tuner("viviserv2", False, reachable=False)
    assert tuner_pool.choose([here, there], WUSA) == (None, "locked")
    assert tuner_pool.choose([tuner("viviserv2", False, reachable=False)], WUSA) == (None, "unavailable")


def test_a_tuner_whose_antenna_misses_the_rf_is_passed_over():
    here = tuner("viviserv", True, lock=LOCK, rf=[7, 9, 14])
    there = tuner("viviserv2", False, rf=[7, 14])
    assert tuner_pool.choose([here, there], WUSA) == (None, "locked")
    assert tuner_pool.choose([there], WUSA) == (None, "none")
    # No reception on record: taken to get the whole plan.
    assert tuner_pool.choose([tuner("viviserv2", False)], WUSA)[1] == "retune"


def test_the_summary_counts_the_free_tuners():
    tuners = [tuner("viviserv", True, lock=LOCK), tuner("viviserv2", False),
              tuner("viviserv2", False, lane="digital-b", reachable=False)]
    assert tuner_pool.summary(tuners) == {"total": 3, "reachable": 2, "available": 1}


# -- the same plan on both nodes --------------------------------------------------------------
def test_every_plan_channel_has_its_own_number_on_the_dial():
    """Both nodes number the dial alike only if no plan channel is left to a node's own
    lineup_state.json: each has a fixed number in lineup.json, or is hidden."""
    plan = lineup.Lineup(ROOT / "lineup.json", ROOT / "no-such-lineup-state.json").plan()
    channels = json.loads((ROOT / "channels.json").read_text())
    keys = [f"{lineup.ANTENNA}/{key}" for key in lineup.antenna_keys(channels)]
    assert [key for key in keys if key not in plan.fixed and key not in plan.hidden] == []


def test_a_scan_replaces_only_the_reception_of_what_it_scanned(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "load_channels", lambda: [{"rf_channel": rf} for rf in (7, 9, 14)])
    assert server.load_reception() is None
    server.save_reception([9, 10], {10})      # never scanned: the rest of the plan's kept
    assert server.load_reception() == [7, 10, 14]
    server.save_reception([7], set())
    assert server.load_reception() == [10, 14]
    assert json.loads(server.RECEPTION_FILE.read_text())["node"] == server.FEED_CONFIG.node


def test_a_second_nodes_rescan_keeps_the_shared_plan(monkeypatch):
    saved, received = [], []
    monkeypatch.setattr(server.HOST, "_cache", {**server.HOST._cache,
                                                "role": server.host_config.Resolved("second", "auto", "")})
    monkeypatch.setattr(server, "save_channels", saved.append)
    monkeypatch.setattr(server, "save_reception", lambda scanned, got: received.append((list(scanned), got)))
    _run_rescan(monkeypatch)
    assert saved == [] and received == [([7, 8], {8})]


def _run_rescan(monkeypatch):
    from io import BytesIO
    import feed_node
    monkeypatch.setattr(server, "scanning", False)
    monkeypatch.setattr(server, "preempt_guide_refresh", lambda: None)
    monkeypatch.setattr(server, "lock_holder", lambda *args: None)
    monkeypatch.setattr(server, "busy_reason", lambda: None)
    monkeypatch.setattr(server, "stop_digital", lambda: None)
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    monkeypatch.setattr(server.atsc_scan, "open_frontend", lambda adapter, writable=False: 41)
    monkeypatch.setattr(feed_node.os, "close", lambda fd: None)
    monkeypatch.setattr(server.atsc_scan, "scan_channel", lambda rf, **_: {
        "rf_channel": rf, "frequency_hz": rf * 1_000_000, "frequency_mhz": float(rf), "locked": rf == 8,
        "snr_db": 20.0, "signal_percent": 50, "virtual_channels": [{"number": "8.1"}] if rf == 8 else []})
    monkeypatch.setattr(server, "channels_from_scan", lambda found: [{"id": "w8", "rf_channel": 8}] if found else [])
    handler = server.TVHandler.__new__(server.TVHandler)
    handler.path = "/api/rescan/stream?start=7&end=8"
    handler.wfile = BytesIO()
    handler.send_response = handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.handle_rescan_stream()
    return handler.wfile.getvalue().decode()


# -- tunes and locks across the pool ----------------------------------------------------------
class FakePeer:
    def __init__(self, tuners, answer=None):
        self._tuners, self.answer, self.sent = tuners, answer, []
        self.reachable, self.last_error = True, None

    def node(self):
        return "viviserv2"

    def tuners(self):
        return self._tuners

    def tune(self, body):
        self.sent.append(("tune", body))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer or ({"ok": True, "lane": "digital", "tuner": "viviserv2:digital",
                                "stream": {"id": "g7", "playlist_path": "/stream/peer/digital.m3u8"}}, 200)

    def lock(self, body):
        self.sent.append(("lock", body))
        return {"ok": True, "token": "t", "lock": {"lane": "digital", "owner": body["owner"]}}, 200

    def release(self, body):
        self.sent.append(("release", body))
        return {"ok": True, "released": True}, 200


def pool(monkeypatch, here, there, answer=None):
    peer = FakePeer([there], answer)
    monkeypatch.setattr(server, "peer", peer)
    monkeypatch.setattr(server, "local_tuners", lambda: [here])
    monkeypatch.setattr(server, "load_channels", lambda: [WUSA])
    tuned_here = []

    def perform(channel_id, quality, token=None, override=False):
        tuned_here.append((channel_id, override))
        return {"ok": True}, 200

    monkeypatch.setattr(server, "perform_digital_tune", perform)
    monkeypatch.setattr(server, "lane_tune_response", lambda result, status, lane: ({"ok": True, "lane": lane}, 200))
    monkeypatch.setattr(server, "active_lock", lambda lane, now=None: here["lock"])
    return peer, tuned_here


def test_a_tune_with_this_nodes_tuner_free_stays_here(monkeypatch):
    peer, tuned_here = pool(monkeypatch, tuner("viviserv", True), tuner("viviserv2", False))
    payload, status = server.api_v1_digital_tune({"channel_id": "wusa_hd_9_1"}, "low")
    assert status == 200 and payload["tuner"] == "viviserv:digital" and not payload["relayed"]
    assert tuned_here == [("wusa_hd_9_1", False)] and peer.sent == []


def test_a_tune_with_this_nodes_tuner_locked_is_relayed_from_the_peer(monkeypatch):
    peer, tuned_here = pool(monkeypatch, tuner("viviserv", True, lock=LOCK), tuner("viviserv2", False))
    payload, status = server.api_v1_digital_tune({"channel_id": "wusa_hd_9_1"}, "low")
    assert status == 200 and tuned_here == []
    assert payload["lane"] == payload["tuner"] == "viviserv2:digital" and payload["relayed"]
    assert payload["stream"]["playlist_path"] == "/stream/peer/digital.m3u8"
    assert peer.sent == [("tune", {"channel_id": "wusa_hd_9_1", "quality": "low", "tuner": "viviserv2:digital"})]


def test_the_peers_own_request_never_leaves_its_node(monkeypatch):
    peer, tuned_here = pool(monkeypatch, tuner("viviserv", True, lock=LOCK), tuner("viviserv2", False))
    server.api_v1_digital_tune({"channel_id": "wusa_hd_9_1", "local_only": True}, "low")
    assert tuned_here == [("wusa_hd_9_1", False)] and peer.sent == []


def test_every_tuner_locked_is_refused_naming_one(monkeypatch):
    other = {**LOCK, "owner": "VCR"}
    pool(monkeypatch, tuner("viviserv", True, lock=LOCK), tuner("viviserv2", False, lock=other))
    payload, status = server.api_v1_digital_tune({"channel_id": "wusa_hd_9_1"}, "low")
    assert status == 423 and payload["error"]["code"] == "locked"
    assert payload["tuner"] == "viviserv:digital" and len(payload["tuners"]) == 2


def test_an_override_without_a_tuner_is_this_nodes(monkeypatch):
    peer, tuned_here = pool(monkeypatch, tuner("viviserv", True, lock=LOCK), tuner("viviserv2", False))
    server.api_v1_digital_tune({"channel_id": "wusa_hd_9_1", "override": True}, "low")
    assert tuned_here == [("wusa_hd_9_1", True)] and peer.sent == []


def test_a_named_peer_tuner_is_asked_even_when_this_one_is_free(monkeypatch):
    peer, tuned_here = pool(monkeypatch, tuner("viviserv", True), tuner("viviserv2", False, lock=LOCK))
    body = {"channel_id": "wusa_hd_9_1", "tuner": "viviserv2:digital", "override": True}
    assert server.api_v1_digital_tune(body, "low")[1] == 200
    assert tuned_here == [] and peer.sent[0][1]["override"] is True
    assert server.api_v1_digital_tune({**body, "tuner": "elsewhere:digital"}, "low")[1] == 400


def test_a_peer_that_stops_answering_mid_tune_is_a_503(monkeypatch):
    pool(monkeypatch, tuner("viviserv", True, lock=LOCK), tuner("viviserv2", False),
         answer=peers.PeerUnavailable("timed out"))
    payload, status = server.api_v1_digital_tune({"channel_id": "wusa_hd_9_1"}, "low")
    assert status == 503 and payload["error"]["code"] == "peer_unavailable"


def test_a_lock_on_the_peers_tuner_is_the_peers(monkeypatch):
    peer, _ = pool(monkeypatch, tuner("viviserv", True), tuner("viviserv2", False))
    payload, status = server.pool_lock_request({"lane": "viviserv2:digital", "owner": "me"})
    assert status == 200 and payload["lock"]["lane"] == "viviserv2:digital"
    assert peer.sent == [("lock", {"lane": "digital", "owner": "me"})]
    server.pool_lock_request({"lane": "viviserv2:digital", "token": "t"}, release=True)
    assert peer.sent[-1] == ("release", {"lane": "digital", "token": "t"})
    # This node's own tuner id is its lane, answered here.
    body = {"lane": f"{server.FEED_CONFIG.node}:digital", "owner": "me"}
    assert server.pool_lock_request(body) is None and body["lane"] == "digital"
    assert server.pool_lock_request({"lane": "digital", "owner": "me"}) is None
