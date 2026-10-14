"""TV Thing recorder: timer schedule, guide matching, and recording commands.

Everything here is pure -- no tuner, no processes, no clock of its own -- so
the scheduling rules can be tested without hardware. ``server.py`` owns the
runtime: it tunes the digital lane, holds the tuner lock, and runs ffmpeg.

How a recording is made
-----------------------
The digital lane's ``dvb_source.py`` already demuxes one programme into a
compact transport stream for the live encoder. While a recording is active it
sends a second copy of those datagrams to a *tap* port, and a separate ffmpeg
reads that copy. The recording is therefore encoded from the broadcast
itself, not from the live stream's low-latency HLS: it has its own quality
settings and survives the live encoder being restarted by a safety trip.

Encoding is x264 ``veryfast`` at constant quality, capped at 720 lines and
deinterlaced. On this host that costs about one core -- measured at roughly
100% for 1080i or 720p60 input -- and yields about 1-2 GB per hour, against
the 7 GB per hour of the raw broadcast. Faster presets barely shrink the file
for a fifth more CPU, and GPU encoders are kept out of the recorder for the
same reason the live lane runs on the CPU here.

A timer made with ``from_live`` skips all of that. It copies the live lane's
own HLS -- the H.264 and AAC the live encoder already makes -- with no tap
and no second encode, so it costs a few percent of a core instead of one and
a half. The picture is the live stream's, at the lane's quality (720p at medium), which
is enough for tapes made for machine analysis, such as VizelVision's ad
training. The copy follows one stream generation; when the live encoder
restarts, the recording carries on into the next piece.

The file is written as MPEG-TS first, because a transport stream cut off by
a crash or a power loss is still playable to the last packet. When the
recording ends it is remuxed, without re-encoding, to MP4 with the index at
the front so DLNA clients can seek straight away. While either file is being
written it carries a ``.part`` suffix, which MiniDLNA does not index; the
rename to the final name is what makes it appear in the media library.
"""

import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

RECORDINGS_DIR = Path(os.environ.get(
    "TV_THING_RECORDINGS_DIR", "/mnt/sda1/media/TV Recordings"
))
PART_SUFFIX = ".part"
LIBRARY_EXTENSIONS = (".mp4", ".ts")
# The ad stripper (ad_detection/strip_watch.py, outside TV Thing) files
# "<tape> (no ads).mp4" beside the original, plus a .json of what it cut.
AD_FREE_MARK = " (no ads)"

# Guide times are the broadcaster's schedule, and programmes routinely start
# a little early or run over. Manual timers are taken literally.
GUIDE_PRE_PAD_SECONDS = int(os.environ.get(
    "TV_THING_RECORD_PRE_PAD_SECONDS", "60"
))
GUIDE_POST_PAD_SECONDS = int(os.environ.get(
    "TV_THING_RECORD_POST_PAD_SECONDS", "120"
))
# One Touch Recording with no programme listed, as on the deck it copies.
OTR_DEFAULT_SECONDS = 30 * 60
MIN_RECORDING_SECONDS = 60
# Finished, cancelled, and failed timers kept for the history list.
HISTORY_LIMIT = 60

VIDEO_MAX_HEIGHT = int(os.environ.get("TV_THING_RECORD_MAX_HEIGHT", "720"))
VIDEO_CRF = os.environ.get("TV_THING_RECORD_CRF", "23")
VIDEO_PRESET = os.environ.get("TV_THING_RECORD_PRESET", "veryfast")
# A ceiling, not a target: constant quality keeps static scenes small, and
# the cap stops a noisy broadcast from producing a runaway file.
VIDEO_MAXRATE_KBPS = int(os.environ.get("TV_THING_RECORD_MAXRATE_KBPS", "4000"))
AUDIO_KBPS = int(os.environ.get("TV_THING_RECORD_AUDIO_KBPS", "160"))
RECORD_THREADS = os.environ.get("TV_THING_RECORD_THREADS", "4")
# The largest picture ATSC sends (1920x1080, coded as 1088 lines). A bit
# error in an MPEG-2 sequence header can claim a picture up to 16383 pixels
# square, and ffmpeg would allocate and zero a frame of ~0.4-0.8 GB for it --
# enough to trip the unit's MemoryMax and take the whole service down. With
# this cap the decoder drops such a frame instead.
BROADCAST_MAX_PIXELS = str(1920 * 1088)

