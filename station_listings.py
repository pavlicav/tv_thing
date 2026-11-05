"""VizelVision's schedule, as guide listings for the numbers it holds.

The station's ``GET /v1/playback`` is what its own Schedule page draws: Up next and the weekly
schedule played forward under the queue's rules. A game holds the air until its estimated final,
and what it overruns does not appear. So the guide lists what will air, not what was painted.

A projection begins now, so the programme on air would read as starting this minute. Its Up next
entry in ``GET /v1/queue`` says when it really went on.

The station is another service, and no guide waits on it: a read is short, a failed one is not
tried again for a while, and the last good listing stands in meanwhile.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

AHEAD_SECONDS = 7 * 86400       # As far as the station's schedule runs; it projects up to 8 days.
CACHE_SECONDS = 30              # As often as its own On Air page asks.
STALE_SECONDS = 15 * 60         # A listing kept past that says little about what will air.
TIMEOUT_SECONDS = 3
MAX_BYTES = 4 * 1024 * 1024

OFF_AIR = "off-air"
ANTENNA = "tv-thing-digital"
# What a target's source is called here. A game's channel is its id, so its source names it alone;
# a baseball game has no one source (the station routes it), and its league says enough.
SOURCES = {ANTENNA: "Antenna", "tv-thing-analog": "Cable", "youtube-tv": "YouTube TV", "internet": "Internet"}
GAME_SOURCES = {"mlb": None, "sunday-ticket": "NFL Sunday Ticket", "thursday-night-football": "Prime Video",
                "nhl": "ESPN Unlimited", "nwsl": "ESPN Unlimited"}
# How each kind of programme got on the schedule, as the station's Schedule page words them.
KINDS = {"block": "From the weekly schedule", "show": "A scheduled show", "backup": "From the weekly schedule",
         "recording": "A recording from Up next", "manual": "Queued in Up next", "ident": "Station ID"}


def _seconds(stamp):
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()


def _same_target(left, right):
    return bool(left and right) and all(
        str(left.get(key)) == str(right.get(key)) for key in ("source_id", "channel_id"))


def _origin(target, channel_name):
    """Where a programme comes from, in words: "YouTube TV · NBC", "Antenna · WJLA". None for a
    recording, whose title says it all."""
    source_id = target.get("source_id")
    if source_id in GAME_SOURCES:
        return GAME_SOURCES[source_id]
    channel_id = target.get("channel_id")
    label = target.get("label") or (target.get("resolved_source") or {}).get("label")
    channel = label or channel_name(source_id, channel_id) or channel_id
    return " · ".join(str(part) for part in (SOURCES.get(source_id), channel) if part) \
        if source_id in SOURCES else None


def _event(segment, channel_name):
    """One segment of a projection as a guide event, or None for one with no time in it."""
    start, end = int(_seconds(segment["start"])), int(_seconds(segment["end"]))
    if end <= start:
        return None
    target, kind = segment.get("target") or {}, segment.get("kind")
    event = {"start": start, "duration": end - start, "kind": kind}
    # Whatever its name, a block whose source is the test card puts nothing on the air.
    if kind == "off_air" or target.get("source_id") == OFF_AIR:
        return {**event, "kind": "off_air", "title": "OFF AIR", "description": "The test card."}
    game = segment.get("game") or {}
    origin = _origin(target, channel_name)
    event["title"] = segment.get("title") or game.get("title") or origin or "VizelVision"
    notes = list(segment.get("notes") or [])
    if kind == "game":
        event["live"] = True
        event["episode_title"] = " · ".join(filter(None, [str(game.get("league") or "mlb").upper(), origin]))
        if segment.get("estimated"):
            event["estimated"] = True
            notes.insert(0, "On until its final, and that time is an estimate")
    else:
        if origin:
            event["episode_title"] = origin
        if kind in KINDS:
            notes.insert(0, KINDS[kind])
    if notes:
        event["description"] = ". ".join(notes) + "."
    return event


def _went_on(segment, queue):
    """When the programme on air went on: its Up next entry's, or the last entry to air when
    the projection names none and that one is still what is on (the test card after a final)."""
    aired = [item for item in queue.get("items") or []
             if isinstance(item, dict) and item.get("status") == "succeeded" and item.get("completed_at")]
    own = [item for item in aired if segment.get("item") and item.get("id") == segment["item"]]
    if not own and aired:
        latest = max(aired, key=lambda item: _seconds(item["completed_at"]))
        own = [latest] if _same_target(latest.get("target"), segment.get("target")) else []
    return max((_seconds(item["completed_at"]) for item in own), default=None)


def parse_listings(playback, queue=None, channel_name=lambda source_id, channel_id: None):
    """``GET /v1/playback`` as guide events, the programme on air back to when it went on
    (``GET /v1/queue``), and neighbouring stretches off the air as one."""
    events = []
    for index, segment in enumerate(playback["segments"]):
        event = _event(segment, channel_name)
        if event is None:
            continue
        if index == 0 and queue:
            went_on = _went_on(segment, queue)
            if went_on is not None and went_on < event["start"]:
                event["duration"] += event["start"] - int(went_on)
                event["start"] = int(went_on)
        last = events[-1] if events else None
        if last and last["kind"] == event["kind"] == "off_air" \
                and last["start"] + last["duration"] == event["start"]:
            last["duration"] += event["duration"]
        else:
            events.append(event)
    return events


class StationListings:
    """The station's listings, read when asked for and kept a little while."""

    def __init__(self, base_url, *, channel_name=lambda source_id, channel_id: None, opener=urlopen,
                 clock=time.time):
        self.base_url = base_url.rstrip("/")
        self.channel_name = channel_name
        self.opener = opener
        self.clock = clock
        self._lock = threading.Lock()
        self._next_read = 0
        self._good = None           # (when it was read, its listing)
        self._error = None

    def _get(self, path):
        request = Request(self.base_url + path, headers={"Accept": "application/json"})
        with self.opener(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("response over 4 MiB")
        return json.loads(raw)

    def _read(self, now):
        until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + AHEAD_SECONDS))
        playback = self._get(f"/v1/playback?until={until}")
        try:
            queue = self._get("/v1/queue")
        except (HTTPError, URLError, OSError, TimeoutError, ValueError):
            queue = None    # The programme on air then reads as starting now, and that is all.
        return {"events": parse_listings(playback, queue, self.channel_name),
                "paused": bool(playback.get("paused")), "pause_reason": playback.get("pause_reason")}

    def read(self):
        """``{"events", "available", "paused", "pause_reason", "generated_at", "last_error"}``.
        ``available`` is False, and no events, when the station has not answered for a while."""
        with self._lock:
            now = self.clock()
            if now >= self._next_read:
                try:
                    self._good = (now, self._read(now))
                    self._error = None
                except (HTTPError, URLError, OSError, TimeoutError, ValueError,
                        KeyError, TypeError, AttributeError) as error:
                    self._error = f"VizelVision's schedule did not answer ({type(error).__name__})"
                    print(f"[Guide] {self._error}: {error}", flush=True)
                self._next_read = now + CACHE_SECONDS
            good, error = self._good, self._error
        if good is None or now - good[0] >= STALE_SECONDS:
            return {"events": [], "available": False, "paused": False, "pause_reason": None,
                    "generated_at": None, "last_error": error}
        return {**good[1], "available": True, "generated_at": int(good[0]), "last_error": error}
