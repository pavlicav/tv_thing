#!/usr/bin/env python3
"""Programme guide for the YouTube TV channels, read from the web.

The antenna carries its own schedule (epg.py). YouTube TV's channels carry
nothing TV Thing can read, but their schedules are published, free:

* **Gracenote** supplies YouTube TV's own guide. Its public listings site,
  the one Zap2it used to front, serves two weeks for any station by its
  station id (``api/sslgrid``). A cable network sends one national feed, so
  its listing is the same whichever lineup it is read through; the locals
  are the Lansing stations the Roku's YouTube TV market carries.
* The **free streaming channels** YouTube TV lists alongside them (Stories by
  AMC, Pickleball TV...) are in no public Gracenote lineup. The Plex, Roku,
  Pluto and Samsung TV Plus apps publish their schedules, and i.mjh.nz
  mirrors those as XMLTV.

``yttv_listings.json`` says which one each roku.json row reads. A row it maps
to nothing (Local Now, World at War) has no guide anywhere.

Neither source is an official API, so this asks little of them: one refresh
a day, a second apart. The station endpoint only answers for its full
fourteen days -- about 300 KB a station, 25 MB a refresh -- and only the next
KEEP_HOURS are stored, which keeps the guide the pages download small.
Nothing here touches a tuner.
"""

import argparse
import gzip
import io
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import epg

BASE_DIR = Path(__file__).parent
GUIDE_FILE = BASE_DIR / "yttv_epg.json"
LISTINGS_FILE = BASE_DIR / "yttv_listings.json"
ROKU_FILE = BASE_DIR / "roku.json"

# Two days: enough to plan tonight and tomorrow, and to ride out a refresh
# that fails, without a guide download of several megabytes.
KEEP_HOURS = 48
GRACENOTE_HOURS = "336"     # The endpoint refuses any other span.
REQUEST_GAP_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 40
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/137.0 Safari/537.36")


class ListingsError(ValueError):
    """yttv_listings.json is missing or does not describe usable sources."""


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------

def load_listings(path=LISTINGS_FILE):
    """Read and check yttv_listings.json."""
    try:
        with open(path) as f:
            document = json.load(f)
    except (OSError, ValueError) as error:
        raise ListingsError(f"{os.path.basename(path)}: {error}") from None
    if not isinstance(document, dict) or not isinstance(document.get("channels"), dict):
        raise ListingsError("yttv_listings.json needs a channels object")
    for name, entry in document["channels"].items():
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise ListingsError(f"{name}: an entry is an object or null")
        station = entry.get("gracenote")
        if station is not None and (isinstance(station, bool) or not isinstance(station, int)):
            raise ListingsError(f"{name}: a Gracenote station id is a whole number")
        if station is None and not (entry.get("xmltv") and entry.get("channel")):
            raise ListingsError(f"{name}: needs a gracenote station, or an xmltv feed and channel")
    for key in ("url", "lineup_id", "headend_id", "postal_code"):
        if not (document.get("gracenote") or {}).get(key):
            raise ListingsError(f"gracenote.{key} is missing")
    if "{feed}" not in str((document.get("xmltv") or {}).get("url") or ""):
        raise ListingsError("xmltv.url needs a {feed} placeholder")
    return document


def roku_channel_names(path=ROKU_FILE):
    """The YouTube TV rows roku.json lists, by name, in guide order."""
    with open(path) as f:
        return [channel["name"] for channel in json.load(f).get("channels", [])]


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _whole(value):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _event(start, end, title, *, episode_title=None, description=None,
           season=None, episode=None, new=False, live=False, captions=False,
           rating=None):
    """One event in the stored shape: epg.py's, plus what the web adds.

    Empty fields are left out; a guide of a hundred channels is mostly
    titles and times.
    """
    event = {"start": int(start), "duration": int(end) - int(start),
             "title": (title or "").strip() or "No title", "captions": bool(captions)}
    extra = {"episode_title": (episode_title or "").strip() or None,
             "description": (description or "").strip() or None,
             "season": season, "episode": episode,
             "new": True if new else None, "live": True if live else None,
             "rating": rating or None}
    event.update({key: value for key, value in extra.items() if value is not None})
    return event


