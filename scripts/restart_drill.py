#!/usr/bin/env python3
"""The restart drill (INVARIANTS.md): note what is flowing, apply one failure, watch it flow.

It reads, and only reads, TV Thing's status, every running lane's HLS playlist, the
recording in progress, the internet relays named (and the one VizelVision airs), and
VizelVision master's status. Then it runs --command (the failure to inject: a restart, a
kill, a SIGSTOP) and watches for --seconds. It exits 1, naming the invariant, if:

* a lane's stream generation changes (invariants 1 and 5);
* a playlist stops advancing for longer than its target duration plus 2 s (1 and 8);
* a recording stops growing (1 and 4);
* VizelVision's programme on air changes, master falls to the test card, or a new input
  failure appears (1).

With --expect-change (a feed restart), generations may change: every lane that was running
must be running again, and VizelVision back on air, by the end.

Before the feed/control split, any `tv-thing` restart kills every ffmpeg child, so it SHOULD
fail this drill: that is the point. --watch-only runs no command, as a check that everything
is flowing now. Never pass --command against production without the owner's OK.

    scripts/restart_drill.py --watch-only --seconds 30
    scripts/restart_drill.py --command 'sudo systemctl restart tv-thing'
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

STALL_SLACK = 2.0           # seconds past a playlist's target duration before it counts as stalled
RECORDING_STALL = 20.0      # a recording that has not grown for this long has stopped
DEFAULT_VIZELVISION_CONFIG = "/home/vivian/vizelVision/config.master.json"


# -- pure parts (test_restart_drill.py) ---------------------------------------------------------

def parse_playlist(text):
    """(media sequence, target duration, segments) of an HLS media playlist, or None."""
    if not text.startswith("#EXTM3U"):
        return None
    sequence = re.search(r"^#EXT-X-MEDIA-SEQUENCE:(\d+)", text, re.M)
    target = re.search(r"^#EXT-X-TARGETDURATION:(\d+(?:\.\d+)?)", text, re.M)
    segments = len(re.findall(r"^#EXTINF:", text, re.M))
    if not target or not segments:
        return None
    return int(sequence.group(1)) if sequence else 0, float(target.group(1)), segments


def first_variant(text):
    """The first rendition a master playlist names, or None for a media playlist."""
    if "#EXT-X-STREAM-INF" not in text:
        return None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            for uri in lines[i + 1:]:
                if uri.strip() and not uri.startswith("#"):
                    return uri.strip()
    return None


def running_lanes(status):
    """{lane: {generation, selection, playlist}} for each lane whose stream is active."""
    lanes = {}
    for name, lane in (status.get("lanes") or {}).items():
        stream = (lane or {}).get("stream") or {}
        if not stream.get("active"):
            continue
        playlist = next((p.get("url") for p in stream.get("playback") or []
                         if isinstance(p, dict) and p.get("protocol") == "hls"), None)
        selection = lane.get("selection") or {}
        lanes[name] = {"generation": stream.get("generation"), "playlist": playlist,
                       "revision": stream.get("revision"), "feed_epoch": status.get("feed_epoch"),
                       "selection": selection.get("id") or selection.get("channel") or selection.get("channel_id")}
    return lanes


class Advance:
    """Whether a playlist keeps advancing: stalled once its newest segment has not moved, or
    it could not be read, for its target duration plus STALL_SLACK.

    The newest segment's number is the media sequence plus the segments listed: a full list
    moves its sequence, and a new stream's list grows at sequence 0 until it is full (12 s of
    0.6 s segments), which once read as a stall right after a retune (2026-10-27)."""

    def __init__(self, now):
        self.moved_at, self.last, self.target = now, None, 6.0
        self.worst = 0.0

    def update(self, now, parsed):
        if parsed is not None:
            sequence, self.target, segments = parsed
            newest = sequence + segments
            if self.last is None or newest != self.last:
                self.last, self.moved_at = newest, now
        still = now - self.moved_at
        self.worst = max(self.worst, still)
        return still > self.target + STALL_SLACK


class Growth:
    """Whether a recording keeps growing."""

    def __init__(self, now, size):
        self.size, self.grew_at, self.worst = size, now, 0.0

    def update(self, now, size):
        if size is not None and size > self.size:
            self.size, self.grew_at = size, now
        still = now - self.grew_at
        self.worst = max(self.worst, still)
        return still > RECORDING_STALL


def active_recordings(payload):
    """{id: (bytes, scheduled end)} of the recordings TV Thing is writing now."""
    out = {}
    for item in (payload or {}).get("active_recordings") or []:
        if isinstance(item, dict) and item.get("id"):
            size = item.get("bytes") if isinstance(item.get("bytes"), (int, float)) else None
            end = item.get("end") if isinstance(item.get("end"), (int, float)) else None
            out[item["id"]] = (size, end)
    return out


def recording_gone(rid, end, wall_now, verdict):
    """A recording TV Thing no longer lists: fine at its scheduled end, a failure before it."""
    if end is not None and wall_now >= end - 5:
        verdict.note(f"recording {rid} reached its scheduled end")
    else:
        verdict.fail([1, 4], f"recording {rid} stopped before its scheduled end")


def on_air(master):
    """(programme id or None, last input failure's time or None) from VizelVision's /v1/master."""
    programme = (master or {}).get("on_air") or {}
    failure = (master or {}).get("last_input_failure") or {}
    return programme.get("id"), failure.get("at")


def internet_on_air(master):
    programme = (master or {}).get("on_air") or {}
    return programme.get("channel_id") if programme.get("source_id") == "internet" else None


class Verdict:
    def __init__(self):
        self.failures, self.notes = [], []

    def fail(self, invariants, message):
        if not any(f["message"] == message for f in self.failures):
            self.failures.append({"invariants": invariants, "message": message})

    def note(self, message):
        if message not in self.notes:
            self.notes.append(message)


def compare_lanes(baseline, current, verdict, *, expect_change):
    """Generation and selection changes between two running_lanes() snapshots."""
    for name, before in baseline.items():
        after = current.get(name)
        if expect_change:
            continue
        if after is None:
            verdict.fail([1], f"the {name} lane stopped running")
        elif after["generation"] != before["generation"]:
            verdict.fail([1, 5] if name == "roku" else [1],
                         f"the {name} lane's generation changed ({before['generation']} → {after['generation']})")
        elif after["selection"] != before["selection"]:
            verdict.fail([1], f"the {name} lane's selection changed ({before['selection']} → {after['selection']})")


def compare_vizelvision(baseline, current, verdict, *, expect_change, final=False):
    (programme, failure_at), (now_programme, now_failure_at) = baseline, current
    if programme is None:
        return
    if now_programme is None:
        if not expect_change or final:
            verdict.fail([1], "VizelVision master is off air (test card)")
    elif now_programme != programme and not expect_change:
        verdict.fail([1], "VizelVision's programme on air changed (the input was dropped and put back)")
    if now_failure_at and now_failure_at != failure_at and not expect_change:
        verdict.fail([1], "VizelVision master reported a new input failure")


# -- the live drill -----------------------------------------------------------------------------

def get(url, *, headers=None, timeout=2.0, raw=False):
    try:
        with urlopen(Request(url, headers=headers or {}), timeout=timeout) as response:
            body = response.read(8 * 1024 * 1024)
        return body.decode("utf-8", "replace") if raw else json.loads(body)
    except (HTTPError, URLError, OSError, ValueError):
        return None


class Drill:
    def __init__(self, args):
        self.args = args
        self.tv = args.tv.rstrip("/") + "/"
        self.vv = args.vizelvision.rstrip("/") + "/" if args.vizelvision else None
        self.vv_headers = {}
        if self.vv and args.vizelvision_config and Path(args.vizelvision_config).exists():
            key = (json.loads(Path(args.vizelvision_config).read_text()).get("server") or {}).get("api_key")
            if key:
                self.vv_headers = {"X-API-Key": key}
        self.verdict, self.timeline = Verdict(), []

    def event(self, t0, text):
        self.timeline.append((round(time.monotonic() - t0, 1), text))

    def master(self):
        return get(self.vv + "v1/master", headers=self.vv_headers) if self.vv else None

    def relay_playlist(self, stream_id):
        """The media playlist URL of an internet relay (its first rendition)."""
        base = urljoin(self.tv, f"stream/internet/{stream_id}/index.m3u8")
        text = get(base, raw=True, timeout=5)
        variant = first_variant(text) if text else None
        return urljoin(base, variant) if variant else base

    def run(self):
        args, verdict = self.args, self.verdict
        t0 = time.monotonic()
        status = get(self.tv + "api/v1/status")
        if status is None:
            print("TV Thing's status did not answer: nothing to compare against", file=sys.stderr)
            return 2
        lanes = running_lanes(status)
        master = self.master()
        vv_baseline = on_air(master) if master is not None else None
        relays = list(dict.fromkeys(args.relay + ([internet_on_air(master)] if internet_on_air(master) else [])))
        playlists = {name: urljoin(self.tv, lane["playlist"]) for name, lane in lanes.items() if lane["playlist"]}
        playlists.update({f"internet/{r}": self.relay_playlist(r) for r in relays})
        advance = {name: Advance(time.monotonic()) for name in playlists}
        recordings = active_recordings(get(self.tv + "api/v1/recordings"))
        growth = {rid: Growth(time.monotonic(), size or 0) for rid, (size, _) in recordings.items()}
        baseline = {"lanes": lanes, "playlists": playlists, "recordings": {r: s for r, (s, _) in recordings.items()},
                    "vizelvision": {"on_air": vv_baseline[0], "last_input_failure": vv_baseline[1]} if vv_baseline else None}

        command = None
        if args.command:
            self.event(t0, f"run: {args.command}")
            command = subprocess.Popen(args.command, shell=True)
        status_down = None
        end = time.monotonic() + args.seconds
        next_recordings = next_master = 0.0
        last_lanes = lanes
        while time.monotonic() < end:
            now = time.monotonic()
            if command is not None and command.poll() is not None:
                self.event(t0, f"command exited {command.returncode}")
                if command.returncode:
                    verdict.note(f"the command exited {command.returncode}")
                command = None
            current = get(self.tv + "api/v1/status")
            if current is None:
                if status_down is None:
                    status_down = now
                    self.event(t0, "TV Thing's API stopped answering")
            else:
                if status_down is not None:
                    self.event(t0, f"TV Thing's API answers again after {now - status_down:.0f} s")
                    status_down = None
                last_lanes = running_lanes(current)
                compare_lanes(lanes, last_lanes, verdict, expect_change=args.expect_change)
            for name, url in playlists.items():
                text = get(url, raw=True)
                if advance[name].update(time.monotonic(), parse_playlist(text) if text else None) and not args.expect_change:
                    verdict.fail([8] if name.startswith("internet/") else [1],
                                 f"{name}'s playlist stopped advancing")
            if now >= next_recordings and growth:
                next_recordings = now + 5
                answer = get(self.tv + "api/v1/recordings")
                sizes = active_recordings(answer)
                # Control down or frozen (A2-A4): a size is unknown, not stalled. Judged on its return.
                for rid, tracker in list(growth.items()) if answer is not None else []:
                    if answer is not None and rid not in sizes:
                        recording_gone(rid, recordings[rid][1], time.time(), verdict)
                        del growth[rid]
                    elif tracker.update(time.monotonic(), (sizes.get(rid) or (None, None))[0]) and not args.expect_change:
                        verdict.fail([1, 4], f"recording {rid} stopped growing")
            if now >= next_master and vv_baseline:
                next_master = now + 2
                master = self.master()
                if master is not None:
                    compare_vizelvision(vv_baseline, on_air(master), verdict, expect_change=args.expect_change)
            time.sleep(max(0.0, args.interval - (time.monotonic() - now)))
        if command is not None:
            verdict.note("the command was still running at the end")
        if status_down is not None:
            verdict.fail([1], f"TV Thing's API did not come back within {args.seconds:.0f} s")
        if args.expect_change:
            for name in lanes:
                if name not in last_lanes:
                    verdict.fail([1], f"the {name} lane was not running again by the end")
            if vv_baseline:
                master = self.master()
                if master is not None:
                    compare_vizelvision(vv_baseline, on_air(master), verdict, expect_change=True, final=True)
        for name, tracker in advance.items():
            verdict.note(f"{name}: longest without a new segment {tracker.worst:.1f} s (target {tracker.target:.0f} s)")
        self.report(baseline)
        return 1 if verdict.failures else 0

    def report(self, baseline):
        verdict = self.verdict
        if self.args.json:
            print(json.dumps({"ok": not verdict.failures, "baseline": baseline, "timeline": self.timeline,
                              "failures": verdict.failures, "notes": verdict.notes}, indent=2))
            return
        lanes = baseline["lanes"]
        print("Running lanes: " + (", ".join(f"{n} ({l['selection']}, {l['generation']})" for n, l in lanes.items()) or "none"))
        if baseline["recordings"]:
            print("Recording: " + ", ".join(baseline["recordings"]))
        if baseline["vizelvision"]:
            print(f"VizelVision on air: {baseline['vizelvision']['on_air'] or 'nothing (test card)'}")
        for at, text in self.timeline:
            print(f"  +{at:>5.1f} s  {text}")
        for note in verdict.notes:
            print("  · " + note)
        if verdict.failures:
            for f in verdict.failures:
                print(f"FAIL (invariant {', '.join(map(str, f['invariants']))}): {f['message']}")
        else:
            print("OK: every invariant held")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--command", help="the failure to inject, run by the shell")
    what.add_argument("--watch-only", action="store_true", help="inject nothing: is everything flowing?")
    parser.add_argument("--seconds", type=float, default=90.0, help="how long to watch (default 90)")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between polls (default 1)")
    parser.add_argument("--expect-change", action="store_true", help="a feed restart: lanes must come back")
    parser.add_argument("--relay", action="append", default=[], metavar="ID", help="also watch this internet relay")
    parser.add_argument("--tv", default="http://127.0.0.1:8080/", help="TV Thing's base URL")
    parser.add_argument("--vizelvision", default="http://127.0.0.1:6970/", help="VizelVision's web URL ('' to skip)")
    parser.add_argument("--vizelvision-config", default=DEFAULT_VIZELVISION_CONFIG, help="for its API key")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return Drill(parser.parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
