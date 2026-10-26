"""The feed's own state (feed_node.py): feed.json, each lane's revision and lifecycle, the
epoch, and commands applied once each, only at the state they were issued against."""
import copy
import json
import threading

import pytest

import feed_node

CONFIG = json.loads(open(__file__.replace("test_feed_node.py", "feed.json")).read())


def node(clock=None):
    return feed_node.FeedNode(feed_node.parse_config(CONFIG), **({"clock": clock} if clock else {}))


def test_the_shipped_feed_json():
    config = feed_node.parse_config(CONFIG)
    assert [lane.id for lane in config.lanes] == ["digital", "analog", "roku"]
    assert config.lane("roku").device == "roku" and config.device("roku").kind == "capture"
    assert [lane.id for lane in config.lanes if lane.video] == ["digital", "analog"]
    assert config.node == "viviserv"


@pytest.mark.parametrize("change, message", [
    (lambda c: c["lanes"][0].update(device="dvb9"), "unknown device"),
    (lambda c: c["devices"][0].update(kind="satellite"), "kind must be one of"),
    (lambda c: c["lanes"].append(dict(c["lanes"][0])), "share an id"),
    (lambda c: c["lanes"][0].update(playlist="../etc/passwd"), "plain file names"),
    (lambda c: c["lanes"][0].update(playlist="digital.ts"), "plain file names"),
    (lambda c: c["lanes"][0].pop("process"), "a lane needs"),
    (lambda c: c.update(lanes=[]), "lists no lanes"),
    (lambda c: c.update(node="a b"), "node must be"),
])
def test_feed_json_mistakes_are_named(change, message):
    broken = copy.deepcopy(CONFIG)
    change(broken)
    with pytest.raises(feed_node.FeedConfigError, match=message):
        feed_node.parse_config(broken)


def test_the_revision_moves_with_the_lifecycle_and_the_generation():
    feed = node()
    assert feed.view("digital") == {"revision": 0, "lifecycle": "idle"}
    assert feed.transition("digital", "stopping") == 0, "stopping a stopped lane changes nothing"
    assert feed.transition("digital", "idle") == 0
    assert feed.transition("digital", "starting") == 1
    assert feed.transition("digital", "running", generation="g1") == 2
    assert feed.transition("digital", "running", generation="g1") == 2, "the same generation"
    assert feed.transition("digital", "running", generation="g2") == 3, "a supervisor restart"
    assert feed.transition("digital", "error") == 4
    assert feed.view("analog")["revision"] == 0, "lanes are counted apart"
    with pytest.raises(ValueError):
        feed.transition("digital", "sleeping")


def test_each_start_has_its_own_epoch():
    assert node().epoch != node().epoch


def test_a_command_runs_once_at_its_revision():
    feed = node()
    ran = []

    def tune():
        ran.append(1)
        return feed.transition("digital", "running", generation=f"g{len(ran)}")

    expected = feed.precondition("digital")
    assert feed.command("op-1", "digital", expected, tune) == 1
    assert feed.command("op-1", "digital", expected, tune) == 1, "a retry replays the first answer"
    assert ran == [1]
    with pytest.raises(feed_node.Conflict) as refused:
        feed.command("op-2", "digital", expected, tune)
    assert "moved on" in str(refused.value) and refused.value.current["revision"] == 1
    assert ran == [1]


def test_a_command_from_before_a_feed_restart_is_refused():
    old, new = node(), node()
    with pytest.raises(feed_node.Conflict, match="feed restarted"):
        new.command("op-1", "digital", old.precondition("digital"), lambda: None)


def test_a_failed_command_replays_its_error():
    feed = node()

    def broken():
        raise OSError("frontend busy")

    expected = feed.precondition("digital")
    with pytest.raises(OSError):
        feed.command("op-1", "digital", expected, broken)
    with pytest.raises(OSError, match="frontend busy"):
        feed.command("op-1", "digital", expected, lambda: "never")


def test_a_retry_while_it_runs_is_in_progress():
    feed = node()
    inside, release = threading.Event(), threading.Event()
    expected = feed.precondition("digital")
    worker = threading.Thread(target=feed.command, args=("op-1", "digital", expected,
                                                         lambda: (inside.set(), release.wait(5))))
    worker.start()
    assert inside.wait(5)
    with pytest.raises(feed_node.InProgress):
        feed.command("op-1", "digital", expected, lambda: None)
    release.set()
    worker.join(5)


