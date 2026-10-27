#!/usr/bin/env python3
"""Navigate the Roku while the VCR records the digital lane, and count tasks.

This is what failed on 2026-10-13: the digital packager, a VCR recording and
the Roku verifier together reached the unit's TasksMax, the kernel refused
three threads, and each refusal failed a Roku tune. Passes when the
navigation is verified, the recording ran throughout on its first recorder,
and the unit's cgroup refused nothing. Split, pass
--cgroup for each service (the feed records, control's verifier decodes).

    task_headroom_check.py --url http://127.0.0.1:8098 \
        --cgroup /sys/fs/cgroup/system.slice/tv-thing-candidate-X.service
"""
import argparse
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stream_sanity_check import claim_lane, http_json, wait_roku_idle  # noqa: E402


def refusals(cgroup):
    for line in (cgroup / "pids.events").read_text().splitlines():
        key, value = line.split()
        if key == "max":
            return int(value)
    return 0


def taping(url, timer_id):
    """What went wrong with the recording, or None while its first recorder runs."""
    payload = http_json(url, "/api/v1/recordings", timeout=5)
    timer = next((t for t in payload["timers"] + payload["history"] if t["id"] == timer_id), None)
    if timer is None:
        return "the recording is gone"
    if timer["status"] != "recording" or timer.get("attempts", 1) > 1:
        return (f"the recording did not keep running: {timer['status']}, attempt "
                f"{timer.get('attempts')}, {timer.get('note')}")
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--cgroup", type=Path, action="append", required=True)
    parser.add_argument("--target", default="youtube-tv:NBC")
    # The owner's YouTube TV navigation cap for the Roku lane.
    parser.add_argument("--max-navigation-seconds", type=float, default=150)
    args = parser.parse_args()

    before = {cgroup: refusals(cgroup) for cgroup in args.cgroup}
    limits = {cgroup: (cgroup / "pids.max").read_text().strip() for cgroup in args.cgroup}
    token = claim_lane(args.url, "roku", "task headroom check",
                       "Roku navigation during a VCR recording", 600)
    recording = None
    failures = []
    peaks = dict.fromkeys(args.cgroup, 0)
    try:
        wait_roku_idle(args.url, token)
        recording = http_json(args.url, "/api/v1/recordings", method="POST",
                              payload={"mode": "otr", "from_live": True})
        recording = recording["timer"]["id"]
        time.sleep(15)   # let the recorder's encoder get going first
        trouble = taping(args.url, recording)
        if trouble:
            failures.append(trouble)
        else:
            source, _, channel_id = args.target.partition(":")
            tune = http_json(args.url, "/api/v1/tune", method="POST", timeout=20,
                             payload={"source": source, "channel_id": channel_id,
                                      "token": token, "force": True})
            if not tune.get("ok"):
                failures.append(f"tune refused: {tune.get('error')}")
        started = time.monotonic()
        state = None
        while not failures and time.monotonic() - started < args.max_navigation_seconds:
            trouble = taping(args.url, recording)
            if trouble:
                failures.append(trouble)
                break
            for cgroup in args.cgroup:
                peaks[cgroup] = max(peaks[cgroup], int((cgroup / "pids.current").read_text()))
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
            try:
                http_json(args.url, "/api/v1/recordings/cancel", method="POST",
                          payload={"id": recording})
            except urllib.error.HTTPError as error:
                if error.code != 409:     # 409: it had already ended, as reported
                    raise
        http_json(args.url, "/api/v1/tuner/lock/release", method="POST",
                  payload={"lane": "roku", "token": token})
    refused = sum(refusals(cgroup) - before[cgroup] for cgroup in args.cgroup)
    print("Tasks: " + "; ".join(f"{cgroup.name} peak {peaks[cgroup]} of {limits[cgroup]}"
                                for cgroup in args.cgroup) + f"; TasksMax refused {refused}")
    if refused:
        failures.append(f"TasksMax refused {refused} thread(s) or process(es)")
    for failure in failures:
        print(f"FAIL: {failure}")
    print("PASS" if not failures else "FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
