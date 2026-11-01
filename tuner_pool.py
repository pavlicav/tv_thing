"""Which digital tuner a live tune uses, of this node's and its peer's (peers.py).

Every node shows the same dial; any tuner that receives a channel's station (its RF channel
and TSID, ``receives``) may play it. A
tuner is a dict in ``/api/v1/tuners``' shape. ``choose`` takes, in order:

1. a tuner already playing the channel, healthily: watching along changes nothing, so
   this is allowed even when it is locked (server.lane_plays);
2. an available tuner of this node's, an idle one before a running one: today's
   behaviour, where an unlocked tuner is anyone's to retune;
3. an available tuner of the peer's, idle before running, relayed through this node's
   front door;
4. nothing: every tuner that could play the channel is locked, scanning or unknown.

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

    ``why`` is one of "playing", "idle", "retune"; or "none" (no tuner receives it),
    "locked" (each one that does is locked) and "unavailable" (scanning or unknown).
    """
    able = candidates(tuners, channel)
    if not able:
        return None, "none"
    playing = [tuner for tuner in able if plays(tuner, channel["id"])]
    if playing:
        # The available one first: a watcher along of a locked tuner gets the holder's
        # quality, which an unlocked one need not impose.
        return sorted(playing, key=lambda tuner: (not tuner.get("available"), not tuner.get("local")))[0], "playing"
    free = sorted((tuner for tuner in able if tuner.get("available")),
                  key=lambda tuner: (not tuner.get("local"), bool((tuner.get("stream") or {}).get("active"))))
    if free:
        return free[0], "retune" if (free[0].get("stream") or {}).get("active") else "idle"
    if any(tuner.get("lock") for tuner in able):
        return None, "locked"
    return None, "unavailable"


def summary(tuners):
    """``tuners_available`` and the words the Watch page shows."""
    known = [tuner for tuner in tuners if tuner.get("reachable", True)]
    available = sum(1 for tuner in tuners if tuner.get("available"))
    return {"total": len(tuners), "reachable": len(known), "available": available}
