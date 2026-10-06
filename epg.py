#!/usr/bin/env python3
"""ATSC PSIP electronic programme guide.

Every ATSC broadcast carries its own schedule. The Event Information Table
(EIT, table 0xCB) is transmitted in three-hour blocks: EIT-0 covers the block
containing the current time, EIT-1 the next three hours, and so on. A/65
requires stations to send EIT-0 through EIT-3, so **twelve hours of guide is
guaranteed**; a station may send up to EIT-127 (sixteen days) and some send a
few blocks past the minimum, but twelve hours is all that can be relied on.
That number decides the refresh policy -- see REFRESH_TIMES in server.py.

The repetition rates matter as much as the coverage. A/65 asks for EIT-0..3
to repeat at least every 500 ms, but everything beyond that -- EIT-4 and up,
and every ETT -- only every 60 s. Collecting the mandatory twelve hours off a
multiplex therefore takes a couple of seconds, while chasing the long tail or
the ETT programme descriptions takes a minute per frequency. The default is to
take the fast tables and stop; ``descriptions=True`` opts into the slow ones.

Which PID each EIT-k lives on is not fixed: the Master Guide Table (MGT,
0xC7 on the PSIP base PID) lists them. Events are keyed by source_id, which
comes from the same TVCT the channel scan already reads.

Reading any of this needs the digital front-end exclusively, so a refresh
interrupts the digital lane exactly like a band scan does.
"""

import argparse
import errno
import json
import os
import select
import struct
import time
from pathlib import Path

import atsc_scan

BASE_DIR = Path(__file__).parent
GUIDE_FILE = BASE_DIR / "epg.json"
CHANNELS_FILE = BASE_DIR / "channels.json"

# PSIP tables. The MGT, TVCT and STT share the base PID; each EIT and ETT
# gets its own PID, announced by the MGT.
PSIP_PID = 0x1FFB
MGT_TABLE_ID = 0xC7
TVCT_TABLE_ID = 0xC8
EIT_TABLE_ID = 0xCB
ETT_TABLE_ID = 0xCC
STT_TABLE_ID = 0xCD

# MGT table_type ranges (A/65 table 6.3).
MGT_EIT_BASE = 0x0100   # 0x0100..0x017F -> EIT-0..EIT-127
MGT_ETT_BASE = 0x0200   # 0x0200..0x027F -> ETT-0..ETT-127

# PSIP timestamps count seconds from the GPS epoch and, unlike Unix time,
# do not skip leap seconds. The STT carries the current offset; this is the
# fallback for a multiplex whose STT does not arrive (18 s since 2017-01-01).
GPS_EPOCH_UNIX = 315_964_800
DEFAULT_GPS_UTC_OFFSET = 18

EIT_BLOCK_SECONDS = 3 * 3600
GUARANTEED_BLOCKS = 4          # EIT-0..EIT-3, the part A/65 makes mandatory

# The mandatory blocks repeat about twice a second, so a few seconds is a
# generous budget for them. Everything past EIT-3 need only repeat once a
# minute, and measured locally the DC stations that carry them (WETA
# advertises twelve blocks, WRC and ION eight) do use most of that. Giving
# the slow blocks the fast budget does not read them faster -- it just misses
# them, at four wasted seconds each, which made --blocks look broken.
SLOW_BLOCK_TIMEOUT = 65.0

# A section can be 4096 bytes, and the default demux ring holds barely two of
# them -- far too small to collect a whole EIT without overflowing.
SECTION_READ_BYTES = 4096
SECTION_BUFFER_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def decode_multiple_string(data):
    """Decode an A/65 multiple string structure.

    Returns ``(text, compressed_segments)``. Titles and descriptions may be
    Huffman-coded against the two tables in A/65 Annex C; those tables are not
    implemented here, so such segments decode to nothing and are counted
    instead of being silently dropped -- a station that uses them shows up in
    the refresh summary rather than as a guide full of blank titles.
    """
    if not data:
        return "", 0

    strings = []
    compressed = 0
    pos = 1
    for _ in range(data[0]):
        if pos + 4 > len(data):
            break
        segment_count = data[pos + 3]
        pos += 4                       # ISO 639 language code, then the count
        parts = []
        for _ in range(segment_count):
            if pos + 3 > len(data):
                break
            compression, mode, length = data[pos], data[pos + 1], data[pos + 2]
            chunk = data[pos + 3:pos + 3 + length]
            pos += 3 + length
            if compression != 0x00:
                compressed += 1
                continue
            parts.append(_decode_segment(mode, chunk))
        strings.append("".join(parts))

    # Only the first string is used: the rest are other languages, and this
    # is a single-locale guide.
    return (strings[0].strip() if strings else ""), compressed


