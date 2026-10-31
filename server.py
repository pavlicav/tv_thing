#!/usr/bin/env python3
"""TV Thing - ATSC and analog television streaming server."""

import contextlib
import copy
import math
import fcntl
import ipaddress
import resource
import shutil
import shlex
import json
import os
import queue
import re
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

import analog_tuner
import atsc_scan
import channels
import epg
import feed_api
import feed_node
import host_config
import internet_streams
import lineup
import recorder
import roku_lane
import web_basic
import sports_guide
import yttv_epg
from roku_control.errors import (BaseballScheduleError, ChannelNotFoundError,
                                 ConfigError, ControlBusyError, EcpError,
                                 RokuStreamError)
from roku_control.days import today as games_day
from roku_control.sports import estimated_end

BASE_DIR = Path(__file__).parent
# This host's settings (host_config.py, the Config page): the node's role and name, the Roku
# lane, the digital adapter, the encoder, the recordings folder. Read once, at start. With no
# settings file, the environment and viviserv's hardware give what the constants below used to.
HOST = host_config.load()
PULSE_CLIENT_CONFIG = BASE_DIR / "pulse-client-no-shm.conf"
STREAMS_DIR = Path(os.environ.get(
    "TV_THING_STREAMS_DIR", str(BASE_DIR / "streams")
))
CHANNELS_FILE = BASE_DIR / "channels.json"
# The TV Thing channel lineup, and the numbers it has given channels it does
# not list (a rescan's surprises); see lineup.py.
LINEUP_FILE = BASE_DIR / "lineup.json"
INTERNET_STREAMS_FILE = BASE_DIR / "internet_streams.json"
LINEUP_STATE_FILE = BASE_DIR / "lineup_state.json"
WSCAN_OUTPUT = BASE_DIR / "w_scan_output.txt"

# ATSC frequency ranges (lower-edge MHz)
# VHF-Lo has a gap at 72-76 MHz: ch 2-4 are contiguous from 54, ch 5-6 start at 76.
ATSC_FREQUENCIES = []
for _ch in range(2, 5):     # VHF-Lo ch 2-4: 54, 60, 66 MHz
    ATSC_FREQUENCIES.append((_ch, ((_ch - 2) * 6 + 54) * 1_000_000))
for _ch in range(5, 7):     # VHF-Lo ch 5-6: 76, 82 MHz (gap at 72-76)
    ATSC_FREQUENCIES.append((_ch, ((_ch - 5) * 6 + 76) * 1_000_000))
for _ch in range(7, 14):    # VHF-Hi: ch 7-13: 174-210 MHz
    ATSC_FREQUENCIES.append((_ch, ((_ch - 7) * 6 + 174) * 1_000_000))
for _ch in range(14, 37):   # UHF: ch 14-36: 470-602 MHz
    ATSC_FREQUENCIES.append((_ch, ((_ch - 14) * 6 + 470) * 1_000_000))

# Quality presets: name -> (video_bitrate, audio_bitrate, scale)
QUALITY_PRESETS = {
    "high":   {"vb": 4500, "ab": 160, "scale": None},
    "medium": {"vb": 2500, "ab": 128, "scale": "720"},
    "low":    {"vb": 1000, "ab": 96,  "scale": "480"},
}

API_VERSION = "v1"
SOURCE_POLL_INTERVAL_MS = 3000
VIZELVISION_MEDIA_URL = os.environ.get(
    "TV_THING_VIZELVISION_MEDIA_URL",
    # A second node plays the primary's VizelVision, when it knows the primary's address.
    HOST.primary_url(6970, "/v1/master/media/") or "http://127.0.0.1:6970/v1/master/media/"
).rstrip("/") + "/"
VIZELVISION_MAX_MEDIA_BYTES = 16 * 1024 * 1024

# ---------------------------------------------------------------------------
# Guide
#
# Reading the broadcast schedule needs the digital front-end to itself, one
# frequency at a time, so a refresh interrupts live TV exactly like a band
# scan. That is what makes it a scheduled job rather than something done on
# demand behind the viewer's back.
#
# A/65 guarantees only EIT-0..EIT-3 -- twelve hours. A single overnight
# refresh would therefore run out around midday and leave the evening, which
# is the part anyone actually looks up, uncovered. Two slots twelve hours
# apart keep the whole day in range while still doing the interrupting at
# times nobody is watching.
# ---------------------------------------------------------------------------
EPG_ENABLED = os.environ.get("TV_THING_EPG", "1") != "0"
EPG_BLOCKS = int(os.environ.get("TV_THING_EPG_BLOCKS", epg.GUARANTEED_BLOCKS))
# ETTs are only required to repeat once a minute, which turns a one-minute
# sweep into a twenty-minute one. Off unless asked for.
EPG_DESCRIPTIONS = os.environ.get("TV_THING_EPG_DESCRIPTIONS", "0") == "1"
EPG_STARTUP_DELAY = float(os.environ.get("TV_THING_EPG_STARTUP_DELAY", "120"))
# Below this much guide still ahead of the clock, a starting server reads a
# fresh one rather than waiting for the next slot.
EPG_MIN_COVERAGE_HOURS = float(
    os.environ.get("TV_THING_EPG_MIN_COVERAGE_HOURS", "3")
)
# A lock held at 03:00 would otherwise cost the whole overnight refresh: the
# next slot is twelve hours later, which is the guide's entire span, so it
# would run dry by afternoon. Retry until the lock clears instead.
EPG_LOCK_RETRY_SECONDS = float(
    os.environ.get("TV_THING_EPG_LOCK_RETRY_SECONDS", "300")
)


def parse_refresh_times(spec):
    """Parse "03:00,15:00" into [(3, 0), (15, 0)], dropping anything odd."""
    times = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            hour, minute = item.split(":")
            hour, minute = int(hour), int(minute)
        except ValueError:
            continue
        if 0 <= hour < 24 and 0 <= minute < 60:
            times.append((hour, minute))
    return sorted(set(times))


EPG_REFRESH_TIMES = parse_refresh_times(
    os.environ.get("TV_THING_EPG_TIMES", "03:00,15:00")
)

# YouTube TV's channels get their listings from the web (yttv_epg.py), which
# needs no tuner, so one read a day is enough: it stores two days. A starting
# server with less than a day left reads at once, and a read that fails is
# tried again hourly until the next slot.
YTTV_GUIDE_ENABLED = HOST.value("yttv_guide") != "off"    # TV_THING_YTTV_GUIDE=0, or the Config page
YTTV_GUIDE_REFRESH_TIMES = parse_refresh_times(
    os.environ.get("TV_THING_YTTV_GUIDE_TIMES", "04:30")
)
YTTV_GUIDE_MIN_COVERAGE_HOURS = float(
    os.environ.get("TV_THING_YTTV_GUIDE_MIN_COVERAGE_HOURS", "24")
)
YTTV_GUIDE_RETRY_SECONDS = 3600
YTTV_GUIDE_STARTUP_DELAY = 30.0


def next_refresh_at(times, now=None):
    """Unix time of the next scheduled refresh, or None if none are set.

    Local wall-clock times, so a 03:00 slot stays at 03:00 across a daylight
    saving change rather than drifting to 02:00 or 04:00.
    """
    if not times:
        return None
    now = time.time() if now is None else now
    local = time.localtime(now)
    candidates = []
    for day in (0, 1):
        for hour, minute in times:
            stamp = time.mktime((
                local.tm_year, local.tm_mon, local.tm_mday + day,
                hour, minute, 0, 0, 0, -1,
            ))
            if stamp > now:
                candidates.append(stamp)
    return min(candidates) if candidates else None

# A channel-specific, direct Linux DVB source sends one selected program to a
# matching ffmpeg process. Every tune gets a fresh encoder timestamp domain;
# carrying an encoder across unrelated broadcast clocks can make CFR duplicate
# millions of frames while it tries to bridge the discontinuity.
TV_UDP_PORT = int(os.environ.get("TV_THING_TV_UDP_PORT", 18320))
TV_UDP_FIFO_PACKETS = int(
    os.environ.get("TV_THING_UDP_FIFO_PACKETS", "65536")
)



def tv_udp_input(port=TV_UDP_PORT):
    """ffmpeg input URL for one source-to-encoder generation."""
    return (
        f"udp://127.0.0.1:{port}"
        f"?fifo_size={TV_UDP_FIFO_PACKETS}"
        "&buffer_size=1048576&overrun_nonfatal=1"
    )


TV_UDP_INPUT = tv_udp_input()
ALWAYS_ON = os.environ.get("TV_THING_ALWAYS_ON", "1") != "0"
PLAYBACK_STATE_FILE = STREAMS_DIR / "playback_state.json"
HLS_SEGMENT_TIME = float(os.environ.get("TV_THING_HLS_SEGMENT_TIME", "0.6"))
VIDEO_ENCODER = HOST.value("video_encoder")    # TV_THING_VIDEO_ENCODER, the Config page, else cpu
VAAPI_DEVICE = os.environ.get("TV_THING_VAAPI_DEVICE", "/dev/dri/renderD128")
NVENC_PROBE_ATTEMPTS = int(
    os.environ.get("TV_THING_NVENC_PROBE_ATTEMPTS", "3")
)
NVENC_PROBE_RETRY_SECONDS = float(
    os.environ.get("TV_THING_NVENC_PROBE_RETRY_SECONDS", "0.5")
)
TV_SOURCE_BACKEND = "linux-dvb"
TV_SOURCE_STOP_TIMEOUT = float(
    os.environ.get("TV_THING_SOURCE_STOP_TIMEOUT", "0.5")
)
TV_PACKAGER_STOP_TIMEOUT = float(
    os.environ.get("TV_THING_PACKAGER_STOP_TIMEOUT", "0.5")
)
FFMPEG_PROBE_SIZE = os.environ.get("TV_THING_FFMPEG_PROBE_SIZE", "512k")
FFMPEG_ANALYZE_DURATION = os.environ.get(
    "TV_THING_FFMPEG_ANALYZE_DURATION", "750000"
)
# ffmpeg sizes its decoder, filter and x264 thread pools to the host's 28
# cores: a digital packager ran 122 threads and the Roku verifier 58. The unit
# allows 192 tasks and four cores, and on 2026-10-13 the packager, a VCR
# recording and a Roku navigation together reached the cap: the kernel refused
# three threads, and each refusal failed a Roku tune ("Error reinitializing
# filters ... Resource temporarily unavailable"). Four of each keeps a
# packager near 30 threads and encodes 1080i no slower on four cores.
FFMPEG_THREADS = os.environ.get("TV_THING_FFMPEG_THREADS", "4")
TV_MAX_DUPLICATED_FRAMES = int(os.environ.get(
    "TV_THING_MAX_DUPLICATED_FRAMES", "1000"
))
TV_MAX_UDP_OVERRUNS = int(os.environ.get(
    "TV_THING_MAX_UDP_OVERRUNS", "1"
))
# Backward timestamps ffmpeg's muxer patched, one after another: a flood of filler audio
# (as first_pts=0 made at every 5.1/stereo switch, below), or anything else that sends the
# audio chain back in time. A fresh encoder is clean. A healthy generation logs a few at
# its start.
TV_MAX_NON_MONOTONOUS = int(os.environ.get(
    "TV_THING_MAX_NON_MONOTONOUS", "100"
))
# An encoder holds about 200 MiB. Past this, whatever the cause, it is replaced.
TV_MAX_PACKAGER_RSS_MIB = float(os.environ.get(
    "TV_THING_MAX_PACKAGER_RSS_MIB", "400"
))
TV_MAX_SEGMENT_BYTES = int(os.environ.get(
    "TV_THING_MAX_SEGMENT_BYTES", str(4 * 1024 * 1024)
))
TV_SEGMENT_STARTUP_TIMEOUT = float(os.environ.get(
    "TV_THING_SEGMENT_STARTUP_TIMEOUT", "12"
))
TV_SEGMENT_STALL_TIMEOUT = float(os.environ.get(
    "TV_THING_SEGMENT_STALL_TIMEOUT", "5"
))
# The NVENC packagers trip here: below the card's 97 C slowdown (owner, 2026-10-06).
TV_MAX_GPU_TEMP_C = float(os.environ.get(
    "TV_THING_MAX_GPU_TEMP_C", "90"
))
TV_HEALTH_POLL_SECONDS = float(os.environ.get(
    "TV_THING_HEALTH_POLL_SECONDS", "0.5"
))

# ---------------------------------------------------------------------------
# Lanes
#
# The HVR-1600 carries two independent RF front-ends: the Samsung S5H1409
# ATSC demodulator behind /dev/dvb/adapter0, and the analog tuner feeding the
# CX23418 hardware encoder behind /dev/video*. They tune separately, so one
# digital and one analog stream can run at the same time -- verified by
# holding a DVB lock while streaming NTSC off the analog side.
#
# This board's tuner has no usable FM section: its EEPROM does not advertise
# radio, and forcing cx18 radio=1 reports an identical 50% signal and stereo
# flag at every frequency from 65 to 108 MHz. FM support was removed rather
# than left as a control that cannot work.
#
# The third lane is not on the HVR-1600 at all: the source Roku feeds a
# Hauppauge HD PVR, whose own H.264/AAC is copied to HLS (see roku_lane.py).
# It has its own lock, and it is not a "video lane" for the legacy
# /stream/live.m3u8 alias: preparing the Roku for someone else must never
# flip what the living-room Roku app is watching.
# ---------------------------------------------------------------------------
#
# The devices and the lanes on them are listed in feed.json (feed_node.py),
# which also keeps each lane's revision and lifecycle and the feed's epoch.
# The code below still knows these three lanes by name.
# ---------------------------------------------------------------------------
# Which process this is (Phase 2 of the split, INVARIANTS.md). "all" runs everything in one
# process, as before the split. "feed" (feed_server.py) holds the hardware and the streams and
# serves /stream/ and /feed/v1/. "control" runs the API, the UI and the policy, and reaches
# the feed only through RemoteFeed.
ROLE = os.environ.get("TV_THING_ROLE", "all")
if ROLE not in ("all", "feed", "control"):
    raise SystemExit(f"TV_THING_ROLE must be all, feed or control, not {ROLE!r}")
FEED_URL = os.environ.get("TV_THING_FEED_URL", "http://127.0.0.1:8086/")
FEED_CONFIG_FILE = Path(os.environ.get(
    "TV_THING_FEED_CONFIG", str(BASE_DIR / "feed.json")
))
# feed.json as this host has it: on the primary, as written; a second node names itself and
# describes its own hardware (host_config.Host.feed_config).
FEED_CONFIG = HOST.feed_config(feed_node.load_config(FEED_CONFIG_FILE))
LANE_DIGITAL = "digital"
LANE_ANALOG = "analog"
LANE_ROKU = "roku"
_missing_lanes = {LANE_DIGITAL, LANE_ANALOG, LANE_ROKU} - {lane.id for lane in FEED_CONFIG.lanes}
if _missing_lanes:
    raise feed_node.FeedConfigError(f"{FEED_CONFIG_FILE} lacks the lanes {', '.join(sorted(_missing_lanes))}")
VIDEO_LANES = tuple(lane.id for lane in FEED_CONFIG.lanes if lane.video)
ALL_LANES = tuple(lane.id for lane in FEED_CONFIG.lanes)

FRONTEND_DIGITAL = FEED_CONFIG.lane(LANE_DIGITAL).device
FRONTEND_ANALOG = FEED_CONFIG.lane(LANE_ANALOG).device
FRONTEND_ROKU = FEED_CONFIG.lane(LANE_ROKU).device
# The DVB adapter the digital lane tunes: feed.json's, unless this host's settings say otherwise.
DIGITAL_ADAPTER = FEED_CONFIG.device(FRONTEND_DIGITAL).adapter or 0
LANE_FRONTEND = {lane.id: lane.device for lane in FEED_CONFIG.lanes}

# Each lane owns its own playlist and segment prefix so stopping or
# restarting one lane never disturbs the other's files.
LANE_PLAYLIST = {lane.id: lane.playlist for lane in FEED_CONFIG.lanes}
LANE_SEGMENT_PREFIX = {lane.id: lane.segment_prefix for lane in FEED_CONFIG.lanes}
# Files a lane owns, removed when that lane stops.
LANE_FILES = {lane.id: lane.files for lane in FEED_CONFIG.lanes}
# Legacy single-stream playlist. Older clients (the Roku app, cached web
# pages) only know /stream/live.m3u8, so it is served as an alias for
# whichever video lane was most recently tuned.
LEGACY_PLAYLIST = "live.m3u8"


def new_lane_state():
    return {
        "active": False,
        "selection": None,
        "quality": "medium",
        "stream_id": None,
        "health": None,
        "started_at": None,
        "video_encoder": None,
        "tuner": None,
        "error": None,
        # Recording tap ports the current source generation also feeds.
        "taps": [],
    }


lanes = {name: new_lane_state() for name in ALL_LANES}
# The feed's own record of each lane: its revision and lifecycle, this run's epoch, and
# the commands applied (feed_node.py). Every start, stop and crash below reports to it.
feed = feed_node.FeedNode(FEED_CONFIG)
# The control plane's only way to the feed: its lanes' state, and every change to them.
# In one process it calls the functions below; split, it speaks HTTP (see INVARIANTS.md).
feed_client = (feed_api.RemoteFeed(FEED_URL, FEED_CONFIG, new_lane_state) if ROLE == "control"
               else feed_node.LocalFeed(feed, sys.modules[__name__]))
# The feed's /feed/v1/, which RemoteFeed asks (the feed role only).
feed_http = feed_api.FeedAPI(feed_client) if ROLE == "feed" else None
tuning_lanes = set()

# Per-lane child processes. Keys are "<lane>_<role>".
procs = {
    "digital_source": None,
    "digital_packager": None,
    "analog_packager": None,
    "roku_capture": None,
}

# The video lane whose playlist /stream/live.m3u8 currently mirrors.
primary_video_lane = LANE_DIGITAL

lock = threading.RLock()
# Set on the way out, before anything is stopped: from then on no lane starts. Stopping a
# lane wakes its keeper, and a recording ending wakes the always-on one; a process either
# starts then outlives the server, holding its tuner or the HD PVR, and keeps the unit
# stopping until systemd kills it (the Roku capture, 2026-10-06; the digital lane, 2026-10-25).
shutting_down = threading.Event()
scanning = False      # True while a band scan is running
guide_refreshing = False   # True while a PSIP guide refresh holds the tuner
# Set to ask a running guide refresh to stop at the next frequency boundary.
guide_abort = threading.Event()
# Set whenever a refresh finishes, so the scheduler re-arms on the new time.
guide_wakeup = threading.Event()
guide_status = {"last_error": None, "last_reason": None, "running": False,
                "deferred_by_lock": False}
# YouTube TV's listings, read from the web; see yttv_guide_worker.
yttv_guide_status = {"running": False, "last_error": None, "last_attempt": None,
                     "failed_channels": {}}
yttv_guide_wakeup = threading.Event()
always_on_wakeup = threading.Event()
_video_encoder_config = None
_disabled_video_encoders = set()
_stream_serial = 0


def busy_reason():
    """Why a tune request must be refused right now, or None."""
    if scanning:
        return "Channel scan in progress"
    if guide_refreshing:
        return "Guide refresh in progress"
    return None


def preempt_guide_refresh(timeout=8.0):
    """Cut a guide refresh short so a person asking for the tuner wins.

    The refresh is the only thing that takes the digital front-end without
    anyone asking, so it always yields. It checks the flag between
    frequencies and inside each section read, which puts the wait in the
    region of a second rather than the length of a sweep.
    """
    if not guide_refreshing:
        return
    guide_abort.set()
    deadline = time.monotonic() + timeout
    while guide_refreshing and time.monotonic() < deadline:
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Tuner locks
#
# Another service -- a recorder, a set-top holding a programme -- can claim a
# front-end so nothing retunes it out from under them.
#
# The lock guards *changes* only. The always-on keeper still restarts the
# locked channel if its encoder dies, because a lock whose stream cannot
# recover is worse than no lock at all. That is exactly why the lock is not
# part of busy_reason(): the keeper consults that, and folding the lock in
# would leave a crashed recording dead until the lock expired.
#
# Ownership is a token issued at acquire time. Anyone may see that a lock
# exists and who holds it, but only the holder's token releases it early --
# with one deliberate exception, an override from the web UI, which is logged.
# ---------------------------------------------------------------------------
LOCK_FILE = STREAMS_DIR / "tuner_locks.json"
# A lock is a promise to keep a tuner pointed somewhere. An unbounded one is a
# way to lose the tuner to a service that crashed without releasing it, so
# every lock has a ceiling and expires on its own.
LOCK_MAX_SECONDS = float(os.environ.get("TV_THING_LOCK_MAX_SECONDS", 6 * 3600))
LOCK_DEFAULT_SECONDS = float(
    os.environ.get("TV_THING_LOCK_DEFAULT_SECONDS", 3600)
)
# Broadcasts run over their listed end. A programme lock that expires exactly
# on the guide's timestamp drops the tuner during the last minute of the
# thing it was protecting.
LOCK_PROGRAM_GRACE_SECONDS = float(
    os.environ.get("TV_THING_LOCK_PROGRAM_GRACE_SECONDS", 120)
)

# lane -> lock entry. Guarded by `lock`, like the lane state it protects.
tuner_locks = {}


def load_tuner_locks():
    """Restore locks written before a restart, dropping those that expired.

    A service that claimed the tuner should not silently lose it because the
    server was restarted underneath it.
    """
    try:
        with open(LOCK_FILE) as f:
            stored = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    now = time.time()
    return {
        lane: entry for lane, entry in stored.items()
        if lane in ALL_LANES and isinstance(entry, dict)
        and entry.get("expires_at", 0) > now and entry.get("token")
    }


def save_tuner_locks():
    """Atomically persist the lock table."""
    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = LOCK_FILE.with_suffix(".tmp")
        with open(temp, "w") as f:
            json.dump(tuner_locks, f)
        os.replace(temp, LOCK_FILE)
    except OSError as error:
        # Losing the file costs the lock its restart-survival, not its
        # authority in this process, so this must never fail a request.
        print(f"Could not persist tuner locks: {error}")


def active_lock(lane, now=None):
    """The live lock on one lane, or None. Expired locks are dropped here.

    Expiry is lazy on purpose: a timer thread would be a second thing that
    could get the answer wrong.
    """
    with lock:
        now = time.time() if now is None else now
        entry = tuner_locks.get(lane)
        if entry is None:
            return None
        if entry.get("expires_at", 0) <= now:
            tuner_locks.pop(lane, None)
            save_tuner_locks()
            return None
        return entry


def lock_view(entry, now=None):
    """The public form of a lock: everything except the secret."""
    if not entry:
        return None
    now = time.time() if now is None else now
    return {
        "lane": entry["lane"],
        "owner": entry["owner"],
        "reason": entry.get("reason"),
        "channel_id": entry.get("channel_id"),
        "acquired_at": entry["acquired_at"],
        "expires_at": entry["expires_at"],
        "seconds_remaining": max(0, round(entry["expires_at"] - now)),
        "until": entry.get("until", "duration"),
        "program": entry.get("program"),
    }


def program_end_for(channel_id, now):
    """When the programme now airing on a channel ends, per the guide.

    Returns ``(expires_at, program)``, or ``(None, reason)`` when the guide
    cannot answer -- an unknown channel, or a station that sends no EIT.
    """
    channel = next(
        (item for item in load_channels() if item["id"] == channel_id), None
    )
    if channel is None:
        return None, "that channel is not in the channel list"
    event = epg.upcoming(epg.load_guide(), channel["number"], at=int(now))
    if not event:
        return None, (f"nothing is listed on {channel['number']} right now; "
                      "refresh the guide, or lock for a fixed time instead")
    return event["start"] + event["duration"] + LOCK_PROGRAM_GRACE_SECONDS, {
        "title": event.get("title"),
        "start": event["start"],
        "duration": event["duration"],
        "channel": channel["number"],
    }


def lock_holder(lane, token=None, override=False, now=None):
    """The lock standing in the way of a change, or None if the way is clear.

    Holding the token or overriding both clear the way; the difference is
    that one of them gets logged.
    """
    held = active_lock(lane, now)
    if held is None:
        return None
    if override or (token and secrets.compare_digest(str(token),
                                                     held["token"])):
        return None
    return held


def override_tuner_lock(lane):
    """Revoke an explicitly overridden lease while holding the tuner mutex."""
    held = active_lock(lane)
    if held:
        tuner_locks.pop(lane, None)
        save_tuner_locks()
        print(f"Tuner lock on {lane} overridden by tune/scan/stop "
              f"(was held by {held['owner']})")


def lock_refusal(held, action):
    """The legacy-shaped refusal body for a blocked change."""
    remaining = max(0, round(held["expires_at"] - time.time()))
    minutes = max(1, round(remaining / 60))
    return {
        "error": (f"{held['lane']} tuner is locked by {held['owner']} "
                  f"for about {minutes} more minute"
                  f"{'s' if minutes != 1 else ''}"),
        "action": action,
        "lock": lock_view(held),
    }, 423


def acquire_tuner_lock(body):
    """Claim a front-end for a service, or renew a claim it already holds."""
    if not isinstance(body, dict):
        body = {}
    lane = body.get("lane", LANE_DIGITAL)
    if lane not in ALL_LANES:
        return api_v1_error(
            f"lane must be one of {', '.join(ALL_LANES)}", 400, "invalid_lane"
        )
    owner = str(body.get("owner") or "").strip()
    if not owner:
        return api_v1_error(
            "owner is required: name the service claiming the tuner",
            400, "owner_required",
        )

    now = time.time()
    token = body.get("token")
    override = bool(body.get("override"))

    with lock:
        held = active_lock(lane, now)
        renewing = bool(
            held and token
            and secrets.compare_digest(str(token), held["token"])
        )
        if held and not renewing and not override:
            return api_v1_error(
                f"{lane} tuner is already locked by {held['owner']}",
                423, "locked", extra={"lock": lock_view(held, now)},
            )

        if lane == LANE_DIGITAL and busy_reason() and not renewing:
            return api_v1_error(busy_reason(), 409, "busy")
        until = body.get("until", "duration")
        program = None
        channel_id = body.get("channel_id")
        if until == "program_end" and lane == LANE_ROKU:
            return api_v1_error(
                "the Roku has no programme guide; lock it for a duration",
                400, "invalid_until",
            )
        if until == "program_end":
            if not channel_id and lane == LANE_DIGITAL:
                channel_id = (feed_client.lane(lane)["selection"] or {}).get("id")
            if not channel_id:
                return api_v1_error(
                    "locking until the end of a programme needs a channel_id, "
                    "and nothing is tuned on that lane",
                    400, "channel_required",
                )
            requested, program = program_end_for(channel_id, now)
            if requested is None:
                return api_v1_error(
                    f"cannot lock until the end of a programme: {program}",
                    409, "no_programme",
                )
        elif until == "duration":
            try:
                seconds = float(
                    body.get("duration_seconds", LOCK_DEFAULT_SECONDS)
                )
            except (TypeError, ValueError):
                return api_v1_error(
                    "duration_seconds must be a number", 400,
                    "invalid_duration",
                )
            if not math.isfinite(seconds) or seconds <= 0:
                return api_v1_error(
                    "duration_seconds must be positive", 400,
                    "invalid_duration",
                )
            requested = now + seconds
        else:
            return api_v1_error(
                'until must be "duration" or "program_end"', 400,
                "invalid_until",
            )

        expires_at = min(requested, now + LOCK_MAX_SECONDS)
        entry = {
            "lane": lane,
            "owner": owner,
            "token": held["token"] if renewing else secrets.token_urlsafe(24),
            "reason": body.get("reason"),
            "channel_id": channel_id,
            "acquired_at": held["acquired_at"] if renewing else now,
            "expires_at": expires_at,
            "until": until,
            "program": program,
        }
        overrode = bool(held and not renewing)
        tuner_locks[lane] = entry
        save_tuner_locks()

    if overrode:
        print(f"Tuner lock on {lane} overridden: "
              f"{held['owner']} -> {owner}")
    return {
        "api_version": API_VERSION,
        "ok": True,
        "renewed": renewing,
        "overrode": overrode,
        # Say so rather than silently handing back a shorter lock than asked.
        "truncated": expires_at < requested,
        "max_seconds": LOCK_MAX_SECONDS,
        "token": entry["token"],
        "lock": lock_view(entry, now),
    }, 200


def release_tuner_lock(body):
    """Release a lock early. Only the holder, or a deliberate override, may."""
    if not isinstance(body, dict):
        body = {}
    lane = body.get("lane", LANE_DIGITAL)
    if lane not in ALL_LANES:
        return api_v1_error(
            f"lane must be one of {', '.join(ALL_LANES)}", 400, "invalid_lane"
        )
    token = body.get("token")
    override = bool(body.get("override"))

    with lock:
        held = active_lock(lane)
        if held is None:
            return {
                "api_version": API_VERSION, "ok": True, "released": False,
                "overrode": False, "lane": lane, "lock": None,
                "note": "no lock was held",
            }, 200
        owns = bool(token and secrets.compare_digest(str(token),
                                                     held["token"]))
        if not owns and not override:
            return api_v1_error(
                f"{lane} tuner is locked by {held['owner']}; releasing it "
                "early needs that service's token",
                403, "not_lock_owner", extra={"lock": lock_view(held)},
            )
        tuner_locks.pop(lane, None)
        save_tuner_locks()

    if not owns:
        print(f"Tuner lock on {lane} released by override "
              f"(was held by {held['owner']})")
    return {
        "api_version": API_VERSION, "ok": True, "released": True,
        "overrode": not owns, "lane": lane,
        "released_lock": lock_view(held), "lock": None,
    }, 200


def tuner_lock_payload():
    """Every live lock, for status documents and the web UI."""
    now = time.time()
    with lock:
        return {
            lane: lock_view(active_lock(lane, now), now)
            for lane in ALL_LANES
        }


def load_channels():
    if CHANNELS_FILE.exists():
        with open(CHANNELS_FILE) as f:
            return json.load(f)
    return []


channel_lineup = lineup.Lineup(LINEUP_FILE, LINEUP_STATE_FILE)
# Every channel, whatever carries it: the lineup, the source catalog, the guide and tune
# iterate it (channels.py). Its adapters read this module when asked.
CHANNELS = channels.registry(sys.modules[__name__])


def resolve_internet_stream(stream):
    """The playlist URL a stream plays now, for the relay (the feed's). Finding it is control's:
    in one process it is found here; the feed role asks control's
    GET /internal/internet/<id>/resolve (internal_resolve). A fixed URL is in the file the feed
    reads itself, so it is never asked for: a fixed stream starts while control is down."""
    if ROLE == "feed" and not internet_streams.RESOLVERS[stream.resolver].fixed:
        return internet_streams.resolve_through(CONTROL_URL, stream)
    return internet_streams.resolve_stream(stream)


def internal_resolve(stream_id):
    """``(payload, status)`` for control's GET /internal/internet/<id>/resolve: the URL the
    stream plays now (internet_streams.resolve_stream). The URL carries the CDN's token, so
    this is never routed by Apache and answers this host only."""
    try:
        stream = web_streams.streams().get(stream_id)
    except internet_streams.StreamConfigError as error:
        return {"error": str(error)}, 500
    if stream is None:
        return {"error": f"No internet stream {stream_id!r}"}, 404
    try:
        return {"url": internet_streams.resolve_stream(stream)}, 200
    except internet_streams.StreamUnavailable as error:
        return {"error": str(error)}, error.status
    except HTTPError as error:
        return {"error": f"{stream.name}'s look-up answered {error.code}"}, 502
    except (URLError, OSError, TimeoutError, ValueError) as error:
        return {"error": f"{stream.name}'s look-up failed: {error}"}, 502


