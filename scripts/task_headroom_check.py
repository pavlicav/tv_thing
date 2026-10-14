#!/usr/bin/env python3
"""Navigate the Roku while the VCR records the digital lane, and count tasks.

This is what failed on 2026-10-13: the digital packager, a VCR recording and
the Roku verifier together reached the unit's TasksMax, the kernel refused
three threads, and each refusal failed a Roku tune. Passes when the
navigation is verified and the unit's cgroup refused nothing.

    task_headroom_check.py --url http://127.0.0.1:8098 \
        --cgroup /sys/fs/cgroup/system.slice/tv-thing-candidate-X.service
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stream_sanity_check import claim_lane, http_json, wait_roku_idle  # noqa: E402


def refusals(cgroup):
    for line in (cgroup / "pids.events").read_text().splitlines():
        key, value = line.split()
        if key == "max":
            return int(value)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--target", default="youtube-tv:NBC")
    # The owner's YouTube TV navigation cap for the Roku lane.
    parser.add_argument("--max-navigation-seconds", type=float, default=150)
    args = parser.parse_args()

    before = refusals(args.cgroup)
    limit = (args.cgroup / "pids.max").read_text().strip()
    token = claim_lane(args.url, "roku", "task headroom check",
                       "Roku navigation during a VCR recording", 600)
    recording = None
    failures = []
    peak = 0
    try:
        wait_roku_idle(args.url, token)
        recording = http_json(args.url, "/api/v1/recordings", method="POST",
                              payload={"mode": "otr", "from_live": True})
        recording = recording["timer"]["id"]
        time.sleep(15)   # let the recorder's encoder get going first
        source, _, channel_id = args.target.partition(":")
        tune = http_json(args.url, "/api/v1/tune", method="POST", timeout=20,
                         payload={"source": source, "channel_id": channel_id,
                                  "token": token, "force": True})
        if not tune.get("ok"):
            failures.append(f"tune refused: {tune.get('error')}")
        started = time.monotonic()
        state = None
        while not failures and time.monotonic() - started < args.max_navigation_seconds:
            peak = max(peak, int((args.cgroup / "pids.current").read_text()))
            lane = http_json(args.url, "/api/v1/status", timeout=3)["lanes"]["roku"]
            operation = (lane.get("roku") or {}).get("operation") or {}
            state = operation.get("state")
            if state == "succeeded":
                break
            if state in ("failed", "cancelled"):
                failures.append(f"navigation {state}: {operation.get('message')}")
            time.sleep(1)
        else:
            if not failures:
                failures.append(f"not verified within {args.max_navigation_seconds:g}s")
        print(f"{args.target} {state} after {time.monotonic() - started:.0f}s "
              f"during a digital recording", flush=True)
    finally:
        if recording:
            http_json(args.url, "/api/v1/recordings/cancel", method="POST",
                      payload={"id": recording})
        http_json(args.url, "/api/v1/tuner/lock/release", method="POST",
                  payload={"lane": "roku", "token": token})
    refused = refusals(args.cgroup) - before
    print(f"Tasks: peak {peak} of {limit}; TasksMax refused {refused}")
    if refused:
        failures.append(f"TasksMax refused {refused} thread(s) or process(es)")
    for failure in failures:
        print(f"FAIL: {failure}")
    print("PASS" if not failures else "FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
