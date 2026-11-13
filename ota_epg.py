#!/usr/bin/env python3
"""The antenna's programme guide from the web, for what the broadcast does not carry.

The broadcast's own guide (epg.py) is read off the air at 03:00 and 15:00, and A/65 only
promises twelve hours of it: each read runs dry about two hours before the next one, and
longer when a lock defers the read. Some channels' PSIP carries no events at all (DC's 49.x,
47.1, 31.x on 2026-11-09). So from about 13:00 to 15:00, and 01:00 to 03:00, the antenna's
rows were empty.

The listings are published, free: Gracenote's over-the-air grid (the listings site Zap2it
used to front, ``api/grid`` with lineup ``USA-OTA<ZIP>``) lists every station receivable
around a ZIP code by channel number, six hours per request. ``ota_listings.json`` names each
market's ZIP code; this reads the next KEEP_HOURS of this box's market, a second apart, and
matches them to channels.json by number. Nothing here touches a tuner.

``combine`` is what the guide's readers use: the broadcast's events wherever they reach,
each channel continued from the web from where its own last event ends. The broadcast wins
because it is what the station is sending now; the web is a day's plan.
"""

import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import epg
import lineup
import yttv_epg

BASE_DIR = Path(__file__).parent
GUIDE_FILE = BASE_DIR / "ota_epg.json"
LISTINGS_FILE = BASE_DIR / "ota_listings.json"

KEEP_HOURS = 48
SPAN_HOURS = 6              # The grid refuses any longer span (400).
REQUEST_GAP_SECONDS = 1.0
CALL_LETTERS = re.compile(r"[KW][A-Z]{2,3}")


class ListingsError(ValueError):
    """ota_listings.json is missing or malformed."""


def load_listings(path=LISTINGS_FILE):
    try:
        with open(path) as f:
            listings = json.load(f)
    except (OSError, ValueError) as error:
        raise ListingsError(f"{Path(path).name}: {error}") from error
    if not isinstance(listings.get("gracenote"), dict) or not listings["gracenote"].get("url") \
            or not isinstance(listings.get("markets"), dict):
        raise ListingsError(f"{Path(path).name} needs gracenote.url and markets")
    return listings


def market_settings(listings, market):
    """``{"url", "lineup_id", "postal_code"}`` for one market, or None when it has no ZIP code."""
    entry = listings["markets"].get(market) or {}
    postal = str(entry.get("postal_code") or "").strip()
    if not postal:
        return None
    return {"url": listings["gracenote"]["url"], "postal_code": postal,
            "lineup_id": entry.get("lineup_id") or f"USA-OTA{postal}"}


def _time(text):
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parse_grid(document):
    """``{channel number: {station id: {"call_sign", "events"}}}`` from one api/grid answer."""
    found = {}
    channels = document.get("channels") if isinstance(document, dict) else None
    for channel in channels if isinstance(channels, list) else []:
        if not isinstance(channel, dict):
            continue
        number = str(channel.get("channelNo") or "").strip()
        if not number:
            continue
        events = []
        for item in channel.get("events") or []:
            if not isinstance(item, dict):
                continue
            start, end = _time(item.get("startTime")), _time(item.get("endTime"))
            if start is None or end is None or end <= start:
                continue
            program = item.get("program") or {}
            tags = {str(tag) for tag in (item.get("tags") or []) + (item.get("flag") or [])}
            events.append(yttv_epg._event(
                start, end, program.get("title"),
                episode_title=program.get("episodeTitle"),
                description=program.get("shortDesc"),
                season=yttv_epg._whole(program.get("season")),
                episode=yttv_epg._whole(program.get("episode")),
                new="New" in tags, live="Live" in tags, captions="CC" in tags,
                rating=item.get("rating")))
        station = str(channel.get("channelId") or channel.get("callSign") or number)
        entry = found.setdefault(number, {}).setdefault(
            station, {"call_sign": channel.get("callSign"), "events": []})
        entry["events"] += events
    return found


def _letters(name):
    match = CALL_LETTERS.match(str(name or "").upper())
    return match.group(0) if match else None