def test_two_commands_against_one_revision_apply_once():
    feed = node()
    expected = feed.precondition("digital")
    results, barrier = [], threading.Barrier(2)

    def attempt(op):
        barrier.wait(5)
        try:
            results.append(feed.command(op, "digital", expected,
                                        lambda: feed.transition("digital", "running", generation=op)))
        except feed_node.Conflict:
            results.append("refused")

    workers = [threading.Thread(target=attempt, args=(f"op-{n}",)) for n in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(5)
    assert sorted(map(str, results)) == ["1", "refused"]


def test_old_commands_are_forgotten_but_still_refused():
    now = [1000.0]
    feed = node(clock=lambda: now[0])
    expected = feed.precondition("digital")
    feed.command("op-1", "digital", expected, lambda: feed.transition("digital", "starting"))
    now[0] += feed_node.OP_MEMORY_SECONDS + 1
    feed.command("op-2", "digital", feed.precondition("digital"), lambda: None)   # tidies the old one away
    assert "op-1" not in feed._ops
    with pytest.raises(feed_node.Conflict):
        feed.command("op-1", "digital", expected, lambda: None)


def test_memory_is_bounded():
    feed = node()
    for n in range(feed_node.OP_MEMORY + 5):
        feed.command(f"op-{n}", "analog", feed.precondition("analog"), lambda: None)
    assert len(feed._ops) <= feed_node.OP_MEMORY + 1


def test_status():
    feed = node()
    feed.transition("roku", "running", generation="c1")
    status = feed.status()
    assert status["node"] == "viviserv" and status["feed_epoch"] == feed.epoch
    assert status["lanes"]["roku"] == {"revision": 1, "lifecycle": "running"}


# -- LocalFeed: control's way to the feed, in one process ---------------------------------------

class FakeOps:
    """The parts of server.py LocalFeed calls, by name, as it does."""
    LANE_DIGITAL, LANE_ANALOG = "digital", "analog"

    def __init__(self):
        self.lock = threading.RLock()
        self.lanes = {lane: {"selection": None, "quality": "medium", "stream_id": None, "health": None,
                             "error": None, "taps": []} for lane in ("digital", "analog", "roku")}
        self.running, self.calls, self.primary_video_lane = set(), [], "digital"

    def lane_is_running(self, lane):
        return lane in self.running

    def lane_processes_alive(self, lane):
        return lane in self.running

    def lane_stream_ready(self, lane):
        return lane in self.running

    def start_digital(self, channel, quality):
        self.calls.append(("start_digital", channel["id"], quality))
        if channel["id"] == "dead":
            return False, "no lock on RF 9", False
        self.running.add("digital")
        self.lanes["digital"].update(selection=channel, quality=quality, stream_id="s1")
        return True, None, False

    def start_analog(self, channel, quality):
        self.calls.append(("start_analog", channel, quality))
        return True, None, {"device": "/dev/video0"}

    def stop_lane(self, lane):
        self.calls.append(("stop_lane", lane))
        self.running.discard(lane)


def local():
    ops = FakeOps()
    return feed_node.LocalFeed(node(), ops), ops


def test_a_lane_is_read_as_a_copy_with_the_feeds_counts():
    client, ops = local()
    client.tune_digital({"id": "wusa"}, "low")
    lane = client.lane("digital")
    assert lane["running"] and lane["alive"] and lane["ready"] and lane["stream_id"] == "s1"
    assert lane["lifecycle"] == "idle" and lane["revision"] == 0, "the fake does not report transitions"
    lane["selection"]["id"] = "changed"
    assert ops.lanes["digital"]["selection"]["id"] == "wusa", "a copy, not the feed's own"
    assert client.running_lanes() == ["digital"] and client.primary_video_lane() == "digital"


def test_each_change_is_a_feed_command():
    client, ops = local()
    assert client.tune_digital({"id": "wusa"}, "low") == (True, None, False)
    assert client.tune_analog(4, "high")[2] == {"device": "/dev/video0"}
    client.stop("digital")
    assert [c[0] for c in ops.calls] == ["start_digital", "start_analog", "stop_lane"]
    assert [op["lane"] for op in client.node._ops.values()] == ["digital", "analog", "digital"]


def test_a_failed_tune_is_recorded_on_the_lane():
    client, ops = local()
    assert client.tune_digital({"id": "dead"}, "low")[0] is False
    assert ops.lanes["digital"]["error"] == "no lock on RF 9"


def test_a_conflict_is_reread_and_tried_once_more(monkeypatch):
    client, ops = local()
    stale = [("old-epoch", 0)]
    real = client.node.precondition
    monkeypatch.setattr(client.node, "precondition", lambda lane: stale.pop() if stale else real(lane))
    assert client.tune_digital({"id": "wusa"}, "low")[0] is True
    assert len(ops.calls) == 1, "the refused attempt never ran"


def test_stop_all_stops_every_lane():
    client, ops = local()
    client.stop_all()
    assert [c for c in ops.calls] == [("stop_lane", "digital"), ("stop_lane", "analog"), ("stop_lane", "roku")]


# -- Recorders: the feed's VCR processes and their journal -----------------------------------------

class FakeProc:
    def __init__(self, command):
        self.command, self.returncode, self.written, self.stdin, self.stderr = command, None, "", self, None

    def write(self, text):
        self.written += text

    def flush(self):
        pass

    def close(self):
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def recorders(tmp_path, now):
    spawned = []

    def spawn(command):
        spawned.append(FakeProc(command))
        return spawned[-1]
    return feed_node.Recorders(tmp_path / "journal.jsonl", spawn, clock=lambda: now[0]), spawned


def test_a_recorder_is_journalled_from_start_to_end(tmp_path):
    now = [1000.0]
    feed, spawned = recorders(tmp_path, now)
    rec = feed.start(["ffmpeg", "x"], tmp_path / "a.ts.part", 2000, kind="tap", lane="digital", tap_port=18384)
    assert feed.view(rec)["alive"] and feed.live() == [rec]
    feed.stop(rec)
    assert spawned[0].written == "q"
    view = feed.view(rec)
    assert view["alive"] is False and view["ended"] == "stopped" and view["returncode"] == 0
    events = feed.journal()
    assert [(e["event"], e.get("reason")) for e in events] == [("start", None), ("end", "stopped")]
    assert events[0]["tap_port"] == 18384 and events[0]["until"] == 2000


def test_one_writer_per_part(tmp_path):
    feed, spawned = recorders(tmp_path, [1000.0])
    first = feed.start(["ffmpeg"], tmp_path / "a.ts.part", 2000)
    assert feed.start(["ffmpeg"], tmp_path / "a.ts.part", 2000) == first
    assert len(spawned) == 1
    feed.stop(first)
    assert feed.start(["ffmpeg"], tmp_path / "a.ts.part", 2000) != first, "a new piece may start once it ended"


def test_the_feed_stops_a_recording_at_its_end_by_itself(tmp_path):
    now = [1000.0]
    feed, spawned = recorders(tmp_path, now)
    rec = feed.start(["ffmpeg"], tmp_path / "a.ts.part", 1500)
    feed.sweep()
    assert spawned[0].written == ""
    now[0] = 1500
    feed.sweep()
    assert spawned[0].written == "q" and feed.view(rec)["ended"] == "until"
    assert feed.journal()[-1]["reason"] == "until"


def test_an_exit_is_noticed_and_journalled(tmp_path):
    feed, spawned = recorders(tmp_path, [1000.0])
    rec = feed.start(["ffmpeg"], tmp_path / "a.ts.part", 2000)
    spawned[0].returncode = 1
    view = feed.view(rec)
    assert view["alive"] is False and view["ended"] == "exited" and view["returncode"] == 1
    assert feed.journal()[-1]["returncode"] == 1
    feed.stop(rec)
    assert len(feed.journal()) == 2, "ending twice journals once"


def test_a_failed_spawn_starts_nothing(tmp_path):
    def broken(command):
        raise OSError("no ffmpeg")
    feed = feed_node.Recorders(tmp_path / "journal.jsonl", broken)
    with pytest.raises(OSError):
        feed.start(["ffmpeg"], tmp_path / "a.ts.part", 2000)
    assert feed.live() == [] and feed.journal() == []


def test_stop_all_and_unknown_ids(tmp_path):
    feed, spawned = recorders(tmp_path, [1000.0])
    for name in ("a", "b"):
        feed.start(["ffmpeg"], tmp_path / f"{name}.ts.part", 2000)
    feed.stop_all(timeout=2)
    assert [p.written for p in spawned] == ["q", "q"] and feed.live() == []
    assert feed.view("nope") is None
    feed.stop("nope")      # nothing to do