def _decode_segment(mode, chunk):
    """Decode one uncompressed segment.

    The mode byte is the high byte of every UTF-16 code point in the segment,
    so mode 0x00 is Latin-1 and the rest are other Unicode pages. Mode 0x3F is
    the exception: the segment is already UTF-16BE.
    """
    if mode == 0x3F:
        try:
            return chunk.decode("utf-16-be")
        except (UnicodeDecodeError, ValueError):
            return ""
    if mode <= 0x3E:
        return "".join(chr((mode << 8) | byte) for byte in chunk)
    return ""


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _section_key(data):
    """Identity of a section within its table: extension, version, number."""
    return (
        struct.unpack_from(">H", data, 3)[0],
        (data[5] >> 1) & 0x1F,
        data[6],
    )


def parse_stt_section(data):
    """Read the System Time Table (0xCD): the GPS-to-UTC offset in force."""
    if not data or len(data) < 16 or data[0] != STT_TABLE_ID:
        return None
    return {
        "system_time": struct.unpack_from(">I", data, 9)[0],
        "gps_utc_offset": data[13],
    }


def parse_mgt_section(data):
    """Read the Master Guide Table (0xC7): which PID carries which sub-table.

    Returns ``{"eit": {block: pid}, "ett": {block: pid}}``.
    """
    if not data or len(data) < 11 or data[0] != MGT_TABLE_ID:
        return {"eit": {}, "ett": {}}

    tables = {"eit": {}, "ett": {}}
    count = struct.unpack_from(">H", data, 9)[0]
    pos = 11
    for _ in range(count):
        if pos + 11 > len(data):
            break
        table_type = struct.unpack_from(">H", data, pos)[0]
        pid = struct.unpack_from(">H", data, pos + 2)[0] & 0x1FFF
        desc_len = struct.unpack_from(">H", data, pos + 9)[0] & 0x0FFF
        pos += 11 + desc_len
        if MGT_EIT_BASE <= table_type < MGT_EIT_BASE + 128:
            tables["eit"][table_type - MGT_EIT_BASE] = pid
        elif MGT_ETT_BASE <= table_type < MGT_ETT_BASE + 128:
            tables["ett"][table_type - MGT_ETT_BASE] = pid
    return tables


def parse_eit_section(data):
    """Read one Event Information Table section (0xCB).

    Returns ``{"source_id", "events", "compressed"}``, or None if the section
    is not an EIT. Times stay in GPS seconds here; the caller converts once it
    knows the offset from the STT.
    """
    if not data or len(data) < 14 or data[0] != EIT_TABLE_ID:
        return None

    source_id = struct.unpack_from(">H", data, 3)[0]
    events = []
    compressed = 0
    pos = 10
    for _ in range(data[9]):
        # event_id(2) start_time(4) ETM_location+length(3) title_length(1),
        # then the title, then a 2-byte descriptor length.
        if pos + 12 > len(data):
            break
        event_id = struct.unpack_from(">H", data, pos)[0] & 0x3FFF
        start_gps = struct.unpack_from(">I", data, pos + 2)[0]
        duration = (
            (data[pos + 6] << 16) | (data[pos + 7] << 8) | data[pos + 8]
        ) & 0x0FFFFF
        title_length = data[pos + 9]
        title_raw = data[pos + 10:pos + 10 + title_length]
        pos += 10 + title_length
        if pos + 2 > len(data):
            break
        desc_len = struct.unpack_from(">H", data, pos)[0] & 0x0FFF
        descriptors = data[pos + 2:pos + 2 + desc_len]
        pos += 2 + desc_len

        title, title_compressed = decode_multiple_string(title_raw)
        compressed += title_compressed
        events.append({
            "event_id": event_id,
            "start_gps": start_gps,
            "duration": duration,
            "title": title,
            "captions": _has_caption_service(descriptors),
        })

    return {"source_id": source_id, "events": events, "compressed": compressed}