def match(found, channels):
    """``{number: (station id, listing)}``: each antenna channel's listing in ``found``.

    A number listed once is that station. A number listed twice (two stations' subchannels
    share it; DC's 44.1 is WRC's and WZDC's) is the one whose call sign begins with the call
    letters in the channel's name, or in its major channel's ``.1`` name (44.2 "XITOS" takes
    44.1 "WZDC"'s). Otherwise it is left unlisted rather than guessed.
    """
    names = {str(channel.get("number")): channel.get("name") for channel in channels}
    matched = {}
    for channel in channels:
        number = str(channel.get("number"))
        candidates = found.get(number) or {}
        if len(candidates) == 1:
            matched[number] = next(iter(candidates.items()))
            continue
        for letters in (_letters(channel.get("name")), _letters(names.get(number.split(".")[0] + ".1"))):
            if not letters:
                continue
            fits = [(station, listing) for station, listing in candidates.items()
                    if str(listing.get("call_sign") or "").upper().startswith(letters)]
            if len(fits) == 1:
                matched[number] = fits[0]
                break
    return matched


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_grid(settings, at):
    """SPAN_HOURS of every station in the market's lineup from ``at``, parsed."""
    query = urllib.parse.urlencode({
        "lineupId": settings["lineup_id"], "timespan": str(SPAN_HOURS), "headendId": "lineupId",
        "country": "USA", "timezone": "", "device": "-", "postalCode": settings["postal_code"],
        "isOverride": "true", "time": str(int(at)), "pref": "16,128", "userId": "-",
        "aid": "orbebb", "languagecode": "en-us"})
    request = urllib.request.Request(f"{settings['url']}?{query}", headers={
        "Accept": "application/json", "User-Agent": yttv_epg.USER_AGENT})
    return parse_grid(json.loads(yttv_epg._read(request)))


def refresh(settings, channels, now=None, *, fetch=fetch_grid, sleep=time.sleep, hours=KEEP_HOURS,
            progress=None):
    """Read the next ``hours`` of the market's grid and match it to ``channels``.

    Returns a guide in epg.py's shape keyed by antenna number; raises when no span answered.
    A span that fails leaves a gap the next read fills, and is counted in ``failed_spans``.
    """
    started = time.monotonic()
    now = int(time.time()) if now is None else int(now)
    report = progress or (lambda message: None)
    first = now - now % 3600
    spans = list(range(first, now + hours * 3600, SPAN_HOURS * 3600))
    found, errors = {}, []
    for index, at in enumerate(spans):
        if index:
            sleep(REQUEST_GAP_SECONDS)
        try:
            grid = fetch(settings, at)
        except Exception as error:   # noqa: BLE001 - one span never sinks the rest
            errors.append(f"{time.strftime('%a %H:%M', time.localtime(at))}: {error}")
            report(f"span {index + 1}/{len(spans)}: {error}")
            continue
        for number, stations in grid.items():
            for station, listing in stations.items():
                entry = found.setdefault(number, {}).setdefault(
                    station, {"call_sign": listing["call_sign"], "events": []})
                entry["events"] += listing["events"]
        report(f"span {index + 1}/{len(spans)}: {len(grid)} channels")
    if spans and len(errors) == len(spans):
        raise RuntimeError(f"no span answered: {errors[0]}")
    listed = {}
    for number, (station, listing) in match(found, channels).items():
        events = yttv_epg.window(yttv_epg.tidy(listing["events"]), now, hours)
        listed[number] = {"number": number, "source": "gracenote", "station": station,
                          "call_sign": listing["call_sign"], "events": events}
    guide = {"generated_at": now, "lineup_id": settings["lineup_id"], "channels": listed,
             "unlisted": sorted({str(c.get("number")) for c in channels} - set(listed),
                                key=_number_key),
             "failed_spans": errors, "elapsed_seconds": round(time.monotonic() - started, 1)}
    guide.update(epg.summarize(guide))
    return guide


def _number_key(number):
    return [int(part) if part.isdigit() else 0 for part in str(number).split(".")]


def merge_guides(stored, fresh, now=None):
    """Keep a channel's last listing, flagged stale, while a read misses it."""
    if stored and stored.get("lineup_id") != fresh.get("lineup_id"):
        return fresh            # another market's listings say nothing about this one
    return yttv_epg.merge_guides(stored, fresh, now)


