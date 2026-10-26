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