def _has_caption_service(descriptors):
    """True if the event carries a caption_service_descriptor (tag 0x86)."""
    pos = 0
    while pos + 2 <= len(descriptors):
        tag, length = descriptors[pos], descriptors[pos + 1]
        if tag == 0x86:
            return True
        pos += 2 + length
    return False


def parse_ett_section(data):
    """Read one Extended Text Table section (0xCC): a programme description.

    The ETM_id ties the text back to an event: source_id in the high 16 bits,
    event_id in the next 14, and the low two bits set to 0b10. A channel-level
    ETM has those bits clear and is not a programme description.
    """
    if not data or len(data) < 14 or data[0] != ETT_TABLE_ID:
        return None
    etm_id = struct.unpack_from(">I", data, 9)[0]
    if etm_id & 0x03 != 0x02:
        return None
    text, compressed = decode_multiple_string(data[13:-4])
    return {
        "source_id": (etm_id >> 16) & 0xFFFF,
        "event_id": (etm_id >> 2) & 0x3FFF,
        "text": text,
        "compressed": compressed,
    }


def gps_to_unix(start_gps, gps_utc_offset=DEFAULT_GPS_UTC_OFFSET):
    """Convert a PSIP GPS timestamp to Unix time."""
    return start_gps + GPS_EPOCH_UNIX - gps_utc_offset


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def _complete_extensions(sections):
    """Which table_id_extensions have every section of some version present."""
    by_table = {}
    for (extension, version, number), raw in sections.items():
        by_table.setdefault((extension, version), {})[number] = raw[7]
    done = set()
    for (extension, _version), numbers in by_table.items():
        last = max(numbers.values())
        if set(numbers) == set(range(last + 1)):
            done.add(extension)
    return done


def collect_sections(adapter, pid, table_id, timeout, expected=None,
                     until=None, stop=None):
    """Gather every distinct section of one table from a PID.

    One EIT PID carries the schedule for every service on the multiplex, each
    as a separate table keyed by source_id, and each of those may span several
    sections. A single read gets one section, so this drains the filter until
    every source in ``expected`` has a complete set -- or until the budget
    runs out, which is what a station that simply does not send an EIT for one
    of its services costs.

    ``until`` is the same idea for tables that carry no completeness marker:
    it is handed each newly seen section and returns True when the caller has
    everything it came for. The ETTs need it, because a description table is
    one self-contained section per event and nothing in the stream says how
    many to expect.
    """
    sections = {}
    fd = atsc_scan.open_section_filter(
        adapter, pid, table_id, buffer_bytes=SECTION_BUFFER_BYTES
    )
    try:
        deadline = time.monotonic() + timeout
        while True:
            if stop is not None and stop.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            try:
                raw = os.read(fd, SECTION_READ_BYTES)
            except OSError as exc:
                if exc.errno == errno.EOVERFLOW:
                    # The ring filled faster than this loop drained it. The
                    # kernel has already reset it, and every table repeats,
                    # so the lost sections come round again.
                    continue
                if exc.errno in (errno.EAGAIN, errno.ETIMEDOUT):
                    continue
                raise
            if len(raw) < 9:
                continue
            # Sections repeat forever; only the first copy of each is new.
            key = _section_key(raw)
            if key in sections:
                continue
            sections[key] = raw
            if expected and expected <= _complete_extensions(sections):
                break
            if until is not None and until(raw):
                break
    finally:
        atsc_scan.close_section_filter(fd)
    return list(sections.values())