# ---------------------------------------------------------------------------
# The guide its readers see
# ---------------------------------------------------------------------------

def combine(broadcast, web):
    """The broadcast's guide with each channel continued from the web where its events end.

    Neither argument is changed. The result keeps the broadcast guide's fields (its read
    time, what it covered when read) with the counts and horizon of the whole, and ``web``:
    the web listing's read time and coverage. A channel the web continued says so in
    ``web_from`` (where its listing takes over), and is no longer stale: what it shows
    reaches ahead either way. None when there is neither.
    """
    if not broadcast and not web:
        return None
    own_channels = (broadcast or {}).get("channels") or {}
    web_channels = (web or {}).get("channels") or {}
    channels = {}
    for number in list(own_channels) + [n for n in web_channels if n not in own_channels]:
        own, listed = own_channels.get(number), web_channels.get(number)
        own_events = (own or {}).get("events") or []
        if not listed or not listed.get("events"):
            channels[number] = own
            continue
        reach = max((event["start"] + event["duration"] for event in own_events), default=None)
        fill = [event for event in listed["events"] if reach is None or event["start"] >= reach]
        channel = dict(own) if own else {"number": number}
        channel["events"] = own_events + fill
        if fill:
            channel["web_from"] = fill[0]["start"]
            channel["listing"] = {key: listed[key] for key in ("source", "station", "call_sign")
                                  if listed.get(key) is not None}
            channel.pop("stale", None)
        channels[number] = channel
    combined = {**(broadcast or {"generated_at": web.get("generated_at")}), "channels": channels}
    whole = epg.summarize({"generated_at": combined.get("generated_at"), "channels": channels})
    combined.update({key: whole[key] for key in ("event_count", "channels_with_events")})
    combined["horizon"] = max((broadcast or {}).get("horizon") or 0, whole["horizon"] or 0) or None
    if broadcast is None:
        combined["coverage_hours"] = whole["coverage_hours"]
    combined["web"] = None if not web else {
        key: web.get(key) for key in ("generated_at", "lineup_id", "horizon", "channels_with_events",
                                      "unlisted", "failed_spans")}
    return combined


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_cache = {"signature": None, "guide": None}


def load_guide(path=None):
    """The stored web listing, or None. Parsed once per change of the file."""
    path = Path(path or GUIDE_FILE)
    try:
        stat = path.stat()
    except OSError:
        return None
    signature = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
    if _cache["signature"] != signature:
        try:
            with open(path) as f:
                guide = json.load(f)
        except (OSError, ValueError):
            guide = None
        _cache.update(signature=signature, guide=guide)
    return _cache["guide"]


def save_guide(guide, path=None):
    """Atomically replace the stored web listing."""
    path = Path(path or GUIDE_FILE)
    temp = path.with_suffix(".tmp")
    with open(temp, "w") as f:
        json.dump(guide, f, separators=(",", ":"))
    os.replace(temp, path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--market", default="dc", help="the market in ota_listings.json (default dc)")
    parser.add_argument("--hours", type=int, default=KEEP_HOURS)
    parser.add_argument("--show", help="print one channel's stored listing and stop")
    args = parser.parse_args(argv)
    if args.show:
        channel = ((load_guide() or {}).get("channels") or {}).get(args.show)
        for event in (channel or {}).get("events", []):
            print(time.strftime("%a %H:%M", time.localtime(event["start"])), event["title"])
        return 0 if channel else 1
    settings = market_settings(load_listings(), args.market)
    if settings is None:
        parser.error(f"ota_listings.json has no ZIP code for {args.market}")
    with open(lineup.local_or_default(epg.CHANNELS_FILE)) as f:
        channels = json.load(f)
    channels = channels.get("channels", channels) if isinstance(channels, dict) else channels
    fresh = refresh(settings, channels, hours=args.hours, progress=print)
    guide = merge_guides(load_guide(), fresh)
    save_guide(guide)
    print(f"{guide['event_count']} programmes on {guide['channels_with_events']} channels, "
          f"{guide['coverage_hours']} h ahead, in {fresh['elapsed_seconds']} s; "
          f"unlisted: {', '.join(fresh['unlisted']) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
