#!/usr/bin/env python3
"""The web streams stack's check (AGENTS.md): a web-basic stream plays, keeps its CPU in
bounds, does not grow its memory, and stops when nobody watches.

    python3 scripts/web_stream_check.py sports-guide
    python3 scripts/web_stream_check.py cairo-radio --minutes 10
    python3 scripts/web_stream_check.py internet/pentulive --base http://127.0.0.1:8085

It watches one stream as a viewer would -- its playlist every two seconds, each new
segment once -- for ``--minutes``, sampling the web-basic service's process tree
(the service, and a slide channel's renderer and encoder) every ten seconds. It fails
when:

* no segment comes within --max-start-seconds, a segment is empty or not MPEG-TS, or
  the playlist stops advancing for --max-stall-seconds;
* CPU averages over --max-cpu-average, or a sample exceeds --max-cpu-peak (percent of
  one core);
* memory (RSS) exceeds --max-rss-mib, or grows by more than --max-rss-growth-mib from
  the first minute after warm-up to the last (a leak);
* a slide channel's video still runs idle + 20 s after the last request, or the tree
  does not come back to within --max-rss-left-mib of where it began.

A relay (internet/<id>) has no processes of its own, so its CPU and memory are the
service's. Read-only: it asks for the stream as any viewer does and touches nothing
else; it stops by itself after --minutes.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stream_sanity_check import process_group_sample  # noqa: E402

UNIT = "tv-thing-web-basic"
SAMPLE_SECONDS = 10


def get(url, timeout=10):
    """``(status, body)``; an HTTP error is an answer, a failed connection is (None, error)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return None, str(error).encode()


def media_playlist(base, name):
    """``(url of the media playlist, its text)``, following a relay's first variant; None
    when there is none yet."""
    url = f"{base.rstrip('/')}/stream/{name}/index.m3u8"
    status, body = get(url)
    if status != 200:
        return None, None
    text = body.decode(errors="replace")
    if "#EXT-X-STREAM-INF" in text:
        variant = next(line for line in text.splitlines() if line and not line.startswith("#"))
        url = url.rsplit("/", 1)[0] + "/" + variant
        status, body = get(url)
        if status != 200:
            return None, None
        text = body.decode(errors="replace")
    return url, text


def segments(text):
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def slide_running(status_url, name):
    status, body = get(status_url, timeout=3)
    if status != 200:
        raise RuntimeError(f"web-basic's status did not answer ({status}): {body[:120]!r}")
    return bool((json.loads(body)["slides"].get(name) or {}).get("running"))


def main_pid():
    found = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", UNIT],
                           capture_output=True, text=True, timeout=5).stdout.strip()
    return int(found or 0)