def parse_gracenote(document):
    """Events from one api/sslgrid answer: ``{date: [airing, ...]}``."""
    events = []
    if not isinstance(document, dict):
        return events
    for day in document.values():
        for item in day if isinstance(day, list) else []:
            if not isinstance(item, dict):
                continue
            start, end = _whole(item.get("startTime")), _whole(item.get("endTime"))
            if start is None or end is None or end <= start:
                continue
            program = item.get("program") or {}
            tags = {str(tag) for tag in (item.get("tag") or []) + (item.get("flag") or [])}
            events.append(_event(
                start, end, program.get("title"),
                episode_title=program.get("episodeTitle"),
                description=program.get("shortDesc"),
                season=_whole(program.get("season")),
                episode=_whole(program.get("episode")),
                new="New" in tags, live="Live" in tags, captions="CC" in tags,
                rating=item.get("rating")))
    return tidy(events)


def _xmltv_time(text):
    """XMLTV's ``20261019180000 +0000`` as a Unix time."""
    text = (text or "").strip()
    try:
        if " " in text:
            return int(datetime.strptime(text, "%Y%m%d%H%M%S %z").timestamp())
        return int(datetime.strptime(text[:14] + " +0000", "%Y%m%d%H%M%S %z").timestamp())
    except ValueError:
        return None


def _xmltv_episode(programme):
    """Season and episode from an xmltv_ns episode-num (zero-based)."""
    for number in programme.findall("episode-num"):
        if number.get("system") != "xmltv_ns":
            continue
        parts = (number.text or "").split(".")
        if len(parts) < 2:
            continue
        season = _whole(parts[0].split("/")[0].strip())
        episode = _whole(parts[1].split("/")[0].strip())
        return (season + 1 if season is not None else None,
                episode + 1 if episode is not None else None)
    return None, None


def parse_xmltv(data, wanted):
    """Events for the channels named in ``wanted``, from an XMLTV document.

    ``wanted`` holds display names; returns ``{display name: [event, ...]}``.
    The feeds list hundreds of channels, so only the wanted ones are kept.
    """
    ids, events = {}, {name: [] for name in wanted}
    for _, element in ET.iterparse(io.BytesIO(data), events=("end",)):
        if element.tag == "channel":
            for name in element.findall("display-name"):
                if name.text in events and element.get("id") not in ids:
                    ids[element.get("id")] = name.text
            element.clear()
        elif element.tag == "programme":
            name = ids.get(element.get("channel"))
            start, end = _xmltv_time(element.get("start")), _xmltv_time(element.get("stop"))
            if name and start is not None and end is not None and end > start:
                season, episode = _xmltv_episode(element)
                events[name].append(_event(
                    start, end, element.findtext("title"),
                    episode_title=element.findtext("sub-title"),
                    description=element.findtext("desc"),
                    season=season, episode=episode,
                    new=element.find("new") is not None,
                    live=element.find("live") is not None,
                    captions=element.find("subtitles") is not None))
            element.clear()
    return {name: tidy(found) for name, found in events.items()}


def tidy(events):
    """In start order, one per start time, none overlapping the one before."""
    kept = []
    for event in sorted(events, key=lambda e: (e["start"], -e["duration"])):
        if kept and event["start"] < kept[-1]["start"] + kept[-1]["duration"]:
            continue
        kept.append(event)
    return kept


def window(events, now, hours=KEEP_HOURS):
    """The events still running at ``now`` or starting in the next ``hours``."""
    until = now + hours * 3600
    return [e for e in events if e["start"] + e["duration"] > now and e["start"] < until]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _read(request):
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("the listing is too large")
    return data