web_streams = internet_streams.InternetStreams(
    INTERNET_STREAMS_FILE, resolver=lambda stream: resolve_internet_stream(stream))


def antenna_on_dial(channels):
    """``[(lineup key, channel)]`` for the antenna's channels lineup.json does not hide."""
    return [(key, channel) for key, channel in zip(lineup.antenna_keys(channels), channels)
            if not channel_lineup.hides(lineup.ANTENNA, key)]


def numbered_channels(channels):
    """Antenna channels with their TV Thing numbers (tv_number, tv_name,
    tv_section). A channel lineup.json does not list -- one a rescan has
    just found -- is given the next number in its section here. One it hides
    is left out."""
    shown = antenna_on_dial(channels)
    views = channel_lineup.number(lineup.ANTENNA, [(key, channel.get("name")) for key, channel in shown])
    return [dict(channel, **(view or {})) for (_, channel), view in zip(shown, views)]


def internet_views():
    """The internet streams with their TV Thing numbers and the playlist
    each plays from; [] when internet_streams.json is unusable."""
    try:
        streams = [stream for stream in web_streams.streams().values()
                   if not channel_lineup.hides(internet_streams.SOURCE, stream.id)]
    except internet_streams.StreamConfigError as error:
        print(f"[Internet] {error}", flush=True)
        return []
    views = channel_lineup.number(internet_streams.SOURCE,
                                  [(stream.id, stream.name) for stream in streams])
    return [dict(view or {}, channel_id=stream.id, name=stream.name, note=stream.note,
                 stream_path=f"/stream/internet/{stream.id}/index.m3u8")
            for stream, view in zip(streams, views)]


def internet_guide_channels():
    """The internet streams that publish listings (``guide`` in internet_streams.json) as
    guide rows keyed ``internet/<id>``. A listing that does not answer leaves its row empty."""
    try:
        streams = web_streams.streams()
    except internet_streams.StreamConfigError:
        return {}
    channels = {}
    for view in internet_views():
        stream = streams.get(view["channel_id"])
        if stream is None or not stream.guide:
            continue
        key = f"internet/{stream.id}"
        channels[key] = {"key": key, "kind": "internet", "source_id": internet_streams.SOURCE,
                         "channel_id": stream.id, "name": stream.name, "note": stream.note,
                         **{field: view.get(field) for field in ("tv_number", "tv_name", "tv_section")},
                         "stream_path": view["stream_path"], "events": web_streams.listings(stream.id)}
    return channels


def numbered_roku_listings(listings):
    """Number the items of Roku catalogs, ``{source_id: listing}``, in place.

    A day's games are numbered together across every listing given, so a
    caller numbering one game source passes the sources it shares a block
    with (``channel_lineup.game_partners``) as well. Items lineup.json hides
    (a YouTube TV row off the dial) are left out.
    """
    for source_id, listing in listings.items():
        listing["items"] = [item for item in listing["items"]
                            if not channel_lineup.hides(source_id, str(item.get("id")))]
    pools = {source_id: listing["items"] for source_id, listing in listings.items()
             if source_id in roku_lane.GAME_SOURCES or source_id in roku_lane.LISTED_SOURCES}
    games = channel_lineup.number_games(pools)
    for source_id, listing in listings.items():
        if source_id in pools:
            views = [games.get((source_id, str(item.get("id")))) for item in listing["items"]]
        else:
            views = channel_lineup.number(source_id, [
                (item.get("id"), item.get("name")) for item in listing["items"]])
        listing["items"] = [dict(item, **(view or {}))
                            for item, view in zip(listing["items"], views)]
    return listings


def channel_sort_key(channel):
    """Primaries first by major number, then subchannels grouped by major."""
    try:
        major, minor = (int(part) for part in channel["number"].split("."))
    except (ValueError, KeyError):
        return (2, 0, 0)
    return (0, major, 0) if minor == 1 else (1, major, minor)


def channels_from_scan(results):
    """Build channels.json records from a native scan.

    One scan pass already carries everything a record needs: the PSIP name
    and virtual channel number, the service ID, and the elementary PIDs from
    the PMT.
    """
    channels = []
    for result in results:
        for service in result["virtual_channels"]:
            # Without elementary PIDs the tuner source cannot select the
            # program, so an unreadable PMT means the service is unusable.
            if not service.get("video_pid"):
                continue
            number = service["number"]
            slug = re.sub(r"[^a-zA-Z0-9]", "_", service["name"]).strip("_")
            channels.append({
                "name": service["name"],
                "frequency": result["frequency_hz"],
                "video_pid": service["video_pid"],
                "audio_pid": service["audio_pid"],
                "service_id": service["service_id"],
                "modulation": "VSB_8",
                "id": f"{slug.lower()}_{number.replace('.', '_')}",
                "number": number,
                "rf_channel": result["rf_channel"],
            })
    channels.sort(key=channel_sort_key)
    return channels


def save_channels(channels):
    """Atomically replace the channel list."""
    temp = CHANNELS_FILE.with_suffix(".tmp")
    with open(temp, "w") as f:
        json.dump(channels, f, indent=2)
    os.replace(temp, CHANNELS_FILE)


def load_playback_state():
    """Return the saved channel and quality, falling back to channel one."""
    channels = load_channels()
    if not channels:
        return None, "medium"

    saved = {}
    try:
        with open(PLAYBACK_STATE_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError, TypeError):
        pass

    channel_id = saved.get("channel_id")
    channel = next((c for c in channels if c["id"] == channel_id), channels[0])
    quality = saved.get("quality", "medium")
    if quality not in QUALITY_PRESETS:
        quality = "medium"
    return channel, quality


def save_playback_state(channel, quality):
    """Atomically remember what the always-on stream should resume."""
    try:
        STREAMS_DIR.mkdir(exist_ok=True)
        temp = PLAYBACK_STATE_FILE.with_suffix(".tmp")
        with open(temp, "w") as f:
            json.dump({"channel_id": channel["id"], "quality": quality}, f)
        os.replace(temp, PLAYBACK_STATE_FILE)
    except OSError as e:
        print(f"Could not save playback state: {e}")


def kill_process(proc, timeout=2):
    """Terminate a media child promptly, escalating if it ignores SIGTERM."""
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def allocate_udp_port(base=TV_UDP_PORT, span=32, exclude=()):
    """Return a free localhost UDP port for one encoder generation."""
    for offset in range(span):
        port = base + offset
        if port in exclude:
            continue
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            probe.close()
        return port
    return base


def next_stream_id():
    """Return a cache-safe identifier for one fresh encoder generation."""
    global _stream_serial
    _stream_serial += 1
    return f"{time.time_ns()}-{_stream_serial}"


def new_stream_health(stream_id):
    """Create the JSON-safe health record exposed by ``/api/status``."""
    return {
        "ok": True,
        "stream_id": stream_id,
        "reason": None,
        "duplicated_frames": 0,
        "udp_overruns": 0,
        "non_monotonous": 0,
        "max_segment_bytes": 0,
        "max_gpu_temp_c": None,
        "max_rss_mib": None,
    }


def record_packager_warning(line, health):
    """Record a dangerous ffmpeg warning and return a trip reason, if any."""
    duplicate = re.search(r"More than (\d+) frames duplicated", line)
    if duplicate:
        count = int(duplicate.group(1))
        health["duplicated_frames"] = max(
            health["duplicated_frames"], count
        )
        if count >= TV_MAX_DUPLICATED_FRAMES:
            return f"ffmpeg duplicated at least {count} frames"

    if "Circular buffer overrun" in line:
        health["udp_overruns"] += 1
        if health["udp_overruns"] >= TV_MAX_UDP_OVERRUNS:
            return f"ffmpeg UDP input overran {health['udp_overruns']} time(s)"

    if "Non-monotonous DTS" in line:
        health["non_monotonous"] += 1
        if health["non_monotonous"] >= TV_MAX_NON_MONOTONOUS:
            return (f"ffmpeg patched {health['non_monotonous']} timestamps "
                    "that went backwards")
    return None


def process_rss_mib(pid, proc_root=Path("/proc")):
    """A process's resident memory in MiB, or None once it has gone."""
    try:
        for line in (proc_root / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def trip_packager(packager, health, reason):
    """Stop an unhealthy packager once, before it can consume the host."""
    if not health.get("ok", True):
        return
    health["ok"] = False
    health["reason"] = reason
    print(f"TV stream safety trip: {reason}", flush=True)
    if packager.poll() is None:
        packager.terminate()


def read_nvidia_temperature():
    """Return the first NVIDIA GPU temperature, or None when unavailable."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi", "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return float(result.stdout.splitlines()[0].strip())
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    return None


def watch_hls_packager(packager, encoder, stream_id, health,
                       lane=LANE_DIGITAL):
    """Enforce bounded HLS growth, progress, and NVIDIA temperature.

    Each lane is watched against its own playlist and segment prefix, so a
    stalled analog stream never trips the digital one and vice versa.
    """
    segment_prefix = LANE_SEGMENT_PREFIX[lane]
    started = time.monotonic()
    playlist = lane_playlist_path(lane)
    last_playlist_mtime = None
    last_progress = started
    next_temperature_check = started
    pattern = f"{segment_prefix}_{stream_id}_*"

    while packager.poll() is None:
        time.sleep(TV_HEALTH_POLL_SECONDS)
        now = time.monotonic()

        rss = process_rss_mib(getattr(packager, "pid", None))
        if rss is not None:
            health["max_rss_mib"] = round(max(health["max_rss_mib"] or 0, rss), 1)
            if TV_MAX_PACKAGER_RSS_MIB > 0 and rss > TV_MAX_PACKAGER_RSS_MIB:
                trip_packager(
                    packager, health,
                    f"ffmpeg's memory reached {rss:.0f} MiB "
                    f"(limit {TV_MAX_PACKAGER_RSS_MIB:g} MiB)",
                )
                return

        for segment in STREAMS_DIR.glob(pattern):
            try:
                size = segment.stat().st_size
            except OSError:
                continue
            health["max_segment_bytes"] = max(
                health["max_segment_bytes"], size
            )
            if size > TV_MAX_SEGMENT_BYTES:
                trip_packager(
                    packager, health,
                    f"{segment.name} grew to {size} bytes "
                    f"(limit {TV_MAX_SEGMENT_BYTES})",
                )
                return

        try:
            playlist_mtime = playlist.stat().st_mtime_ns
        except OSError:
            playlist_mtime = None
        if playlist_mtime is not None and playlist_mtime != last_playlist_mtime:
            last_playlist_mtime = playlist_mtime
            last_progress = now

        if last_playlist_mtime is None:
            if now - started > TV_SEGMENT_STARTUP_TIMEOUT:
                trip_packager(
                    packager, health,
                    f"no HLS segment within {TV_SEGMENT_STARTUP_TIMEOUT:g}s",
                )
                return
        elif now - last_progress > TV_SEGMENT_STALL_TIMEOUT:
            trip_packager(
                packager, health,
                f"HLS playlist stalled for {now - last_progress:.1f}s",
            )
            return

        if (encoder["codec"] == "h264_nvenc"
                and TV_MAX_GPU_TEMP_C > 0
                and now >= next_temperature_check):
            temperature = read_nvidia_temperature()
            next_temperature_check = now + 2
            if temperature is not None:
                previous = health["max_gpu_temp_c"]
                health["max_gpu_temp_c"] = temperature if previous is None \
                    else max(previous, temperature)
                if temperature >= TV_MAX_GPU_TEMP_C:
                    trip_packager(
                        packager, health,
                        f"GPU reached {temperature:g} C "
                        f"(limit {TV_MAX_GPU_TEMP_C:g} C)",
                    )
                    return


def watch_tv_packager(packager, encoder, stream_id, health):
    """Compatibility wrapper for the digital packager resource watcher."""
    watch_hls_packager(packager, encoder, stream_id, health, LANE_DIGITAL)


def clear_streams(prefixes):
    for f in STREAMS_DIR.glob("*"):
        if f.name.startswith(prefixes):
            # ffmpeg's `temp_file` segments appear and vanish under us. A
            # FileNotFoundError here used to escape through stop_digital()
            # and kill the always-on thread, orphaning a tuner process that
            # then held the DVB frontend against every later tune.
            try:
                f.unlink()
            except OSError:
                pass


def lane_playlist_path(lane):
    return STREAMS_DIR / LANE_PLAYLIST[lane]


def lane_stream_ready(lane):
    """A current-generation playlist references at least one complete segment.

    This is media availability, not a guarantee about a client's buffer.
    """
    state = lanes[lane]
    if state["started_at"] is None or not state["stream_id"]:
        return False
    playlist = lane_playlist_path(lane)
    try:
        if playlist.stat().st_mtime < state["started_at"]:
            return False
        names = [line.strip() for line in playlist.read_text().splitlines()
                 if line.strip() and not line.startswith("#")]
        prefix = f"{LANE_SEGMENT_PREFIX[lane]}_{state['stream_id']}_"
        return bool(names) and all(
            Path(name).name == name and name.startswith(prefix)
            and name.endswith(".ts") and (STREAMS_DIR / name).stat().st_size > 0
            for name in names[-3:]
        )
    except (OSError, UnicodeError):
        return False


def lane_is_running(lane):
    """Whether this lane's encoder (or, for the Roku, its capture) is alive."""
    proc = procs[FEED_CONFIG.lane(lane).process]
    return proc is not None and proc.poll() is None


def lane_processes_alive(lane):
    """Whether every process the lane needs is alive: the digital lane is its tuner
    source and its encoder, the others one process each."""
    roles = ("digital_source", "digital_packager") if lane == LANE_DIGITAL \
        else (FEED_CONFIG.lane(lane).process,)
    return all(procs[role] is not None and procs[role].poll() is None for role in roles)


def reset_lane(lane, lifecycle="idle"):
    """Clear a lane's published state without touching its processes.

    ``lifecycle`` is what the lane is doing now: idle after a stop, error after
    a process died on its own, starting while a new generation comes up."""
    lanes[lane] = new_lane_state()
    feed.transition(lane, lifecycle)


def publish_legacy_playlist(lane):
    """Point the legacy /stream/live.m3u8 alias at a video lane."""
    global primary_video_lane
    if lane in VIDEO_LANES:
        primary_video_lane = lane


def stop_digital():
    """Stop the digital lane and release the DVB frontend."""
    feed.transition(LANE_DIGITAL, "stopping")
    source = procs["digital_source"]
    procs["digital_source"] = None
    kill_process(source, timeout=TV_SOURCE_STOP_TIMEOUT)

    packager = procs["digital_packager"]
    procs["digital_packager"] = None
    kill_process(packager, timeout=TV_PACKAGER_STOP_TIMEOUT)

    reset_lane(LANE_DIGITAL)
    clear_streams(LANE_FILES[LANE_DIGITAL])


def stop_analog():
    """Stop the analog TV lane and release the analog tuner."""
    feed.transition(LANE_ANALOG, "stopping")
    packager = procs["analog_packager"]
    procs["analog_packager"] = None
    kill_process(packager, timeout=TV_PACKAGER_STOP_TIMEOUT)

    reset_lane(LANE_ANALOG)
    clear_streams(LANE_FILES[LANE_ANALOG])


def stop_lane(lane):
    if lane == LANE_DIGITAL:
        stop_digital()
    elif lane == LANE_ANALOG:
        stop_analog()
    elif lane == LANE_ROKU:
        if roku is not None:
            roku.cancel()
        stop_roku()


def stop_all():
    stop_analog()
    stop_digital()
    stop_lane(LANE_ROKU)


def _probe_video_encoder(config):
    """Return whether ffmpeg can open a hardware encoder on this machine."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        *config.get("input_args", []),
        # Some hardware APIs (notably VideoToolbox) reject tiny diagnostic
        # frames even though normal broadcast resolutions work.
        "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=0.1",
        "-frames:v", "1",
    ]
    filters = config.get("filter_suffix", [])
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += [
        "-c:v", config["codec"], *config.get("encoder_args", []),
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def select_video_encoder():
    """Choose a working GPU H.264 encoder, with libx264 as the fallback."""
    global _video_encoder_config

    if _video_encoder_config is not None:
        return _video_encoder_config

    candidates = [
        {
            "codec": "h264_nvenc", "label": "NVIDIA NVENC",
            "encoder_args": [
                "-preset", "p2", "-tune", "ll", "-rc", "cbr",
                "-zerolatency", "1", "-delay", "0", "-bf", "0",
                "-forced-idr", "1",
            ],
        },
        {
            "codec": "h264_qsv", "label": "Intel Quick Sync",
            "encoder_args": ["-preset", "veryfast", "-bf", "0"],
        },
        {
            "codec": "h264_vaapi", "label": "VA-API",
            "input_args": ["-vaapi_device", VAAPI_DEVICE],
            "filter_suffix": ["format=nv12", "hwupload"],
            "encoder_args": ["-bf", "0"],
        },
        {
            "codec": "h264_videotoolbox", "label": "VideoToolbox",
            "encoder_args": ["-realtime", "1", "-bf", "0"],
        },
    ]

    aliases = {
        "nvenc": "h264_nvenc", "qsv": "h264_qsv",
        "vaapi": "h264_vaapi", "videotoolbox": "h264_videotoolbox",
    }
    requested = aliases.get(VIDEO_ENCODER, VIDEO_ENCODER)
    if requested not in ("auto", "cpu", "libx264"):
        candidates = [c for c in candidates if c["codec"] == requested]
    elif requested in ("cpu", "libx264"):
        candidates = []

    for config in candidates:
        if config["codec"] in _disabled_video_encoders:
            continue
        attempts = NVENC_PROBE_ATTEMPTS \
            if config["codec"] == "h264_nvenc" else 1
        for attempt in range(attempts):
            if _probe_video_encoder(config):
                config = dict(config)
                config["hardware"] = True
                _video_encoder_config = config
                print(f"Video encoder: {config['label']} ({config['codec']})")
                return config
            if attempt + 1 < attempts:
                time.sleep(NVENC_PROBE_RETRY_SECONDS)

    _video_encoder_config = {
        "codec": "libx264", "label": "x264 (CPU)", "hardware": False,
        "encoder_args": ["-preset", "veryfast", "-tune", "zerolatency"],
    }
    if requested not in ("auto", "cpu", "libx264"):
        print(f"Requested video encoder {requested} is unavailable; using libx264")
    else:
        print("No working GPU H.264 encoder found; using libx264")
    return _video_encoder_config


def build_tv_packager_command(quality, encoder=None, stream_id="test",
                              udp_port=TV_UDP_PORT):
    """Build one tune-scoped ffmpeg command for the digital lane."""
    encoder = encoder or select_video_encoder()
    preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["medium"])
    hls_path = str(lane_playlist_path(LANE_DIGITAL))
    segment_pattern = str(
        STREAMS_DIR
        / f"{LANE_SEGMENT_PREFIX[LANE_DIGITAL]}_{stream_id}_%09d.ts"
    )
    vb = preset["vb"]

    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        *encoder.get("input_args", []),
        "-fflags", "+discardcorrupt",
        # See recorder.BROADCAST_MAX_PIXELS: a corrupt header must not
        # allocate a gigabyte frame.
        "-max_pixels", recorder.BROADCAST_MAX_PIXELS,
        "-thread_queue_size", "4096",
        "-probesize", FFMPEG_PROBE_SIZE,
        "-analyzeduration", FFMPEG_ANALYZE_DURATION,
        # Decoder and filter threads; the encoder's follow -c:v.
        "-threads", FFMPEG_THREADS, "-filter_threads", FFMPEG_THREADS,
        "-i", tv_udp_input(udp_port),
        "-map", "0:v:0", "-map", "0:a:0?",
    ]

    # Deinterlace before an optional GPU upload. This supports interlaced ATSC
    # on every encoder while still moving the expensive H.264 encode to GPU.
    filters = ["bwdif=mode=send_frame:parity=auto:deint=interlaced"]
    if preset["scale"]:
        filters.append(f"scale=-2:{preset['scale']}")
    filters += encoder.get("filter_suffix", [])
    cmd += ["-vf", ",".join(filters)]
    cmd += [
        "-c:v", encoder["codec"], *encoder.get("encoder_args", []),
        "-threads", FFMPEG_THREADS,
        "-b:v", f"{vb}k", "-maxrate", f"{round(vb * 1.2)}k",
        "-bufsize", f"{vb * 2}k",
        # bwdif=send_frame keeps the usual 29.97 fps ATSC cadence. Kepler
        # NVENC may defer a forced keyframe until the GOP boundary, so align
        # the GOP itself with the HLS target instead of relying on force alone.
        "-g", str(max(1, round(HLS_SEGMENT_TIME * 30))),
        "-keyint_min", "1",
        "-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEGMENT_TIME:g})",
        # Preserve the station cadence. CFR gap filling is unsafe for live
        # broadcast timestamps because one discontinuity can synthesize an
        # unbounded number of duplicate frames.
        "-fps_mode", "passthrough",
        "-c:a", "aac", "-b:a", f"{preset['ab']}k", "-ac", "2",
        # No first_pts: a station switching its AC-3 between 5.1 and stereo makes ffmpeg
        # rebuild this filter, and with first_pts=0 the new one started at zero and filled
        # the encoder's whole run with silence at once -- 24,656 patched audio timestamps
        # and ~850 MiB kept on WJLA after 525 s (2026-10-26); reproduced offline, where
        # without it a switch patches 1. async alone still corrects drift.
        "-af", "aresample=async=1000",
        # A damaged/sparse AC-3 PES must not make the muxer queue video
        # indefinitely while waiting for an audio timestamp. Keep at most
        # half a second of A/V interleave skew and flush completed packets.
        "-max_interleave_delta", "500000",
        "-flush_packets", "1",
        "-f", "hls", "-hls_time", f"{HLS_SEGMENT_TIME:g}",
        "-hls_list_size", str(max(16, round(12 / HLS_SEGMENT_TIME))),
        "-hls_allow_cache", "0",
        "-hls_flags",
        "delete_segments+omit_endlist+independent_segments+temp_file",
        "-hls_segment_filename", segment_pattern,
        hls_path,
    ]
    return cmd


def build_analog_packager_command(device, quality, encoder=None,
                                  stream_id="test"):
    """Build a bounded V4L2/ALSA-to-HLS command for the analog tuner."""
    encoder = encoder or select_video_encoder()
    preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["medium"])
    playlist = str(lane_playlist_path(LANE_ANALOG))
    segment_pattern = str(
        STREAMS_DIR
        / f"{LANE_SEGMENT_PREFIX[LANE_ANALOG]}_{stream_id}_%09d.ts"
    )
    vb = preset["vb"]

    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        *encoder.get("input_args", []),
        "-fflags", "+discardcorrupt",
        # Raw V4L2 needs no decoder threads; see FFMPEG_THREADS.
        "-filter_threads", FFMPEG_THREADS,
        "-thread_queue_size", "1024",
        "-f", "v4l2", "-timestamps", "mono2abs",
        *device.get("input_args", []),
        "-i", device["device"],
    ]
    audio_device = device.get("audio_device")
    if audio_device:
        cmd += [
            "-thread_queue_size", "2048",
            "-f", "alsa", "-i", audio_device,
        ]

    cmd += ["-map", "0:v:0"]
    if audio_device:
        cmd += ["-map", "1:a:0?"]

    # V4L2 ``mono2abs`` and ALSA do not reliably begin in the same timestamp
    # domain. Rebase each input before sync/segmentation; otherwise ffmpeg can
    # try to bridge the initial epoch-sized gap, growing the first HLS segment
    # until the service's file-size guard kills it with SIGXFSZ. The deinterlace
    # remains a no-op for progressive webcams.
    filters = [
        "setpts=PTS-STARTPTS",
        "bwdif=mode=send_frame:parity=auto:deint=interlaced",
    ]
    if preset["scale"]:
        filters.append(f"scale=-2:{preset['scale']}")
    # The cx18 YUV node emits 4:2:2. Browsers and Roku expect H.264 4:2:0.
    filters.append("format=yuv420p")
    filters += encoder.get("filter_suffix", [])
    cmd += ["-vf", ",".join(filters)]
    cmd += [
        "-c:v", encoder["codec"], *encoder.get("encoder_args", []),
        "-threads", FFMPEG_THREADS,
        "-b:v", f"{vb}k", "-maxrate", f"{round(vb * 1.2)}k",
        "-bufsize", f"{vb * 2}k",
        "-g", str(max(1, round(HLS_SEGMENT_TIME * 30))),
        "-keyint_min", "1",
        "-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEGMENT_TIME:g})",
        # V4L2 drivers can emit a short burst with duplicate startup times.
        # VFR drops duplicate timestamps instead of asking the HLS muxer to
        # repair non-monotonic DTS values.
        "-fps_mode", "vfr",
    ]
    if audio_device:
        cmd += [
            "-c:a", "aac", "-b:a", f"{preset['ab']}k", "-ac", "2",
            "-af", (
                "asetpts=PTS-STARTPTS,"
                "aresample=async=1000:first_pts=0,asetpts=N/SR/TB"
            ),
        ]
    else:
        cmd += ["-an"]
    cmd += [
        "-max_interleave_delta", "500000",
        "-flush_packets", "1",
        "-f", "hls", "-hls_time", f"{HLS_SEGMENT_TIME:g}",
        "-hls_list_size", str(max(16, round(12 / HLS_SEGMENT_TIME))),
        "-hls_allow_cache", "0",
        "-hls_flags",
        "delete_segments+omit_endlist+independent_segments+temp_file",
        "-hls_segment_filename", segment_pattern,
        playlist,
    ]
    return cmd


def audio_capture_environment():
    """Use PulseAudio socket transport so LimitFSIZE does not kill ALSA.

    Every child that opens an ALSA capture device needs this: the default
    shared-memory transport creates a file larger than the service's
    LimitFSIZE, and the kernel kills the process with SIGXFSZ (exit -25).
    """
    environment = os.environ.copy()
    environment["PULSE_CLIENTCONFIG"] = str(PULSE_CLIENT_CONFIG)
    return environment


def build_tv_source_command(channel, udp_port=TV_UDP_PORT, tap_ports=()):
    """Build the direct demux-to-UDP source command."""
    taps = [arg for port in tap_ports for arg in ("--tap-port", str(port))]
    return [
        sys.executable,
        str(BASE_DIR / "dvb_source.py"),
        "--adapter", str(DIGITAL_ADAPTER),
        "--frequency", str(channel["frequency"]),
        "--service-id", str(channel.get("service_id", 1)),
        "--video-pid", str(channel["video_pid"]),
        "--audio-pid", str(channel["audio_pid"]),
        "--udp-host", "127.0.0.1",
        "--udp-port", str(udp_port),
        *taps,
    ]


def start_digital_packager(quality, stream_id, udp_port=TV_UDP_PORT):
    """Start the digital lane's tune-scoped encoder with safety monitoring."""
    encoder = select_video_encoder()
    cmd = build_tv_packager_command(quality, encoder, stream_id, udp_port)
    health = new_stream_health(stream_id)

    try:
        packager = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as e:
        return False, f"Could not start TV encoder: {e}"

    procs["digital_packager"] = packager
    lanes[LANE_DIGITAL].update({
        "health": health,
        "stream_id": stream_id,
        "video_encoder": encoder["codec"],
        "quality": quality,
    })

    def monitor():
        recent = deque(maxlen=50)
        if packager.stderr:
            for line in packager.stderr:
                line = line.rstrip()
                recent.append(line)
                print(f"digital ffmpeg: {line}", flush=True)
                reason = record_packager_warning(line, health)
                if reason:
                    trip_packager(packager, health, reason)
        packager.wait()
        print(f"Digital packager exited with code {packager.returncode}")
        if health["ok"] and packager.returncode not in (0, -9, 255):
            health["ok"] = False
            health["reason"] = f"ffmpeg exited with code {packager.returncode}"

        unexpected = False
        with lock:
            if procs["digital_packager"] is packager:
                procs["digital_packager"] = None
                source = procs["digital_source"]
                procs["digital_source"] = None
                kill_process(source, timeout=TV_SOURCE_STOP_TIMEOUT)
                reset_lane(LANE_DIGITAL, "error")
                unexpected = True
        if unexpected and encoder["hardware"]:
            # A probe can pass even when a real broadcast exposes a driver or
            # pixel-format limitation. Disable that backend for this process;
            # the always-on restart will select another GPU or libx264.
            global _video_encoder_config
            _disabled_video_encoders.add(encoder["codec"])
            _video_encoder_config = None
            print(f"Disabling failed GPU encoder {encoder['codec']}")
        if unexpected and ALWAYS_ON:
            always_on_wakeup.set()

    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(
        target=watch_tv_packager,
        args=(packager, encoder, stream_id, health),
        daemon=True,
    ).start()
    return True, None


def start_digital(channel, quality="medium", taps=None):
    """Start a fresh DVB tuner and encoder generation for one channel.

    Returns ``(ok, error, False)``. Restarting ffmpeg is intentional: each
    broadcaster has an independent MPEG clock, and those timestamp domains
    must never share CFR or encoder state.

    Only the digital front-end is touched, so an analog stream keeps
    running across a digital retune. ``taps`` are the recording tap ports
    the source feeds besides the encoder (by default, what this process's
    recordings need).
    """
    if shutting_down.is_set():
        return False, "The server is shutting down", False
    STREAMS_DIR.mkdir(exist_ok=True)

    # Stop both halves before opening the next timestamp domain. Marking the
    # process slots empty first makes their monitor threads treat this as an
    # intentional replacement rather than a crash.
    stop_digital()
    feed.transition(LANE_DIGITAL, "starting")
    stream_id = next_stream_id()
    # A fresh port per generation. Reusing one fixed port raced with the
    # previous encoder's socket on a fast retune, and ffmpeg lost the tune
    # to EADDRINUSE.
    udp_port = allocate_udp_port()
    ok, error = start_digital_packager(quality, stream_id, udp_port)
    if not ok:
        reset_lane(LANE_DIGITAL)
        return False, error, False

    taps = recording_tap_ports(channel) if taps is None else list(taps)
    source_cmd = build_tv_source_command(channel, udp_port, taps)
    lanes[LANE_DIGITAL]["started_at"] = time.time()

    def abandon(message):
        packager = procs["digital_packager"]
        procs["digital_packager"] = None
        kill_process(packager, timeout=TV_PACKAGER_STOP_TIMEOUT)
        reset_lane(LANE_DIGITAL)
        clear_streams(LANE_FILES[LANE_DIGITAL])
        return False, message, False

    try:
        source_proc = subprocess.Popen(
            source_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as e:
        return abandon(f"Could not start TV tuner: {e}")

    # Missing executables are caught by Popen. Poll once for an immediately
    # rejected invocation, but do not impose an unconditional delay on every
    # request; the monitor handles later source failures.
    if source_proc.poll() is not None:
        stderr = source_proc.stderr.read().strip() \
            if source_proc.stderr else ""
        last = stderr.split("\n")[-1] if stderr \
            else f"exit code {source_proc.returncode}"
        return abandon(f"Tuner failed to start: {last}")

    procs["digital_source"] = source_proc
    lanes[LANE_DIGITAL].update({
        "active": True,
        "selection": channel,
        "quality": quality,
        "tuner": {"backend": TV_SOURCE_BACKEND, "adapter": DIGITAL_ADAPTER},
        "taps": taps,
    })
    publish_legacy_playlist(LANE_DIGITAL)
    feed.transition(LANE_DIGITAL, "running", generation=stream_id)
    # Remember what the always-on keeper should resume after a restart.
    save_playback_state(channel, quality)

    def monitor():
        recent = deque(maxlen=20)
        if source_proc.stderr:
            for line in source_proc.stderr:
                line = line.rstrip()
                recent.append(line)
                print(f"dvb source: {line}", flush=True)
        source_proc.wait()
        print(f"DVB source exited with code {source_proc.returncode}")
        with lock:
            if procs["digital_source"] is source_proc:
                procs["digital_source"] = None
                packager = procs["digital_packager"]
                procs["digital_packager"] = None
                kill_process(packager, timeout=TV_PACKAGER_STOP_TIMEOUT)
                reset_lane(LANE_DIGITAL, "error")
                if ALWAYS_ON:
                    always_on_wakeup.set()

    threading.Thread(target=monitor, daemon=True).start()
    print(f"Digital: {channel['name']} @ {channel['frequency']} Hz")
    return True, None, False


def start_analog_packager(device, quality, cable_channel):
    """Start a fresh HLS generation on the analog tuner."""
    STREAMS_DIR.mkdir(exist_ok=True)
    stop_analog()
    clear_streams(LANE_FILES[LANE_ANALOG])
    feed.transition(LANE_ANALOG, "starting")

    encoder = select_video_encoder()
    stream_id = next_stream_id()
    health = new_stream_health(stream_id)
    command = build_analog_packager_command(
        device, quality, encoder, stream_id
    )
    lanes[LANE_ANALOG]["started_at"] = time.time()

    try:
        packager = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=audio_capture_environment(),
        )
    except OSError as exc:
        reset_lane(LANE_ANALOG)
        return False, f"Could not start analog encoder: {exc}"

    if packager.poll() is not None:
        stderr = packager.stderr.read().strip() if packager.stderr else ""
        last = stderr.split("\n")[-1] if stderr \
            else f"exit code {packager.returncode}"
        reset_lane(LANE_ANALOG)
        return False, f"Analog tuner failed to start: {last}"

    procs["analog_packager"] = packager
    lanes[LANE_ANALOG].update({
        "active": True,
        "selection": {"channel": cable_channel},
        "quality": quality,
        "stream_id": stream_id,
        "health": health,
        "video_encoder": encoder["codec"],
        "tuner": dict(device),
    })
    publish_legacy_playlist(LANE_ANALOG)
    feed.transition(LANE_ANALOG, "running", generation=stream_id)

    def monitor():
        if packager.stderr:
            for line in packager.stderr:
                line = line.rstrip()
                print(f"analog ffmpeg: {line}", flush=True)
                reason = record_packager_warning(line, health)
                if reason:
                    trip_packager(packager, health, reason)
        packager.wait()
        print(f"Analog packager exited with code {packager.returncode}")
        if health["ok"] and packager.returncode not in (0, -9, 255):
            health["ok"] = False
            health["reason"] = f"ffmpeg exited with code {packager.returncode}"
        with lock:
            if procs["analog_packager"] is packager:
                procs["analog_packager"] = None
                reset_lane(LANE_ANALOG, "error")

    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(
        target=watch_hls_packager,
        args=(packager, encoder, stream_id, health, LANE_ANALOG),
        daemon=True,
    ).start()
    return True, None


def active_or_discovered_analog_tuner():
    """Return the tuner in use, or the best idle one."""
    active = lanes[LANE_ANALOG]["tuner"]
    if active:
        return active
    tuners = analog_tuner.discover_analog_tuners()
    return tuners[0] if tuners else None


def start_analog(channel, quality="medium"):
    """Tune one fixed US cable channel on the analog front-end.

    The digital lane is untouched: the two front-ends are independent.
    """
    if shutting_down.is_set():
        return False, "The server is shutting down", None
    try:
        frequency_hz = analog_tuner.cable_frequency_hz(channel)
    except (TypeError, ValueError):
        return False, "Cable channel must be between 1 and 135", None

    # Some drivers allow QUERYCAP while busy but reject ENUMINPUT or S_INPUT,
    # so release the analog input before probing it.
    stop_analog()
    tuners = analog_tuner.discover_analog_tuners()
    if not tuners:
        return False, "No accessible analog TV tuner found", None
    tuner = tuners[0]

    try:
        analog_tuner.configure_tuner(
            tuner["device"],
            tuner["input_index"],
            tuner["tuner_index"],
            frequency_hz,
        )
    except OSError as exc:
        return False, f"Could not tune cable channel {channel}: {exc}", None

    ok, error = start_analog_packager(tuner, quality, int(channel))
    return ok, error, tuner


def always_on_worker():
    """Keep the saved digital channel running across failures.

    Only the digital lane is kept alive. Analog is an explicit, user-driven
    selection and is never auto-restarted; because it uses a different
    front-end it also never blocks the digital lane.
    """
    while True:
        always_on_wakeup.wait(timeout=5)
        always_on_wakeup.clear()
        if shutting_down.is_set():
            return

        if not ALWAYS_ON:
            continue

        ok, error = True, None
        try:
            with lock:
                digital = feed_client.lane(LANE_DIGITAL)
                # An unknown lane is one the feed could not vouch for just now,
                # not a dead one: control waking from a freeze reads unknown
                # while another thread refetches, and retuning on that
                # restarted a healthy lane (drill A3, 2026-10-27).
                # busy_reason() rather than the scanning flag: anything
                # holding the digital front-end -- a band scan, a guide
                # refresh -- means restarting the lane can only fail with
                # EBUSY, once every wakeup, for as long as it lasts.
                if digital["alive"] or digital.get("unknown") or busy_reason():
                    continue

                channel, quality = load_playback_state()
                if channel is None:
                    continue
                ok, error, _ = feed_client.tune_digital(
                    channel, quality, taps=recording_tap_ports(channel))
        except Exception as failure:   # noqa: BLE001 - keeper must not die
            # This thread is the only thing that brings the digital lane
            # back. If it ever raises, the lane stays down until someone
            # restarts the service, so absorb and retry instead.
            print(f"Always-on keeper error: {failure!r}")
            ok, error = False, str(failure)

        if not ok:
            print(f"Always-on restart failed: {error}")
            always_on_wakeup.set()
            time.sleep(5)


# ---------------------------------------------------------------------------
# The Roku lane
#
# The source Roku's HDMI goes through a component converter into a Hauppauge
# HD PVR, which encodes H.264 (CBR, 0.53 s GOP) and AAC itself. One ffmpeg is
# the only reader of that device: it copies the transport stream -- no
# transcode -- to the lane's HLS and to a private loopback UDP tap. The tap
# feeds the on-demand verification decoder (roku_control/lane_feedback.py)
# and nothing else. What the Roku is showing, and every navigation, is
# roku_lane.py's business.
#
# The capture is always on while the lane is configured: the HD PVR stays
# drained and the lane has a picture whatever the Roku shows, menus included.
# Whether that picture is a *verified* selection is the lane's selection and
# generation, not the stream's readiness.
# ---------------------------------------------------------------------------
ROKU_CONFIG_FILE = Path(os.environ.get(
    "TV_THING_ROKU_CONFIG", str(BASE_DIR / "roku.json")
))
ROKU_TAP_PORT = int(os.environ.get("TV_THING_ROKU_TAP_PORT", "18370"))
# One GOP per segment: the HD PVR keys every 0.533 s.
ROKU_HLS_SEGMENT_TIME = float(os.environ.get(
    "TV_THING_ROKU_HLS_SEGMENT_TIME", "0.5"
))
# How long a verified selection may be reused without any check.
ROKU_REUSE_MAX_AGE = float(os.environ.get(
    "TV_THING_ROKU_REUSE_MAX_AGE", "900"
))
ROKU_RESTART_BACKOFF = (2, 5, 10, 30)
V4L2_CTL = host_config.tool("v4l2-ctl", "/usr/bin/v4l2-ctl")
# A capture that has run this long is steady: its failures are forgotten.
ROKU_STEADY_SECONDS = 60

# Built in main() from roku.json; None when the lane is not configured. The capture needs only
# roku.json's feedback section (the HD PVR, its input), which the feed role loads on its own.
# A host whose settings turn the lane off (host_config: a second node, say) builds neither.
roku = None
roku_feedback = None
roku_wakeup = threading.Event()
# Replaced wholesale, never mutated, so roku_lane can read it without the
# server mutex (it calls back from threads that may already hold its own).
roku_capture_state = {"running": False, "stream_id": None, "error": None,
                      "tap_url": None}


def roku_tap_input(port=ROKU_TAP_PORT):
    # A short buffer (8192 packets, about 1.9 s at 6.5 Mbit/s): the verifier's
    # frames are evidence of what is on screen *now*. A decoder too slow to
    # keep up loses packets rather than drifting seconds behind the Roku.
    return (f"udp://127.0.0.1:{port}?fifo_size=8192&buffer_size=1048576"
            "&overrun_nonfatal=1&timeout=5000000")


def roku_capture_source():
    """The capture as roku_lane sees it. Lock-free on purpose (see above)."""
    state = roku_capture_state
    proc = procs.get("roku_capture")
    running = bool(state["running"] and proc is not None
                   and proc.poll() is None)
    return {**state, "running": running}


def roku_device():
    return roku_feedback.device if roku_feedback is not None else None


def build_roku_capture_command(device, stream_id, tap_port=ROKU_TAP_PORT):
    """Copy the HD PVR's transport stream to HLS and to the verifier tap."""
    playlist = str(lane_playlist_path(LANE_ROKU))
    segment_pattern = str(
        STREAMS_DIR / f"{LANE_SEGMENT_PREFIX[LANE_ROKU]}_{stream_id}_%09d.ts"
    )
    # A newly opened HD PVR can start before its codec headers; 32 KiB of
    # probe exits with "unknown codec" (seen in vizelVision), so allow ~3 s.
    copy_streams = ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                    # Parameter sets ahead of every keyframe: a player or
                    # decoder joining mid-stream needs no out-of-band headers.
                    "-bsf:v", "dump_extra=freq=keyframe"]
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-probesize", "2097152", "-analyzeduration", "3000000",
        "-f", "mpegts", "-i", device,
        *copy_streams,
        "-f", "hls",
        "-hls_time", str(ROKU_HLS_SEGMENT_TIME),
        "-hls_list_size", "20",
        "-hls_allow_cache", "0",
        "-hls_flags",
        "delete_segments+omit_endlist+independent_segments+temp_file",
        "-hls_segment_filename", segment_pattern,
        playlist,
        *copy_streams,
        "-f", "mpegts", f"udp://127.0.0.1:{tap_port}?pkt_size=1316",
    ]