ACTIVE = ("scheduled", "recording")
FINISHED = ("done", "partial", "failed", "missed", "cancelled", "conflict")


def new_id():
    return secrets.token_hex(6)


def empty_schedule():
    return {"timers": [], "rules": []}


def normalize_schedule(stored):
    """Accept a stored schedule, dropping anything malformed."""
    if not isinstance(stored, dict):
        return empty_schedule()
    timers = [
        timer for timer in stored.get("timers", [])
        if isinstance(timer, dict) and timer.get("id")
        and isinstance(timer.get("start"), (int, float))
        and isinstance(timer.get("end"), (int, float))
    ]
    rules = [
        rule for rule in stored.get("rules", [])
        if isinstance(rule, dict) and rule.get("id") and rule.get("title")
    ]
    return {"timers": timers, "rules": rules}


def overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and start_b < end_a


def timer_lane(timer):
    """The lane a timer records from. Timers from before the Roku lane are digital."""
    return timer.get("lane") or "digital"


def find_conflict(timers, start, end, exclude_id=None, lane="digital"):
    """The live timer that already has this lane's one tuner then, or None.

    The card has a single ATSC front-end and there is a single source Roku,
    so two recordings on one lane that overlap in time conflict, whatever
    their channels. Recordings on different lanes never do.
    """
    for timer in timers:
        if timer["id"] == exclude_id or timer.get("status") not in ACTIVE:
            continue
        if timer_lane(timer) != lane:
            continue
        if overlaps(start, end, timer["start"], timer["end"]):
            return timer
    return None


def make_timer(channel, start, end, *, title=None, kind="manual",
               program=None, rule_id=None, from_live=False, now=None,
               lane="digital", target=None):
    """A new scheduled timer. ``start`` and ``end`` include any padding.

    A Roku timer (``lane="roku"``) names its Roku ``target`` -- a YouTube TV
    channel, a game, or Manual Roku -- and always copies the lane's stream.
    """
    now = time.time() if now is None else now
    return {
        "id": new_id(),
        "kind": kind,
        "lane": lane,
        "target": target,
        "channel_id": channel["id"],
        "channel_number": channel.get("number"),
        "channel_name": channel.get("name"),
        "start": int(start),
        "end": int(end),
        "title": title or None,
        "program": program,
        "rule_id": rule_id,
        "from_live": bool(from_live) or lane == "roku",
        "status": "scheduled",
        "note": None,
        "file": None,
        "bytes": None,
        "created_at": int(now),
        "started_at": None,
        "finished_at": None,
    }


def guide_window(event):
    """The padded recording window for one guide event."""
    return (event["start"] - GUIDE_PRE_PAD_SECONDS,
            event["start"] + event["duration"] + GUIDE_POST_PAD_SECONDS)


def program_of(event, number):
    return {
        "title": event.get("title"),
        "start": event["start"],
        "duration": event["duration"],
        "channel": number,
    }


def title_key(title):
    """Case- and punctuation-blind form of a programme title for matching."""
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def guide_events(guide, now, channel_number=None):
    """Every guide event that has not finished yet, as (number, channel, event)."""
    for number, channel in (guide or {}).get("channels", {}).items():
        if channel_number and number != channel_number:
            continue
        for event in channel.get("events", []):
            if event["start"] + event["duration"] > now:
                yield number, channel, event


def search_guide(guide, query, now, limit=80):
    """Upcoming and current airings whose title contains ``query``."""
    needle = title_key(query)
    if not needle:
        return []
    hits = [
        (number, channel, event)
        for number, channel, event in guide_events(guide, now)
        if needle in title_key(event.get("title"))
    ]
    hits.sort(key=lambda hit: (hit[2]["start"], channel_sort_key(hit[0])))
    return [
        {
            "channel_number": number,
            "channel_name": channel.get("name"),
            "channel_id": channel.get("id"),
            "title": event.get("title"),
            "start": event["start"],
            "duration": event["duration"],
            "airing": event["start"] <= now,
        }
        for number, channel, event in hits[:limit]
    ]