def fetch_gracenote(settings, station, now):
    """Fourteen days of one Gracenote station, as parsed events."""
    body = json.dumps({
        "lineupId": settings["lineup_id"], "headendId": settings["headend_id"],
        "countryCode": "USA", "postalCode": settings["postal_code"], "device": "X",
        "prgsvcid": str(station), "timespan": GRACENOTE_HOURS, "timestamp": str(int(now)),
        "IsSSLinkNavigation": "true", "userId": "-", "aid": "orbebb", "languagecode": "en-us",
    }).encode()
    request = urllib.request.Request(settings["url"], data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": USER_AGENT})
    return parse_gracenote(json.loads(_read(request)))


def fetch_xmltv(settings, feed):
    """One app's XMLTV feed, uncompressed."""
    request = urllib.request.Request(settings["url"].format(feed=feed),
                                     headers={"User-Agent": USER_AGENT})
    data = _read(request)
    if data[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
            data = f.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ValueError("the feed is too large")
    return data


def refresh(listings, names, now=None, *, gracenote=fetch_gracenote, xmltv=fetch_xmltv,
            sleep=time.sleep, progress=None, hours=KEEP_HOURS):
    """Read every row in ``names`` from its source.

    Returns a guide in epg.py's shape, keyed by row name. A row whose source
    fails is left out and named in ``errors``; merge_guides keeps its last
    listing meanwhile.
    """
    started = time.monotonic()
    now = int(time.time()) if now is None else int(now)
    report = progress or (lambda message: None)
    sources = listings["channels"]
    channels, errors = {}, {}
    unmapped = [name for name in names if not sources.get(name)]

    stations = [(name, sources[name]) for name in names
                if sources.get(name) and sources[name].get("gracenote") is not None]
    for index, (name, entry) in enumerate(stations):
        if index:
            sleep(REQUEST_GAP_SECONDS)
        try:
            events = gracenote(listings["gracenote"], entry["gracenote"], now)
        except Exception as error:   # noqa: BLE001 - one station never sinks the rest
            errors[name] = f"Gracenote {entry['gracenote']}: {error}"
            report(f"{name}: {errors[name]}")
            continue
        channels[name] = {"name": name, "source": "gracenote",
                          "station": entry["gracenote"],
                          **({"call_sign": entry["call_sign"]} if entry.get("call_sign") else {}),
                          "events": window(events, now, hours)}
        report(f"{name}: {len(channels[name]['events'])} programmes")

    feeds = {}
    for name in names:
        entry = sources.get(name)
        if entry and entry.get("gracenote") is None:
            feeds.setdefault(entry["xmltv"], []).append((name, entry["channel"]))
    for feed, rows in feeds.items():
        try:
            found = parse_xmltv(xmltv(listings["xmltv"], feed), {channel for _, channel in rows})
        except Exception as error:   # noqa: BLE001 - one feed never sinks the rest
            for name, _ in rows:
                errors[name] = f"{feed}: {error}"
            report(f"{feed}: {error}")
            continue
        for name, channel in rows:
            if not found.get(channel):
                errors[name] = f"{feed} lists nothing for {channel!r}"
                report(f"{name}: {errors[name]}")
                continue
            channels[name] = {"name": name, "source": "xmltv", "feed": feed,
                              "feed_channel": channel,
                              "events": window(found[channel], now, hours)}
            report(f"{name}: {len(channels[name]['events'])} programmes")

    guide = {"generated_at": now, "channels": channels, "errors": errors,
             "unmapped": unmapped, "requested": len(names) - len(unmapped),
             "elapsed_seconds": round(time.monotonic() - started, 1)}
    guide.update(epg.summarize(guide))
    return guide


def merge_guides(stored, fresh, now=None):
    """Keep a row's last listing, flagged stale, while its source is failing."""
    merged = epg.merge_guides(stored, fresh, now)
    merged.update(epg.summarize(merged))
    return merged


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_cache = {"signature": None, "guide": None}


def load_guide(path=None):
    """The stored guide, or None. Parsed once per change of the file."""
    path = Path(path or GUIDE_FILE)
    try:
        stat = path.stat()
    except OSError:
        return None
    # save_guide swaps in a new file, so the inode tells two saves apart
    # even within one tick of the clock.
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
    """Atomically replace the stored guide."""
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
    parser.add_argument("--show", metavar="NAME",
                        help="print one row's stored schedule instead of refreshing")
    parser.add_argument("--only", metavar="NAME", action="append",
                        help="refresh just this row (repeatable); prints, does not save")
    args = parser.parse_args(argv)

    if args.show:
        channel = ((load_guide() or {}).get("channels") or {}).get(args.show)
        if not channel:
            print(f"No stored listing for {args.show!r}")
            return 1
        for event in channel["events"]:
            print(time.strftime("%a %H:%M", time.localtime(event["start"])),
                  event["title"], f"-- {event['episode_title']}" if event.get("episode_title") else "")
        return 0

    listings = load_listings()
    names = args.only or roku_channel_names()
    fresh = refresh(listings, names, progress=print)
    if args.only:
        return 0 if not fresh["errors"] else 1
    guide = merge_guides(load_guide(), fresh)
    save_guide(guide)
    print(f"{guide['event_count']} programmes on {guide['channels_with_events']} channels, "
          f"{guide['coverage_hours']} h ahead, in {fresh['elapsed_seconds']}s; "
          f"{len(fresh['errors'])} failed; no listings for {', '.join(fresh['unmapped']) or 'none'}")
    return 0 if len(fresh["errors"]) < fresh["requested"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