def collect_frequency(fd, frequency_hz, adapter=0, blocks=GUARANTEED_BLOCKS,
                      block_timeout=4.0, slow_block_timeout=SLOW_BLOCK_TIMEOUT,
                      lock_timeout=2.0, settle=0.15,
                      descriptions=False, description_timeout=70.0,
                      stop=None):
    """Tune one frequency and read the guide for every service on it.

    Returns ``{"services": [...], "gps_utc_offset": int|None, ...}``. Each
    service carries its virtual channel number, so the caller does not need
    channels.json to be current.
    """
    atsc_scan.tune(fd, frequency_hz)
    time.sleep(settle)
    sample = atsc_scan.wait_for_lock(fd, timeout=lock_timeout, stop=stop)
    result = {
        "frequency": frequency_hz,
        "locked": sample["locked"],
        "snr_db": sample["snr_db"],
        "services": [],
        "eit_blocks": [],
        "compressed": 0,
        "gps_utc_offset": None,
    }
    if not sample["locked"]:
        result["error"] = "no lock"
        return result

    # The three base tables sit on one PID and repeat within about half a
    # second, so one batched read gets the channel list, the sub-table PIDs
    # and the leap-second offset together.
    base = atsc_scan.read_sections(adapter, [
        ("mgt", PSIP_PID, MGT_TABLE_ID),
        ("vct", PSIP_PID, TVCT_TABLE_ID),
        ("stt", PSIP_PID, STT_TABLE_ID),
    ], timeout=4.0)

    services = atsc_scan.parse_tvct_section(base["vct"]) if base["vct"] else []
    if not services:
        result["error"] = "no VCT"
        return result

    stt = parse_stt_section(base["stt"])
    offset = stt["gps_utc_offset"] if stt else DEFAULT_GPS_UTC_OFFSET
    result["gps_utc_offset"] = offset

    by_source = {}
    for service in services:
        by_source[service["source_id"]] = {
            "number": service["number"],
            "name": service["name"],
            "source_id": service["source_id"],
            "service_id": service["service_id"],
            "frequency": frequency_hz,
            "events": {},
        }
    expected = set(by_source)

    tables = parse_mgt_section(base["mgt"])
    # Take the low-numbered blocks first: they are both the ones that repeat
    # quickly and the ones covering the next few hours.
    wanted = sorted(tables["eit"])[:blocks]
    result["eit_blocks"] = wanted
    result["advertised_blocks"] = sorted(tables["eit"])

    compressed = 0
    for block in wanted:
        if stop is not None and stop.is_set():
            break
        raw_sections = collect_sections(
            adapter, tables["eit"][block], EIT_TABLE_ID,
            timeout=(block_timeout if block < GUARANTEED_BLOCKS
                     else slow_block_timeout),
            expected=expected, stop=stop,
        )
        for raw in raw_sections:
            parsed = parse_eit_section(raw)
            if not parsed:
                continue
            service = by_source.get(parsed["source_id"])
            if service is None:
                continue
            compressed += parsed["compressed"]
            for event in parsed["events"]:
                event["start"] = gps_to_unix(event.pop("start_gps"), offset)
                event["block"] = block
                # Long events repeat in every block they overlap; the first
                # copy already carries the full duration.
                service["events"].setdefault(event["event_id"], event)

    if descriptions and tables["ett"]:
        compressed += _collect_descriptions(
            adapter, tables, wanted, by_source, description_timeout, stop
        )

    result["compressed"] = compressed
    result["services"] = [
        {**service, "events": sorted(service.pop("events").values(),
                                     key=lambda item: item["start"])}
        for service in by_source.values()
    ]
    return result


def _collect_descriptions(adapter, tables, blocks, by_source, timeout, stop):
    """Attach ETT programme descriptions to already-collected events.

    Kept separate because it is the expensive half by a wide margin. ETTs need
    only repeat once a minute, and measured against a real multiplex one block
    yields a section roughly every three seconds -- so a frequency costs
    minutes here against the second or so its EITs cost. Knowing which events
    are still missing text is what makes it finish at all: the table has no
    completeness marker, so without that it can only run out the clock.
    """
    compressed = 0
    for block in blocks:
        pid = tables["ett"].get(block)
        if pid is None or (stop is not None and stop.is_set()):
            continue
        pending = {
            (service["source_id"], event_id)
            for service in by_source.values()
            for event_id, event in service["events"].items()
            if event.get("block") == block
        }
        if not pending:
            continue

        def attach(raw, pending=pending):
            nonlocal compressed
            parsed = parse_ett_section(raw)
            if not parsed:
                return False
            compressed += parsed["compressed"]
            service = by_source.get(parsed["source_id"])
            if service is not None:
                event = service["events"].get(parsed["event_id"])
                if event is not None and parsed["text"]:
                    event["description"] = parsed["text"]
            pending.discard((parsed["source_id"], parsed["event_id"]))
            return not pending

        collect_sections(adapter, pid, ETT_TABLE_ID, timeout=timeout,
                         until=attach, stop=stop)
    return compressed


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def frequencies_from_channels(channels):
    """Unique frequencies to visit, in RF channel order."""
    seen = {}
    for channel in channels:
        seen.setdefault(channel["frequency"], channel.get("rf_channel"))
    return [{"frequency": frequency, "rf_channel": rf}
            for frequency, rf in sorted(seen.items())]