def channel_sort_key(number):
    try:
        major, _, minor = str(number).partition(".")
        return (int(major), int(minor or 0))
    except ValueError:
        return (9999, 0)


def find_event(guide, channel_number, start):
    """The guide event on one channel starting at ``start``, or None."""
    channel = (guide or {}).get("channels", {}).get(channel_number)
    for event in (channel or {}).get("events", []):
        if event["start"] == start:
            return channel, event
    return None


def expand_rules(schedule, guide, channels, now):
    """Add a timer for every airing a series rule matches but has not got.

    Returns the timers added. An airing that would collide with another
    recording is added as a ``conflict`` timer, so the timer list says why
    it will not be taped instead of leaving it silently missing -- and so it
    is not reconsidered every time the scheduler looks.
    """
    by_number = {channel.get("number"): channel for channel in channels}
    added = []
    for rule in schedule["rules"]:
        wanted = title_key(rule["title"])
        seen = {
            (timer.get("channel_number"), (timer.get("program") or {})
             .get("start"))
            for timer in schedule["timers"] if timer.get("rule_id") == rule["id"]
        }
        for number, _guide_channel, event in guide_events(
                guide, now, rule.get("channel_number")):
            if title_key(event.get("title")) != wanted:
                continue
            if (number, event["start"]) in seen:
                continue
            channel = by_number.get(number)
            if channel is None:
                continue
            start, end = guide_window(event)
            # An airing already under way is still worth taping, but only
            # from now; one with less than a minute left is not.
            start = max(start, int(now))
            if end - start < MIN_RECORDING_SECONDS:
                continue
            timer = make_timer(
                channel, start, end, title=event.get("title"), kind="series",
                program=program_of(event, number), rule_id=rule["id"], now=now,
            )
            clash = find_conflict(schedule["timers"], start, end)
            if clash:
                timer["status"] = "conflict"
                timer["note"] = (f"clashes with {describe(clash)}; the card "
                                 "has one digital tuner")
            schedule["timers"].append(timer)
            seen.add((number, event["start"]))
            added.append(timer)
    return added


def describe(timer):
    title = timer.get("title") or (
        f"CH {timer['channel_number']}" if timer.get("channel_number")
        else timer.get("channel_name") or "Recording")
    stamp = datetime.fromtimestamp(timer["start"]).strftime("%a %H:%M")
    return f"{title} ({stamp})"


def due_timer(timers, now, lane=None):
    """The scheduled timer whose window contains ``now``, earliest first.

    With ``lane``, only that lane's timers are considered.
    """
    due = [
        timer for timer in timers
        if timer.get("status") == "scheduled"
        and timer["start"] <= now < timer["end"]
        and (lane is None or timer_lane(timer) == lane)
    ]
    return min(due, key=lambda timer: timer["start"]) if due else None


def expire_missed(timers, now):
    """Mark scheduled timers whose whole window has passed. Returns them."""
    missed = []
    for timer in timers:
        if timer.get("status") == "scheduled" and timer["end"] <= now:
            timer["status"] = "missed"
            timer["note"] = timer.get("note") or "the window passed unrecorded"
            timer["finished_at"] = int(now)
            missed.append(timer)
    return missed


def prune_history(timers, limit=HISTORY_LIMIT):
    """Keep every live timer and only the most recent finished ones."""
    live = [timer for timer in timers if timer.get("status") in ACTIVE]
    done = [timer for timer in timers if timer.get("status") not in ACTIVE]
    done.sort(key=lambda timer: timer.get("finished_at") or timer["end"],
              reverse=True)
    kept = live + done[:limit]
    kept.sort(key=lambda timer: timer["start"])
    return kept


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^\w .,'&()!+-]+", re.UNICODE)


def safe_title(text):
    cleaned = _UNSAFE.sub(" ", str(text or "")).strip(" .")
    return re.sub(r"\s+", " ", cleaned)[:80] or "Recording"


