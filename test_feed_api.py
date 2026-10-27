"""The feed's HTTP API and RemoteFeed (feed_api.py): LocalFeed's calls over HTTP, and what
control sees when the feed is down or hung. A real HTTP feed in-process; no hardware."""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import feed_api
import feed_node

CONFIG = feed_node.parse_config(json.loads((Path(__file__).parent / "feed.json").read_text()))


def blank():
    return {"active": False, "selection": None, "quality": "medium", "stream_id": None, "health": None,
            "started_at": None, "video_encoder": None, "tuner": None, "error": None, "taps": []}


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.stdin = SimpleNamespace(write=lambda s: None, flush=lambda: None, close=self._exit)
        self.stderr = None

    def _exit(self):
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class Ops:
    """The parts of server.py the feed calls, with fake hardware."""
    LANE_DIGITAL, LANE_ANALOG, LANE_ROKU = "digital", "analog", "roku"

    def __init__(self, node, tmp_path):
        self.node, self.lock = node, threading.RLock()
        self.lanes = {lane.id: blank() for lane in CONFIG.lanes}
        self.running, self.calls, self.primary_video_lane = {"roku"}, [], "digital"
        self.recorders = feed_node.Recorders(tmp_path / "journal.jsonl", spawn=lambda command: FakeProc())
        self.SLIDE_STREAMS = {"sports-guide": SimpleNamespace(status=lambda: {"running": False})}
        self.opened, self.closed = [], []
        self.atsc_scan = SimpleNamespace(
            measure=lambda adapter: {"snr_db": 30.0, "adapter": adapter},
            scan_channel=self._scan, open_frontend=lambda adapter, writable=False: self.opened.append(adapter) or 7)
        self.epg = SimpleNamespace(refresh=self._refresh)
        self.guide_gate = threading.Event()
        self.guide_gate.set()

    def lane_is_running(self, lane):
        return lane in self.running

    lane_processes_alive = lane_stream_ready = lane_is_running

    def roku_capture_source(self):
        return {"running": True, "stream_id": "cap-1", "error": None, "tap_url": "udp://127.0.0.1:1"}

    def start_digital(self, channel, quality, taps=None):
        self.calls.append(("start_digital", channel["id"], quality, taps))
        self.running.add("digital")
        self.lanes["digital"].update(selection=channel, quality=quality, stream_id=f"s{len(self.calls)}")
        self.node.transition("digital", "running", generation=f"s{len(self.calls)}")
        return True, None, False

    def start_analog(self, channel, quality):
        self.calls.append(("start_analog", channel, quality))
        return False, "no cable plugged in", None

    def stop_lane(self, lane):
        self.calls.append(("stop_lane", lane))
        self.running.discard(lane)
        self.node.transition(lane, "idle")

    def start_recorder(self, kind, part, until, *, lane=None, exclude=(), start_index=0):
        port = 18484 if kind == "tap" else None
        return self.recorders.start(["ffmpeg"], part, until, kind=kind, lane=lane, tap_port=port), port

    def _scan(self, rf, adapter=0, fd=None, **options):
        if rf == 99:
            raise OSError("Could not tune RF 99")
        return {"rf_channel": rf, "fd": fd, "options": options}

    def _refresh(self, adapter=0, stop=None, progress=None, **options):
        for step in range(3):
            if progress:
                progress({"step": step})
            self.guide_gate.wait(5)
            if stop is not None and stop.is_set():
                return {"error": "stopped", "events": step}
        return {"channels": options.get("channels"), "events": 3, "adapter": adapter}


def serve(api):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            feed_api.answer(self, api)

        do_POST = do_GET

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture
def feed(tmp_path, monkeypatch):
    node = feed_node.FeedNode(CONFIG)
    ops = Ops(node, tmp_path)
    local = feed_node.LocalFeed(node, ops)
    monkeypatch.setattr(feed_node.os, "close", lambda fd: ops.closed.append(fd))
    httpd = serve(feed_api.FeedAPI(local))
    remote = feed_api.RemoteFeed(f"http://127.0.0.1:{httpd.server_address[1]}/", CONFIG, blank,
                                 timeout=1.0, command_timeout=3.0, scan_timeout=5.0, cache_seconds=0)
    yield SimpleNamespace(node=node, ops=ops, local=local, remote=remote, httpd=httpd)
    httpd.shutdown()
    httpd.server_close()