def select_roku_input(device, input_index):
    """Pick the HD PVR's input before the reader opens it (component = 0)."""
    if input_index is None:
        return
    try:
        subprocess.run(
            [V4L2_CTL, "--device", device, f"--set-input={input_index}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"Could not select the HD PVR input: {error}")


def start_roku_capture():
    """Start a fresh capture generation. Returns (ok, error)."""
    global roku_capture_state
    if roku_feedback is None:
        return False, "The Roku lane is not configured"
    if shutting_down.is_set():
        return False, "The server is shutting down"
    device = roku_device()
    STREAMS_DIR.mkdir(exist_ok=True)
    with lock:
        if lane_is_running(LANE_ROKU):
            return True, None
        stream_id = next_stream_id()
        clear_streams(LANE_FILES[LANE_ROKU])
        reset_lane(LANE_ROKU, "starting")
        lanes[LANE_ROKU]["started_at"] = time.time()
    select_roku_input(device, roku_feedback.input_index)
    health = new_stream_health(stream_id)
    try:
        capture = subprocess.Popen(
            build_roku_capture_command(device, stream_id),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True,
        )
    except OSError as exc:
        with lock:
            reset_lane(LANE_ROKU)
        return False, f"Could not start the Roku capture: {exc}"
    with lock:
        procs["roku_capture"] = capture
        lanes[LANE_ROKU].update({
            "active": True,
            "quality": "source",
            "stream_id": stream_id,
            "health": health,
            "video_encoder": "copy",
            "tuner": {"device": device,
                      "name": roku_feedback.source_name},
        })
        roku_capture_state = {"running": True, "stream_id": stream_id,
                              "error": None,
                              "tap_url": roku_tap_input(ROKU_TAP_PORT)}
        feed.transition(LANE_ROKU, "running", generation=stream_id)

    def monitor():
        global roku_capture_state
        if capture.stderr:
            for line in capture.stderr:
                line = line.rstrip()
                print(f"roku ffmpeg: {line}", flush=True)
        capture.wait()
        print(f"Roku capture exited with code {capture.returncode}")
        if health["ok"] and capture.returncode not in (0, -9, -15, 255):
            health["ok"] = False
            health["reason"] = f"ffmpeg exited with code {capture.returncode}"
        with lock:
            if procs["roku_capture"] is capture:
                procs["roku_capture"] = None
                roku_capture_state = {
                    "running": False, "stream_id": None,
                    "error": health.get("reason"), "tap_url": None,
                }
                reset_lane(LANE_ROKU, "error")
        roku_wakeup.set()

    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(
        target=watch_hls_packager,
        args=(capture, {"codec": "copy"}, stream_id, health, LANE_ROKU),
        daemon=True,
    ).start()
    return True, None


def stop_roku():
    """Stop the Roku capture. The keeper starts a fresh generation."""
    global roku_capture_state
    feed.transition(LANE_ROKU, "stopping")
    capture = procs["roku_capture"]
    procs["roku_capture"] = None
    roku_capture_state = {"running": False, "stream_id": None,
                          "error": None, "tap_url": None}
    kill_process(capture, timeout=TV_PACKAGER_STOP_TIMEOUT)
    reset_lane(LANE_ROKU)
    clear_streams(LANE_FILES[LANE_ROKU])
    roku_wakeup.set()


def roku_keeper():
    """Keep the Roku capture running, backing off while it keeps failing.

    A capture that dies on its own within ROKU_STEADY_SECONDS of starting is a failure too.
    With the HD PVR gone, ffmpeg starts and exits at once; counting only failed starts, the
    keeper restarted it about nine times a second (2026-10-25)."""
    failures, started = 0, None
    while True:
        roku_wakeup.wait(timeout=5)
        roku_wakeup.clear()
        if shutting_down.is_set():
            return
        if roku_feedback is None:
            continue
        try:
            if lane_is_running(LANE_ROKU):
                if started is not None and time.time() - started > ROKU_STEADY_SECONDS:
                    failures = 0
                continue
            # Died young: on its own (the lane is in error, not stopped), soon after starting.
            if (started is not None and time.time() - started < ROKU_STEADY_SECONDS
                    and feed.view(LANE_ROKU)["lifecycle"] == "error"):
                failures += 1
                print(f"Roku capture died {time.time() - started:.0f} s after starting")
            started = None
            if failures:
                time.sleep(ROKU_RESTART_BACKOFF[min(failures, len(ROKU_RESTART_BACKOFF)) - 1])
                if shutting_down.is_set():
                    return
            ok, error = start_roku_capture()
            if ok:
                started = time.time()
        except Exception as failure:   # noqa: BLE001 - keeper must not die
            ok, error = False, repr(failure)
        if not ok:
            failures += 1
            print(f"Roku capture restart failed: {error}")
            roku_wakeup.set()


def roku_unavailable_message():
    """Why there is no Roku lane: not configured, or turned off on this host."""
    setting = HOST.roku(ROKU_CONFIG_FILE)
    if ROKU_CONFIG_FILE.is_file() and setting.value == "off":
        return f"The Roku lane is off on {FEED_CONFIG.node} ({setting.reason})"
    return "The Roku lane is not configured"


def roku_unavailable():
    return api_v1_error(roku_unavailable_message(), 503, "roku_unavailable")


def roku_call(action):
    """Run one roku_lane action and map its errors onto API responses."""
    if roku is None:
        return roku_unavailable()
    try:
        return action()
    except RokuStreamError as exc:
        return roku_error(exc)


def roku_error(exc):
    """The API response for a roku_lane / roku_control error."""
    try:
        raise exc
    except ChannelNotFoundError as exc:
        return api_v1_error(str(exc), 404, "unknown_channel",
                            extra={"details": exc.details})
    except ConfigError as exc:
        return api_v1_error(str(exc), 400, exc.code)
    except ControlBusyError as exc:
        return api_v1_error(str(exc), 409, "roku_busy")
    except BaseballScheduleError as exc:
        return api_v1_error(str(exc), 502, exc.code)
    except EcpError as exc:
        return api_v1_error(str(exc), 502, "roku_unreachable")
    except RokuStreamError as exc:
        return api_v1_error(str(exc), 500, exc.code)


def roku_change_refusal(body, action):
    """The roku lock standing in the way of a change, as a response, or None.

    An override revokes the lock, like a digital tune's override does.
    """
    body = body if isinstance(body, dict) else {}
    token = body.get("token")
    override = bool(body.get("override"))
    with lock:
        held = lock_holder(LANE_ROKU, token, override)
        if held:
            return api_v1_error(
                f"roku is locked by {held['owner']}", 423, "locked",
                extra={"lock": lock_view(held), "action": action},
            )
        if override:
            override_tuner_lock(LANE_ROKU)
    return None


def perform_roku_tune(body):
    request_started = time.monotonic()
    if roku is None:
        return roku_unavailable()
    refusal = roku_change_refusal(body, "tune")

    def tune():
        if refusal:
            # A locked Roku can still be watched: asking for what it already
            # shows moves nothing. Anything else is the holder's to change.
            watched = roku.watch(body)
            if watched is None:
                return refusal
            status, result = watched
        else:
            status, result = roku.tune(body)
        if status == 409:
            return api_v1_error(result["error"], 409, result["code"])
        lane = lane_payload(LANE_ROKU)
        return {
            "api_version": API_VERSION,
            "ok": True,
            "lane": LANE_ROKU,
            "source": {"id": body.get("source"), "kind": body.get("source"),
                       "name": lane["name"]},
            "selection": lane["selection"],
            "quality": lane["quality"],
            "stream": lane["stream"],
            "encoder": lane["encoder"],
            "reused": result.get("reused", False),
            "resumed": result.get("resumed", False),
            "stream_reused": result.get("reused", False),
            "operation": result.get("operation") or lane["roku"]["operation"],
            "active_lanes": feed_client.running_lanes(),
            "request_ms": round((time.monotonic() - request_started) * 1000, 1),
        }, status
    return roku_call(tune)


def roku_remote(body, action):
    """A remote action (key, text, app), after the roku lock allows it."""
    refusal = roku_change_refusal(body, action)
    if refusal:
        return refusal

    def run():
        if action == "keypress":
            result = roku.keypress(body.get("key"))
        elif action == "text":
            result = roku.text(body.get("text"))
        else:
            result = roku.launch(body.get("app_id"))
        return {"api_version": API_VERSION, "ok": True, **result,
                "generation": roku.generation()}, 200
    return roku_call(run)


def roku_cancel(body):
    refusal = roku_change_refusal(body, "cancel")
    if refusal:
        return refusal
    return roku_call(lambda: (
        {"api_version": API_VERSION, "ok": True, **roku.cancel()}, 200))


def roku_check(_body):
    # The check never presses a key, so it needs no lock.
    return roku_call(lambda: (
        {"api_version": API_VERSION, "ok": True, **roku.check(),
         "generation": roku.generation()}, 200))


def roku_sources_available():
    if roku is None:
        return False
    try:
        return Path(roku_device()).exists()
    except (TypeError, OSError):
        return False


def api_v1_roku_source(source_id, requested_date=None):
    def catalog():
        listing = roku.catalog(source_id, requested_date)
        listings = {source_id: listing}
        # A day's games are numbered across the whole block, listed sources (NHL) too,
        # so a game has the same number here as in the guide.
        for partner in channel_lineup.game_partners(source_id):
            if partner != source_id and (partner in roku_lane.SOURCES
                                         or partner in roku_lane.LISTED_SOURCES):
                try:
                    listings[partner] = roku.catalog(partner, listing.get("date") or requested_date)
                except Exception:   # noqa: BLE001 - numbering never fails the catalog asked for
                    pass    # Numbered without it; its own request will say why.
        numbered_roku_listings(listings)
        available = roku_sources_available()
        source = next(item for item in (*roku.source_list(available),
                                        *roku.listed_source_list(available))
                      if item["id"] == source_id)
        return {
            "api_version": API_VERSION,
            "source": dict(source, count=len(listing["items"]),
                           stream_path=f"/stream/{LANE_PLAYLIST[LANE_ROKU]}"),
            **listing,
        }, 200
    return roku_call(catalog)


def api_v1_roku_payload():
    """The Roku engineering view: capture, verifier, Roku and operations."""
    if roku is None:
        return {"api_version": API_VERSION, "configured": False}, 200
    from roku_control.calibration import calibration_status
    try:
        calibration = calibration_status(roku.feedback)
    except Exception as exc:   # noqa: BLE001 - diagnostics never fail the page
        calibration = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "api_version": API_VERSION,
        "configured": True,
        "lane": lane_payload(LANE_ROKU),
        "roku_url": roku.ecp.base_url,
        "calibration": calibration,
        "remote_keys": list(roku_lane.REMOTE_KEYS),
        "channels": [channel.name for channel in roku.config.channels],
    }, 200


def roku_calibrate(body):
    """The controller's calibrations, which can move the Roku: lock-checked."""
    refusal = roku_change_refusal(body, "calibrate")
    if refusal:
        return refusal
    kind = body.get("kind")
    channel = body.get("channel")
    controller = roku.controller if roku is not None else None

    def run():
        if kind == "guide":
            result = controller.calibrate(channel)
        elif kind == "verify":
            result = controller.verify_current_row(channel)
        elif kind == "network":
            result = controller.calibrate_network(channel)
        elif kind == "current-network":
            result = controller.calibrate_current_network(channel)
        elif kind == "profile":
            result = controller.calibrate_profile()
        elif kind == "sunday-ticket":
            _target, game = roku.normalize({
                "source_id": roku_lane.SUNDAY_TICKET,
                "channel_id": body.get("game_id"), "date": body.get("date"),
            })
            result = controller.calibrate_sunday_ticket(game)
        else:
            raise ConfigError(
                "kind must be guide, verify, network, current-network, "
                "profile or sunday-ticket")
        return {"api_version": API_VERSION, "ok": True,
                "result": json.loads(json.dumps(result, default=str))}, 200
    return roku_call(run)


# ---------------------------------------------------------------------------
# Recording (the VCR)
#
# A recording borrows the digital lane the way a VCR borrows the TV: it tunes
# the lane to its channel, holds the tuner lock for the length of the
# programme, and asks dvb_source.py to copy the transport stream to a tap
# port that a dedicated ffmpeg encodes into the DLNA library. Live viewers
# keep watching -- they simply see what is being taped.
#
# One thread, recording_worker(), starts and stops every recorder process
# (split, it asks the feed, whose _recorder_spawner() starts each one).
# HTTP handlers only edit the schedule and wake it, so nothing that can block
# (tuning, preempting a guide refresh, remuxing) ever runs while a request
# holds the server mutex. See recorder.py for how the file is encoded.
# ---------------------------------------------------------------------------
RECORDINGS_DIR = recorder.RECORDINGS_DIR
RECORDINGS_FILE = STREAMS_DIR / "recordings.json"
RECORDING_OWNER = "VCR"
RECORD_TAP_PORT_BASE = int(os.environ.get(
    "TV_THING_RECORD_TAP_PORT", str(TV_UDP_PORT + 64)
))
# Refuse to start a recording below this much free space, and stop one that
# is running below the second figure, rather than fill the disk MiniDLNA and
# the rest of the host live on.
RECORDING_MIN_FREE_BYTES = int(float(os.environ.get(
    "TV_THING_RECORD_MIN_FREE_GB", "5")) * 1024 ** 3)
RECORDING_STOP_FREE_BYTES = int(float(os.environ.get(
    "TV_THING_RECORD_STOP_FREE_GB", "1")) * 1024 ** 3)
# The unit caps every file at 16 MB (LimitFSIZE) so a runaway HLS segment
# dies early. Recorder processes alone raise their soft limit to this ceiling,
# which needs the unit's hard limit to allow it.
RECORDING_MAX_BYTES = int(float(os.environ.get(
    "TV_THING_RECORD_MAX_GB", "16")) * 1024 ** 3)
RECORDING_MIN_FILE_LIMIT = 2 * 1024 ** 3
RECORDING_MAX_SECONDS = LOCK_MAX_SECONDS
RECORDING_RETRY_SECONDS = float(os.environ.get(
    "TV_THING_RECORD_RETRY_SECONDS", "15"))
RECORDING_STALL_SECONDS = 30
# A recorder that dies is restarted into another piece while the window is
# still open, but not forever.
RECORDING_MAX_ATTEMPTS = 5
RECORDER_STOP_TIMEOUT = 8.0
# What the feed keeps across restarts, outside the streams directory (which a
# deploy may clean or replace): the recordings journal. The checkout's state/ (git-ignored)
# in production, split or not, so either build reads what the other wrote.
STATE_DIR = Path(os.environ.get("TV_THING_STATE_DIR", str(BASE_DIR / "state")))
RECORDINGS_JOURNAL = STATE_DIR / "feed" / "recordings-journal.jsonl"
# The feed's recorder processes (feed_node.Recorders); the timers, pieces and
# finalizing below are control's.
recorders = feed_node.Recorders(RECORDINGS_JOURNAL, spawn=lambda command: _spawn_recorder(command),
                                stop_timeout=RECORDER_STOP_TIMEOUT)
ACTIVE_OR_FINISHING = (*recorder.ACTIVE, "finishing")

recording_schedule = None       # loaded on first use; guarded by `lock`
active_recording = None         # owned by recording_worker(): the digital tape
roku_recording = None           # ...and the Roku lane's, which can run alongside
recording_wakeup = threading.Event()
_recording_retry_at = {}        # timer id -> monotonic time of next attempt
_rules_expanded_for = None      # guide generated_at the rules last saw


def load_recording_schedule():
    try:
        with open(RECORDINGS_FILE) as f:
            return recorder.normalize_schedule(json.load(f))
    except (OSError, ValueError, TypeError):
        return recorder.empty_schedule()


def recording_state():
    """The schedule, loaded on first use. Call with `lock` held."""
    global recording_schedule
    if recording_schedule is None:
        recording_schedule = load_recording_schedule()
    return recording_schedule


def save_recording_schedule():
    """Atomically persist the schedule. Call with `lock` held."""
    try:
        RECORDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = RECORDINGS_FILE.with_suffix(".tmp")
        with open(temp, "w") as f:
            json.dump(recording_state(), f)
        os.replace(temp, RECORDINGS_FILE)
    except OSError as error:
        print(f"Could not persist recording schedule: {error}")


def find_timer(timer_id):
    return next((timer for timer in recording_state()["timers"]
                 if timer["id"] == timer_id), None)


def current_recording(lane):
    """The recording running on one lane, or None."""
    return roku_recording if lane == LANE_ROKU else active_recording


def _set_current_recording(lane, current):
    global active_recording, roku_recording
    if lane == LANE_ROKU:
        roku_recording = current
    else:
        active_recording = current


def running_recordings():
    return [current for current in (active_recording, roku_recording)
            if current is not None]


def recording_tap_ports(channel):
    """Tap ports a digital source for ``channel`` must feed right now."""
    current = active_recording
    if current and current["tap_port"] and channel \
            and current["channel_id"] == channel.get("id"):
        return [current["tap_port"]]
    return []


def file_size_limit():
    """The hard RLIMIT_FSIZE a recorder can raise itself to, in bytes."""
    _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    return None if hard == resource.RLIM_INFINITY else hard


def recording_storage_status():
    """Whether recordings can be written, and what to do if not."""
    problems = []
    free = None
    directory = RECORDINGS_DIR
    if not directory.is_dir():
        problems.append({
            "code": "missing_dir",
            "message": f"{directory} does not exist",
            "fix": (f'sudo install -d -o {os.environ.get("USER", "vivian")} '
                    f'-g minidlna -m 2775 "{directory}"'),
        })
    elif not os.access(directory, os.W_OK | os.X_OK):
        problems.append({
            "code": "not_writable",
            "message": f"{directory} is not writable by this service",
            "fix": (f'sudo chown {os.environ.get("USER", "vivian")}:minidlna '
                    f'"{directory}" && sudo chmod 2775 "{directory}"'),
        })
    else:
        try:
            free = shutil.disk_usage(directory).free
        except OSError:
            free = None
        if free is not None and free < RECORDING_MIN_FREE_BYTES:
            problems.append({
                "code": "low_space",
                "message": (f"only {free / 1024 ** 3:.1f} GB free; recording "
                            f"needs {RECORDING_MIN_FREE_BYTES / 1024 ** 3:g}"),
                "fix": "erase recordings or free space on that disk",
            })
    limit = file_size_limit()
    if limit is not None and limit < RECORDING_MIN_FILE_LIMIT:
        problems.append({
            "code": "file_size_limit",
            "message": (f"the service caps files at {limit / 1024 ** 2:.0f} MB "
                        "(LimitFSIZE), which would cut a recording short"),
            "fix": ("set LimitFSIZE=16M:16G in tv-thing.service, then "
                    "systemctl daemon-reload and restart"),
        })
    return {
        "directory": str(directory),
        "ready": not problems,
        "free_bytes": free,
        "problems": problems,
    }


PR_SET_PDEATHSIG = 1


def _recorder_preexec():
    """preexec_fn for recorder children.

    Lets one recorder write a full-length file, and no more, past the unit's
    16 MB soft cap. And ties it to the server's life: ffmpeg reading a UDP
    tap waits forever for packets, so a recorder orphaned by a crashed or
    killed server outside systemd -- a staging gate, say -- would otherwise
    run unbounded. The death signal follows the spawning *thread*, not the
    process, so recorders are only started from _recorder_spawner(), which
    lives as long as the process; the remux runs synchronously in its
    caller's thread, which outlives it.
    """
    _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    ceiling = RECORDING_MAX_BYTES if hard == resource.RLIM_INFINITY \
        else min(RECORDING_MAX_BYTES, hard)
    resource.setrlimit(resource.RLIMIT_FSIZE, (ceiling, hard))
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(
            PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def recording_view(timer, now=None):
    """The public form of a timer."""
    now = time.time() if now is None else now
    view = {key: value for key, value in timer.items()
            if key not in ("part", "lock_token", "pieces", "stem")}
    view["seconds_until_start"] = max(0, round(timer["start"] - now))
    view["seconds_remaining"] = max(0, round(timer["end"] - max(now, timer["start"])))
    view["lane"] = recorder.timer_lane(timer)
    current = current_recording(view["lane"])
    if current and current["timer_id"] == timer["id"]:
        size = 0
        for part in [*map(Path, timer.get("pieces") or []), current["part"]]:
            try:
                size += part.stat().st_size
            except OSError:
                pass
        view["bytes"] = size
        view["elapsed_seconds"] = round(now - current["started_at"])
        view["signal"] = current["signal"]
    return view


def recording_inputs():
    """The VCR's inputs: every lane that can record, and what it can tape.

    The VCR page builds its SOURCE menu from this list, so another tuner or
    a capture card that learns to record appears there by being listed
    here. Each source is a catalog under /api/v1/sources; ``modes`` are the
    POST api/v1/recordings modes the input takes; ``held_by`` is whoever
    other than the VCR holds the lane, since a timer waits for them (or,
    with ``shares_with_holder``, tapes alongside a holder already showing
    its target); ``lead_seconds`` is how long a timer can take to reach its
    target before the tape starts.
    """
    def held_by(lane):
        entry = active_lock(lane)
        return entry["owner"] if entry and entry["owner"] != RECORDING_OWNER \
            else None

    inputs = [{
        "lane": LANE_DIGITAL,
        "name": "Digital tuner",
        "available": True,
        "modes": ["manual", "guide", "otr"],
        "lead_seconds": 0,
        "shares_with_holder": False,
        "held_by": held_by(LANE_DIGITAL),
        "sources": [{"id": "digital", "name": "Broadcast", "dated": False,
                     "typical_seconds": None,
                     "items_path": "/api/v1/sources/digital"}],
    }]
    if roku is not None:
        available = roku_sources_available()
        inputs.append({
            "lane": LANE_ROKU,
            "name": "Roku",
            "available": available,
            "modes": ["manual", "otr"],
            "lead_seconds": roku_lane.NAVIGATION_SECONDS,
            "shares_with_holder": True,
            "held_by": held_by(LANE_ROKU),
            "sources": roku.source_list(available),
        })
    return inputs


def recordings_payload():
    now = time.time()
    with lock:
        state = recording_state()
        timers = [recording_view(timer, now) for timer in state["timers"]]
        rules = [dict(rule) for rule in state["rules"]]
        active_ids = [current["timer_id"] for current in running_recordings()]
    running = [timer for timer in timers if timer["id"] in active_ids]
    return {
        "api_version": API_VERSION,
        "now": int(now),
        "inputs": recording_inputs(),
        # The digital tape first, for clients that show only one.
        "active": running[0] if running else None,
        "active_recordings": running,
        "timers": [timer for timer in timers
                   if timer["status"] in recorder.ACTIVE],
        "history": sorted(
            (timer for timer in timers
             if timer["status"] not in recorder.ACTIVE),
            key=lambda timer: timer.get("finished_at") or timer["end"],
            reverse=True,
        ),
        "rules": rules,
        "library": recorder.list_library(RECORDINGS_DIR),
        "storage": recording_storage_status(),
        "encoding": {
            "video": f"H.264 (x264 {recorder.VIDEO_PRESET}, CRF "
                     f"{recorder.VIDEO_CRF}, up to {recorder.VIDEO_MAX_HEIGHT}p)",
            "audio": f"AAC stereo {recorder.AUDIO_KBPS} kbps",
            "container": "MP4",
            "from_live": "the live stream's own H.264 and AAC, copied",
            "roku": "the HD PVR's own H.264 and AAC, copied (about 2.9 GB an hour)",
        },
        "padding": {"before_seconds": recorder.GUIDE_PRE_PAD_SECONDS,
                    "after_seconds": recorder.GUIDE_POST_PAD_SECONDS},
        "max_seconds": RECORDING_MAX_SECONDS,
    }


def recording_summary():
    """What is being taped right now, for status documents."""
    current = active_recording or roku_recording
    if not current:
        return None
    with lock:
        timer = find_timer(current["timer_id"])
        return recording_view(timer) if timer else None


def _channel_by(channel_id=None, number=None):
    for channel in load_channels():
        if (channel_id and channel["id"] == channel_id) or \
                (number and channel.get("number") == number):
            return channel
    return None


def _schedule_timer(timer):
    """Add a timer unless it collides with one that has the tuner then."""
    lane = recorder.timer_lane(timer)
    with lock:
        state = recording_state()
        clash = recorder.find_conflict(state["timers"], timer["start"],
                                       timer["end"], lane=lane)
        if clash:
            return api_v1_error(
                f"that overlaps {recorder.describe(clash)}; "
                + ("there is one source Roku" if lane == LANE_ROKU
                   else "the card has one digital tuner"),
                409, "conflict",
                extra={"conflict": recording_view(clash)},
            )
        state["timers"].append(timer)
        state["timers"] = recorder.prune_history(state["timers"])
        save_recording_schedule()
    recording_wakeup.set()
    return {"api_version": API_VERSION, "ok": True,
            "timer": recording_view(timer)}, 201


def create_recording(body):
    """Schedule a recording: by channel and time, by guide airing, or now."""
    if not isinstance(body, dict):
        return api_v1_error("JSON object required", 400)
    mode = body.get("mode", "manual")
    lane = body.get("lane") or LANE_DIGITAL
    if lane == LANE_ROKU:
        return create_roku_recording(body, mode)
    if lane != LANE_DIGITAL:
        return api_v1_error(
            f"lane must be {LANE_DIGITAL} or {LANE_ROKU}; no other lane records",
            400, "invalid_lane")
    # Copy the live lane instead of encoding the broadcast again; see
    # recorder.py.
    from_live = body.get("from_live") is True
    now = time.time()

    if mode == "manual":
        channel = _channel_by(channel_id=body.get("channel_id"),
                              number=body.get("channel_number"))
        if channel is None:
            return api_v1_error("unknown channel", 404, "unknown_channel")
        try:
            start = float(body["start"])
            end = float(body["end"])
        except (KeyError, TypeError, ValueError):
            return api_v1_error("start and end must be Unix times", 400,
                                "invalid_time")
        if not (math.isfinite(start) and math.isfinite(end)):
            return api_v1_error("start and end must be finite", 400,
                                "invalid_time")
        if end <= now:
            return api_v1_error("that time has already passed", 400,
                                "in_the_past")
        start = max(start, now)
        if end - start < recorder.MIN_RECORDING_SECONDS:
            return api_v1_error("a recording must be at least a minute long",
                                400, "too_short")
        if end - start > RECORDING_MAX_SECONDS:
            return api_v1_error(
                f"a recording can be at most "
                f"{RECORDING_MAX_SECONDS / 3600:g} hours", 400, "too_long")
        title = str(body.get("title") or "").strip()[:120] or None
        return _schedule_timer(recorder.make_timer(
            channel, start, end, title=title, kind="manual",
            from_live=from_live, now=now,
        ))

    if mode == "guide":
        number = body.get("channel_number")
        found = recorder.find_event(epg.load_guide(), number,
                                    body.get("event_start"))
        channel = _channel_by(number=number)
        if found is None or channel is None:
            return api_v1_error("that airing is not in the guide", 404,
                                "unknown_event")
        _guide_channel, event = found
        start, end = recorder.guide_window(event)
        if end <= now:
            return api_v1_error("that programme has already finished", 400,
                                "in_the_past")
        return _schedule_timer(recorder.make_timer(
            channel, max(start, now), end, title=event.get("title"),
            kind="guide", program=recorder.program_of(event, number),
            from_live=from_live, now=now,
        ))

    if mode == "otr":
        # One Touch Recording: whatever the digital lane is showing, until
        # the programme ends (or half an hour if the guide does not say).
        channel = feed_client.lane(LANE_DIGITAL)["selection"]
        if not channel:
            channel, _quality = load_playback_state()
        if channel is None:
            return api_v1_error("nothing is tuned on the digital lane", 409,
                                "nothing_tuned")
        event = epg.upcoming(epg.load_guide(), channel.get("number"),
                             at=int(now))
        if event:
            end = event["start"] + event["duration"] \
                + recorder.GUIDE_POST_PAD_SECONDS
            program = recorder.program_of(event, channel.get("number"))
            title = event.get("title")
        else:
            end, program, title = now + recorder.OTR_DEFAULT_SECONDS, None, None
        if end - now < recorder.MIN_RECORDING_SECONDS:
            end = now + recorder.OTR_DEFAULT_SECONDS
        return _schedule_timer(recorder.make_timer(
            channel, now, min(end, now + RECORDING_MAX_SECONDS), title=title,
            kind="otr", program=program, from_live=from_live, now=now,
        ))

    return api_v1_error('mode must be "manual", "guide", or "otr"', 400,
                        "invalid_mode")


def _recording_window(body, now, default_seconds=None):
    """Validated (start, end) from a body, or an error response."""
    try:
        start = float(body.get("start", now))
        end = float(body["end"]) if body.get("end") is not None \
            else start + default_seconds if default_seconds else float(body["end"])
    except (KeyError, TypeError, ValueError):
        return None, api_v1_error("start and end must be Unix times", 400,
                                  "invalid_time")
    if not (math.isfinite(start) and math.isfinite(end)):
        return None, api_v1_error("start and end must be finite", 400,
                                  "invalid_time")
    if end <= now:
        return None, api_v1_error("that time has already passed", 400,
                                  "in_the_past")
    start = max(start, now)
    if end - start < recorder.MIN_RECORDING_SECONDS:
        return None, api_v1_error("a recording must be at least a minute long",
                                  400, "too_short")
    if end - start > RECORDING_MAX_SECONDS:
        return None, api_v1_error(
            f"a recording can be at most "
            f"{RECORDING_MAX_SECONDS / 3600:g} hours", 400, "too_long")
    return (start, end), None


def create_roku_recording(body, mode):
    """A Roku timer: a channel or game at a time, a YouTube TV airing from
    the guide, or whatever is on now.

    The tape is a copy of the Roku lane's stream, so it needs no encoder.
    A timer navigates the Roku when it starts, holding the roku lock as the
    VCR; ``otr`` tapes what the Roku shows now, without moving it.
    """
    if roku is None:
        return roku_unavailable()
    now = time.time()
    if mode == "otr":
        selection = roku.selection_view()
        target = ({key: selection[key] for key in ("source_id", "channel_id", "date")
                   if key in selection}
                  if selection and not selection.get("manual")
                  else {"source_id": roku_lane.MANUAL, "channel_id": "manual"})
        name = (selection or {}).get("name") or "Manual Roku"
        window, error = _recording_window(
            {"start": now, "end": body.get("end")}, now,
            default_seconds=recorder.OTR_DEFAULT_SECONDS)
    elif mode == "manual":
        try:
            target, game = roku.normalize(body.get("target") or body)
        except RokuStreamError as exc:
            return roku_error(exc)
        name = (game or {}).get("title") or (
            "Manual Roku" if target["source_id"] == roku_lane.MANUAL
            else f"{roku_lane.SOURCES[target['source_id']][0]} {target['channel_id']}")
        window, error = _recording_window(body, now)
    elif mode == "guide":
        # A YouTube TV airing from the guide (yttv_epg.py), by its channel and
        # start. The timer begins early enough for the Roku to reach the
        # channel before the padded start, as the VCR page sets a game's.
        try:
            target, _game = roku.normalize(body.get("target") or body)
        except RokuStreamError as exc:
            return roku_error(exc)
        if target["source_id"] != roku_lane.YOUTUBE_TV:
            return api_v1_error("only YouTube TV channels have guide airings", 400,
                                "invalid_mode")
        found = recorder.find_event(yttv_epg.load_guide(), target["channel_id"],
                                    body.get("event_start"))
        if found is None:
            return api_v1_error("that airing is not in the guide", 404, "unknown_event")
        _channel, event = found
        start, end = recorder.guide_window(event)
        if end <= now:
            return api_v1_error("that programme has already finished", 400,
                                "in_the_past")
        window, error = (max(now, start - roku_lane.NAVIGATION_SECONDS), end), None
        name = f"{roku_lane.SOURCES[roku_lane.YOUTUBE_TV][0]} {target['channel_id']}"
        body = dict(body, title=event.get("title"))
    else:
        return api_v1_error('a Roku recording\'s mode must be "manual", "guide" or "otr"',
                            400, "invalid_mode")
    if error:
        return error
    start, end = window
    title = str(body.get("title") or "").strip()[:120] or (
        name if target["source_id"] in roku_lane.GAME_SOURCES else None)
    return _schedule_timer(recorder.make_timer(
        {"id": target["channel_id"], "number": None, "name": name},
        start, end, title=title, kind=mode, now=now,
        program=recorder.program_of(event, None) if mode == "guide" else None,
        lane=LANE_ROKU, target=target,
    ))


def cancel_recording(body):
    """Cancel a scheduled timer, or stop the one recording now."""
    timer_id = (body or {}).get("id") if isinstance(body, dict) else None
    with lock:
        timer = find_timer(timer_id)
        if timer is None:
            return api_v1_error("no such timer", 404, "unknown_timer")
        if timer["status"] == "recording":
            # The worker owns the process; ask it to stop and finalize.
            timer["stop_requested"] = True
        elif timer["status"] in ("scheduled", "conflict"):
            timer["status"] = "cancelled"
            timer["finished_at"] = int(time.time())
        else:
            return api_v1_error(f"that timer is already {timer['status']}",
                                409, "not_cancellable")
        save_recording_schedule()
        view = recording_view(timer)
    recording_wakeup.set()
    return {"api_version": API_VERSION, "ok": True, "timer": view}, 200


def create_recording_rule(body):
    """Record every airing of a programme, by its guide name."""
    if not isinstance(body, dict):
        return api_v1_error("JSON object required", 400)
    title = str(body.get("title") or "").strip()
    if not recorder.title_key(title):
        return api_v1_error("title is required", 400, "title_required")
    number = body.get("channel_number") or None
    if number and _channel_by(number=number) is None:
        return api_v1_error("unknown channel", 404, "unknown_channel")
    with lock:
        state = recording_state()
        for rule in state["rules"]:
            if recorder.title_key(rule["title"]) == recorder.title_key(title) \
                    and rule.get("channel_number") == number:
                return {"api_version": API_VERSION, "ok": True,
                        "rule": rule, "existing": True}, 200
        rule = {"id": recorder.new_id(), "title": title[:120],
                "channel_number": number, "created_at": int(time.time())}
        state["rules"].append(rule)
        added = recorder.expand_rules(state, epg.load_guide(), load_channels(),
                                      time.time())
        save_recording_schedule()
    recording_wakeup.set()
    return {"api_version": API_VERSION, "ok": True, "rule": rule,
            "timers_added": [recording_view(timer) for timer in added]}, 201


def delete_recording_rule(body):
    """Stop matching new airings; timers it already set are cancelled too."""
    rule_id = (body or {}).get("id") if isinstance(body, dict) else None
    with lock:
        state = recording_state()
        rule = next((r for r in state["rules"] if r["id"] == rule_id), None)
        if rule is None:
            return api_v1_error("no such rule", 404, "unknown_rule")
        state["rules"].remove(rule)
        for timer in state["timers"]:
            if timer.get("rule_id") == rule_id \
                    and timer["status"] in ("scheduled", "conflict"):
                timer["status"] = "cancelled"
                timer["finished_at"] = int(time.time())
        save_recording_schedule()
    return {"api_version": API_VERSION, "ok": True, "rule": rule}, 200


def erase_recording(body):
    """Delete one finished file from the library."""
    name = (body or {}).get("name") if isinstance(body, dict) else None
    path = recorder.library_file(RECORDINGS_DIR, name)
    if path is None:
        return api_v1_error("no such recording in the library", 404,
                            "unknown_recording")
    try:
        path.unlink()
    except OSError as error:
        return api_v1_error(f"could not erase: {error}", 500, "erase_failed")
    print(f"Erased recording {path.name}")
    return {"api_version": API_VERSION, "ok": True, "erased": path.name}, 200


def recording_search_payload(query):
    now = time.time()
    results = recorder.search_guide(epg.load_guide(), query, now)
    with lock:
        timers = recording_state()["timers"]
        for result in results:
            start, end = recorder.guide_window(result)
            result["scheduled"] = any(
                timer["status"] in recorder.ACTIVE
                and timer.get("channel_number") == result["channel_number"]
                and (timer.get("program") or {}).get("start") == result["start"]
                for timer in timers
            )
            clash = recorder.find_conflict(timers, max(start, now), end)
            result["conflict"] = None if result["scheduled"] or not clash \
                else recorder.describe(clash)
    return {"api_version": API_VERSION, "query": query, "results": results,
            "guide_available": epg.load_guide() is not None}


# --- the worker -----------------------------------------------------------

def _start_recorder_process(timer):
    """Claim the tuner, start the encoder, and tune. Returns an error or None."""
    storage = recording_storage_status()
    if not storage["ready"]:
        return storage["problems"][0]["message"]
    if recorder.timer_lane(timer) == LANE_ROKU:
        return _start_roku_recorder(timer)

    # A guide refresh would refuse the lock as busy; a timer outranks it the
    # way a person tuning does.
    preempt_guide_refresh()
    now = time.time()
    payload, status = acquire_tuner_lock({
        "lane": LANE_DIGITAL,
        "owner": RECORDING_OWNER,
        "duration_seconds": max(60, timer["end"] - now + 30),
        "reason": f"recording {timer.get('title') or 'CH ' + str(timer.get('channel_number'))}",
        "channel_id": timer["channel_id"],
        "token": timer.get("lock_token"),
    })
    if status != 200:
        return payload["error"]["message"]
    token = payload["token"]

    channel = _channel_by(channel_id=timer["channel_id"])
    if channel is None:
        release_tuner_lock({"lane": LANE_DIGITAL, "token": token})
        return "that channel is no longer in the channel list"

    # A timer resuming after a restart or a recorder crash keeps its stem and
    # writes the next piece; the pieces become one file when it ends.
    try:
        stem = Path(timer["stem"]) if timer.get("stem") else \
            recorder.unique_stem(RECORDINGS_DIR,
                                 recorder.recording_basename(timer))
    except OSError as error:
        release_tuner_lock({"lane": LANE_DIGITAL, "token": token})
        return str(error)
    part = _next_piece(timer, stem)
    from_live = bool(timer.get("from_live"))
    if from_live:
        # No tap and no encoder of its own: _tend_live_copy() starts copying
        # once the lane's live stream is ready.
        tap_port, recorder_id = None, None
    else:
        # A source generation on another channel may still be feeding the
        # last recording's tap; listening there would tape that channel until
        # the retune. On the same channel, reusing the port is what lets
        # back-to-back timers continue without a retune.
        digital = feed_client.lane(LANE_DIGITAL)
        same_channel = (digital["selection"] or {}).get("id") == channel["id"]
        busy_taps = () if same_channel else tuple(digital["taps"])
        try:
            recorder_id, tap_port = feed_client.start_recorder(
                "tap", part, timer["end"], exclude=busy_taps)
        except OSError as error:
            release_tuner_lock({"lane": LANE_DIGITAL, "token": token})
            return f"could not start the recorder: {error}"

    global active_recording
    current = {
        "timer_id": timer["id"],
        "channel_id": channel["id"],
        # The feed's recorder process writing this piece (feed_client.recorder()).
        "recorder": recorder_id,
        "until": timer["end"],
        "tap_port": tap_port,
        "from_live": from_live,
        # The live stream generation a from_live copy is reading.
        "generation": None,
        "part": part,
        "stem": stem,
        "token": token,
        "started_at": time.time(),
        "last_size": 0,
        "last_growth": time.monotonic(),
        "signal": "tuning",
    }
    active_recording = current

    # Tune through the same path a person does, holding the lock's token.
    # A tap is attached because the channel now matches the recording; a
    # from_live recording has none, so a lane already on the channel is kept.
    quality = feed_client.lane(LANE_DIGITAL)["quality"] or "medium"
    result, status = perform_digital_tune(channel["id"], quality, token=token)
    if status != 200:
        _stop_recorder_process(current)
        active_recording = None
        release_tuner_lock({"lane": LANE_DIGITAL, "token": token})
        _discard_empty(part)
        return result.get("error") or "the tuner would not tune"

    with lock:
        timer.update({
            "status": "recording",
            "note": None,
            "started_at": timer.get("started_at") or int(time.time()),
            "part": str(part),
            "stem": str(stem),
            "lock_token": token,
            "attempts": timer.get("attempts", 0) + 1,
        })
        save_recording_schedule()
    print(f"VCR: recording {timer.get('title') or channel['name']} "
          f"on {channel.get('number')} to {part.name}"
          + (" (copying the live stream)" if from_live else ""))
    return None


def roku_on_target(target):
    """Whether the Roku lane shows a timer's target right now.

    A Manual Roku target tapes whatever is on the Roku; any other must be
    the verified selection, never a menu on the way to it.
    """
    if roku is None:
        return False
    if target.get("source_id") == roku_lane.MANUAL:
        return True
    selection = roku.selection_view()
    return bool(selection and not selection.get("manual")
                and roku_lane.same_target(selection, target))


def _start_roku_recorder(timer):
    """Claim the Roku as the VCR and put the timer's target on it.

    When someone else holds the Roku -- vizelVision's master, a person at
    the remote -- the VCR tapes it without the lock if it already shows the
    target, and otherwise waits and retries rather than taking it away.
    """
    if roku is None:
        return "the Roku lane is not configured"
    target = dict(timer.get("target") or {})
    now = time.time()
    payload, status = acquire_tuner_lock({
        "lane": LANE_ROKU,
        "owner": RECORDING_OWNER,
        "duration_seconds": max(60, timer["end"] - now + 30),
        "reason": f"recording {recorder.describe(timer)}",
        "token": timer.get("lock_token"),
    })
    token, shared = None, False
    if status == 200:
        token = payload["token"]
    elif status == 423 and roku_on_target(target):
        shared = True
    else:
        return payload["error"]["message"]

    # A Manual Roku timer tapes whatever is on; anything else is navigated
    # to, unless it is already verified there (or is being shared).
    if token and target.get("source_id") != roku_lane.MANUAL:
        tune_status, result = roku_call(lambda: roku.tune(dict(target)))
        if tune_status not in (200, 202):
            release_tuner_lock({"lane": LANE_ROKU, "token": token})
            return result["error"]["message"]

    try:
        stem = Path(timer["stem"]) if timer.get("stem") else \
            recorder.unique_stem(RECORDINGS_DIR,
                                 recorder.recording_basename(timer))
    except OSError as error:
        if token:
            release_tuner_lock({"lane": LANE_ROKU, "token": token})
        return str(error)
    current = {
        "lane": LANE_ROKU,
        "timer_id": timer["id"],
        "channel_id": timer["channel_id"],
        "target": target,
        "recorder": None,
        "until": timer["end"],
        "tap_port": None,
        # The Roku is always taped from its live stream: _tend_live_copy()
        # starts once the target is verified on screen.
        "from_live": True,
        "generation": None,
        "part": _next_piece(timer, stem),
        "stem": stem,
        "token": token,
        "shared": shared,
        "started_at": time.time(),
        "last_size": 0,
        "last_growth": time.monotonic(),
        "signal": "tuning",
    }
    _set_current_recording(LANE_ROKU, current)
    with lock:
        timer.update({
            "status": "recording",
            "note": "sharing the Roku with its holder" if shared else None,
            "started_at": timer.get("started_at") or int(time.time()),
            "part": str(current["part"]),
            "stem": str(stem),
            "lock_token": token,
            "attempts": timer.get("attempts", 0) + 1,
        })
        save_recording_schedule()
    print(f"VCR: recording the Roku ({recorder.describe(timer)}) "
          f"to {current['part'].name}" + (" while sharing it" if shared else ""))
    return None


def _next_piece(timer, stem):
    """The first unused piece file for ``timer`` under ``stem``."""
    index = len(timer.get("pieces") or []) + 1
    part = recorder.piece_part(stem, index)
    while part.exists():
        index += 1
        part = recorder.piece_part(stem, index)
    return part


def start_recorder(kind, part, until, *, lane=None, exclude=(), start_index=0, generation=None):
    """Start one recorder process (feed side). ``kind`` "tap" records a port
    the digital source feeds, chosen here; "live" copies ``lane``'s live HLS.
    Returns (recorder id, tap port or None); raises OSError if ffmpeg won't start."""
    if kind == "tap":
        tap_port = allocate_udp_port(RECORD_TAP_PORT_BASE, exclude=exclude)
        command = recorder.build_record_command(tv_udp_input(tap_port), part)
    elif kind == "live":
        tap_port = None
        command = recorder.build_live_copy_command(lane_playlist_path(lane), part, start_index=start_index)
    else:
        raise ValueError(f"unknown recorder kind {kind!r}")
    return recorders.start(command, part, until, kind=kind, lane=lane, tap_port=tap_port,
                           generation=generation), tap_port


def recorder_alive(current):
    """Whether a recording's current recorder process runs (control side)."""
    view = feed_client.recorder(current["recorder"]) if current.get("recorder") else None
    return view is not None and view["alive"]


_spawn_requests = None           # _recorder_spawner()'s queue, made on first use
_spawn_requests_lock = threading.Lock()


def _spawn_recorder(cmd):
    """Start a recorder process from _recorder_spawner() and return it.

    Whichever thread asks may end at once -- in the split, the feed starts a
    recorder from the thread answering control's /feed/v1/ request -- and the
    recorder's death signal (_recorder_preexec) would end it with that thread.
    """
    global _spawn_requests
    with _spawn_requests_lock:
        if _spawn_requests is None:
            _spawn_requests = queue.Queue()
            threading.Thread(target=_recorder_spawner, args=(_spawn_requests,),
                             name="recorder-spawner", daemon=True).start()
    reply = queue.Queue(maxsize=1)
    _spawn_requests.put((cmd, reply))
    proc, error = reply.get()
    if error is not None:
        raise error
    return proc


def _recorder_spawner(requests):
    """The one thread that starts recorders; it lives as long as the process."""
    while True:
        cmd, reply = requests.get()
        try:
            reply.put((subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True,
                preexec_fn=_recorder_preexec,
            ), None))
        except Exception as error:   # noqa: BLE001 - the caller raises it
            reply.put((None, error))


def _tend_live_copy(current):
    """Keep a from_live recording copying the lane's current live stream.

    The copy follows one stream generation. When the live encoder restarts
    (a safety trip, or the always-on keeper reviving a dead source) its
    playlist starts again under new segment names, so the piece taped so far
    is set aside and the next one starts on the new generation once it is
    ready -- without giving up the tuner or counting as a failed attempt. A
    copy that dies on its own generation is left to the caller, which
    resumes it like a crashed encoder.
    """
    lane = current.get("lane", LANE_DIGITAL)
    state = feed_client.lane(lane)
    generation = state["stream_id"]
    with lock:
        on_target = roku_on_target(current["target"]) if lane == LANE_ROKU \
            else (state["selection"] or {}).get("id") == current["channel_id"]
    ready = generation is not None and on_target and state["ready"]
    if current["recorder"] is not None:
        if generation == current["generation"]:
            return
        _stop_recorder_process(current)
        print(f"VCR: live stream {current['generation']} ended; "
              "carrying on with the next one")
        _file_live_piece(current)
    if not ready:
        return
    try:
        recorder_id, _ = feed_client.start_recorder(
            "live", current["part"], current["until"], lane=lane,
            start_index=-3 if lane == LANE_ROKU else 0, generation=generation)
    except OSError as error:
        print(f"VCR: could not start the live copy: {error}")
        return
    current.update({"recorder": recorder_id, "generation": generation})


def _file_live_piece(current):
    """Set a stopped live copy's file aside as a piece; aim at the next."""
    part = current["part"]
    current.update({"recorder": None, "generation": None, "last_size": 0})
    try:
        taped = part.stat().st_size > 0
    except OSError:
        taped = False
    if not taped:
        # Nothing to keep: the next copy writes the same file.
        _discard_empty(part)
        return
    with lock:
        timer = find_timer(current["timer_id"])
        if timer is None:
            return
        timer.setdefault("pieces", []).append(str(part))
        current["part"] = _next_piece(timer, current["stem"])
        timer["part"] = str(current["part"])
        save_recording_schedule()


def _stop_recorder_process(current, timeout=RECORDER_STOP_TIMEOUT):
    """Ask the feed to finish this recording's file ("q"), escalating only if
    ffmpeg will not."""
    if current.get("recorder") is not None:
        feed_client.stop_recorder(current["recorder"], timeout=timeout)


def _discard_empty(part):
    try:
        if part.exists() and part.stat().st_size == 0:
            part.unlink()
    except OSError:
        pass


def finalize_recording_file(part, stem=None):
    """Turn a finished ``.ts.part`` into a library file. Returns its path.

    Remuxes to MP4 when that works, and otherwise keeps the transport
    stream, which every DLNA client here plays -- a recording is never lost
    to a failed rewrap.
    """
    stem = stem or recorder.stem_of_part(part)
    if stem is None or not part.exists():
        return None
    if part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        return None
    mp4 = stem.with_name(stem.name + ".mp4")
    mp4_part = recorder.part_of(mp4)
    try:
        result = subprocess.run(
            recorder.build_remux_command(part, mp4_part),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, timeout=1800,
            preexec_fn=_recorder_preexec,
        )
        remuxed = result.returncode == 0 and mp4_part.exists() \
            and mp4_part.stat().st_size > 0
        if not remuxed:
            print(f"VCR: remux failed ({result.stderr.strip()[-300:]}); "
                  "keeping the transport stream")
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"VCR: remux failed ({error}); keeping the transport stream")
        remuxed = False
    if remuxed:
        os.replace(mp4_part, mp4)
        part.unlink(missing_ok=True)
        return mp4
    mp4_part.unlink(missing_ok=True)
    ts = stem.with_name(stem.name + ".ts")
    os.replace(part, ts)
    return ts