def recording_basename(timer):
    """The library name for a timer, without an extension.

    ``Title - 7.1 WJLA - 2026-09-23 2000``: title first, so a DLNA folder
    listing sorts episodes of one programme together.
    """
    program = timer.get("program") or {}
    stamp = datetime.fromtimestamp(
        program.get("start") or timer["start"]
    ).strftime("%Y-%m-%d %H%M")
    channel = " ".join(
        part for part in (timer.get("channel_number"),
                          safe_title(timer.get("channel_name") or ""))
        if part and part != "Recording"
    )
    # Untitled manual timers are named by channel and time alone.
    title = safe_title(timer["title"]) if timer.get("title") else ""
    return " - ".join(part for part in (title, channel, stamp) if part)


def stem_names(stem):
    """Every file one recording can occupy, from first write to library."""
    return [stem.with_name(stem.name + ext) for ext in (
        ".ts" + PART_SUFFIX, ".mp4" + PART_SUFFIX, ".ts", ".mp4",
    )]


def unique_stem(directory, basename):
    """A path stem in ``directory`` that no recording file uses yet."""
    for index in range(1, 1000):
        suffix = "" if index == 1 else f" ({index})"
        stem = directory / f"{basename}{suffix}"
        if not any(path.exists() for path in stem_names(stem)):
            return stem
    raise OSError(f"no free file name for {basename}")


def part_of(path):
    """The in-progress name for ``path``; MiniDLNA ignores the suffix."""
    return path.with_name(path.name + PART_SUFFIX)


def recording_part(stem):
    """Where the transport stream is written while recording."""
    return stem.with_name(stem.name + ".ts" + PART_SUFFIX)


def piece_part(stem, index):
    """Where piece ``index`` (from 1) of one recording is written.

    A recording cut short by a server restart or a recorder crash carries on
    into another piece under the same stem. Pieces stay hidden ``.part``
    files until the timer ends, and are then joined into one library file.
    """
    if index <= 1:
        return recording_part(stem)
    return stem.with_name(f"{stem.name}.{index}.ts{PART_SUFFIX}")


def stem_of_part(part):
    """``X.ts.part`` -> ``X``, the stem a leftover part was recorded under."""
    name = part.name
    for ext in (".ts" + PART_SUFFIX, ".mp4" + PART_SUFFIX):
        if name.endswith(ext):
            return part.with_name(name[:-len(ext)])
    return None


def library_entry(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "name": path.name,
        "bytes": stat.st_size,
        "modified": int(stat.st_mtime),
    }


def ad_strip_summary(path):
    """What the ad stripper cut to make the ad-free copy ``path``, from the
    report it writes beside it; None if there is no readable report."""
    try:
        with open(path.with_suffix(".json")) as f:
            report = json.load(f)
        blocks = report["blocks"]
        return {
            "source": report["file"],
            "recorded_seconds": round(float(report["duration"])),
            "kept_seconds": round(sum(end - start for start, end in report["kept"])),
            # pre-roll before the show and the next programme after it are
            # padding, not breaks
            "breaks": sum(1 for block in blocks[1:-1] if block["label"] == "ad"),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def list_library(directory):
    """Finished recordings in the library, newest first. An ad-free copy
    carries ``ad_strip``: its original's name and what was cut."""
    try:
        paths = [
            path for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in LIBRARY_EXTENSIONS
            and not path.name.startswith(".")
        ]
    except OSError:
        return []
    entries = [entry for entry in map(library_entry, paths) if entry]
    for entry in entries:
        if Path(entry["name"]).stem.endswith(AD_FREE_MARK):
            entry["ad_strip"] = ad_strip_summary(directory / entry["name"])
    entries.sort(key=lambda entry: entry["modified"], reverse=True)
    return entries


def library_file(directory, name):
    """Resolve a client-supplied library name, refusing anything else."""
    name = str(name or "")
    if (not name or "/" in name or "\\" in name or name.startswith(".")
            or Path(name).suffix.lower() not in LIBRARY_EXTENSIONS):
        return None
    path = directory / name
    return path if path.is_file() else None


LIBRARY_CONTENT_TYPES = {".mp4": "video/mp4", ".ts": "video/mp2t"}


def byte_range(header, size):
    """The inclusive (start, end) a Range header asks for, or None for all.

    Only the single ``bytes=`` range browsers send when seeking a video is
    honoured; anything else is answered with the whole file. Raises
    ValueError when the range lies wholly past the end (HTTP 416).
    """
    match = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header or "")
    if not match or match.group(1) == match.group(2) == "":
        return None
    first, last = match.groups()
    if first == "":                       # bytes=-N: the final N bytes
        length = int(last)
        if length == 0:
            raise ValueError("empty suffix range")
        return max(0, size - length), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("range not satisfiable")
    return start, end


