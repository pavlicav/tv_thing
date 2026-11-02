"""Which digital tuner a live tune uses, of this node's and its peer's (peers.py).

Every node shows the same dial; any tuner that receives a channel's station (its RF channel
and TSID, ``receives``) may play it. A
tuner is a dict in ``/api/v1/tuners``' shape. Each has a signal grade for a channel
(``grade``): how well its antenna plays that station, good or not yet measured before fair
before poor. ``choose`` takes, in order:

1. a tuner already playing the channel, healthily: watching along changes nothing, so
   this is allowed even when it is locked (server.lane_plays). Unless it plays it worse
   than an available tuner would: then the channel moves to that one;
2. an available tuner, the best graded first; of those, this node's before the peer's,
   and an idle one before a running one: an unlocked tuner is anyone's to retune, and the
   peer's is relayed through this node's front door;
3. nothing: every tuner that could play the channel is locked, scanning or unknown.

So with both nodes' tuners healthy a tune stays on this node; one that this node's antenna
plays poorly goes to the peer's, when the peer's plays it well.

Locks are what move a tune: a locked tuner is never chosen for a change, so locking this
node's tuner sends the next tune to the peer's. An unknown one (its node has not answered)
is never chosen at all.
"""
from __future__ import annotations


def receives(tuner, channel):
    """Whether a tuner's antenna gets ``channel``.

    A station is its RF channel and its transport stream ID (TSID), which the FCC gives each
    station once, nationwide: a node in another city whose scan found another station on
    the same RF channel heard another TSID there, and never plays this one. Where either
    side has no TSID on record (a plan row or a reception from before TSIDs), the RF
    channel alone decides, as it did before. A tuner with no reception at all is taken to
    get everything (local_tuners decides which ones may say so).
    """
    rf, tsid = channel.get("rf_channel"), channel.get("tsid")
    got = tuner.get("rf_channels")
    if got is None or rf is None:
        return True
    if rf not in got:
        return False
    heard = (tuner.get("tsids") or {}).get(str(rf))
    return tsid is None or heard is None or heard == tsid


# A grade's rank: the lower, the better. Not yet measured counts as good: it is learnt by
# playing, and a node prefers its own tuner until it knows better.
RANKS = {"good": 0, None: 0, "fair": 1, "poor": 2}


def grade(tuner, channel):
    """How well ``tuner`` plays ``channel``'s station: "good", "fair", "poor" or None (not
    measured). What it plays now, measured, says; else its antenna's last word on the RF
    channel (signal_health.py)."""
    rf = channel.get("rf_channel")
    live = tuner.get("signal") or {}
    if live.get("verdict") and live.get("rf") == rf:
        return live["verdict"]
    return (tuner.get("health") or {}).get(str(rf))


def rank(tuner, channel):
    return RANKS.get(grade(tuner, channel), 0)


def plays(tuner, channel_id):
    stream = tuner.get("stream") or {}
    return (tuner.get("reachable", True)
            and (tuner.get("selection") or {}).get("id") == channel_id
            and stream.get("active") and stream.get("ready") and not stream.get("error"))


def candidates(tuners, channel):
    """The tuners that could play ``channel`` (a channels.json row) at all."""
    return [tuner for tuner in tuners if tuner.get("kind", "digital") == "digital"
            and receives(tuner, channel)]


def choose(tuners, channel):
    """``(tuner, why)``: the tuner to play ``channel`` on, or ``(None, why not)``.

    ``why`` is one of "playing", "idle", "retune", "better" (the channel plays elsewhere,
    worse than on this one); or "none" (no tuner receives it),
    "locked" (each one that does is locked) and "unavailable" (scanning or unknown).
    """
    able = candidates(tuners, channel)
    if not able:
        return None, "none"
    playing = sorted((tuner for tuner in able if plays(tuner, channel["id"])),
                     # The available one first: a watcher along of a locked tuner gets the
                     # holder's quality, which an unlocked one need not impose.
                     key=lambda tuner: (rank(tuner, channel), not tuner.get("available"), not tuner.get("local")))
    free = sorted((tuner for tuner in able if tuner.get("available") and tuner not in playing),
                  key=lambda tuner: (rank(tuner, channel), not tuner.get("local"),
                                     bool((tuner.get("stream") or {}).get("active"))))
    if playing and not (free and rank(free[0], channel) < rank(playing[0], channel)):
        return playing[0], "playing"
    if free:
        if playing:
            return free[0], "better"
        return free[0], "retune" if (free[0].get("stream") or {}).get("active") else "idle"
    if any(tuner.get("lock") for tuner in able):
        return None, "locked"
    return None, "unavailable"


def summary(tuners):
    """``tuners_available`` and the words the Watch page shows."""
    known = [tuner for tuner in tuners if tuner.get("reachable", True)]
    available = sum(1 for tuner in tuners if tuner.get("available"))
    return {"total": len(tuners), "reachable": len(known), "available": available}