def media_duration(path):
    """A media file's duration in seconds, or None if ffprobe cannot tell."""
    try:
        result = subprocess.run(
            recorder.build_probe_command(path), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=120,
        )
        return float(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def join_recording_pieces(pieces, stem):
    """Join a recording's pieces into ``stem``.mp4. Returns it, or None.

    The pieces are removed only once the joined file is at least as long as
    they are together, so a join that stops early keeps every piece.
    """
    mp4 = stem.with_name(stem.name + ".mp4")
    mp4_part = recorder.part_of(mp4)
    listing = stem.with_name(f".{stem.name}.ffconcat")
    joined = False
    try:
        listing.write_text(recorder.concat_list(pieces))
        result = subprocess.run(
            recorder.build_join_command(listing, mp4_part),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, timeout=1800,
            preexec_fn=_recorder_preexec,
        )
        if result.returncode != 0 or not mp4_part.exists():
            print(f"VCR: join failed ({result.stderr.strip()[-300:]})")
        else:
            expected = [media_duration(piece) for piece in pieces]
            got = media_duration(mp4_part)
            if None in expected or got is None:
                print("VCR: join not verified (could not read durations)")
            elif got < sum(expected) - 2 * len(pieces):
                print(f"VCR: join too short ({got:.0f} s of "
                      f"{sum(expected):.0f} s)")
            else:
                joined = True
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"VCR: join failed ({error})")
    finally:
        listing.unlink(missing_ok=True)
    if not joined:
        mp4_part.unlink(missing_ok=True)
        return None
    os.replace(mp4_part, mp4)
    for piece in pieces:
        piece.unlink(missing_ok=True)
    return mp4