def content_disposition(name, download):
    """Inline for the player, attachment for the download button.

    The plain ``filename`` is an ASCII fallback; ``filename*`` carries the
    real name (titles like "Señor" or "Jeopardy!") for browsers that read it.
    """
    from urllib.parse import quote
    fallback = re.sub(r'[^A-Za-z0-9 ._()!-]', "_", name)
    kind = "attachment" if download else "inline"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def build_record_command(input_url, output_path):
    """Encode the tapped broadcast to a crash-tolerant transport stream.

    ffmpeg is stopped with ``q`` on stdin, so ``-nostdin`` is deliberately
    absent: a signal interrupts its file writes as well as its input and
    loses the trailer.
    """
    filters = [
        "bwdif=mode=send_frame:parity=auto:deint=interlaced",
        f"scale=w=-2:h='min({VIDEO_MAX_HEIGHT},ih)'",
    ]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-fflags", "+discardcorrupt",
        "-max_pixels", BROADCAST_MAX_PIXELS,
        "-thread_queue_size", "4096",
        "-probesize", "2M", "-analyzeduration", "1500000",
        # The decoder's and the filters' pools default to one thread per
        # core, 28 here, against the unit's 192-task cap (server.FFMPEG_THREADS).
        "-threads", RECORD_THREADS, "-filter_threads", RECORD_THREADS,
        "-i", input_url,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", VIDEO_CRF,
        "-maxrate", f"{VIDEO_MAXRATE_KBPS}k",
        "-bufsize", f"{VIDEO_MAXRATE_KBPS * 2}k",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        # x264 defaults to 1.5 threads per core, each frame thread holding
        # its own buffers. Four still keep 1080i in real time here, at 220 MB
        # rather than 300, which matters under the unit's MemoryMax.
        "-threads", RECORD_THREADS,
        # A keyframe every ~4 s keeps seeking responsive without costing
        # much compression.
        "-g", "120",
        # Broadcast timing passes through: CFR gap filling across a signal
        # drop can synthesize an unbounded run of duplicate frames.
        "-fps_mode", "passthrough",
        "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ac", "2",
        "-af", "aresample=async=1000",
        "-f", "mpegts", str(output_path),
    ]


def build_live_copy_command(playlist_path, output_path, start_index=0):
    """Copy the live lane's HLS into a transport stream, without re-encoding.

    It reads one generation's playlist from its first listed segment, so a
    recording that moves on to a restarted live stream loses only the gap,
    and one starting on a running stream also keeps the dozen seconds
    before. Stopped with ``q`` like the encoding recorder.

    The Roku's playlist keeps the navigation that led to a verified channel,
    so its copies start near the live edge instead (``start_index=-3``).
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-live_start_index", str(start_index),
        "-i", str(playlist_path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy",
        "-f", "mpegts", str(output_path),
    ]


def build_remux_command(ts_path, mp4_path):
    """Rewrap a finished recording as MP4 without re-encoding."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-i", str(ts_path),
        "-map", "0", "-c", "copy",
        "-movflags", "+faststart",
        "-f", "mp4", str(mp4_path),
    ]


def concat_list(paths):
    """An ffmpeg concat-demuxer script naming ``paths`` in order."""
    lines = ["ffconcat version 1.0"]
    for path in paths:
        quoted = str(path).replace("'", "'\\''")
        lines.append(f"file '{quoted}'")
    return "\n".join(lines) + "\n"


def build_join_command(list_path, mp4_path):
    """Join the pieces of an interrupted recording into one MP4.

    The concat demuxer lays each piece after the last by its duration, so
    the timestamps run on across the gap instead of restarting.
    """
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-map", "0", "-c", "copy",
        "-movflags", "+faststart",
        "-f", "mp4", str(mp4_path),
    ]


def build_probe_command(path):
    """Print a media file's duration in seconds."""
    return [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
