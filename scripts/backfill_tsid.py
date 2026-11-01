#!/usr/bin/env python3
"""Add each plan channel's station TSID to channels.json, read off the air, without a rescan.

A rescan rewrites the plan from whatever it finds that day; this only adds ``tsid`` to the
rows it can confirm, and changes nothing else. For each RF channel of the plan it tunes,
reads the station's virtual channel table, and stamps that table's TSID on the plan's rows
for that RF channel, but only when every one of those rows' virtual channels and service
IDs is in the table: a station that does not match the plan is reported, never stamped.

It opens the tuner itself, so the tuner must be free: stop production (``tv-thing`` and
``tv-thing-feed``) first. ``--reception`` also records each TSID heard in this node's
state/reception.json, for the RF channels it already lists.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atsc_scan  # noqa: E402


def read_station(fd, adapter, rf, lock_timeout):
    """``(tsid or None, {(number, service_id)})`` heard on ``rf``."""
    atsc_scan.tune(fd, atsc_scan.rf_channel_frequency_hz(rf))
    if not atsc_scan.wait_for_lock(fd, timeout=lock_timeout)["locked"]:
        return None, set()
    services = atsc_scan.read_virtual_channels(adapter, timeout=4.0)
    if not services:
        return None, set()
    return services[0]["tsid"], {(item["number"], item["service_id"]) for item in services}


def stamp(channels, heard):
    """Stamp ``heard`` (``{rf: (tsid, services)}``) on ``channels`` in place.
    Returns ``{rf: (tsid, why)}``, why one of "stamped", "no lock", "mismatch: ..."."""
    report = {}
    for rf, (tsid, services) in heard.items():
        rows = [row for row in channels if row.get("rf_channel") == rf]
        missing = [row["number"] for row in rows if (row["number"], row["service_id"]) not in services]
        if tsid is None:
            report[rf] = (None, "no lock")
        elif missing:
            report[rf] = (tsid, "mismatch: the station has no " + ", ".join(missing))
        else:
            for row in rows:
                row["tsid"] = tsid
            report[rf] = (tsid, "stamped")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channels", default=str(Path(__file__).resolve().parent.parent / "channels.json"))
    parser.add_argument("--adapter", type=int, default=0)
    parser.add_argument("--rf", type=int, action="append", help="only this RF channel (repeatable)")
    parser.add_argument("--lock-timeout", type=float, default=3.0)
    parser.add_argument("--reception", help="this node's state/reception.json, to record TSIDs in too")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    path = Path(args.channels)
    channels = json.loads(path.read_text())
    wanted = args.rf or sorted({row["rf_channel"] for row in channels if row.get("rf_channel")})
    heard = {}
    fd = atsc_scan.open_frontend(args.adapter, writable=True)
    try:
        for rf in wanted:
            heard[rf] = read_station(fd, args.adapter, rf, args.lock_timeout)
    finally:
        os.close(fd)

    report = stamp(channels, heard)
    for rf, (tsid, why) in sorted(report.items()):
        print(f"RF {rf:2d}: TSID {'-' if tsid is None else f'0x{tsid:04X} ({tsid})'}: {why}")
    if args.dry_run:
        return 0
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(channels, indent=2))       # as server.save_channels writes it
    os.replace(temp, path)
    if args.reception:
        reception_path = Path(args.reception)
        reception = json.loads(reception_path.read_text())
        tsids = reception.setdefault("tsids", {})
        for rf, (tsid, _why) in report.items():
            if tsid is not None and rf in reception.get("rf_channels", []):
                tsids[str(rf)] = tsid
        reception["tsids"] = {key: tsids[key] for key in sorted(tsids, key=int)}
        temp = reception_path.with_suffix(".tmp")
        temp.write_text(json.dumps(reception, indent=2) + "\n")
        os.replace(temp, reception_path)
    return 0 if all(why in ("stamped", "no lock") for _tsid, why in report.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