def finalize_recording(parts, stem=None):
    """Turn every piece of one recording into library files. Returns them.

    A timer interrupted by a server restart or a recorder crash leaves more
    than one piece; they are joined into a single MP4. If the join fails,
    each piece is filed on its own, so nothing that was taped is lost.
    """
    pieces = []
    for part in parts:
        try:
            if part.stat().st_size > 0:
                pieces.append(part)
            else:
                part.unlink()
        except OSError:
            pass
    if not pieces:
        # Nothing left to file: a finalize that was cut short after filing them (control
        # restarted) finds what it filed, rather than calling the recording lost.
        done = _filed_output(stem or (recorder.stem_of_part(parts[0]) if parts else None))
        return [done] if done is not None else []
    stem = stem or recorder.stem_of_part(pieces[0])
    if len(pieces) > 1:
        joined = join_recording_pieces(pieces, stem)
        if joined is not None:
            return [joined]
        print("VCR: filing the pieces separately")
    paths = []
    for index, part in enumerate(pieces):
        piece_stem = stem if index == 0 else \
            recorder.unique_stem(stem.parent, stem.name)
        path = finalize_recording_file(part, piece_stem)
        if path is not None:
            paths.append(path)
    return paths


def _filed_output(stem):
    """The library file a recording's stem became, if it was already filed."""
    if stem is None:
        return None
    for suffix in (".mp4", ".ts"):
        candidate = stem.with_name(stem.name + suffix)
        try:
            if candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def _finish_timer(timer_id, parts, status, note, stem=None):
    """Finalize the files in the background and record the outcome."""
    def run():
        paths = []
        try:
            paths = finalize_recording(parts, stem)
        except OSError as error:
            print(f"VCR: could not finalize {parts[-1]}: {error}")
        with lock:
            timer = find_timer(timer_id)
            if timer is None:
                return
            for path in paths:
                files = timer.setdefault("files", [])
                timer["file"] = path.name
                if path.name in files:
                    continue                    # filed by a finalize that was cut short
                files.append(path.name)
                try:
                    timer["bytes"] = (timer.get("bytes") or 0) + path.stat().st_size
                except OSError:
                    pass
            if timer["status"] != "scheduled":     # not resumed meanwhile
                timer.pop("pieces", None)
                timer.pop("stem", None)
                timer.pop("finish_as", None)
                timer["status"] = status if paths else "failed"
                timer["note"] = note if paths or note \
                    else "nothing was recorded"
                timer["finished_at"] = int(time.time())
            save_recording_schedule()
        for path in paths:
            print(f"VCR: {path.name} is in the library")

    threading.Thread(target=run, daemon=True).start()


def _finishing(timer, part=None, status=None):
    """Mark a timer as finishing. Call with `lock` held.

    Its pieces stay on the timer until the finalizer has filed them, so a
    restart part-way through finalizing does it again instead of stranding
    them, ending as ``status`` (kept on the timer for that) as it would have.
    Returns ``(parts, stem)`` for _finish_timer().
    """
    pieces = timer.setdefault("pieces", [])
    if part is not None and str(part) not in pieces:
        pieces.append(str(part))
    timer["status"] = "finishing"
    if status is not None:
        timer["finish_as"] = status
    stem = timer.get("stem")
    return ([Path(piece) for piece in timer.get("pieces") or []],
            Path(stem) if stem else None)


def _end_active_recording(status, note, resume=False, lane=LANE_DIGITAL):
    """Stop the running recording and hand its file to the finalizer.

    With ``resume`` the file is kept as a piece and the timer starts the
    next one when it can; the pieces are joined when the timer ends.
    """
    current = current_recording(lane)
    if current is None:
        return
    _stop_recorder_process(current)
    _set_current_recording(lane, None)
    if current["token"]:
        release_tuner_lock({"lane": lane, "token": current["token"]})
    parts, stem = [current["part"]], None
    with lock:
        timer = find_timer(current["timer_id"])
        if timer is not None:
            timer.pop("stop_requested", None)
            timer.pop("part", None)
            timer.pop("lock_token", None)
            if resume:
                timer.setdefault("pieces", []).append(str(current["part"]))
                timer["status"] = "scheduled"
                timer["note"] = note
                _recording_retry_at[timer["id"]] = \
                    time.monotonic() + RECORDING_RETRY_SECONDS
            else:
                parts, stem = _finishing(timer, current["part"], status)
                timer["note"] = note
            save_recording_schedule()
    print(f"VCR: stopped ({note or status})")
    if not resume:
        _finish_timer(current["timer_id"], parts, status, note, stem)


def _check_roku_recording(now):
    """Decide whether the running Roku recording should keep going."""
    current = roku_recording

    def end(status, note, resume=False):
        return _end_active_recording(status, note, resume=resume, lane=LANE_ROKU)
    with lock:
        timer = find_timer(current["timer_id"])
        stop_requested = bool(timer and timer.get("stop_requested"))
        held = active_lock(LANE_ROKU)
    if timer is None:
        return end("partial", "the timer was removed")
    if stop_requested:
        return end("done", "stopped early")
    if now >= timer["end"]:
        return end("done", None)
    if current["token"] and (held is None or held.get("token") != current["token"]):
        owner = held["owner"] if held else "someone"
        return end("partial", f"the Roku was taken over by {owner}")
    target = current["target"]
    on_target = roku_on_target(target)
    operation = roku.operation_view() if roku is not None else None
    navigating = bool(operation and operation["active"]
                      and roku_lane.same_target(operation["target"], target))
    if not on_target and not navigating:
        if current["recorder"] is not None or current["last_size"]:
            who = held["owner"] if held else "someone"
            return end("partial", f"the Roku was moved off it by {who}")
        if operation and roku_lane.same_target(operation["target"], target) \
                and operation["state"] in ("failed", "cancelled"):
            resume = timer.get("attempts", 1) < RECORDING_MAX_ATTEMPTS
            return end("partial", f"the Roku could not find it: {operation['message']}"
                       + ("; trying again" if resume else ""), resume=resume)
        if current["shared"]:
            return end("partial", "the Roku's holder moved it before taping began")
        current["signal"] = "tuning"
        return None
    _tend_live_copy(current)
    if current["recorder"] is not None and not recorder_alive(current):
        resume = timer.get("attempts", 1) < RECORDING_MAX_ATTEMPTS
        exit_code = (feed_client.recorder(current["recorder"]) or {}).get("returncode")
        return end("partial",
                   f"the recorder stopped unexpectedly (exit {exit_code})"
                   + ("; resuming" if resume else ""), resume=resume)
    storage = recording_storage_status()
    if storage["free_bytes"] is not None \
            and storage["free_bytes"] < RECORDING_STOP_FREE_BYTES:
        return end("partial", "the disk is nearly full")
    try:
        size = current["part"].stat().st_size
    except OSError:
        size = 0
    if size > current["last_size"]:
        current["last_size"] = size
        current["last_growth"] = time.monotonic()
        current["signal"] = "ok"
    elif time.monotonic() - current["last_growth"] > RECORDING_STALL_SECONDS:
        current["signal"] = "lost"
    return None


def _check_active_recording(now):
    """Decide whether the running recording should keep going."""
    current = active_recording
    with lock:
        timer = find_timer(current["timer_id"])
        stop_requested = bool(timer and timer.get("stop_requested"))
        held = active_lock(LANE_DIGITAL)
    tuned = (feed_client.lane(LANE_DIGITAL)["selection"] or {}).get("id")
    if timer is None:
        return _end_active_recording("partial", "the timer was removed")
    if stop_requested:
        return _end_active_recording("done", "stopped early")
    if now >= timer["end"]:
        return _end_active_recording("done", None)
    if held is None or held.get("token") != current["token"] \
            or tuned not in (None, current["channel_id"]):
        owner = held["owner"] if held else "someone"
        return _end_active_recording(
            "partial", f"the tuner was taken over by {owner}")
    if current.get("from_live"):
        _tend_live_copy(current)
    if current["recorder"] is not None and not recorder_alive(current):
        resume = timer.get("attempts", 1) < RECORDING_MAX_ATTEMPTS
        exit_code = (feed_client.recorder(current["recorder"]) or {}).get("returncode")
        return _end_active_recording(
            "partial",
            f"the recorder stopped unexpectedly (exit {exit_code})"
            + ("; resuming" if resume else ""),
            resume=resume,
        )
    storage = recording_storage_status()
    if storage["free_bytes"] is not None \
            and storage["free_bytes"] < RECORDING_STOP_FREE_BYTES:
        return _end_active_recording("partial", "the disk is nearly full")

    try:
        size = current["part"].stat().st_size
    except OSError:
        size = 0
    if size > current["last_size"]:
        current["last_size"] = size
        current["last_growth"] = time.monotonic()
        current["signal"] = "ok"
    elif time.monotonic() - current["last_growth"] > RECORDING_STALL_SECONDS:
        # Keep going: a dropout usually recovers, and the always-on keeper
        # restarts a dead source, which feeds the same tap again.
        current["signal"] = "lost"


def recording_tick(now=None):
    """One pass of the scheduler. Returns nothing; all effects are state."""
    global _rules_expanded_for
    now = time.time() if now is None else now
    if active_recording is not None:
        _check_active_recording(now)
    if roku_recording is not None:
        _check_roku_recording(now)

    with lock:
        state = recording_state()
        running = {current["timer_id"] for current in running_recordings()}
        changed = bool(recorder.expire_missed(
            [timer for timer in state["timers"] if timer["id"] not in running],
            now,
        ))
        guide = None
        if state["rules"]:
            guide = epg.load_guide()
            stamp = (guide or {}).get("generated_at")
            # Expand when the guide changes, and every tick costs nothing
            # when it has not: rules only ever add airings they have not seen.
            if stamp != _rules_expanded_for or changed:
                changed |= bool(recorder.expand_rules(
                    state, guide, load_channels(), now))
                _rules_expanded_for = stamp
        # A timer can end between pieces -- cancelled, or its window closing
        # before a resume got the tuner back -- and still hold what it taped.
        leftovers = []
        for timer in state["timers"]:
            if timer.get("pieces") and timer["status"] not in ACTIVE_OR_FINISHING:
                parts, stem = _finishing(timer, status="partial")
                leftovers.append((timer["id"], parts, stem, timer.get("note")))
                changed = True
        if changed:
            state["timers"] = recorder.prune_history(state["timers"])
            save_recording_schedule()
        # Each lane tapes one thing at a time; the lanes run side by side.
        due = [
            timer for timer in (
                recorder.due_timer(state["timers"], now, lane=lane)
                for lane in (LANE_DIGITAL, LANE_ROKU)
                if current_recording(lane) is None
            ) if timer is not None
        ]

    for timer_id, parts, stem, note in leftovers:
        _finish_timer(timer_id, parts, "partial", note, stem)

    for timer in due:
        if time.monotonic() < _recording_retry_at.get(timer["id"], 0):
            continue
        error = _start_recorder_process(timer)
        if error:
            _recording_retry_at[timer["id"]] = \
                time.monotonic() + RECORDING_RETRY_SECONDS
            with lock:
                if timer.get("note") != error:
                    timer["note"] = error
                    save_recording_schedule()
            print(f"VCR: could not start {recorder.describe(timer)}: {error}")
        else:
            _recording_retry_at.pop(timer["id"], None)


def recording_worker():
    while True:
        recording_wakeup.wait(timeout=2)
        recording_wakeup.clear()
        try:
            recording_tick()
        except Exception as failure:   # noqa: BLE001 - the VCR must not die
            print(f"VCR worker error: {failure!r}")
            time.sleep(2)


def recorder_keeper():
    """The feed's own watch over its recorders: notes exits and ends each
    recording on time, whether or not control is there to ask (feed side)."""
    while True:
        time.sleep(2)
        try:
            recorders.sweep()
        except Exception as failure:   # noqa: BLE001 - the keeper must not die
            print(f"Recorder keeper error: {failure!r}")


def recover_recordings():
    """After a restart: take back what is still recording, and finish what is not.

    Only parts this instance's own schedule names are touched, so a staging
    server pointed at the same library can never finalize the production
    server's recording in progress.

    * Split, a restart of control leaves the feed's recorders running: a
      recording the feed is still writing is taken back as it is (same
      recorder, same lock, same piece).
    * One the feed stopped at its end while control was away is done.
    * Any other was cut short. A timer whose window is still open goes back
      to ``scheduled``, keeps what it taped as a piece, and resumes into the
      next one, renewing the lock it already holds rather than being refused
      by it; its pieces are joined into one file when it ends. A timer past
      its window is filed as partial.
    """
    now = time.time()
    leftovers = []
    stale_tokens = []
    adopted = []
    writing = {}
    if ROLE == "control":
        try:
            writing = {view["part"]: view for view in feed_client.live_recorders()}
        except OSError as error:
            print(f"VCR: the feed did not say what it is recording ({error}); "
                  "leaving every recording as it is")
            return
    try:
        journal = feed_client.recorder_journal()
    except OSError as error:
        print(f"VCR: could not read the recorders' journal ({error})")
        journal = []
    # Why each part's last recorder ended, by the feed's own account.
    ended = {record["part"]: record.get("reason") for record in journal if record.get("event") == "end"}
    with lock:
        for timer in recording_state()["timers"]:
            if timer["status"] not in ("recording", "finishing"):
                continue
            if timer["status"] == "recording" and timer.get("part") in writing:
                adopted.append((timer, writing[timer["part"]]))
                continue
            part = timer.pop("part", None)
            token = timer.pop("lock_token", None)
            timer.pop("stop_requested", None)
            if timer["status"] == "recording" and part and ended.get(part) == "until":
                # The feed ended it on time by itself: nothing was cut short.
                parts, stem = _finishing(timer, part, "done")
                timer["note"] = None
                leftovers.append((timer["id"], parts, stem, "done", None))
                if token:
                    stale_tokens.append((recorder.timer_lane(timer), token))
                continue
            resume = timer["status"] == "recording" \
                and timer["end"] > now + recorder.MIN_RECORDING_SECONDS \
                and timer.get("attempts", 0) < RECORDING_MAX_ATTEMPTS
            if resume:
                if part:
                    timer.setdefault("pieces", []).append(part)
                timer["status"] = "scheduled"
                timer["note"] = "resumed after a server restart"
                timer["lock_token"] = token
            elif timer["status"] == "finishing" and timer.get("finish_as"):
                # A finalize cut short: do it again, ending as it was going to.
                parts, stem = _finishing(timer, part)
                leftovers.append((timer["id"], parts, stem, timer["finish_as"], timer.get("note")))
            else:
                parts, stem = _finishing(timer, part, "partial")
                note = "interrupted by a server restart"
                leftovers.append((timer["id"], parts, stem, "partial", note))
                timer["note"] = note
                if token:
                    stale_tokens.append((recorder.timer_lane(timer), token))
        save_recording_schedule()
    for timer, view in adopted:
        _adopt_recording(timer, view)
    for lane, token in stale_tokens:
        release_tuner_lock({"lane": lane, "token": token})
    for timer_id, parts, stem, status, note in leftovers:
        _finish_timer(timer_id, parts, status, note, stem)


def _adopt_recording(timer, view):
    """Take back a recording the feed is still writing, after control restarted: the same
    recorder, lock, piece and (for a live copy) stream generation, so nothing is cut."""
    lane = recorder.timer_lane(timer)
    part = Path(timer["part"])
    try:
        size = part.stat().st_size
    except OSError:
        size = 0
    current = {
        "timer_id": timer["id"],
        "channel_id": timer["channel_id"],
        "recorder": view["id"],
        "until": timer["end"],
        "tap_port": view.get("tap_port"),
        "from_live": lane == LANE_ROKU or bool(timer.get("from_live")),
        "generation": view.get("generation"),
        "part": part,
        "stem": Path(timer["stem"]),
        "token": timer.get("lock_token"),
        "started_at": view.get("started_at") or time.time(),
        "last_size": size,
        "last_growth": time.monotonic(),
        "signal": "ok",
    }
    if lane == LANE_ROKU:
        current.update(lane=LANE_ROKU, target=dict(timer.get("target") or {}),
                       shared=current["token"] is None)
    _set_current_recording(lane, current)
    print(f"VCR: took back {recorder.describe(timer)}, still recording {part.name} in the feed")


def shutdown_recording():
    """On exit: close the files cleanly and leave them for recover_recordings().

    The feed's job (its recorders), done when the feed stops."""
    # systemd kills the unit 5 s after asking; the transport stream is
    # playable even if ffmpeg does not get to finish.
    recorders.stop_all(timeout=2)


def run_guide_refresh(reason="manual", progress=None, blocks=None,
                      descriptions=None):
    """Take the digital front-end and rebuild the broadcast guide.

    Returns ``(guide, error)``. A refresh that is cut short still saves what
    it read: the frequencies it did reach are current, and the ones it did
    not keep their previous entries.
    """
    global guide_refreshing

    with lock:
        # A lock outranks the schedule: a recording running at 03:00 must not
        # lose its tuner to a guide sweep.
        held = lock_holder(LANE_DIGITAL)
        if held:
            # Flagged rather than string-matched: the scheduler retries a
            # lock, but must not spin on an ordinary failure.
            guide_status["deferred_by_lock"] = True
            return None, (f"digital tuner is locked by {held['owner']}")
        guide_status["deferred_by_lock"] = False
        busy = busy_reason()
        if busy:
            return None, busy
        guide_refreshing = True
        guide_status["running"] = True
        guide_abort.clear()
        # Only the digital front-end is needed. An analog stream uses the
        # other tuner and plays straight through a refresh.
        feed_client.stop(LANE_DIGITAL)
    # dvb_source closes its descriptors before stop_digital returns; give the
    # kernel the same settle window a rescan does before claiming them.
    time.sleep(0.2)

    try:
        guide = feed_client.read_guide(
            FRONTEND_DIGITAL,
            channels=load_channels(),
            blocks=EPG_BLOCKS if blocks is None else blocks,
            descriptions=(EPG_DESCRIPTIONS if descriptions is None
                          else descriptions),
            stop=guide_abort,
            progress=progress,
        )
        guide["reason"] = reason
        if guide.get("error"):
            guide_status["last_error"] = guide["error"]
            return guide, guide["error"]
        guide = epg.merge_guides(epg.load_guide(), guide)
        epg.save_guide(guide)
        guide_status["last_error"] = None
        guide_status["last_reason"] = reason
        return guide, None
    except Exception as error:   # noqa: BLE001 - reported, never fatal
        guide_status["last_error"] = str(error)
        return None, str(error)
    finally:
        # The claim side already waits for the driver to settle after
        # stop_digital. The release side needs it too: the always-on keeper
        # is only held off while the flag is set, so clearing it the instant
        # the sweep's descriptor closes has it racing the driver for the
        # front-end and logging EBUSY before the retry succeeds.
        time.sleep(0.3)
        with lock:
            guide_refreshing = False
            guide_status["running"] = False
        guide_wakeup.set()
        if ALWAYS_ON:
            always_on_wakeup.set()


def guide_worker():
    """Refresh the guide on its schedule, and once at startup if it is stale.

    The startup pass is what stops a fresh install, or a machine that was off
    overnight, from sitting with an empty guide until the next slot. It waits
    a couple of minutes first so it does not fight the always-on lane for the
    tuner while the service is still coming up.
    """
    if not EPG_ENABLED:
        return

    stored = epg.load_guide()
    # What matters is how much guide is left, not how old the file is. A
    # twelve-hour guide read at 03:00 is ten hours old by 13:00 with barely
    # two hours still ahead of it -- an age test calls that fresh, which is
    # exactly backwards.
    if stored is None or remaining_coverage_hours(stored) < EPG_MIN_COVERAGE_HOURS:
        if not guide_wakeup.wait(timeout=EPG_STARTUP_DELAY):
            _refresh_until_the_tuner_is_free("startup")
    guide_wakeup.clear()

    while True:
        target = next_refresh_at(EPG_REFRESH_TIMES)
        if target is None:
            return
        if guide_wakeup.wait(timeout=max(1.0, target - time.time())):
            # A manual refresh already ran; re-arm on the next slot instead
            # of interrupting again straight away.
            guide_wakeup.clear()
            continue
        _refresh_until_the_tuner_is_free("scheduled")
        guide_wakeup.clear()


def _refresh_until_the_tuner_is_free(reason):
    """Run a refresh, retrying for as long as a lock is what is stopping it.

    Only a lock is retried. An ordinary failure -- no channels, a dead
    front-end -- is left for the next slot rather than retried every few
    minutes forever.
    """
    while _run_scheduled_refresh(reason):
        target = next_refresh_at(EPG_REFRESH_TIMES)
        # Never retry past the next scheduled slot; that slot's own run takes
        # over from here.
        if target and time.time() + EPG_LOCK_RETRY_SECONDS >= target:
            print("Guide refresh still deferred at the next slot; "
                  "leaving it to that run")
            return
        if guide_wakeup.wait(timeout=EPG_LOCK_RETRY_SECONDS):
            # A manual refresh got through in the meantime.
            guide_wakeup.clear()
            return


def _run_scheduled_refresh(reason):
    """Run one refresh. Returns True if a tuner lock deferred it."""
    guide_status["deferred_by_lock"] = False
    try:
        guide, error = run_guide_refresh(reason=reason)
    except Exception as failure:   # noqa: BLE001 - keeper must not die
        print(f"Guide refresh error: {failure!r}")
        return False
    if error and guide_status["deferred_by_lock"]:
        print(f"Guide refresh ({reason}) deferred: {error}")
        return True
    if error:
        print(f"Guide refresh ({reason}) failed: {error}")
    elif guide:
        print(f"Guide refresh ({reason}): {guide['event_count']} events on "
              f"{guide['channels_with_events']} channels, "
              f"{guide['coverage_hours']}h ahead, "
              f"in {guide['elapsed_seconds']}s")
    return False


def run_yttv_guide_refresh(reason="manual"):
    """Read YouTube TV's listings from the web and store them.

    Returns True when anything was read. A row whose source failed keeps its
    last listing, marked stale, until that runs out.
    """
    yttv_guide_status.update(running=True, last_attempt=int(time.time()))
    try:
        listings = yttv_epg.load_listings()
        names = [str(item["id"]) for item in roku.catalog(roku_lane.YOUTUBE_TV)["items"]]
        fresh = yttv_epg.refresh(listings, names)
        if fresh["requested"] and len(fresh["errors"]) >= fresh["requested"]:
            raise RuntimeError("no source answered: "
                               + next(iter(fresh["errors"].values())))
        guide = yttv_epg.merge_guides(yttv_epg.load_guide(), fresh)
        guide["reason"] = reason
        yttv_epg.save_guide(guide)
    except Exception as error:   # noqa: BLE001 - the worker must not die
        yttv_guide_status["last_error"] = str(error)
        print(f"YouTube TV guide ({reason}) failed: {error}", flush=True)
        return False
    finally:
        yttv_guide_status["running"] = False
    yttv_guide_status.update(last_error=None, failed_channels=fresh["errors"])
    print(f"YouTube TV guide ({reason}): {guide['event_count']} programmes on "
          f"{guide['channels_with_events']} channels, {guide['coverage_hours']}h ahead, "
          f"in {fresh['elapsed_seconds']}s"
          + (f"; failed: {', '.join(sorted(fresh['errors']))}" if fresh["errors"] else ""),
          flush=True)
    return True


def yttv_guide_next_wait(succeeded, now=None):
    """``(seconds, reason)`` until the next YouTube TV read; None to stop.

    A read that failed outright is tried again in an hour, unless the next
    slot comes sooner.
    """
    now = time.time() if now is None else now
    slot = next_refresh_at(YTTV_GUIDE_REFRESH_TIMES, now)
    wait = slot - now if slot else None
    if not succeeded and (wait is None or wait > YTTV_GUIDE_RETRY_SECONDS):
        return YTTV_GUIDE_RETRY_SECONDS, "retry"
    return (None, None) if wait is None else (max(1.0, wait), "scheduled")


def yttv_guide_worker():
    """Read YouTube TV's listings on their schedule, and at startup when short.

    Nothing is tuned, so nothing waits on a lock or interrupts a viewer. The
    startup read waits a moment so it does not compete with the service
    coming up.
    """
    if not YTTV_GUIDE_ENABLED or roku is None:
        return
    due = remaining_coverage_hours(yttv_epg.load_guide()) < YTTV_GUIDE_MIN_COVERAGE_HOURS
    if due:
        yttv_guide_wakeup.wait(timeout=YTTV_GUIDE_STARTUP_DELAY)
        yttv_guide_wakeup.clear()
    reason = "startup"
    while True:
        succeeded = run_yttv_guide_refresh(reason) if due else True
        wait, reason = yttv_guide_next_wait(succeeded)
        if wait is None:
            return
        yttv_guide_wakeup.wait(timeout=wait)
        yttv_guide_wakeup.clear()
        due = True


def remaining_coverage_hours(guide, now=None):
    """Hours of guide still ahead of the clock, from the stored horizon."""
    if not guide:
        return 0
    horizon = guide.get("horizon")
    if not horizon:
        return 0
    now = time.time() if now is None else now
    return round(max(0, horizon - now) / 3600, 1)


def yttv_guide_channels():
    """YouTube TV's rows as guide channels, keyed as the lineup keys them.

    ``{"youtube-tv/NBC": channel}``, every row roku.json lists, numbered.
    A row with no listing -- none exists for it, or it has not been read
    yet -- is there with no events, so the guide can still offer it.
    Returns ``(channels, stored guide or None)``.
    """
    guide = yttv_epg.load_guide()
    if roku is None:
        return {}, guide
    stored = (guide or {}).get("channels") or {}
    catalog = numbered_roku_listings(
        {roku_lane.YOUTUBE_TV: roku.catalog(roku_lane.YOUTUBE_TV)})[roku_lane.YOUTUBE_TV]
    channels = {}
    for item in catalog["items"]:
        channel_id = str(item["id"])
        found = stored.get(channel_id) or {}
        key = f"{roku_lane.YOUTUBE_TV}/{channel_id}"
        channels[key] = {
            **{field: item[field] for field in ("tv_number", "tv_name", "tv_section")
               if field in item},
            "key": key, "kind": "roku", "source_id": roku_lane.YOUTUBE_TV,
            "channel_id": channel_id, "name": item.get("name"),
            "listing": {field: found[field] for field in
                        ("source", "station", "call_sign", "feed", "feed_channel")
                        if field in found} or None,
            **({"stale": True} if found.get("stale") else {}),
            "events": found.get("events") or [],
        }
    return channels, guide


# Days of games the guide reads when it is not given a window: as far ahead
# as YouTube TV's listings go.
SPORTS_GUIDE_HOURS = 48
_EASTERN = ZoneInfo("America/New_York")
# The line under a game's title in the guide: its sport, and what carries it.
SPORTS_LABELS = {
    roku_lane.SUNDAY_TICKET: "NFL Football · Sunday Ticket",
    roku_lane.THURSDAY_NIGHT_FOOTBALL: "NFL Football · Prime Video",
    roku_lane.MLB: "MLB Baseball",
    roku_lane.NHL: "NHL Hockey · ESPN Unlimited",
}


def _nhl_text(game, stream):
    """How an ESPN Unlimited NHL game streams, for its guide description."""
    if stream["exclusive"]:
        text = "An ESPN+ exclusive on ESPN Unlimited in YouTube TV: no blackouts."
    else:
        text = "Out of market on ESPN Unlimited in YouTube TV."
        for side in ("home", "away"):
            if stream.get(side):
                text += f" {side.title()} stream: {', '.join(stream[side])}."
    if stream["blackout"]:
        names = {team.get("abbreviation"): team.get("nickname") or team.get("name")
                 for team in (game.get("away") or {}, game.get("home") or {})}
        out = " and ".join(f"the {names.get(team) or team} in {market}"
                           for team, market in stream["blackout"].items())
        text += (f" May be blacked out: ESPN Unlimited blacks out {out}, and YouTube TV"
                 " places the Roku in DC or Lansing.")
    return text