def test_reads_are_the_local_feeds(feed):
    assert feed.remote.lane("roku") == json.loads(json.dumps(feed.local.lane("roku")))
    assert feed.remote.running_lanes() == ["roku"] and feed.remote.primary_video_lane() == "digital"
    assert feed.remote.capture_source("roku")["stream_id"] == "cap-1"
    assert feed.remote.status()["feed_epoch"] == feed.node.epoch
    assert feed.remote.slides() == {"sports-guide": {"running": False}}


def test_a_tune_is_a_command_at_the_lanes_revision(feed):
    before = feed.remote.lane("digital")["revision"]
    channel = {"id": "wrc_4_1", "number": "4.1"}
    assert feed.remote.tune_digital(channel, "low", taps=[18484]) == (True, None, False)
    assert feed.ops.calls == [("start_digital", "wrc_4_1", "low", [18484])]
    after = feed.remote.lane("digital")
    assert after["revision"] > before and after["selection"] == channel
    assert feed.remote.tune_analog(70, "low") == (False, "no cable plugged in", None)


def test_a_lost_answer_is_retried_with_the_same_op_id_and_applies_once(feed):
    real = feed.remote.opener
    lost = {"done": False}

    def lose_first_tune_answer(request, timeout=None):
        response = real(request, timeout=timeout)
        if request.full_url.endswith("/tune") and not lost["done"]:
            lost["done"] = True
            response.close()
            raise socket.timeout("timed out")          # the feed did it; the answer never came
        return response

    feed.remote.opener = lose_first_tune_answer
    assert feed.remote.tune_digital({"id": "wrc_4_1"}, "low") == (True, None, False)
    assert [c[0] for c in feed.ops.calls] == ["start_digital"], "tuned once, not twice"


def test_a_stale_command_is_reread_and_reissued_once(feed):
    real_expected = feed.remote._expected
    calls = {"n": 0}

    def stale_first(lane, fresh=False):
        calls["n"] += 1
        epoch, revision = real_expected(lane, fresh)
        return [epoch, revision - 1] if calls["n"] == 1 else [epoch, revision]

    feed.remote._expected = stale_first
    feed.remote.stop("roku")
    assert feed.ops.calls == [("stop_lane", "roku")] and calls["n"] == 2
    feed.remote._expected = lambda lane, fresh=False: [real_expected(lane)[0], -5]
    with pytest.raises(feed_node.Conflict):
        feed.remote.stop("digital")
    assert feed.ops.calls == [("stop_lane", "roku")], "a stale command never applies"


def test_a_feed_that_is_down_reads_unknown_and_refuses_commands(feed):
    feed.remote.lane("digital")                       # a snapshot to remember
    feed.httpd.shutdown()
    feed.httpd.server_close()
    started = time.monotonic()
    lane = feed.remote.lane("digital")
    assert time.monotonic() - started < 1.5
    assert lane["lifecycle"] == "unknown" and lane["running"] is True and lane["alive"] is False
    assert lane["ready"] is False, "never served, never free"
    assert feed.remote.capture_source("roku")["running"] is False
    assert set(feed.remote.running_lanes()) == {"digital", "analog", "roku"}
    with pytest.raises(feed_api.FeedUnavailable):
        feed.remote.tune_digital({"id": "x"}, "low")
    with pytest.raises(OSError):                       # control's signal and scan paths answer 503
        feed.remote.measure("digital")