def refresh(channels=None, adapter=0, blocks=GUARANTEED_BLOCKS,
            block_timeout=4.0, slow_block_timeout=SLOW_BLOCK_TIMEOUT,
            lock_timeout=2.0, descriptions=False,
            stop=None, progress=None):
    """Sweep every known frequency and build a complete guide.

    ``progress`` is called with a dict per step so the web UI can stream the
    sweep; ``stop`` is checked between frequencies so a tune request can cut
    a scheduled refresh short instead of waiting it out.
    """
    if channels is None:
        channels = load_channels()
    targets = frequencies_from_channels(channels)
    # Join on (frequency, service_id), not on the virtual channel number.
    # The number is the field that goes stale: a channel list built before
    # the PSIP numbers were read carries sequential fallbacks, so matching on
    # it pairs one station's schedule with another station's tuning data.
    # Frequency and service_id both come from the same live VCT read.
    known = {(item["frequency"], item["service_id"]): item
             for item in channels}

    def report(**payload):
        if progress is not None:
            progress(payload)

    started = time.monotonic()
    guide = {
        "generated_at": int(time.time()),
        "gps_utc_offset": DEFAULT_GPS_UTC_OFFSET,
        "blocks_requested": blocks,
        "descriptions": bool(descriptions),
        "channels": {},
        "frequencies": [],
        "compressed_strings": 0,
    }
    if not targets:
        guide["error"] = "No channels known. Run a band scan first."
        return guide

    report(event="status", total=len(targets),
           message=f"Reading the guide from {len(targets)} frequencies...")

    fd = atsc_scan.open_frontend(adapter, writable=True)
    try:
        for index, target in enumerate(targets, 1):
            if stop is not None and stop.is_set():
                guide["aborted"] = True
                break
            report(event="frequency", index=index, total=len(targets),
                   frequency=target["frequency"], rf_channel=target["rf_channel"])
            try:
                outcome = collect_frequency(
                    fd, target["frequency"], adapter=adapter, blocks=blocks,
                    block_timeout=block_timeout,
                    slow_block_timeout=slow_block_timeout,
                    lock_timeout=lock_timeout,
                    descriptions=descriptions, stop=stop,
                )
            except OSError as failure:
                # One frequency that will not read -- a demux hiccup, a tune
                # that errors -- must not cost the eleven that would have.
                outcome = {
                    "frequency": target["frequency"], "locked": False,
                    "snr_db": None, "services": [], "eit_blocks": [],
                    "compressed": 0, "gps_utc_offset": None,
                    "error": str(failure),
                }
            outcome["rf_channel"] = target["rf_channel"]
            guide["compressed_strings"] += outcome["compressed"]
            if outcome.get("gps_utc_offset"):
                guide["gps_utc_offset"] = outcome["gps_utc_offset"]

            for service in outcome["services"]:
                match = known.get(
                    (service["frequency"], service["service_id"])
                ) or {}
                guide["channels"][service["number"]] = {
                    "number": service["number"],
                    # Both read from the air a moment ago, so the broadcast
                    # name wins over whatever the channel list remembers.
                    "name": service["name"] or match.get("name") or "",
                    "id": match.get("id"),
                    "source_id": service["source_id"],
                    "service_id": service["service_id"],
                    "frequency": service["frequency"],
                    "rf_channel": target["rf_channel"],
                    "events": service["events"],
                }

            guide["frequencies"].append({
                "frequency": outcome["frequency"],
                "rf_channel": target["rf_channel"],
                "locked": outcome["locked"],
                "snr_db": outcome["snr_db"],
                "eit_blocks": outcome["eit_blocks"],
                "advertised_blocks": outcome.get("advertised_blocks", []),
                "services": len(outcome["services"]),
                "events": sum(len(item["events"])
                              for item in outcome["services"]),
                "error": outcome.get("error"),
            })
            report(event="result", index=index, total=len(targets),
                   **guide["frequencies"][-1])
    finally:
        os.close(fd)

    guide["elapsed_seconds"] = round(time.monotonic() - started, 1)
    guide.update(summarize(guide))
    report(event="done", **{key: guide[key] for key in
                            ("channels_with_events", "event_count",
                             "coverage_hours", "elapsed_seconds")})
    return guide