def check(args):
    failures, samples = [], []
    slide = not args.name.startswith("internet/")
    pid = args.pid or main_pid()
    if pid <= 0:
        return [f"{UNIT} is not running (no MainPID); pass --pid"], {}
    process_group_sample(pid)                        # the CPU baseline
    began = process_group_sample(pid)["rss_mib"]
    if slide:
        deadline = time.monotonic() + args.idle_seconds + 20
        while slide_running(args.status, args.name):
            if time.monotonic() > deadline:
                return [f"{args.name} kept running with nobody watching before the check"], {}
            time.sleep(1)

    started = time.monotonic()
    url, text = None, None
    while time.monotonic() - started < args.max_start_seconds:
        url, text = media_playlist(args.base, args.name)
        if text and segments(text):
            break
        time.sleep(0.5)
    start_seconds = time.monotonic() - started
    if not (text and segments(text)):
        return [f"{args.name} gave no segment within {args.max_start_seconds:g}s"], {}

    seen, fetched, sizes = set(), 0, []
    advanced = time.monotonic()
    next_sample = time.monotonic()
    end = started + args.minutes * 60
    while time.monotonic() < end:
        url, text = media_playlist(args.base, args.name)
        new = [segment for segment in segments(text or "") if segment not in seen]
        for segment in new:
            seen.add(segment)
            status, body = get(url.rsplit("/", 1)[0] + "/" + segment, timeout=15)
            if status != 200 or not body:
                failures.append(f"segment {segment}: {status}, {len(body or b'')} bytes")
            elif segment.endswith(".ts") and body[0] != 0x47:
                failures.append(f"segment {segment} is not MPEG-TS")
            else:
                fetched += 1
                sizes.append(len(body))
        if new:
            advanced = time.monotonic()
        elif time.monotonic() - advanced > args.max_stall_seconds:
            failures.append(f"the playlist did not advance for {args.max_stall_seconds:g}s")
            advanced = time.monotonic()
        if time.monotonic() >= next_sample:
            sample = process_group_sample(pid)
            sample["at"] = time.monotonic() - started
            samples.append(sample)
            next_sample += SAMPLE_SECONDS
        time.sleep(2)

    cpu = [s["cpu_percent"] for s in samples if s["cpu_percent"] is not None]
    rss = [(s["at"], s["rss_mib"]) for s in samples]
    peak_rss = max(value for _, value in rss)
    if cpu and statistics.mean(cpu) > args.max_cpu_average:
        failures.append(f"CPU averaged {statistics.mean(cpu):.0f}% (limit {args.max_cpu_average:g}%)")
    if cpu and max(cpu) > args.max_cpu_peak:
        failures.append(f"CPU peaked at {max(cpu):.0f}% (limit {args.max_cpu_peak:g}%)")
    if peak_rss > args.max_rss_mib:
        failures.append(f"RSS reached {peak_rss:.0f} MiB (limit {args.max_rss_mib:g})")
    settled = [(at, value) for at, value in rss if at >= args.warmup_seconds]
    first = [value for at, value in settled if at < args.warmup_seconds + 60]
    last = [value for at, value in settled if at >= rss[-1][0] - 60]
    growth = statistics.median(last) - statistics.median(first) if first and last else None
    if growth is None:
        failures.append(f"too short to judge memory: watch longer than {args.warmup_seconds + 120:g}s")
    elif growth > args.max_rss_growth_mib:
        failures.append(f"RSS grew {growth:.1f} MiB while watched (limit {args.max_rss_growth_mib:g}): a leak")

    stopped_seconds = None
    left = None
    if slide:
        quiet = time.monotonic()
        while slide_running(args.status, args.name):
            if time.monotonic() - quiet > args.idle_seconds + 20:
                failures.append(f"{args.name} still running {args.idle_seconds + 20:g}s after its last viewer")
                break
            time.sleep(1)
        else:
            stopped_seconds = time.monotonic() - quiet
        time.sleep(2)
        after = process_group_sample(pid)
        left = after["rss_mib"] - began
        if after["ffmpeg_count"]:
            failures.append(f"an encoder outlived the stream ({after['ffmpeg_count']} ffmpeg left)")
        if left > args.max_rss_left_mib:
            failures.append(f"the service kept {left:.0f} MiB more than before it (limit {args.max_rss_left_mib:g})")

    report = {
        "stream": args.name, "minutes": args.minutes, "start_seconds": round(start_seconds, 1),
        "segments": fetched, "segment_kib_mean": round(statistics.mean(sizes) / 1024, 1) if sizes else None,
        "cpu_average": round(statistics.mean(cpu), 1) if cpu else None, "cpu_peak": round(max(cpu), 1) if cpu else None,
        "rss_began_mib": round(began, 1), "rss_peak_mib": round(peak_rss, 1),
        "rss_growth_mib": None if growth is None else round(growth, 1),
        "stopped_after_seconds": None if stopped_seconds is None else round(stopped_seconds, 1),
        "rss_left_mib": None if left is None else round(left, 1),
    }
    return failures, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="sports-guide, cairo-radio, or internet/<id> for a relay")
    parser.add_argument("--base", default="http://127.0.0.1:8080", help="where to ask for the stream")
    parser.add_argument("--status", default="http://127.0.0.1:8085/web/v1/status", help="web-basic's status")
    parser.add_argument("--pid", type=int, help=f"web-basic's process (default: {UNIT}'s MainPID)")
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--warmup-seconds", type=float, default=60)
    parser.add_argument("--idle-seconds", type=float, default=60, help="web-basic's idle stop")
    parser.add_argument("--max-start-seconds", type=float, default=20)
    parser.add_argument("--max-stall-seconds", type=float, default=12)
    parser.add_argument("--max-cpu-average", type=float, default=100)
    parser.add_argument("--max-cpu-peak", type=float, default=200)
    parser.add_argument("--max-rss-mib", type=float, default=600)
    parser.add_argument("--max-rss-growth-mib", type=float, default=24)
    parser.add_argument("--max-rss-left-mib", type=float, default=32)
    args = parser.parse_args(argv)
    try:
        failures, report = check(args)
    except RuntimeError as error:
        failures, report = [str(error)], {}
    print(json.dumps(report, indent=1))
    for failure in failures:
        print("FAIL:", failure)
    print("OK: within its limits" if not failures else f"FAILED: {len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