def test_a_hung_feed_never_hangs_control():
    """INVARIANTS.md C2: control reports a SIGSTOPped feed's lanes unknown, and its API
    threads don't block. One status page asks for every lane, the running list and more."""
    hung = socket.socket()
    hung.bind(("127.0.0.1", 0))
    hung.listen(64)                                    # accepts, never answers (SIGSTOP)
    remote = feed_api.RemoteFeed(f"http://127.0.0.1:{hung.getsockname()[1]}/", CONFIG, blank, timeout=0.5)
    started = time.monotonic()
    assert remote.lane("roku")["lifecycle"] == "unknown"
    assert time.monotonic() - started < 1.5
    started = time.monotonic()
    for _ in range(20):                                # a status page's worth of reads, and more
        assert remote.lane("digital")["lifecycle"] == "unknown"
        remote.running_lanes(), remote.slides(), remote.capture_source("roku")
    assert time.monotonic() - started < 0.2, "answered from what is known, not one timeout each"
    results, threads = [], []
    for _ in range(8):                                 # eight API threads at once, after the window
        threads.append(threading.Thread(target=lambda: results.append(remote.lane("analog")["lifecycle"])))
    time.sleep(1.1)
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert results == ["unknown"] * 8 and time.monotonic() - started < 1.5, "one waits, the rest do not"
    hung.close()


def test_navigation_round_trips_and_a_restarted_control_reads_it_back(feed):
    record = feed_node.LaneRecord(feed.remote, "roku")
    record.begin_navigation("op-1")
    assert feed.remote.navigation("roku")["navigating"] is True
    assert record.end_navigation("op-1", {"channel_id": "ESPN"}) is True
    version = record.state()["selection_version"]
    assert record.renew_selection(version, {"capture_id": "cap-1"}) is True
    again = feed_api.RemoteFeed(feed.remote.base_url[:-len(feed_api.PREFIX)], CONFIG, blank, cache_seconds=0)
    assert feed_node.LaneRecord(again, "roku").state()["selection"] == {"channel_id": "ESPN", "capture_id": "cap-1"}
    assert feed.node.navigation("roku")["selection"]["channel_id"] == "ESPN"


def test_recorders_round_trip_one_writer_per_part(feed, tmp_path):
    part = tmp_path / "show.ts.part"
    rec_id, port = feed.remote.start_recorder("tap", part, 2_000_000_000)
    assert port == 18484
    assert feed.remote.start_recorder("tap", part, 2_000_000_000)[0] == rec_id, "a retry finds the same writer"
    assert feed.remote.recorder(rec_id)["alive"] is True
    assert [view["id"] for view in feed.remote.live_recorders()] == [rec_id]
    feed.remote.stop_recorder(rec_id, timeout=1)
    assert feed.remote.recorder(rec_id)["alive"] is False
    assert feed.remote.recorder("nope") is None


def test_front_end_operations(feed):
    assert feed.remote.measure("digital") == {"snr_db": 30.0, "adapter": 0}
    assert feed.remote.scan_channel("digital", 9, lock_timeout=2.0)["rf_channel"] == 9
    with pytest.raises(OSError, match="Could not tune RF 99"):
        feed.remote.scan_channel("digital", 99)
    tuned = []
    results = list(feed.remote.sweep("digital", [7, 8, 9], 1.5, before=tuned.append))
    assert [r["rf_channel"] for r in results] == [7, 8, 9] and tuned == [7, 8, 9]
    assert all(r["fd"] == 7 for r in results) and feed.ops.closed == [7]


def test_a_guide_read_streams_its_progress_and_stops_on_request(feed):
    steps = []
    guide = feed.remote.read_guide("digital", progress=steps.append, channels=[{"id": "x"}], blocks=4)
    assert guide == {"channels": [{"id": "x"}], "events": 3, "adapter": 0} and len(steps) == 3
    feed.ops.guide_gate.clear()
    stop = threading.Event()
    reader = threading.Thread(target=lambda: steps.append(feed.remote.read_guide("digital", stop=stop)))
    reader.start()
    time.sleep(0.3)
    stop.set()                                         # control's guide_abort
    time.sleep(0.6)
    feed.ops.guide_gate.set()
    reader.join(5)
    assert steps[-1] == {"error": "stopped", "events": 0}


def test_only_this_host_may_ask():
    assert feed_api.loopback("127.0.0.1") and feed_api.loopback("::1") and feed_api.loopback("::ffff:127.0.0.1")
    assert not feed_api.loopback("10.0.0.98") and not feed_api.loopback("192.168.1.50") and not feed_api.loopback(None)