def sports_event(source_id, item, now):
    """One game of a Roku game catalog as a guide event; None once it is over.

    It runs from its start to the projected final (estimated_end), and
    carries its target (``source_id``, ``channel_id``) and ``playable``:
    whether the Roku can be sent to it.
    """
    game = item.get("game") or {}
    if game.get("finished") is True or str(item.get("state") or "").lower() == "final":
        return None
    try:
        start = datetime.fromisoformat(str(item["start_time"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    if start.tzinfo is None:
        return None
    end = estimated_end(roku_lane.GAME_LEAGUES[source_id], game,
                        datetime.fromtimestamp(now, timezone.utc))
    name = item.get("name") or "Game"
    nicknames = [(game.get(side) or {}).get("nickname") for side in ("away", "home")]
    extra = {}
    if source_id == roku_lane.NHL:
        stream = item.get("espn_unlimited")
        if stream is None:
            return None
        text = _nhl_text(game, stream)
        extra = {"exclusive": stream["exclusive"],
                 **({"blackout": sorted(set(stream["blackout"].values()))} if stream["blackout"] else {})}
    elif source_id == roku_lane.MLB:
        route = item.get("route") or {}
        text = (f"On {route.get('channel')} from YouTube TV." if route.get("provider") == "youtube_tv"
                else "On MLB.TV." if route.get("provider") == "mlb"
                else "Nothing the Roku can reach carries it.")
    elif source_id == roku_lane.THURSDAY_NIGHT_FOOTBALL:
        text = "Thursday Night Football, on Prime Video."
    else:
        text = ("On NFL Sunday Ticket in YouTube TV. A game a local station carries is"
                " blacked out there, and the Roku plays that station instead.")
    text = f"{name}. {text}"
    return {"start": int(start.timestamp()),
            "duration": max(60, int((end - start).total_seconds())),
            "title": " at ".join(nicknames) if all(nicknames) else name,
            "episode_title": SPORTS_LABELS.get(source_id), "description": text, "live": True,
            "source_id": source_id, "channel_id": str(item.get("id")),
            "playable": bool(item.get("playable")) and source_id in roku_lane.SOURCES, **extra}


def sports_guide_channels(floor, ceiling, now):
    """Every game source's games as guide rows, on the Live Sports numbers.

    ``{"games/61": channel}``, one row per number. A games block numbers each
    day's games of all its sources together by start time, so a row holds
    that place in every day's slate, whatever the sport. Games already over
    are left out. A schedule that does not answer costs its own games only.
    Returns ``(channels, summary)``.
    """
    if roku is None:
        return {}, None
    # Games' days, which turn at 3 AM: a night's late games keep their rows past midnight.
    first, last = (date.fromisoformat(games_day(datetime.fromtimestamp(moment, _EASTERN)))
                   for moment in (floor, ceiling or now + SPORTS_GUIDE_HOURS * 3600))
    days = [(first + timedelta(days=n)).isoformat() for n in range(max(0, (last - first).days) + 1)]
    sources = (*roku_lane.GAME_SOURCES, *roku_lane.LISTED_SOURCES)
    # A request per schedule and day; one at a time, a slow one would hold up the guide.
    with ThreadPoolExecutor(max_workers=min(16, len(days) * len(sources))) as pool:
        asked = {(day, source_id): pool.submit(roku.catalog, source_id, day)
                 for day in days for source_id in sources}
    channels, errors, counts = {}, [], {}
    for day in days:
        listings = {}
        for source_id in sources:
            try:
                listings[source_id] = asked[(day, source_id)].result()
            except Exception as error:  # noqa: BLE001 - one schedule never sinks the guide
                errors.append({"source_id": source_id, "date": day, "message": str(error)})
        for source_id, listing in numbered_roku_listings(listings).items():
            for item in listing["items"]:
                event = sports_event(source_id, item, now)
                number = item.get("tv_number")
                if event is None or not isinstance(number, int):
                    continue
                section = item.get("tv_section") or "Live Sports"
                key = f"games/{number}"
                channels.setdefault(key, {
                    "key": key, "kind": "games", "tv_number": number, "tv_name": section,
                    "tv_section": item.get("tv_section"), "name": section, "events": [],
                })["events"].append(dict(event, date=day))
                counts[source_id] = counts.get(source_id, 0) + 1
    for channel in channels.values():
        channel["events"].sort(key=lambda event: event["start"])
    return channels, {"available": bool(counts) or not errors, "games": sum(counts.values()),
                      "by_source": counts, "errors": errors}


# VizelVision's Sports Guide (channel 6): today's games by sport, each on the
# channel that carries it. The day turns at 3 AM (roku_control/days.py); for
# three hours more, the night's games still on stay in it.
SPORTS_GUIDE_LATE_HOURS = 3
# Every viewer of the slides asks; the scoreboards change by the minute at most.
SPORTS_GUIDE_CACHE_SECONDS = 60
_sports_guide_cache = {"day": None, "at": 0.0, "payload": None}
# The pages that play the station's slide channels (lineup.SLIDES), their data,
# and the video a Roku plays instead (web_basic.py makes it). Cairo Radio's page plays
# that video: its card is drawn for the video, and it has no listings of its own.
SLIDE_PAGES = {"sports-guide": {"page_path": "sports-guide.html", "data_path": "/api/v1/sports-guide",
                                "stream_path": "/stream/sports-guide/index.m3u8"},
               "cairo-radio": {"page_path": "cairo-radio.html", "data_path": None,
                               "stream_path": "/stream/cairo-radio/index.m3u8"}}
HTTP_PORT = int(os.environ.get("PORT", {"feed": 8086, "control": 8087}.get(ROLE, 8080)))
# Where the Sports Guide renderer reads its slides: control's API (the feed role's renderer
# asks across the split).
CONTROL_URL = os.environ.get("TV_THING_CONTROL_URL",
                             f"http://127.0.0.1:{8087 if ROLE == 'feed' else HTTP_PORT}")
SLIDES_IDLE_SECONDS = float(os.environ.get("TV_THING_SLIDES_IDLE_SECONDS", "60"))
SLIDES_START_TIMEOUT = float(os.environ.get("TV_THING_SLIDES_START_TIMEOUT", "8"))
# The slide channels' video is web-basic's (web_basic.py, its own service and limits). Split,
# Apache sends their paths there, and these are never asked for; the one process (the
# rollback) makes them itself, in STREAMS_DIR.
SLIDE_STREAMS = web_basic.slide_streams(STREAMS_DIR, CONTROL_URL, SLIDES_IDLE_SECONDS)
# Split, control reads the slide channels' video from web-basic (its /web/v1/status).
web_basic_status = web_basic.StatusClient(
    os.environ.get("TV_THING_WEB_BASIC_URL", "http://127.0.0.1:8085"), lineup.SLIDES)


def slides_worker():
    """Stop slide video nobody watches; restart video that died or stalled."""
    while True:
        time.sleep(1)
        for stream in SLIDE_STREAMS.values():
            try:
                stream.check()
            except Exception as error:  # noqa: BLE001 - the worker must not die
                print(f"[{stream.name}] supervisor error: {error!r}")


def slide_views():
    """The station's slide channels with the pages that play them."""
    return [dict(view, **SLIDE_PAGES[view["channel_id"]]) for view in channel_lineup.slides()]


def api_v1_sports_guide(now=None):
    """Today's watchable games by sport, for the Sports Guide's slides.

    See sports_guide.py for what is listed and how. A schedule that does not
    answer costs its own games only and is named in ``errors``.
    """
    clock = int(time.time()) if now is None else now
    eastern = datetime.fromtimestamp(clock, _EASTERN)
    today = games_day(eastern)
    cache = _sports_guide_cache
    if (now is None and cache["day"] == today
            and time.monotonic() - cache["at"] < SPORTS_GUIDE_CACHE_SECONDS):
        return cache["payload"]
    payload = {"api_version": API_VERSION, "date": today, "generated_at": clock,
               "channel": next((view for view in slide_views() if view["channel_id"] == "sports-guide"), None)}
    if roku is None:
        return {**payload, "available": False, "games": 0, "sports": [], "errors": [
            {"message": f"{roku_unavailable_message()}, so TV Thing has no schedules."}]}
    days = [today]
    night = games_day(eastern - timedelta(hours=SPORTS_GUIDE_LATE_HOURS))
    if night != today:
        days.append(night)
    sources = (*roku_lane.GAME_SOURCES, *roku_lane.LISTED_SOURCES)

    def scoreboard(league, day):
        if league == "nfl":   # with Sunday Ticket's and Prime Video's targets
            return roku.sports.games(league, day)["games"]
        return roku.sports.scoreboard.games(league, day, power_five=False)

    with ThreadPoolExecutor(max_workers=16) as pool:
        asked = {("league", league, day): pool.submit(scoreboard, league, day)
                 for day in days for league in sports_guide.ESPN_LEAGUES}
        asked.update({("source_id", source_id, day): pool.submit(roku.catalog, source_id, day)
                      for day in days for source_id in sources})
        youtube_tv = pool.submit(roku.catalog, roku_lane.YOUTUBE_TV)
    errors, results = [], {}
    for (kind, name, day), future in asked.items():
        try:
            results[(kind, name, day)] = future.result()
        except Exception as error:  # noqa: BLE001 - one schedule never sinks the guide
            errors.append({kind: name, "date": day, "message": str(error)})
    try:
        cable = numbered_roku_listings(
            {roku_lane.YOUTUBE_TV: youtube_tv.result()})[roku_lane.YOUTUBE_TV]["items"]
    except Exception as error:  # noqa: BLE001 - the antenna's networks still place games
        cable = []
        errors.append({"source_id": roku_lane.YOUTUBE_TV, "date": None, "message": str(error)})
    channels = sports_guide.Channels(numbered_channels(load_channels()), cable)
    rows = []
    for day in days:
        catalogs = numbered_roku_listings({source_id: results[("source_id", source_id, day)]
                                           for source_id in sources if ("source_id", source_id, day) in results})
        espn = {league: results.get(("league", league, day)) for league in sports_guide.ESPN_LEAGUES}
        rows += sports_guide.day_games(espn, catalogs, channels, live_only=day != today)
    payload = {**payload, "available": bool(rows) or not errors, "games": len(rows),
               "sports": sports_guide.by_sport(rows), "errors": errors}
    if now is None:
        cache.update(day=today, at=time.monotonic(), payload=payload)
    return payload


def yttv_guide_summary(guide, now):
    """How much YouTube TV guide there is, for the guide's meta line."""
    return {
        "available": bool(guide and guide.get("channels_with_events")),
        "generated_at": (guide or {}).get("generated_at"),
        "age_seconds": now - guide["generated_at"] if guide and guide.get("generated_at")
        else None,
        "coverage_hours": remaining_coverage_hours(guide, now),
        "event_count": (guide or {}).get("event_count", 0),
        "channels_with_events": (guide or {}).get("channels_with_events", 0),
        "refresh_times": [f"{h:02d}:{m:02d}" for h, m in YTTV_GUIDE_REFRESH_TIMES],
        "next_refresh": next_refresh_at(YTTV_GUIDE_REFRESH_TIMES)
        if YTTV_GUIDE_ENABLED else None,
        "refreshing": yttv_guide_status["running"],
        "last_error": yttv_guide_status["last_error"],
        "failed_channels": sorted(yttv_guide_status["failed_channels"]),
    }


def guide_payload(number=None, window_hours=None, now=None):
    """The stored guide, with events that have already finished dropped.

    Past events are dead weight in a grid that starts at the current hour,
    and a full day of them across seventy channels is most of the response.

    The antenna's channels are keyed by their antenna number, as always;
    YouTube TV's follow, keyed ``youtube-tv/NAME`` (see yttv_guide_channels),
    then the day's games on the Live Sports numbers, keyed ``games/NUMBER``
    (see sports_guide_channels), and the internet streams that publish
    listings, keyed ``internet/ID`` (see internet_guide_channels).
    ``available`` and the counts beside it describe the broadcast guide;
    ``youtube_tv`` and ``sports`` the others.
    """
    now = int(time.time()) if now is None else now
    # Keep the hour already in progress so the grid has a left edge. One row asked
    # for (the Watch page asks per channel): no schedule but its own is read.
    window = channels.GuideWindow(now, now - 3600, now + window_hours * 3600 if window_hours else None,
                                  number)
    rows, summary = CHANNELS.guide(window)
    return {**summary, "channels": rows}


def name_antenna_games(channels):
    """The antenna's channels, their games named by their teams from ESPN.

    See sports_guide.name_games. A scoreboard that does not answer leaves
    its airings titled by their sport.
    """
    wanted = sports_guide.scoreboards_wanted(channels) if roku is not None else set()
    if not wanted:
        return channels
    with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        asked = {key: pool.submit(roku.sports.scoreboard.games, *key, power_five=False) for key in wanted}
    scoreboards = {}
    for key, future in asked.items():
        try:
            scoreboards[key] = future.result()
        except Exception:  # noqa: BLE001 - one schedule never sinks the guide
            continue
    return sports_guide.name_games(channels, scoreboards)


def guide_now_payload(now=None):
    """What is on right now, and what is next, per channel.

    Antenna channels by number, YouTube TV's as ``youtube-tv/NAME``, internet
    streams with listings as ``internet/ID``.
    """
    entries, summary = CHANNELS.guide_now(int(time.time()) if now is None else now)
    return {**summary, "channels": entries}


def normalize_quality(value):
    """Return a supported quality, preserving the legacy medium fallback."""
    return value if value in QUALITY_PRESETS else "medium"


def lane_stream_payload(lane):
    """Return a coherent snapshot while tuner operations are serialized."""
    with lock:
        return _lane_stream_payload(lane)


def _lane_stream_payload(lane):
    """Describe one lane's stream in the shape API clients consume."""
    state = feed_client.lane(lane)
    active = state["running"]
    ready = active and state["ready"]
    health = state.get("health") or {}
    failure = state.get("error") or health.get("reason")
    if health.get("ok") is False and not failure:
        failure = "Stream health check failed"
    # The Roku's picture keeps flowing while it navigates -- menus and all --
    # so "ready" stays about the media. "navigating" says the selection is in
    # motion; a lease holder fails closed on the generation, which changed
    # when the navigation began.
    operation = roku.operation_view() if lane == LANE_ROKU and roku is not None \
        else None
    navigating = bool(operation and operation["active"])
    if lane in tuning_lanes:
        phase = "tuning"
    elif failure:
        phase = "failed"
    elif navigating:
        phase = "navigating"
    elif ready:
        phase = "ready"
    else:
        phase = "buffering" if active else "idle"
    ready = ready and not failure and lane not in tuning_lanes
    # A Roku lease must fail closed when the picture stops being the verified
    # selection, not only when the reader restarts (see roku_lane.py).
    generation = (roku.generation() if lane == LANE_ROKU and roku is not None
                  else state["stream_id"])
    return {
        "state": phase,
        "error": {"code": "stream_failed", "message": failure} if failure else None,
        "generation": generation,
        "playback": [{"protocol": "hls", "url": f"stream/{LANE_PLAYLIST[lane]}",
                      "content_type": "application/vnd.apple.mpegurl"}],
        "shared": True,
        "lane": lane,
        "active": active,
        "ready": ready,
        "playlist_path": f"/stream/{LANE_PLAYLIST[lane]}",
        "content_type": ("application/vnd.apple.mpegurl"),
        "kind": "video",
        "id": state["stream_id"],
        "segment_duration_seconds": (ROKU_HLS_SEGMENT_TIME if lane == LANE_ROKU
                                     else HLS_SEGMENT_TIME),
        "health": state["health"],
        "started_at": state["started_at"],
        # The feed's count of this lane's changes; a command is issued against it.
        "revision": state["revision"],
    }


def lane_payload(lane):
    """Return a coherent snapshot while tuner operations are serialized."""
    with lock:
        return _lane_payload(lane)


def _lane_payload(lane):
    """Full description of one lane: what it is, what it is doing."""
    state = feed_client.lane(lane)
    config = FEED_CONFIG.lane(lane)
    frontend = config.device
    payload = {
        "id": lane,
        "name": config.name,
        "description": config.description,
        "node": FEED_CONFIG.node,
        "frontend": frontend,
        "lifecycle": state["lifecycle"],
        # Keys may be moving the picture: a navigation began and has not ended.
        "navigating": state["navigating"],
        # Lanes on different front-ends run concurrently; lanes that share
        # one displace each other.
        "conflicts_with": [
            other for other in ALL_LANES
            if other != lane and LANE_FRONTEND[other] == frontend
        ],
        "selection": state["selection"],
        "quality": state["quality"],
        "encoder": {
            "name": state["video_encoder"],
            "hardware": (
                state["video_encoder"] is not None
                and state["video_encoder"] != "libx264"
            ),
            "backend": config.backend,
        },
        "tuner": state["tuner"],
        "stream": lane_stream_payload(lane),
    }
    if lane == LANE_ROKU:
        payload["available"] = roku is not None
        payload["roku"] = roku.payload() if roku is not None else None
        payload["selection"] = (payload["roku"] or {}).get("selection")
    return payload


def frontend_payload():
    """Report the card's RF front-ends and who currently holds each."""
    result = []
    running = feed_client.running_lanes()
    for device in FEED_CONFIG.devices:
        frontend, label, detail = device.id, device.name, device.detail
        holders = [
            lane for lane in ALL_LANES
            if LANE_FRONTEND[lane] == frontend and lane in running
        ]
        result.append({
            "id": frontend,
            "name": label,
            "detail": detail,
            "lanes": [
                lane for lane in ALL_LANES
                if LANE_FRONTEND[lane] == frontend
            ],
            "in_use_by": holders[0] if holders else None,
            "busy": bool(holders),
        })
    return result


def status_payload():
    """Legacy single-stream status, mirroring the primary video lane.

    Kept so older clients (the Roku app, cached web pages) keep working
    while new clients read /api/v1/status.
    """
    running_lanes = feed_client.running_lanes()
    primary = feed_client.primary_video_lane()
    if primary not in running_lanes:
        other = next(
            (lane for lane in VIDEO_LANES if lane in running_lanes), None
        )
        if other:
            primary = other
    state = feed_client.lane(primary)
    analog = state if primary == LANE_ANALOG else feed_client.lane(LANE_ANALOG)
    running = state["running"]
    legacy_mode = None
    if running:
        legacy_mode = "tv" if primary == LANE_DIGITAL else "cable"

    cable_channel = None
    if analog["selection"]:
        cable_channel = analog["selection"].get("channel")

    return {
        "mode": legacy_mode,
        "channel": (
            state["selection"] if primary == LANE_DIGITAL else None
        ),
        "capture": analog["tuner"],
        "cable_channel": cable_channel,
        "streaming": running,
        "stream_ready": state["ready"],
        "tuner_backend": (
            TV_SOURCE_BACKEND if primary == LANE_DIGITAL else "analog-v4l2"
        ),
        "quality": state["quality"],
        "video_encoder": state["video_encoder"],
        "stream_id": state["stream_id"],
        "stream_health": state["health"],
        "gpu_encoding": (
            state["video_encoder"] is not None
            and state["video_encoder"] != "libx264"
        ),
        "hls_segment_time": HLS_SEGMENT_TIME,
        # The web UI polls this; without it a locked tuner looks like an
        # ordinary one until a tune comes back 423.
        "locks": {
            lane: lock_view(active_lock(lane)) for lane in ALL_LANES
        },
    }


def api_v1_status_payload():
    """Return a coherent snapshot while tuner operations are serialized."""
    with lock:
        return _api_v1_status_payload()


def _api_v1_status_payload():
    """Return the stable, multi-lane status document API clients read."""
    lanes_payload = {lane: lane_payload(lane) for lane in ALL_LANES}
    active = feed_client.running_lanes()
    legacy = status_payload()
    primary = feed_client.primary_video_lane()
    if primary not in active:
        primary = next(
            (lane for lane in VIDEO_LANES if lane in active), primary
        )

    return {
        "api_version": API_VERSION,
        # Which feed this is, and which run of it: a command carries both.
        "node": FEED_CONFIG.node,
        "feed_epoch": feed_client.status()["feed_epoch"],
        "locks": {lane: lock_view(active_lock(lane)) for lane in ALL_LANES},
        "lanes": lanes_payload,
        "active_lanes": active,
        "concurrent": len(active) > 1,
        "frontends": frontend_payload(),
        "primary_video_lane": primary,
        "streams": [
            lanes_payload[lane]["stream"] for lane in ALL_LANES
            if lane in active
        ],
        "busy_reason": busy_reason(),
        "scanning": {"channels": scanning},
        "recording": recording_summary(),
        # The station's slide channels as video, made only while watched.
        "slides": web_basic_status.slides() if ROLE == "control" else feed_client.slides(),
        # --- compatibility view: the primary video lane, flat ---
        "source": {
            "id": primary,
            "kind": primary,
            "name": lanes_payload[primary]["name"],
        } if primary in active else None,
        "selection": lanes_payload[primary]["selection"]
        if primary in active else None,
        "quality": legacy["quality"],
        "stream": {
            "id": legacy["stream_id"],
            "active": legacy["streaming"],
            "ready": legacy["stream_ready"],
            "playlist_path": (
                f"/stream/{LANE_PLAYLIST[primary]}"
                if primary in active else None
            ),
            "content_type": "application/vnd.apple.mpegurl",
            "segment_duration_seconds": legacy["hls_segment_time"],
            "health": legacy["stream_health"],
        },
        "encoder": {
            "name": legacy["video_encoder"],
            "hardware": legacy["gpu_encoding"],
            "backend": legacy["tuner_backend"],
        },
    }


def api_v1_digital_source():
    channels = numbered_channels(load_channels())
    return {
        "api_version": API_VERSION,
        "source": {
            "id": "digital",
            "kind": "digital",
            "lane": LANE_DIGITAL,
            "name": "Digital TV",
            "available": True,
            "count": len(channels),
            "selector": "channel_id",
            "stream_path": f"/stream/{LANE_PLAYLIST[LANE_DIGITAL]}",
        },
        "items": channels,
    }


def api_v1_cable_source():
    tuner = active_or_discovered_analog_tuner()
    channels = sorted(
        analog_tuner.cable_channels(),
        key=lambda channel: (channel["frequency_hz"], channel["number"]),
    )
    return {
        "api_version": API_VERSION,
        "source": {
            "id": "cable",
            "kind": "cable",
            "lane": LANE_ANALOG,
            "name": "Analog TV",
            "available": tuner is not None,
            "count": len(channels),
            "selector": "channel",
            "plan": "US cable STD (NTSC-M)",
            "tuner": tuner,
            "stream_path": f"/stream/{LANE_PLAYLIST[LANE_ANALOG]}",
        },
        "items": channels,
    }


def api_v1_source_catalog():
    """Every source a tune names, each with its lane and kind, from the registry's adapters.

    ``listed``: catalogs the Watch page lists but nothing can tune, apart from
    ``sources`` so vizelVision and the VCR never offer them. ``lineup``: the
    lineup card's sections, the station's own numbers, its slide channels and
    the internet streams. Every catalog item carries its tv_number; the cable
    tab is the analog tuner's RF plan and is not part of the lineup.
    """
    catalog = CHANNELS.catalog()
    return {
        "api_version": API_VERSION,
        "sources": catalog["sources"],
        "listed": catalog["listed"],
        "lineup": {"path": "/api/v1/lineup", "sections": channel_lineup.sections(), **catalog["lineup"]},
        "channels_path": "/api/v1/channels",
        "status_path": "/api/v1/status",
        "tune_path": "/api/v1/tune",
    }


def lineup_listing(requested_date=None):
    """``(channels in number order, Found, lineup.json's problem or None)``: every channel
    the registry lists, with ``requested_date``'s games. A catalog that cannot be read is
    left out and named in the Found's errors."""
    try:
        channel_lineup.plan()
        problem = None
    except lineup.LineupError as error:
        problem = str(error)
    found = channels.Found()
    return sorted(CHANNELS.channels(requested_date, found), key=channels.lineup_order), found, problem


def api_v1_lineup(requested_date=None):
    """Every channel with its TV Thing number, in number order, each in its kind's row.

    The station, its slide channels, the internet streams, the antenna and
    every Roku catalog (the day's games for ``requested_date``).
    """
    listed, found, problem = lineup_listing(requested_date)
    return {
        "api_version": API_VERSION,
        "date": found.date or requested_date,
        "sections": channel_lineup.sections(),
        "channels": [channel.row for channel in listed],
        "errors": found.errors,
        "lineup_error": problem,
    }


def api_v1_channels(requested_date=None):
    """The lineup's channels as one shape (channels.Channel), in number order: what carries
    each, the lane and node that play it, its network and market, the tune that plays it,
    its stream and its guide row."""
    listed, found, problem = lineup_listing(requested_date)
    return {
        "api_version": API_VERSION,
        "date": found.date or requested_date,
        "sections": channel_lineup.sections(),
        "channels": [channel.view() for channel in listed],
        "errors": found.errors,
        "lineup_error": problem,
    }


# ---------------------------------------------------------------------------
# Inputs (static/inputs.html): what feeds the dial beside the lanes /status reports -- the
# three services as systemd sees them, the web streams and where each comes from, and the
# disks the streams and recordings fill. Read-only.

INPUT_SERVICES = (
    ("feed", "tv-thing-feed", FEED_URL,
     "The tuners, the Roku capture, every encoder, the recorders and its relays"),
    ("control", "tv-thing", CONTROL_URL,
     "The API and this site, locks, tunes, Roku navigation, the guides and the VCR's rules"),
    ("web-basic", "tv-thing-web-basic", web_basic_status.url.rsplit("/web/", 1)[0],
     "The slide channels' video and the simple web relays, each only while watched"),
)
SYSTEMD_FIELDS = ("Id", "ActiveState", "SubState", "ActiveEnterTimestamp", "NRestarts", "MainPID",
                  "MemoryCurrent", "MemoryMax", "CPUUsageNSec", "CPUQuotaPerSecUSec", "TasksCurrent")
SYSTEMD_CACHE_SECONDS = 5.0
_systemd_cache = {"at": None, "units": {}}


def _systemd_number(value, scale=1):
    """A systemctl show number, or None for "[not set]", "infinity" and the like."""
    try:
        return int(value) / scale if scale != 1 else int(value)
    except (TypeError, ValueError):
        return None


def _quota_percent(value):
    # CPUQuotaPerSecUSec is "2s", "800ms" or "infinity": CPU time allowed each second.
    for suffix, scale in (("ms", 0.1), ("us", 0.0001), ("s", 100)):
        if value and value.endswith(suffix) and value[:-len(suffix)].replace(".", "", 1).isdigit():
            return round(float(value[:-len(suffix)]) * scale, 1)
    return None


def systemd_units(units, run=subprocess.run, clock=time.monotonic):
    """``{unit: facts}`` for the services' units from ``systemctl show``, read at most every
    five seconds; a unit systemd does not know (or no systemctl at all) is left out."""
    cache = _systemd_cache
    if cache["at"] is not None and clock() - cache["at"] < SYSTEMD_CACHE_SECONDS:
        return cache["units"]
    try:
        shown = run(["systemctl", "show", "--timestamp=unix", "-p", ",".join(SYSTEMD_FIELDS), *units],
                    capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        shown = ""
    found = {}
    for block in shown.split("\n\n"):
        values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit = values.get("Id", "").removesuffix(".service")
        if unit not in units or values.get("ActiveState") is None:
            continue
        since = values.get("ActiveEnterTimestamp", "")
        found[unit] = {
            "state": values["ActiveState"],
            "sub_state": values.get("SubState"),
            "since": _systemd_number(since[1:]) if since.startswith("@") else None,
            "restarts": _systemd_number(values.get("NRestarts")),
            "pid": _systemd_number(values.get("MainPID")) or None,
            "memory_bytes": _systemd_number(values.get("MemoryCurrent")),
            "memory_max_bytes": _systemd_number(values.get("MemoryMax")),
            "cpu_seconds": _systemd_number(values.get("CPUUsageNSec"), 1e9),
            "cpu_quota_percent": _quota_percent(values.get("CPUQuotaPerSecUSec")),
            "tasks": _systemd_number(values.get("TasksCurrent")),
            "read_at": round(time.time(), 3),   # a CPU rate is the CPU time between two reads
        }
    cache["at"], cache["units"] = clock(), found
    return found


# Where a looked-up stream's playlist comes from, by its resolver.
RESOLVER_SITES = {"yle-areena": "areena.yle.fi"}


def _upstream(stream):
    """``(host, origin)`` of an internet stream: where its playlist comes from, and whether
    that is "internal" (this machine or the LAN) or "external" (the web)."""
    url = stream.params.get("url") if internet_streams.RESOLVERS[stream.resolver].fixed else None
    if url is None:
        return RESOLVER_SITES.get(stream.resolver, stream.resolver), "external"
    parts = urlparse(url)
    host = parts.hostname or ""
    try:
        internal = host == "localhost" or ipaddress.ip_address(host).is_private
    except ValueError:
        internal = host.endswith((".local", ".lan"))
    return parts.netloc, "internal" if internal else "external"


def web_inputs():
    """The web streams on the dial, in number order: the house channel, the slide channels
    and the internet relays, with who makes each and where it comes from."""
    listed, _, _ = lineup_listing()
    try:
        configured = web_streams.streams()
    except internet_streams.StreamConfigError:
        configured = {}
    slides = web_basic_status.slides() if ROLE == "control" else feed_client.slides()
    house = urlparse(VIZELVISION_MEDIA_URL).netloc
    out = []
    for channel in listed:
        if channel.kind not in ("vizelvision", "slides", "internet"):
            continue
        entry = {"id": channel.id, "number": channel.number, "name": channel.name,
                 "kind": channel.kind, "stream": channel.stream, "note": channel.detail.get("note")}
        if channel.kind == "vizelvision":
            entry.update(origin="internal", service="vizelvision", upstream=house,
                         made_by="VizelVision's master control", status=None)
        elif channel.kind == "slides":
            entry.update(origin="internal", service="web-basic", upstream=None,
                         made_by="web-basic, drawing its slides", page=channel.detail.get("page_path"),
                         status=slides.get(channel.channel_id))
        else:
            stream = configured.get(channel.channel_id)
            host, origin = _upstream(stream) if stream else (None, None)
            entry.update(origin=origin, service=stream.service if stream else None, upstream=host,
                         resolver=stream.resolver if stream else None,
                         listings=bool(stream and stream.guide),
                         made_by=f"a relay in {stream.service}" if stream else None, status=None)
        out.append(entry)
    return out


def disk_inputs():
    """Free space where the streams, the web streams and the recordings are written, one
    entry a filesystem (named for everything on it); a folder not made yet is left out."""
    places = (("streams", STREAMS_DIR), ("web streams", web_basic.STREAMS_DIR),
              ("recordings", RECORDINGS_DIR))
    filesystems = {}
    for name, path in places:
        try:
            device, usage = os.stat(path).st_dev, shutil.disk_usage(path)
        except OSError:
            continue
        entry = filesystems.setdefault(device, {"names": [], "paths": [], "free_bytes": usage.free,
                                                "total_bytes": usage.total})
        entry["names"].append(name)
        entry["paths"].append(str(path))
    return list(filesystems.values())


def api_v1_inputs():
    """What feeds the dial, for the Inputs page: the services, the web streams and the disks.
    The tuners and the Roku capture are /status's lanes and frontends."""
    units = systemd_units(tuple(unit for _, unit, _, _ in INPUT_SERVICES))
    epoch = feed_client.status()["feed_epoch"]   # asks the feed, so `reachable` is current
    slides = web_basic_status.slides() if ROLE == "control" else {}
    answering = {
        "feed": bool(getattr(feed_client, "reachable", True)),
        "control": True,
        "web-basic": not any(state.get("unavailable") for state in slides.values()) if ROLE == "control" else None,
    }
    services = [{"id": service, "unit": unit, "url": url.rstrip("/"), "role": role,
                 "answering": answering[service], "systemd": units.get(unit)}
                for service, unit, url, role in INPUT_SERVICES]
    return {
        "api_version": API_VERSION,
        "node": FEED_CONFIG.node,
        "role": ROLE,
        "feed_epoch": epoch,
        "services": services,
        "web": web_inputs(),
        "disks": disk_inputs(),
    }


# ---------------------------------------------------------------------------
# Config (config.html): this host's settings and hardware (host_config.py).
#
# Each service reads the settings file when it starts, under its own unit's environment, which
# wins over the file; so each setting is shown as its services will see it. Saving writes the
# file and nothing else, and the page says which services a change waits for: restarting
# control drops nothing, restarting the feed drops every stream and recording.
# ---------------------------------------------------------------------------
PROCESS_STARTED = time.time()
CONFIG_UNITS = {service: unit for service, unit, _, _ in INPUT_SERVICES}
CONFIG_RESTART_NOTES = {
    "feed": "drops every stream and ends every recording",
    "control": "drops nothing",
    "web-basic": "drops only its own web streams",
}


def unit_environments(units, run=subprocess.run):
    """``{unit: {name: value}}``, each loaded unit's Environment= from ``systemctl show``."""
    try:
        shown = run(["systemctl", "show", "-p", "Id,LoadState,Environment", *units],
                    capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for block in shown.split("\n\n"):
        values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit = values.get("Id", "").removesuffix(".service")
        if unit not in units or values.get("LoadState") != "loaded":
            continue
        try:
            words = shlex.split(values.get("Environment", ""))
        except ValueError:
            words = []
        found[unit] = dict(word.split("=", 1) for word in words if "=" in word)
    return found


def config_environments():
    """Each service's environment. One process is every service; split, control's is its own
    and the others' are their units'."""
    own = dict(os.environ)
    if ROLE == "all":
        return {service: own for service in CONFIG_UNITS}
    units = unit_environments(tuple(CONFIG_UNITS.values()))
    return {service: own if service == "control" else units.get(unit, own)
            for service, unit in CONFIG_UNITS.items()}


def config_started():
    """When each service started (unix seconds), or None when unknown."""
    if ROLE == "all":
        return {service: PROCESS_STARTED for service in CONFIG_UNITS}
    units = systemd_units(tuple(CONFIG_UNITS.values()))
    started = {service: (units.get(unit) or {}).get("since") for service, unit in CONFIG_UNITS.items()}
    started["control"] = PROCESS_STARTED
    return started


def config_settings(environments, saved_at, started):
    """The settings for the page, each as its services see it, and whether one waits for a
    restart: the file changed after a service started, to a value other than the one this
    process started with."""
    views = {service: HOST.with_environment(env, saved=host_config.read_settings(HOST.path)[0])
             for service, env in environments.items()}
    at_start = {service: HOST.with_environment(env, saved=HOST.saved) for service, env in environments.items()}
    out = []
    for setting in host_config.SETTINGS:
        first = setting.services[0]
        resolved = views[first].get(setting.id)
        waiting = [service for service in setting.services
                   if saved_at is not None and (started.get(service) or 0) < saved_at
                   and views[service].value(setting.id) != at_start[service].value(setting.id)]
        pinned = [{"service": service, "unit": CONFIG_UNITS[service], "name": setting.env,
                   "value": environments[service][setting.env]}
                  for service in setting.services if setting.env in environments[service]]
        value = resolved.value
        out.append({
            "id": setting.id, "label": setting.label, "help": setting.help,
            "choices": list(setting.choices), "default": setting.default,
            "services": list(setting.services), "saved": views[first].saved.get(setting.id),
            "effective": str(value) if isinstance(value, Path) else value,
            "source": resolved.source, "reason": resolved.reason,
            "environment": pinned, "editable": not pinned,
            "waiting_for": waiting,
        })
    return out


def api_v1_config():
    """This host's settings and what its hardware is, for the Config page. Read-only."""
    path = HOST.path
    try:
        saved_at = path.stat().st_mtime
    except OSError:
        saved_at = None
    settings = config_settings(config_environments(), saved_at, config_started())
    waiting = [service for service in CONFIG_UNITS
               if any(service in item["waiting_for"] for item in settings)]
    _, problems = host_config.read_settings(path)
    return {
        "api_version": API_VERSION,
        "node": FEED_CONFIG.node,
        "node_role": HOST.value("role"),
        "process_role": ROLE,
        "feed_json": {"path": str(FEED_CONFIG_FILE), "node": HOST.primary_node()},
        "file": {"path": str(path), "exists": saved_at is not None,
                 "saved_at": saved_at, "problems": problems},
        "settings": settings,
        "restart": [{"service": service, "unit": CONFIG_UNITS[service],
                     "command": f"sudo systemctl restart {CONFIG_UNITS[service]}",
                     "note": CONFIG_RESTART_NOTES[service]} for service in waiting],
        "roku_lane": {"config": str(ROKU_CONFIG_FILE), "configured": ROKU_CONFIG_FILE.is_file(),
                      "running": roku is not None},
        "hardware": host_config.detect(HOST),
    }


def api_v1_config_update(body):
    """Save settings: ``{"settings": {id: value}}``, where "auto" (or null) is back to auto."""
    changes = body.get("settings") if isinstance(body, dict) else None
    if not isinstance(changes, dict) or not changes:
        return api_v1_error('Send {"settings": {"<id>": "<value>"}}', 400, "invalid_settings")
    environments = config_environments()
    current, _ = host_config.read_settings(HOST.path)
    updated, fields = dict(current), {}
    for key, value in changes.items():
        setting = host_config.BY_ID.get(key)
        if setting is None:
            fields[key] = "There is no such setting"
            continue
        pinned = [CONFIG_UNITS[service] for service in setting.services if setting.env in environments[service]]
        if pinned:
            fields[key] = f"Set by {', '.join(pinned)}'s environment ({setting.env}); change it there"
            continue
        try:
            kept = host_config.check(key, value, saving=True)
        except ValueError as error:
            fields[key] = str(error)
            continue
        if kept is None:
            updated.pop(key, None)
        else:
            updated[key] = kept
    if fields:
        return api_v1_error("Nothing was saved: " + "; ".join(f"{key}: {why}" for key, why in fields.items()),
                            400, "invalid_settings", {"fields": fields})
    try:
        host_config.write_settings(HOST.path, updated)
    except OSError as error:
        return api_v1_error(f"Could not write {HOST.path}: {error}", 500, "config_unwritable")
    print(f"Config: {', '.join(f'{key}={value!r}' for key, value in changes.items())} saved to {HOST.path}")
    return {**api_v1_config(), "ok": True}, 200


def lane_plays(lane, key, value):
    """Whether a lane is streaming the selection whose `key` is `value`, healthily.

    This alone lets anyone watch a locked tuner: asking for the channel it
    already plays changes nothing, so its stream is served as it is -- at the
    holder's quality, feeding the holder's recording taps.
    """
    state = feed_client.lane(lane)
    return (state["running"]
            and state["stream_id"] is not None
            and not state.get("error")
            and (state["selection"] or {}).get(key) == value
            and (state["health"] or {}).get("ok", True))


def perform_digital_tune(channel_id, quality="medium", token=None,
                         override=False):
    request_started = time.monotonic()
    quality = normalize_quality(quality)
    # Checked before the refresh is preempted: a locked tuner should not have
    # its guide sweep cancelled for a tune that is about to be refused.
    held = lock_holder(LANE_DIGITAL, token, override)
    if held and not lane_plays(LANE_DIGITAL, "id", channel_id):
        return lock_refusal(held, "tune")
    # A scheduled guide refresh holds the same front-end. Someone asking to
    # watch something outranks it, so take it back rather than refusing.
    preempt_guide_refresh()
    channel = next(
        (item for item in load_channels() if item["id"] == channel_id), None
    )
    if not channel:
        return {"error": "Channel not found"}, 404

    with lock:
        held = lock_holder(LANE_DIGITAL, token, override)
        watching = bool(held) and lane_plays(LANE_DIGITAL, "id", channel_id)
        if held and not watching:
            return lock_refusal(held, "tune")
        busy = busy_reason()
        if busy:
            return {"error": busy}, 409
        if override:
            override_tuner_lock(LANE_DIGITAL)
        digital = feed_client.lane(LANE_DIGITAL)
        reused = watching or (
            lane_plays(LANE_DIGITAL, "id", channel_id)
            and digital["quality"] == quality
            # A recording that starts on the channel already playing
            # needs a source generation that feeds its tap.
            and digital.get("taps", [])
            == recording_tap_ports(channel))
        if reused:
            ok, error, stream_reused = True, None, True
        else:
            tuning_lanes.add(LANE_DIGITAL)
            try:
                ok, error, stream_reused = feed_client.tune_digital(
                    channel, quality, taps=recording_tap_ports(channel))
            finally:
                tuning_lanes.discard(LANE_DIGITAL)
        state = feed_client.lane(LANE_DIGITAL)
        snapshot = copy.deepcopy(lane_payload(LANE_DIGITAL))
    if not ok:
        return {"error": error or "Failed to tune"}, 500
    return {
        "ok": True,
        "_lane": snapshot,
        "lane": LANE_DIGITAL,
        "channel": channel,
        "stream_reused": stream_reused,
        "stream_id": state["stream_id"],
        "stream_health": state["health"],
        "request_ms": round((time.monotonic() - request_started) * 1000, 1),
        "video_encoder": state["video_encoder"],
        "tuner_backend": TV_SOURCE_BACKEND,
        "hls_segment_time": HLS_SEGMENT_TIME,
    }, 200


def perform_cable_tune(channel, quality="medium", token=None,
                       override=False):
    request_started = time.monotonic()
    quality = normalize_quality(quality)
    try:
        channel = int(channel)
        frequency_hz = analog_tuner.cable_frequency_hz(channel)
    except (TypeError, ValueError):
        return {"error": "Cable channel must be between 1 and 135"}, 400

    with lock:
        held = lock_holder(LANE_ANALOG, token, override)
        watching = bool(held) and lane_plays(LANE_ANALOG, "channel", channel)
        if held and not watching:
            return lock_refusal(held, "tune")
        busy = busy_reason()
        if busy:
            return {"error": busy}, 409
        if override:
            override_tuner_lock(LANE_ANALOG)
        analog = feed_client.lane(LANE_ANALOG)
        reused = watching or (lane_plays(LANE_ANALOG, "channel", channel)
                              and analog["quality"] == quality)
        if reused:
            ok, error, tuner = True, None, analog["tuner"]
        else:
            tuning_lanes.add(LANE_ANALOG)
            try:
                ok, error, tuner = feed_client.tune_analog(channel, quality)
            finally:
                tuning_lanes.discard(LANE_ANALOG)
        state = feed_client.lane(LANE_ANALOG)
        snapshot = copy.deepcopy(lane_payload(LANE_ANALOG))
    if not ok:
        return {"error": error or "Failed to tune cable channel"}, 503
    return {
        "ok": True,
        "_lane": snapshot,
        "stream_reused": reused,
        "lane": LANE_ANALOG,
        "channel": channel,
        "frequency_hz": frequency_hz,
        "source": tuner,
        "stream_id": state["stream_id"],
        "stream_health": state["health"],
        "request_ms": round((time.monotonic() - request_started) * 1000, 1),
        "video_encoder": state["video_encoder"],
        "tuner_backend": "analog-v4l2",
        "hls_segment_time": HLS_SEGMENT_TIME,
    }, 200


def api_v1_scan_plan(include_repacked=False):
    """The selectable RF channel plan, with band and repack status."""
    plan = atsc_scan.rf_channels(include_repacked=include_repacked)
    tuned = {}
    for channel in load_channels():
        tuned.setdefault(channel["rf_channel"], []).append({
            "id": channel["id"],
            "number": channel["number"],
            "name": channel["name"],
        })
    for entry in plan:
        entry["known_channels"] = tuned.get(entry["rf_channel"], [])
    return {
        "api_version": API_VERSION,
        "first_rf_channel": atsc_scan.FIRST_RF_CHANNEL,
        "last_rf_channel_in_use": atsc_scan.LAST_RF_CHANNEL_IN_USE,
        "last_rf_channel_legacy": atsc_scan.LAST_RF_CHANNEL_LEGACY,
        "note": (
            "RF 37-69 was reallocated by the 2017 incentive auction and the "
            "repack completed in 2020. Those channels are excluded from a "
            "default scan and kept selectable for DX."
        ),
        "channels": plan,
    }


def perform_channel_scan(rf_channel, lock_timeout=2.0, dwell=0.0,
                         read_vct=True):
    """Tune one RF channel, measure it, and release the tuner.

    This is the DX path: it holds the digital front-end only for as long as
    the measurement takes. The analog lane is never touched, so an analog
    stream keeps playing throughout.
    """
    global scanning
    try:
        rf_channel = int(rf_channel)
        atsc_scan.rf_channel_frequency_hz(rf_channel)
    except (TypeError, ValueError) as error:
        return {"error": str(error)}, 400

    lock_timeout = max(0.3, min(10.0, float(lock_timeout or 2.0)))
    dwell = max(0.0, min(30.0, float(dwell or 0.0)))

    started = time.monotonic()
    with lock:
        held = lock_holder(LANE_DIGITAL)
        if held:
            return lock_refusal(held, "scan")
        busy = busy_reason()
        if busy:
            return {"error": busy}, 409
        scanning = True
        feed_client.stop(LANE_DIGITAL)
    try:
        # dvb_source closes its descriptors before stop_digital returns.
        time.sleep(0.2)
        result = feed_client.scan_channel(
            FRONTEND_DIGITAL, rf_channel, lock_timeout=lock_timeout,
            dwell=dwell, read_vct=read_vct,
        )
    except OSError as error:
        return {"error": f"Could not tune RF {rf_channel}: {error}"}, 503
    finally:
        with lock:
            scanning = False
        if ALWAYS_ON:
            always_on_wakeup.set()

    result["api_version"] = API_VERSION
    result["ok"] = True
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return result, 200


def api_v1_signal():
    """Sample the digital front-end without tuning or interrupting a stream."""
    try:
        sample = feed_client.measure(FRONTEND_DIGITAL)
    except OSError as error:
        return {
            "api_version": API_VERSION,
            "ok": False,
            "error": str(error),
        }, 503

    selection = feed_client.lane(LANE_DIGITAL)["selection"]
    return {
        "api_version": API_VERSION,
        "ok": True,
        "lane": LANE_DIGITAL,
        "tuned_to": {
            "name": selection.get("name"),
            "number": selection.get("number"),
            "frequency_hz": selection.get("frequency"),
            "rf_channel": selection.get("rf_channel"),
        } if selection else None,
        "signal": sample,
    }, 200


def api_v1_error(message, status, code=None, extra=None):
    codes = {
        400: "invalid_request", 404: "not_found", 409: "busy",
        423: "locked", 500: "tune_failed", 503: "unavailable",
    }
    payload = {
        "api_version": API_VERSION,
        "ok": False,
        "error": {"code": code or codes.get(status, "error"),
                  "message": message},
    }
    # A refusal is far more useful carrying the thing that caused it -- a
    # client that is told "locked" can show who holds it and until when.
    if extra:
        payload.update(extra)
    return payload, status


def api_v1_tune(body):
    """Dispatch one source-neutral tune request to the adapter that tunes its source."""
    if not isinstance(body, dict):
        return api_v1_error("JSON object required", 400)
    adapter, source_id = CHANNELS.tuner(str(body.get("source", "")).lower())
    quality = body.get("quality", "medium")
    if quality not in QUALITY_PRESETS:
        return api_v1_error(
            "quality must be low, medium, or high", 400, "invalid_quality"
        )
    if adapter is None:
        return api_v1_error("source must be " + CHANNELS.tune_choices(), 400, "invalid_source")
    return adapter.tune(source_id, body, quality)


def lane_tune_response(result, status, lane):
    """A digital or analog tune's ``(result, status)`` in the shape every tune answers."""
    if status != 200:
        return api_v1_error(result["error"], status, extra={
            "lock": result["lock"]} if "lock" in result else None)
    current = api_v1_status_payload()
    selected = result.get("_lane", current["lanes"][lane])
    return {
        "api_version": API_VERSION,
        "ok": True,
        "lane": lane,
        "source": {"id": lane, "kind": lane, "name": selected["name"]},
        "selection": selected["selection"],
        "quality": selected["quality"],
        "stream": selected["stream"],
        "encoder": selected["encoder"],
        "stream_reused": result.get("stream_reused", False),
        "active_lanes": current["active_lanes"],
        "request_ms": result["request_ms"],
    }, 200


def api_v1_stop(body):
    """Stop one lane, or every lane, without disturbing the others."""
    if not isinstance(body, dict):
        body = {}
    lane = body.get("lane")
    token = body.get("token")
    override = bool(body.get("override"))
    with lock:
        # Stopping a locked lane ends the thing the lock exists to protect, so it
        # is as much a change as retuning is.
        for candidate in (ALL_LANES if lane in (None, "", "all") else [lane]):
            if candidate not in ALL_LANES:
                continue
            held = lock_holder(candidate, token, override)
            if held:
                return api_v1_error(
                    f"{candidate} tuner is locked by {held['owner']}",
                    423, "locked", extra={"lock": lock_view(held)},
                )
        if override:
            for candidate in (ALL_LANES if lane in (None, "", "all") else [lane]):
                if candidate in ALL_LANES:
                    override_tuner_lock(candidate)
        if lane in (None, "", "all"):
            feed_client.stop_all()
            stopped = list(ALL_LANES)
        elif lane in ALL_LANES:
            feed_client.stop(lane)
            stopped = [lane]
        else:
            return api_v1_error(
                f"lane must be one of {', '.join(ALL_LANES)}",
                400, "invalid_lane",
            )
    if ALWAYS_ON and LANE_DIGITAL in stopped:
        always_on_wakeup.set()
    return {
        "api_version": API_VERSION,
        "ok": True,
        "stopped": stopped,
        "active_lanes": feed_client.running_lanes(),
    }, 200


class TVHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if not self._for_this_role():
            try:
                self._get()
            except feed_api.FeedUnavailable as error:
                self._feed_unavailable(error)

    def do_POST(self):
        if not self._for_this_role():
            try:
                self._post()
            except feed_api.FeedUnavailable as error:
                self._feed_unavailable(error)

    def _for_this_role(self):
        """Answer what this process does not serve; True when it was answered.

        The feed serves /stream/ and, to this host only, /feed/v1/. Control serves the rest,
        and /internal/ to this host only; its /stream/ is the feed's (Apache routes it there)."""
        path = urlparse(self.path).path
        if path.startswith("/internal/"):
            # Control's, to the feed on this host (internal_resolve). One process binds the
            # LAN and asks no one, so it has none.
            if ROLE != "control":
                self.send_json({"error": "Not found", "code": "control_only"}, 404)
            elif not feed_api.loopback(self.client_address[0]):
                self.send_json({"error": "Control's /internal/ answers only this host"}, 403)
            else:
                match = re.fullmatch(r"/internal/internet/([a-z0-9][a-z0-9-]*)/resolve", path)
                payload, status = (internal_resolve(match.group(1)) if match and self.command == "GET"
                                   else ({"error": "Not found"}, 404))
                self.send_json(payload, status)
            return True
        if ROLE == "feed":
            if path.startswith(feed_api.PREFIX):
                feed_api.answer(self, feed_http)
                return True
            if not path.startswith("/stream/"):
                self.send_json({"error": "This is TV Thing's feed; the API is control's",
                                "code": "feed_only"}, 404)
                return True
        elif ROLE == "control" and path.startswith("/stream/"):
            self.send_json({"error": "Streams are served by TV Thing's feed", "code": "control_only"}, 404)
            return True
        return False

    def _feed_unavailable(self, error):
        payload, status = api_v1_error(f"TV Thing's feed is not answering: {error}", 503, "feed_unavailable")
        try:
            self.send_json(payload, status)
        except OSError:
            pass

    def _get(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/v1/openapi.json":
            self.send_json(json.loads((BASE_DIR / "static" / "openapi.json").read_text()))
            return

        if path in ("/api/v1", "/api/v1/"):
            self.send_json({
                "api_version": API_VERSION,
                "name": "TV Thing",
                "description": (
                    "Hauppauge HVR-1600 tuner server. The card has two "
                    "independent RF front-ends, so one digital and one "
                    "analog stream can run at the same time."
                ),
                "lanes": {
                    lane: {
                        "frontend": LANE_FRONTEND[lane],
                        "stream_path": f"/stream/{LANE_PLAYLIST[lane]}",
                        "conflicts_with": [
                            other for other in ALL_LANES
                            if other != lane
                            and LANE_FRONTEND[other] == LANE_FRONTEND[lane]
                        ],
                    }
                    for lane in ALL_LANES
                },
                "openapi_url": "api/v1/openapi.json",
                "endpoints": {
                    "status": {"method": "GET", "path": "/api/v1/status"},
                    "sources": {"method": "GET", "path": "/api/v1/sources"},
                    "lineup": {
                        "method": "GET", "path": "/api/v1/lineup",
                        "query": {"date": "YYYY-MM-DD (games)"},
                        "note": "every channel with its TV Thing number",
                    },
                    "inputs": {
                        "method": "GET", "path": "/api/v1/inputs",
                        "note": "the services, the web streams and where each comes from, "
                                "and the disks; the tuners are status's lanes and frontends",
                    },
                    "config": {
                        "method": "GET", "path": "/api/v1/config",
                        "note": "this host's settings, what each resolves to and why, and its "
                                "hardware; POST {\"settings\": {id: value}} saves them, for "
                                "each service to read when it next starts",
                    },
                    "channels": {
                        "method": "GET", "path": "/api/v1/channels",
                        "query": {"date": "YYYY-MM-DD (games)"},
                        "note": "the lineup's channels in one shape: lane, node, network, "
                                "market, the tune that plays each, its stream and guide row",
                    },
                    "sports_guide": {
                        "method": "GET", "path": "/api/v1/sports-guide",
                        "note": "today's watchable games by sport (channel 6's slides)",
                    },
                    "digital_channels": {
                        "method": "GET",
                        "path": "/api/v1/sources/digital",
                    },
                    "cable_channels": {
                        "method": "GET", "path": "/api/v1/sources/cable",
                    },
                    "streams": {"method": "GET", "path": "/api/v1/streams"},
                    "signal": {
                        "method": "GET", "path": "/api/v1/signal",
                        "note": "live front-end meter; safe while streaming",
                    },
                    "scan_plan": {
                        "method": "GET", "path": "/api/v1/scan/plan",
                        "query": {"include_repacked": "0 | 1"},
                    },
                    "scan_channel": {
                        "method": "POST",
                        "path": "/api/v1/scan/channel",
                        "content_type": "application/json",
                        "body": {
                            "rf_channel": 9,
                            "lock_timeout": 2.0,
                            "dwell": 0.0,
                        },
                        "note": "tune and measure one RF channel (DX)",
                    },
                    "rescan": {
                        "method": "GET",
                        "path": "/api/rescan/stream",
                        "query": {
                            "start": "2", "end": "36",
                            "include_repacked": "0 | 1",
                            "lock_timeout": "1.5",
                        },
                        "note": "server-sent events; sweeps the band",
                    },
                    "guide": {
                        "method": "GET", "path": "/api/v1/guide",
                        "query": {"channel": "9.1", "hours": "12"},
                        "note": ("broadcast PSIP schedule; refreshed at "
                                 + ", ".join(f"{h:02d}:{m:02d}" for h, m
                                             in EPG_REFRESH_TIMES)),
                    },
                    "tuner_lock": {
                        "method": "GET | POST",
                        "path": "/api/v1/tuner/lock",
                        "body": {
                            "lane": "digital | analog | roku",
                            "owner": "recorder",
                            "until": "duration | program_end",
                            "duration_seconds": 3600,
                            "channel_id": "wusa_hd_9_1",
                            "reason": "recording",
                        },
                        "note": ("claim a front-end; POST returns a token "
                                 "that is required to release it early"),
                    },
                    "tuner_lock_release": {
                        "method": "POST",
                        "path": "/api/v1/tuner/lock/release",
                        "body": {"lane": "digital", "token": "..."},
                        "note": "override: true releases someone else's lock",
                    },
                    "guide_now": {
                        "method": "GET", "path": "/api/v1/guide/now",
                        "note": "what is on now and next, per channel",
                    },
                    "guide_refresh": {
                        "method": "GET",
                        "path": "/api/guide/refresh/stream",
                        "query": {"blocks": "4", "descriptions": "0 | 1"},
                        "note": ("server-sent events; takes the digital "
                                 "tuner, like a rescan"),
                    },
                    "health": {"method": "GET", "path": "/api/v1/health"},
                    "roku_sources": {
                        "method": "GET",
                        "path": "/api/v1/sources/{" + "|".join(roku_lane.SOURCES) + "}",
                        "query": {"date": "YYYY-MM-DD (games)"},
                        "note": "YouTube TV channels, games, Manual Roku",
                    },
                    "roku_tune": {
                        "method": "POST", "path": "/api/v1/tune",
                        "body": {"source": "youtube-tv", "channel_id": "ESPN",
                                 "token": "...", "force": False,
                                 "navigate": True, "resume": True},
                        "note": ("200 when the Roku already shows it, else 202 "
                                 "and a navigation operation to poll in "
                                 "/api/v1/status lanes.roku"),
                    },
                    "roku": {
                        "method": "GET", "path": "/api/v1/roku",
                        "note": "capture, verifier, calibration and Roku state",
                    },
                    "roku_remote": {
                        "method": "POST",
                        "paths": ["/api/v1/roku/keypress", "/api/v1/roku/text",
                                  "/api/v1/roku/launch"],
                        "body": {"key": "Select", "text": "news",
                                 "app_id": "195316", "token": "..."},
                        "keys": list(roku_lane.REMOTE_KEYS),
                        "note": "switches the lane to Manual Roku",
                    },
                    "roku_check": {
                        "method": "POST", "path": "/api/v1/roku/check",
                        "note": ("is the verified selection still playing? "
                                 "Renews it without pressing a key"),
                    },
                    "roku_cancel": {
                        "method": "POST", "path": "/api/v1/roku/cancel",
                        "body": {"token": "..."},
                    },
                    "recordings": {
                        "method": "GET | POST",
                        "path": "/api/v1/recordings",
                        "bodies": [
                            {"mode": "manual", "channel_id": "wjla_7_1",
                             "start": 1790190000, "end": 1790193600},
                            {"mode": "guide", "channel_number": "7.1",
                             "event_start": 1790190000},
                            {"mode": "otr"},
                            {"lane": "roku", "mode": "manual",
                             "target": {"source_id": "youtube-tv",
                                        "channel_id": "ESPN"},
                             "start": 1790190000, "end": 1790193600},
                            {"lane": "roku", "mode": "otr"},
                        ],
                        "note": ("timers, series rules, and the library; "
                                 "inputs[] lists the lanes that record"),
                    },
                    "recordings_search": {
                        "method": "GET", "path": "/api/v1/recordings/search",
                        "query": {"q": "news"},
                    },
                    "recordings_file": {
                        "method": "GET", "path": "/api/v1/recordings/file",
                        "query": {"name": "<library name>", "download": "1"},
                        "note": "byte ranges supported; download=1 saves",
                    },
                    "recordings_cancel": {
                        "method": "POST",
                        "path": "/api/v1/recordings/cancel",
                        "body": {"id": "<timer id>"},
                        "note": "cancels a timer, or stops the recording",
                    },
                    "recordings_rules": {
                        "method": "POST",
                        "path": "/api/v1/recordings/rules",
                        "body": {"title": "Jeopardy!", "channel_number": None},
                        "note": "record every airing of a guide name",
                    },
                    "recordings_rules_delete": {
                        "method": "POST",
                        "path": "/api/v1/recordings/rules/delete",
                        "body": {"id": "<rule id>"},
                    },
                    "recordings_erase": {
                        "method": "POST",
                        "path": "/api/v1/recordings/erase",
                        "body": {"name": "<library file name>"},
                    },
                    "tune": {
                        "method": "POST",
                        "path": "/api/v1/tune",
                        "content_type": "application/json",
                        "bodies": [
                            {"source": "digital",
                             "channel_id": "<id from sources/digital>",
                             "quality": "medium"},
                            {"source": "cable", "channel": 3,
                             "quality": "medium"},
                        ],
                    },
                    "stop": {
                        "method": "POST",
                        "path": "/api/v1/stop",
                        "content_type": "application/json",
                        "body": {"lane": "digital | analog | all"},
                    },
                },
                "qualities": {
                    name: {
                        "video_kbps": preset["vb"],
                        "audio_kbps": preset["ab"],
                        "height": preset["scale"],
                    }
                    for name, preset in QUALITY_PRESETS.items()
                },
                "legacy_stream_path": f"/stream/{LEGACY_PLAYLIST}",
            })
            return

        if path == "/api/v1/streams":
            self.send_json({
                "api_version": API_VERSION,
                "streams": [
                    lane_stream_payload(lane) for lane in ALL_LANES
                ],
                "active_lanes": feed_client.running_lanes(),
            })
            return

        if path == "/api/v1/health":
            active = feed_client.running_lanes()
            unhealthy = [
                lane for lane in active
                if (feed_client.lane(lane)["health"] or {}).get("ok") is False
            ]
            self.send_json({
                "api_version": API_VERSION,
                "ok": not unhealthy,
                "active_lanes": active,
                "unhealthy_lanes": unhealthy,
                "busy_reason": busy_reason(),
            }, status=200 if not unhealthy else 503)
            return

        if path == "/api/v1/signal":
            payload, status = api_v1_signal()
            self.send_json(payload, status=status)
            return

        if path == "/api/v1/scan/plan":
            include = parse_qs(parsed.query).get(
                "include_repacked", ["0"]
            )[0] == "1"
            self.send_json(api_v1_scan_plan(include_repacked=include))
            return

        if path == "/api/v1/sources":
            self.send_json(api_v1_source_catalog())
            return

        if path == "/api/v1/lineup":
            self.send_json(api_v1_lineup(
                parse_qs(parsed.query).get("date", [None])[0]))
            return

        if path == "/api/v1/inputs":
            self.send_json(api_v1_inputs())
            return

        if path == "/api/v1/config":
            self.send_json(api_v1_config())
            return

        if path == "/api/v1/channels":
            self.send_json(api_v1_channels(
                parse_qs(parsed.query).get("date", [None])[0]))
            return

        if path == "/api/v1/sports-guide":
            self.send_json(api_v1_sports_guide())
            return

        if path == "/api/v1/sources/digital":
            self.send_json(api_v1_digital_source())
            return

        if path == "/api/v1/sources/cable":
            self.send_json(api_v1_cable_source())
            return

        if (path.startswith("/api/v1/sources/")
                and (path[len("/api/v1/sources/"):] in roku_lane.SOURCES
                     or path[len("/api/v1/sources/"):] in roku_lane.LISTED_SOURCES)):
            payload, status = api_v1_roku_source(
                path[len("/api/v1/sources/"):],
                parse_qs(parsed.query).get("date", [None])[0],
            )
            self.send_json(payload, status=status)
            return

        if path == "/api/v1/roku":
            payload, status = api_v1_roku_payload()
            self.send_json(payload, status=status)
            return

        if path == "/api/v1/roku/apps":
            payload, status = roku_call(lambda: (
                {"api_version": API_VERSION, "apps": roku.apps()}, 200))
            self.send_json(payload, status=status)
            return

        if path.startswith("/api/v1/roku/operations/"):
            operation_id = path[len("/api/v1/roku/operations/"):]
            record = (roku.controller.get_operation(operation_id)
                      if roku is not None
                      and re.fullmatch(r"[0-9a-f]{32}", operation_id) else None)
            if record is None:
                payload, status = api_v1_error("No such operation", 404)
                self.send_json(payload, status=status)
            else:
                self.send_json(json.loads(json.dumps(
                    {"api_version": API_VERSION, "operation": record},
                    default=str)))
            return

        if path == "/api/v1/roku/preview":
            self.send_roku_preview()
            return

        if path == "/api/v1/status":
            self.send_json(api_v1_status_payload())
            return

        if path == "/api/channels":
            self.send_json(load_channels())
            return

        if path == "/api/sources":
            self.send_json({
                "sources": [
                    {
                        "id": "television",
                        "kind": "television",
                        "name": "Television",
                        "available": True,
                    },
                    {
                        "id": "cable",
                        "kind": "cable",
                        "name": "Analog TV",
                        "available": (
                            active_or_discovered_analog_tuner() is not None
                        ),
                    },
                ],
                "poll_interval_ms": SOURCE_POLL_INTERVAL_MS,
            })
            return

        if path == "/api/cable/channels":
            tuner = active_or_discovered_analog_tuner()
            self.send_json({
                "available": tuner is not None,
                "tuner": tuner,
                "plan": "US cable STD (NTSC-M)",
                "channels": analog_tuner.cable_channels(),
            })
            return

        if path == "/api/status":
            self.send_json(status_payload())
            return

        if path.startswith("/stream/vizelvision/"):
            self.send_vizelvision_media(parsed)
            return

        if path.startswith("/stream/internet/"):
            self.send_internet_stream(parsed)
            return

        if path.count("/") == 3 and path.split("/")[2] in SLIDE_STREAMS:
            self.send_slide_media(path.split("/")[2], path.split("/")[3])
            return

        if path.startswith("/stream/"):
            filename = path[len("/stream/"):]
            # /stream/live.m3u8 is an alias kept for clients that predate
            # per-lane playlists. Serve whichever video lane is primary;
            # segment names inside the playlist stay relative to /stream/,
            # so the client follows them without knowing the lane.
            if filename == LEGACY_PLAYLIST:
                lane = primary_video_lane
                if not lane_is_running(lane):
                    lane = next(
                        (name for name in VIDEO_LANES
                         if lane_is_running(name)),
                        lane,
                    )
                filename = LANE_PLAYLIST[lane]
            filepath = STREAMS_DIR / filename
            if filepath.exists() and filepath.is_relative_to(STREAMS_DIR):
                content = filepath.read_bytes()
                self.send_response(200)
                if filename.endswith(".m3u8"):
                    self.send_header("Content-Type",
                                     "application/vnd.apple.mpegurl")
                elif filename.endswith(".ts"):
                    self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.send_error(404)
            return

        if path == "/api/rescan/stream":
            self.handle_rescan_stream()
            return

        if path in ("/api/guide", "/api/v1/guide"):
            query = parse_qs(parsed.query)
            try:
                hours = float(query["hours"][0]) if "hours" in query else None
            except (ValueError, IndexError):
                hours = None
            self.send_json(guide_payload(
                number=query.get("channel", [None])[0], window_hours=hours,
            ))
            return

        if path in ("/api/tuner/lock", "/api/v1/tuner/lock"):
            self.send_json({
                "api_version": API_VERSION,
                "locks": tuner_lock_payload(),
                "max_seconds": LOCK_MAX_SECONDS,
                "default_seconds": LOCK_DEFAULT_SECONDS,
            })
            return

        if path in ("/api/guide/now", "/api/v1/guide/now"):
            self.send_json(guide_now_payload())
            return

        if path == "/api/guide/refresh/stream":
            self.handle_guide_refresh_stream()
            return

        if path == "/api/v1/recordings":
            self.send_json(recordings_payload())
            return

        if path == "/api/v1/recordings/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            self.send_json(recording_search_payload(query[:120]))
            return

        if path == "/api/v1/recordings/file":
            query = parse_qs(parsed.query)
            self.send_recording_file(
                query.get("name", [""])[0],
                download=query.get("download", ["0"])[0] == "1",
            )
            return

        # Static files
        if path == "/" or path == "":
            path = "/index.html"
        filepath = BASE_DIR / "static" / path.lstrip("/")
        if filepath.exists() and filepath.is_relative_to(BASE_DIR / "static"):
            content = filepath.read_bytes()
            self.send_response(200)
            ct = {
                ".html": "text/html",
                ".js": "application/javascript",
                ".css": "text/css",
                ".svg": "image/svg+xml",
                ".pdf": "application/pdf",
                ".woff2": "font/woff2",
            }.get(filepath.suffix, "application/octet-stream")
            self.send_header("Content-Type", ct)
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        self.send_error(404)

    def _post(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/v1/tune":
            try:
                body = self.read_json_body()
            except ValueError as error:
                payload, status = api_v1_error(
                    str(error), 400, "invalid_json"
                )
                self.send_json(payload, status=status)
                return
            payload, status = api_v1_tune(body)
            self.send_json(payload, status=status)
            return

        if parsed.path == "/api/v1/config":
            try:
                body = self.read_json_body()
            except ValueError as error:
                payload, status = api_v1_error(str(error), 400, "invalid_json")
            else:
                payload, status = api_v1_config_update(body)
            self.send_json(payload, status=status)
            return

        if parsed.path == "/api/tune":
            try:
                body = self.read_json_body()
            except ValueError as error:
                self.send_json({"error": str(error)}, status=400)
                return
            payload, status = perform_digital_tune(
                body.get("channel_id"), body.get("quality", "medium"),
                token=body.get("token"), override=bool(body.get("override")),
            )
            self.send_json(payload, status=status)
            return

        if parsed.path in ("/api/tuner/lock", "/api/v1/tuner/lock"):
            try:
                body = self.read_json_body()
            except ValueError as error:
                if parsed.path.startswith("/api/v1/"):
                    payload, status = api_v1_error(str(error), 400, "invalid_json")
                    self.send_json(payload, status=status)
                else:
                    self.send_json({"error": str(error)}, status=400)
                return
            payload, status = acquire_tuner_lock(body)
            self.send_json(payload, status=status)
            return

        if parsed.path in ("/api/tuner/lock/release",
                           "/api/v1/tuner/lock/release"):
            try:
                body = self.read_json_body()
            except ValueError as error:
                if parsed.path.startswith("/api/v1/"):
                    payload, status = api_v1_error(str(error), 400, "invalid_json")
                    self.send_json(payload, status=status)
                else:
                    self.send_json({"error": str(error)}, status=400)
                return
            payload, status = release_tuner_lock(body)
            self.send_json(payload, status=status)
            return

        if parsed.path == "/api/v1/scan/channel":
            try:
                body = self.read_json_body()
            except ValueError as error:
                payload, status = api_v1_error(
                    str(error), 400, "invalid_json"
                )
                self.send_json(payload, status=status)
                return
            payload, status = perform_channel_scan(
                body.get("rf_channel"),
                lock_timeout=body.get("lock_timeout", 2.0),
                dwell=body.get("dwell", 0.0),
                read_vct=body.get("read_vct", True),
            )
            if status != 200:
                payload, status = api_v1_error(payload["error"], status)
            self.send_json(payload, status=status)
            return

        if parsed.path == "/api/v1/stop":
            try:
                body = self.read_json_body()
            except ValueError as error:
                payload, status = api_v1_error(
                    str(error), 400, "invalid_json"
                )
                self.send_json(payload, status=status)
                return
            payload, status = api_v1_stop(body)
            self.send_json(payload, status=status)
            return

        if parsed.path == "/api/cable/tune":
            try:
                body = self.read_json_body()
            except ValueError as error:
                self.send_json({"error": str(error)}, status=400)
                return
            payload, status = perform_cable_tune(
                body.get("channel"), body.get("quality", "medium"),
                token=body.get("token"), override=bool(body.get("override")),
            )
            self.send_json(payload, status=status)
            return

        roku_routes = {
            "/api/v1/roku/cancel": roku_cancel,
            "/api/v1/roku/check": roku_check,
            "/api/v1/roku/keypress": lambda body: roku_remote(body, "keypress"),
            "/api/v1/roku/text": lambda body: roku_remote(body, "text"),
            "/api/v1/roku/launch": lambda body: roku_remote(body, "launch"),
            "/api/v1/roku/calibrate": roku_calibrate,
        }
        if parsed.path in roku_routes:
            try:
                body = self.read_json_body()
            except ValueError as error:
                payload, status = api_v1_error(
                    str(error), 400, "invalid_json"
                )
                self.send_json(payload, status=status)
                return
            if not isinstance(body, dict):
                body = {}
            payload, status = roku_routes[parsed.path](body)
            self.send_json(payload, status=status)
            return

        recording_routes = {
            "/api/v1/recordings": create_recording,
            "/api/v1/recordings/cancel": cancel_recording,
            "/api/v1/recordings/rules": create_recording_rule,
            "/api/v1/recordings/rules/delete": delete_recording_rule,
            "/api/v1/recordings/erase": erase_recording,
        }
        if parsed.path in recording_routes:
            try:
                body = self.read_json_body()
            except ValueError as error:
                payload, status = api_v1_error(
                    str(error), 400, "invalid_json"
                )
                self.send_json(payload, status=status)
                return
            payload, status = recording_routes[parsed.path](body)
            self.send_json(payload, status=status)
            return

        if parsed.path == "/api/stop":
            # Public clients cannot tear down the shared household stream.
            # Maintenance paths (scan and process shutdown) still call the
            # internal stop helpers directly, then the keeper resumes TV.
            always_on_wakeup.set()
            self.send_json({
                "ok": True,
                "always_on": ALWAYS_ON,
                "streaming": any(
                    lane in VIDEO_LANES for lane in feed_client.running_lanes()
                ),
            })
            return

        self.send_error(404)

    def do_OPTIONS(self):
        if urlparse(self.path).path.startswith("/api/"):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()
            return
        self.send_error(404)

    def read_json_body(self):
        """Read one small JSON request without allowing unbounded buffering."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid Content-Length") from error
        if length > 65_536:
            raise ValueError("JSON request body is too large")
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Request body must be valid JSON") from error

    def handle_rescan_stream(self):
        """Sweep the ATSC band natively and stream results as SSE events."""
        global scanning

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        def query_int(name, default):
            try:
                return int(query.get(name, [default])[0])
            except (TypeError, ValueError):
                return default

        # Default to the channels that still carry ATSC. Everything above RF
        # 36 was cleared by the 2020 repack, and sweeping it doubled the scan
        # for no result -- callers wanting it can ask.
        start_channel = max(
            atsc_scan.FIRST_RF_CHANNEL, query_int("start", None)
            or atsc_scan.FIRST_RF_CHANNEL
        )
        include_repacked = query.get("include_repacked", ["0"])[0] == "1"
        default_end = atsc_scan.LAST_RF_CHANNEL_LEGACY if include_repacked \
            else atsc_scan.LAST_RF_CHANNEL_IN_USE
        end_channel = min(
            atsc_scan.LAST_RF_CHANNEL_LEGACY,
            query_int("end", None) or default_end,
        )
        lock_timeout = max(0.3, min(10.0, float(
            query.get("lock_timeout", ["1.5"])[0] or 1.5
        )))

        held = lock_holder(
            LANE_DIGITAL, query.get("token", [None])[0],
            query.get("override", ["0"])[0] == "1",
        )
        if not held:
            preempt_guide_refresh()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def send_sse(event, data):
            payload = (
                f"event: {event}\n"
                f"data: {json.dumps(data) if not isinstance(data, str) else data}"
                "\n\n"
            )
            self.wfile.write(payload.encode())
            self.wfile.flush()

        started_scan = False
        try:
            if held:
                send_sse("done", {
                    "ok": False,
                    "error": (f"digital tuner is locked by {held['owner']}"),
                    "lock": lock_view(held),
                })
                return
            with lock:
                held = lock_holder(LANE_DIGITAL, query.get("token", [None])[0],
                                   query.get("override", ["0"])[0] == "1")
                if held:
                    send_sse("done", {"ok": False, "error": "Tuner locked", "lock": lock_view(held)})
                    return
                busy = busy_reason()
                if busy:
                    send_sse("done", {"ok": False, "error": busy})
                    return
                if query.get("override", ["0"])[0] == "1":
                    override_tuner_lock(LANE_DIGITAL)
                scanning = True
                started_scan = True
                # Only the digital front-end is needed for a scan. The analog
                # lane uses a different tuner, so an analog stream keeps
                # playing right through a rescan.
                send_sse("status", "Releasing the digital tuner...")
                feed_client.stop(LANE_DIGITAL)
            # dvb_source closes its descriptors before stop_digital returns;
            # give the kernel a short settle window before claiming them.
            time.sleep(0.2)

            channels_to_scan = list(range(start_channel, end_channel + 1))
            total = len(channels_to_scan)
            send_sse("status",
                     f"Scanning RF {start_channel}-{end_channel} "
                     f"({total} channels)...")

            found = []
            scan_started = time.monotonic()

            def on_result(result):
                rf = result["rf_channel"]
                index = channels_to_scan.index(rf) + 1
                send_sse("progress", f"{index / total * 100:.0f}")
                send_sse("lock", {
                    "frequency": result["frequency_hz"],
                    "frequency_mhz": f"{result['frequency_mhz']:.0f}",
                    "rf_channel": rf,
                    "locked": result["locked"],
                    "snr_db": result["snr_db"],
                    "signal_percent": result["signal_percent"],
                })
                services = result["virtual_channels"]
                if services:
                    send_sse("identify", {
                        "frequency": result["frequency_hz"],
                        "frequency_mhz": f"{result['frequency_mhz']:.0f}",
                        "rf_channel": rf,
                        "channels": services,
                    })
                    found.append(result)

            # Announce each channel before it is tuned so the grid can show
            # progress, then hand the sweep one channel at a time.
            def announce(rf):
                send_sse("frequency", {
                    "rf_channel": rf,
                    "frequency": atsc_scan.rf_channel_frequency_hz(rf),
                })

            with contextlib.closing(feed_client.sweep(
                    FRONTEND_DIGITAL, channels_to_scan, lock_timeout,
                    before=announce)) as results:
                for result in results:
                    on_result(result)

            elapsed = time.monotonic() - scan_started
            channels = channels_from_scan(found)
            if channels:
                save_channels(channels)
                send_sse("done", {
                    "ok": True,
                    "count": len(channels),
                    "frequencies": len(found),
                    "scanned": total,
                    "elapsed_seconds": round(elapsed, 1),
                })
            else:
                send_sse("done", {
                    "ok": False,
                    "error": (
                        f"No ATSC signal found on RF {start_channel}-"
                        f"{end_channel} after {elapsed:.0f}s. "
                        "Check the antenna connection."
                    ),
                })

        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:   # noqa: BLE001 - report to the client
            try:
                send_sse("done", {"ok": False, "error": str(error)})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        finally:
            if started_scan:
                with lock:
                    scanning = False
                if ALWAYS_ON:
                    always_on_wakeup.set()

    def handle_guide_refresh_stream(self):
        """Rebuild the guide now, streaming each frequency as it completes."""
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        def query_int(name, default):
            try:
                return int(query.get(name, [default])[0])
            except (TypeError, ValueError):
                return default

        blocks = max(1, min(32, query_int("blocks", EPG_BLOCKS)))
        descriptions = query.get("descriptions", ["0"])[0] == "1"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def send_sse(event, data):
            payload = (
                f"event: {event}\n"
                f"data: {json.dumps(data) if not isinstance(data, str) else data}"
                "\n\n"
            )
            self.wfile.write(payload.encode())
            self.wfile.flush()

        def progress(item):
            send_sse(item.pop("event"), item)

        try:
            guide, error = run_guide_refresh(
                reason="manual", progress=progress,
                blocks=blocks, descriptions=descriptions,
            )
            if error:
                send_sse("failed", {"ok": False, "error": error})
            else:
                send_sse("saved", {
                    "ok": True,
                    "event_count": guide["event_count"],
                    "channels_with_events": guide["channels_with_events"],
                    "coverage_hours": guide["coverage_hours"],
                    "elapsed_seconds": guide["elapsed_seconds"],
                    "compressed_strings": guide["compressed_strings"],
                    "aborted": guide.get("aborted", False),
                })
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:   # noqa: BLE001 - report to the client
            try:
                send_sse("failed", {"ok": False, "error": str(error)})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def send_recording_file(self, name, download=False):
        """Stream a finished tape to a browser to play or save.

        Range requests are honoured so the player can seek an hour-long
        recording without downloading all of it first. The file is sent in
        chunks from disk; nothing close to its size is ever held in memory.
        """
        path = recorder.library_file(RECORDINGS_DIR, name)
        if path is None:
            payload, status = api_v1_error("no such recording in the library",
                                           404, "unknown_recording")
            self.send_json(payload, status=status)
            return
        try:
            handle = path.open("rb")
        except OSError as error:
            payload, status = api_v1_error(f"could not open: {error}", 500,
                                           "read_failed")
            self.send_json(payload, status=status)
            return
        with handle:
            size = os.fstat(handle.fileno()).st_size
            try:
                span = recorder.byte_range(self.headers.get("Range"), size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = span if span else (0, size - 1)
            self.send_response(206 if span else 200)
            self.send_header("Content-Type", recorder.LIBRARY_CONTENT_TYPES.get(
                path.suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if span:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Disposition",
                             recorder.content_disposition(path.name, download))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            handle.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    chunk = handle.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_slide_media(self, name, filename):
        """A slide channel's video (web_basic.send_slide): the playlist starts it and waits
        for its first segments; a segment keeps it running."""
        web_basic.send_slide(self, name, SLIDE_STREAMS[name], filename, SLIDES_START_TIMEOUT)

    def send_vizelvision_media(self, parsed):
        """Relay the existing station HLS, without taking any tuner or encoder.

        Relative playlist references stay in this same-origin namespace, both
        directly on :8080 and under /tv/. Only flat HLS assets are accepted.
        """
        filename = parsed.path.removeprefix("/stream/vizelvision/")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:m3u8|mp4|m4s|ts)", filename):
            self.send_json({"error": "Invalid station media path"}, status=400)
            return
        url = VIZELVISION_MEDIA_URL + filename
        if parsed.query:
            url += "?" + parsed.query
        headers = {"Accept-Encoding": "identity"}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        try:
            with urlopen(Request(url, headers=headers), timeout=5) as response:
                content = response.read(VIZELVISION_MAX_MEDIA_BYTES + 1)
                if len(content) > VIZELVISION_MAX_MEDIA_BYTES:
                    self.send_json({"error": "Station media too large"}, status=502)
                    return
                status = response.status
                forwarded = {name: response.headers[name] for name in (
                    "Content-Type", "Content-Range", "Accept-Ranges"
                ) if response.headers.get(name)}
        except HTTPError as error:
            self.send_json({"error": "VizelVision stream unavailable"}, status=error.code)
            return
        except (URLError, OSError, TimeoutError):
            self.send_json({"error": "VizelVision stream unavailable"}, status=502)
            return
        self.send_response(status)
        for name, value in forwarded.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_internet_stream(self, parsed):
        """Relay an internet stream (web_basic.send_relay): its playlists, and its segments
        as they stream in. No tuner, lock or encoder."""
        web_basic.send_relay(self, web_streams, parsed)

    def send_roku_preview(self):
        """What the Roku verifier sees, as an MJPEG stream (engineering)."""
        if roku is None:
            payload, status = roku_unavailable()
            self.send_json(payload, status=status)
            return
        boundary = "rokupreview"
        self.send_response(200)
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        frames = roku.feedback.preview_frames()
        try:
            for jpeg in frames:
                self.wfile.write(
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                    + jpeg + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, RokuStreamError):
            pass
        finally:
            frames.close()

    def send_json(self, data, status=200):
        if isinstance(data, dict):
            data = {key: value for key, value in data.items() if key != "_lane"}
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        if args and feed_api.PREFIX in str(args[0]):
            return                      # RemoteFeed asks twice a second
        print(f"[HTTP] {args[0]}" if args else "")


def reap_orphaned_children(proc_root=Path("/proc"), kill=os.kill):
    """Kill leftover media children from an unclean previous run.

    Three kinds of orphan break a restarted service:

    * ``dvb_source.py`` keeps /dev/dvb/adapter0/frontend0 open, so every
      later tune fails with EBUSY -- a permanently dead digital lane that
      survives service restarts.
    * an orphaned ``ffmpeg`` keeps writing into this project's streams
      directory, so two encoders fight over one playlist while burning a
      core each. The Roku capture is one of these: it also holds the HD PVR.
    * an orphaned Roku verifier keeps the lane's tap port bound.

    Matching is deliberately narrow: this project's own source script, or a
    process whose arguments name this server's streams directory, and in
    both cases only processes owned by this user, and never one of another
    systemd service (feed_node.reapable).
    """
    script = str(BASE_DIR / "dvb_source.py")
    # The Sports Guide's renderer exits once its parent is gone, within a
    # second; this covers one that has not yet. Its encoder writes into the
    # streams directory, so the rule below already covers it.
    renderer = str(BASE_DIR / "sports_guide_video.py")
    streams = str(STREAMS_DIR)
    # The Roku verifier reads the lane's tap and writes to a pipe, so it
    # names neither the source script nor the streams directory. An orphan
    # would keep the tap port bound against every later verifier.
    roku_tap = f"udp://127.0.0.1:{ROKU_TAP_PORT}?"
    recordings = str(RECORDINGS_DIR)
    own_uid = os.getuid()
    try:
        own_cgroup = (proc_root / "self" / "cgroup").read_text()
    except OSError:
        own_cgroup = ""
    reaped = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            if entry.stat().st_uid != own_uid:
                continue
            parts = [
                part.decode("utf-8", "replace")
                for part in (entry / "cmdline").read_bytes().split(b"\0")
            ]
            status = (entry / "status").read_text()
            cgroup = (entry / "cgroup").read_text()
        except OSError:
            continue
        if not feed_node.reapable(own_cgroup, cgroup):
            continue
        if not parts:
            continue
        # Only genuinely orphaned processes. A crashed server's children are
        # reparented to init, while a *running* server's children still have
        # it as their parent -- reaping those would kill the live service
        # from a second instance started for testing.
        parent = next(
            (line.split()[1] for line in status.splitlines()
             if line.startswith("PPid:")),
            None,
        )
        if parent != "1":
            continue
        owns_tuner = script in parts or renderer in parts
        writes_streams = any(part.startswith(streams) for part in parts[1:])
        # An orphaned recorder is ffmpeg writing a .part into the library.
        records = any(part.startswith(recordings)
                      and part.endswith(recorder.PART_SUFFIX)
                      for part in parts[1:])
        reads_roku_tap = any(part.startswith(roku_tap) for part in parts[1:])
        if not (owns_tuner or writes_streams or records or reads_roku_tap):
            continue
        try:
            kill(pid, signal.SIGKILL)
            reaped.append(pid)
        except OSError:
            continue
    if reaped:
        print(f"Reaped orphaned media processes: {reaped}")
    return reaped


SIOCGIFADDR = 0x8915
# Interfaces that are never the LAN address we want to advertise.
NON_LAN_PREFIXES = ("lo", "docker", "br-", "veth", "tailscale", "virbr")


def interface_address(name):
    """Return one interface's current IPv4 address, or None."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(
            probe.fileno(),
            SIOCGIFADDR,
            struct.pack("256s", name.encode()[:15]),
        )
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None
    finally:
        probe.close()


def lan_addresses():
    """Current IPv4 addresses of real LAN interfaces, best guess first."""
    found = []
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return found
    for _index, name in interfaces:
        if name.startswith(NON_LAN_PREFIXES):
            continue
        address = interface_address(name)
        if address and address != "127.0.0.1":
            # Prefer ordinary private ranges over link-local autoconfig.
            rank = 1 if address.startswith("169.254.") else 0
            found.append((rank, address))
    return [address for _rank, address in sorted(found)]


LOOPBACK = "127.0.0.1"


def resolve_bind_spec(spec):
    """Resolve one ``TV_THING_BIND`` entry to an address, or None.

    An entry is either a literal address or ``iface:<name>``, which is
    resolved at startup so a DHCP change does not strand the service on an
    address the host no longer owns.
    """
    spec = spec.strip()
    if not spec:
        return None
    if spec.startswith("iface:"):
        return interface_address(spec.split(":", 1)[1])
    return spec


def bind_candidates(preferred, include_loopback=True):
    """Addresses to listen on, in order.

    ``TV_THING_BIND`` accepts a comma-separated list. Loopback is always
    included by default: the Apache reverse proxy forwards to
    127.0.0.1:8080, and binding only a LAN address left every proxied
    request refused. The wildcard is a last resort rather than the default
    because another service may already hold this port on a different
    interface.
    """
    candidates = []

    def add(address):
        if address and address not in candidates:
            candidates.append(address)

    if include_loopback:
        add(LOOPBACK)
    for spec in preferred.split(","):
        add(resolve_bind_spec(spec))
    for address in lan_addresses():
        add(address)
    add("0.0.0.0")
    return candidates


def open_listeners(server_factory, addresses, port):
    """Bind every usable address, returning (servers, bound, failures)."""
    servers, bound, failures = [], [], []
    for address in addresses:
        # A wildcard listener subsumes the specific ones; only fall back to
        # it when nothing else bound.
        if address == "0.0.0.0" and servers:
            continue
        try:
            servers.append(server_factory((address, port)))
        except OSError as error:
            failures.append(f"{address}: {error}")
            continue
        bound.append(address)
    return servers, bound, failures


HOST_WATCH_SECONDS = float(os.environ.get("TV_THING_HOST_WATCH_SECONDS", "30"))


def listen_on_new_addresses(server_factory, bound, candidates, port):
    """Bind the candidates not bound yet: (servers, addresses, failures).

    Never the wildcard: with loopback already bound it could only collide,
    with the specific listeners and with tailscale0's own :8080.
    """
    fresh = [address for address in candidates
             if address != "0.0.0.0" and address not in bound]
    if not fresh:
        return [], [], []
    return open_listeners(server_factory, fresh, port)


def cgroup_task_limit_hits(proc_cgroup="/proc/self/cgroup",
                           cgroup_root="/sys/fs/cgroup"):
    """How often the unit's TasksMax refused a process or thread, or None."""
    try:
        cgroup = Path(proc_cgroup).read_text().strip().split("::", 1)[1]
        events = Path(cgroup_root) / cgroup.lstrip("/") / "pids.events"
        for line in events.read_text().splitlines():
            key, value = line.split()
            if key == "max":
                return int(value)
    except (OSError, IndexError, ValueError):
        return None
    return None


def watch_host(server_factory, servers, bound, preferred, include_loopback,
               port, stopping, reported=()):
    """Pick up LAN addresses as they appear, and report the task cap.

    A boot can start the server before the network has its addresses. It
    then listened on 127.0.0.1 alone until the next restart (2026-10-06 and
    2026-10-13): Apache's /tv/ proxy worked, but the Roku app and anything
    else on the LAN found nothing on :8080.

    When TasksMax refuses a thread, ffmpeg reports only "Resource
    temporarily unavailable" and the failure looks like a bad capture or a
    dead tuner, so say so in the journal.
    """
    reported = set(reported)   # startup already printed these
    task_limit_hits = cgroup_task_limit_hits() or 0
    while not stopping.wait(HOST_WATCH_SECONDS):
        try:
            added, addresses, failures = listen_on_new_addresses(
                server_factory, bound,
                bind_candidates(preferred, include_loopback), port,
            )
            for server, address in zip(added, addresses):
                servers.append(server)
                bound.append(address)
                threading.Thread(target=server.serve_forever,
                                 daemon=True).start()
                print(f"Now also listening on http://{address}:{port}")
            for failure in failures:
                # An address another service holds fails on every pass.
                if failure not in reported:
                    reported.add(failure)
                    print(f"Skipped {failure}", file=sys.stderr)
            hits = cgroup_task_limit_hits()
            if hits is not None and hits > task_limit_hits:
                print(f"Task limit reached: TasksMax refused "
                      f"{hits - task_limit_hits} new thread(s) or process(es) "
                      f"({hits} since the service started)")
                task_limit_hits = hits
        except Exception as failure:   # noqa: BLE001 - the watch must not die
            print(f"Host watch error: {failure!r}")


def start_roku_lane():
    """Build the Roku lane from roku.json and, in one process, keep its capture running.
    Split, control navigates (its capture is the feed's, read through the feed client)."""
    global roku, roku_feedback
    capture_source = (roku_capture_source if ROLE == "all"
                      else lambda: feed_client.capture_source(LANE_ROKU))
    setting = HOST.roku(ROKU_CONFIG_FILE)
    if ROKU_CONFIG_FILE.is_file() and setting.value == "off":
        # Not even built: nothing here may press a key on a Roku (on a second node, the
        # primary's, which answers from anywhere on the LAN).
        print(f"Roku lane off on this host: {setting.reason} ({setting.source})")
        return
    try:
        roku = roku_lane.load(ROKU_CONFIG_FILE,
                              capture_source=capture_source,
                              record=feed_node.LaneRecord(feed_client, LANE_ROKU),
                              reuse_max_age=ROKU_REUSE_MAX_AGE)
    except RokuStreamError as error:
        # A broken roku.json costs the Roku lane, never the television.
        print(f"Roku lane disabled: {error}")
        roku = None
    if roku is None:
        print(f"Roku lane not configured ({ROKU_CONFIG_FILE})")
        return
    roku_feedback = roku.config.feedback
    print(f"Roku lane: {roku.ecp.base_url} via {roku_device()}")
    if ROLE == "all":
        threading.Thread(target=roku_keeper, daemon=True).start()
        roku_wakeup.set()


def start_roku_capture_lane():
    """The feed role's half of the Roku lane: keep the HD PVR's capture running."""
    global roku_feedback
    from roku_control.config import load_config
    if not ROKU_CONFIG_FILE.is_file():
        print(f"Roku capture not configured ({ROKU_CONFIG_FILE})")
        return
    setting = HOST.roku(ROKU_CONFIG_FILE)
    if setting.value == "off":
        print(f"Roku capture off on this host: {setting.reason} ({setting.source})")
        return
    try:
        roku_feedback = load_config(ROKU_CONFIG_FILE).feedback
    except RokuStreamError as error:
        print(f"Roku capture disabled: {error}")
        return
    print(f"Roku capture: {roku_device()}")
    threading.Thread(target=roku_keeper, daemon=True).start()
    roku_wakeup.set()


def main():
    shutdown_signal = None

    def request_shutdown(signum, _frame):
        nonlocal shutdown_signal
        shutdown_signal = signum
        raise KeyboardInterrupt

    # Handle SIGINT explicitly too. Python's default SIGINT handler also
    # raises KeyboardInterrupt, but leaves shutdown_signal unset, so an
    # interrupted service exited 0 and systemd's Restart=on-failure treated
    # a killed server as a successful run and never brought it back.
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    # Only a process that holds the hardware reaps, clears the streams, and stops them on the
    # way out. Control does none of it: its restart must leave every feed as it is.
    owns_feed = ROLE in ("all", "feed")
    STREAMS_DIR.mkdir(exist_ok=True)
    if owns_feed:
        reap_orphaned_children()
        for lane_files in LANE_FILES.values():
            clear_streams(lane_files)
        clear_streams((LEGACY_PLAYLIST.split(".")[0],))
        for stream in SLIDE_STREAMS.values():
            stream.clear()

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    preferred = os.environ.get("TV_THING_BIND", "")
    include_loopback = os.environ.get("TV_THING_BIND_LOOPBACK", "1") != "0"
    port = HTTP_PORT

    def server_factory(address):
        return ThreadedHTTPServer(address, TVHandler)

    # Split, both processes listen on loopback only, and never pick up the LAN: the front
    # door (Apache) owns :8080, and /feed/v1/ must never be reachable from the LAN.
    candidates = bind_candidates(preferred, include_loopback) if ROLE == "all" else [LOOPBACK]
    servers, bound, failures = open_listeners(server_factory, candidates, port)
    if not servers:
        for failure in failures:
            print(f"Could not bind {failure}", file=sys.stderr)
        raise SystemExit(f"No usable bind address for port {port}")
    for failure in failures:
        print(f"Skipped {failure}", file=sys.stderr)
    print(
        f"TV Thing {'server' if ROLE == 'all' else ROLE} running on "
        + ", ".join(f"http://{address}:{port}" for address in bound)
        + (f" (feed at {FEED_URL})" if ROLE == "control" else "")
    )

    if ROLE != "feed":
        channels = load_channels()
        print(f"Loaded {len(channels)} channels")

        tuner_locks.update(load_tuner_locks())
        for _lane, _entry in tuner_locks.items():
            print(f"Restored {_lane} tuner lock held by {_entry['owner']}, "
                  f"{max(0, round(_entry['expires_at'] - time.time()) // 60)} "
                  "minutes left")
        recover_recordings()
        start_roku_lane()
        threading.Thread(target=always_on_worker, daemon=True).start()
        threading.Thread(target=guide_worker, daemon=True).start()
        threading.Thread(target=yttv_guide_worker, daemon=True).start()
        threading.Thread(target=recording_worker, daemon=True).start()
    if owns_feed:
        if ROLE == "feed":
            start_roku_capture_lane()
        threading.Thread(target=recorder_keeper, daemon=True).start()
        threading.Thread(target=slides_worker, daemon=True).start()
    if ALWAYS_ON and ROLE != "feed":
        always_on_wakeup.set()

    # Every extra listener runs in its own thread; the first stays on the
    # main thread so signals still interrupt serve_forever().
    for extra in servers[1:]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    host_watch_stopping = threading.Event()
    if ROLE == "all":
        threading.Thread(
            target=watch_host,
            args=(server_factory, servers, bound, preferred, include_loopback,
                  port, host_watch_stopping, failures),
            daemon=True,
        ).start()

    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        host_watch_stopping.set()
        shutting_down.set()
        if owns_feed:
            shutdown_recording()
            stop_all()
            for stream in SLIDE_STREAMS.values():
                stream.stop("server shutting down")
        if roku is not None:
            roku.feedback.close()
        for server in list(servers):
            server.shutdown() if server is not servers[0] else None
            server.server_close()
    return 1 if shutdown_signal is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