def merge_guides(stored, fresh, now=None):
    """Carry still-valid channels from an older guide into a new one.

    A frequency that fails to lock -- or a refresh cut short because someone
    tuned in -- would otherwise drop those channels out of the guide
    entirely. Events are absolute timestamps, so the previous copy stays
    correct until it runs out: keep it, flagged as stale, rather than showing
    an empty row.
    """
    if not stored or not stored.get("channels"):
        return fresh
    now = int(time.time()) if now is None else now
    for number, channel in stored["channels"].items():
        if fresh["channels"].get(number, {}).get("events"):
            continue
        live = [event for event in channel.get("events", [])
                if event["start"] + event["duration"] > now]
        if not live:
            continue
        fresh["channels"][number] = {**channel, "events": live, "stale": True}
    fresh.update(summarize(fresh))
    return fresh


def summarize(guide):
    """Coverage figures for a guide, including how far ahead it reaches."""
    now = guide.get("generated_at") or int(time.time())
    horizon = 0
    events = 0
    with_events = 0
    for channel in guide["channels"].values():
        if channel["events"]:
            with_events += 1
        events += len(channel["events"])
        for event in channel["events"]:
            horizon = max(horizon, event["start"] + event["duration"])
    return {
        "event_count": events,
        "channels_with_events": with_events,
        "horizon": horizon or None,
        # What the guide actually reaches, not what the standard promises.
        "coverage_hours": round(max(0, horizon - now) / 3600, 1) if horizon
        else 0,
    }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def load_channels():
    if CHANNELS_FILE.exists():
        with open(CHANNELS_FILE) as f:
            return json.load(f)
    return []


def load_guide():
    """The stored guide, or None if there is not one yet."""
    if not GUIDE_FILE.exists():
        return None
    try:
        with open(GUIDE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_guide(guide):
    """Atomically replace the stored guide."""
    temp = GUIDE_FILE.with_suffix(".tmp")
    with open(temp, "w") as f:
        json.dump(guide, f)
    os.replace(temp, GUIDE_FILE)


def upcoming(guide, number, at=None):
    """The event airing at ``at`` on one channel, or None."""
    at = int(time.time()) if at is None else at
    channel = (guide or {}).get("channels", {}).get(number)
    if not channel:
        return None
    for event in channel["events"]:
        if event["start"] <= at < event["start"] + event["duration"]:
            return event
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--blocks", type=int, default=GUARANTEED_BLOCKS,
                        help="EIT blocks per frequency, 3 hours each "
                             f"(default {GUARANTEED_BLOCKS} = 12 hours). "
                             "Past the first four, each block can take a "
                             "minute to arrive.")
    parser.add_argument("--descriptions", action="store_true",
                        help="also read ETT descriptions (much slower)")
    parser.add_argument("--adapter", type=int, default=0)
    parser.add_argument("--show", metavar="NUMBER",
                        help="print a stored channel's schedule, no tuning")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.show:
        guide = load_guide()
        channel = (guide or {}).get("channels", {}).get(args.show)
        if not channel:
            print(f"No stored guide for {args.show}")
            return 1
        print(f"{channel['number']}  {channel['name']}")
        for event in channel["events"]:
            start = time.strftime("%a %H:%M", time.localtime(event["start"]))
            print(f"  {start}  {event['duration'] // 60:>3}m  {event['title']}")
        return 0

    guide = refresh(
        blocks=args.blocks, descriptions=args.descriptions,
        adapter=args.adapter,
        progress=lambda item: None if args.json else print(_line(item)),
    )
    save_guide(guide)
    if args.json:
        print(json.dumps(guide, indent=2))
    else:
        print(f"\n{guide['event_count']} events on "
              f"{guide['channels_with_events']} channels, "
              f"{guide['coverage_hours']}h ahead, "
              f"in {guide['elapsed_seconds']}s")
        if guide["compressed_strings"]:
            print(f"{guide['compressed_strings']} Huffman-compressed strings "
                  "skipped (A/65 Annex C tables not implemented)")
    return 0


def _line(item):
    if item["event"] == "frequency":
        return (f"RF {item['rf_channel']} "
                f"({item['frequency'] / 1e6:.0f} MHz)...")
    if item["event"] == "result":
        if item.get("error"):
            return f"  {item['error']}"
        return (f"  {item['services']} services, {item['events']} events, "
                f"EIT blocks {item['eit_blocks']}")
    return str(item.get("message", ""))


if __name__ == "__main__":
    raise SystemExit(main())
