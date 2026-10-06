#!/usr/bin/env python3
"""Bounded live-stream and host-resource deployment gate for TV Thing.

Run this on the same host as a staged server. It performs real channel
changes, downloads and validates the resulting HLS, and fails when the server
uses excessive CPU/GPU/memory, heats the GPU past its limit, writes implausibly
large media, stalls, or reports an internal safety trip.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def http_bytes(base_url, path, method="GET", payload=None, timeout=5):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        data=data,
        method=method,
        headers={
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def http_json(base_url, path, method="GET", payload=None, timeout=5):
    return json.loads(http_bytes(base_url, path, method, payload, timeout))


def playlist_segments(text):
    segments = []
    duration = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXTINF:"):
            duration = float(line[len("#EXTINF:"):].split(",", 1)[0])
        elif line and not line.startswith("#"):
            segments.append((line, duration))
            duration = None
    return segments


def transport_stream_errors(data):
    first_sync = data.find(b"\x47", 0, 188)
    if first_sync < 0:
        return {"sync": 1, "transport": 0, "continuity": 0}

    errors = {"sync": 0, "transport": 0, "continuity": 0}
    counters = {}
    for offset in range(first_sync, len(data) - 187, 188):
        packet = data[offset:offset + 188]
        if packet[0] != 0x47:
            errors["sync"] += 1
            continue
        if packet[1] & 0x80:
            errors["transport"] += 1

        pid = ((packet[1] & 0x1f) << 8) | packet[2]
        adaptation_control = (packet[3] >> 4) & 0x03
        has_payload = adaptation_control in (1, 3)
        discontinuity = False
        if adaptation_control in (2, 3) and packet[4] > 0:
            discontinuity = bool(packet[5] & 0x80)
        if has_payload and pid != 0x1fff:
            counter = packet[3] & 0x0f
            previous = counters.get(pid)
            if (previous is not None and not discontinuity
                    and counter != (previous + 1) % 16):
                errors["continuity"] += 1
            counters[pid] = counter
    return errors


def process_group_sample(root_pid):
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,pcpu=,rss=,args="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=3,
        check=True,
    )
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 4)
        if len(fields) != 5:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
            cpu, rss_kib = float(fields[2]), int(fields[3])
        except ValueError:
            continue
        processes[pid] = {
            "ppid": ppid, "cpu": cpu, "rss_kib": rss_kib,
            "args": fields[4],
        }

    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, process in processes.items():
            if pid not in descendants and process["ppid"] in descendants:
                descendants.add(pid)
                changed = True

    selected = [processes[pid] for pid in descendants if pid in processes]
    return {
        "cpu_percent": sum(item["cpu"] for item in selected),
        "rss_mib": sum(item["rss_kib"] for item in selected) / 1024,
        "ffmpeg_count": sum("ffmpeg" in item["args"] for item in selected),
        "source_count": sum("dvb_source.py" in item["args"] for item in selected),
    }


def gpu_sample():
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,temperature.gpu,memory.used",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        values = [value.strip() for value in result.stdout.splitlines()[0].split(",")]
        return {
            "util_percent": float(values[0]),
            "temp_c": float(values[1]),
            "memory_mib": float(values[2]),
        }
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def preferred_channels(channels):
    return sorted(
        channels,
        key=lambda channel: (
            not str(channel.get("number", "")).endswith(".1"),
            channel.get("rf_channel", 999),
            channel.get("number", ""),
        ),
    )


def collect_playlist(base_url, seen_segments, failures, limits,
                     playlist="live.m3u8"):
    try:
        raw = http_bytes(
            base_url, f"/stream/{playlist}?_={time.time_ns()}", timeout=2
        )
    except (OSError, urllib.error.URLError):
        return []

    found = []
    for uri, duration in playlist_segments(raw.decode(errors="replace")):
        if uri in seen_segments:
            continue
        try:
            data = http_bytes(base_url, f"/stream/{uri}", timeout=3)
        except (OSError, urllib.error.URLError) as error:
            failures.append(f"playlist segment unavailable: {uri}: {error}")
            continue
        errors = transport_stream_errors(data)
        seen_segments[uri] = {
            "bytes": len(data), "duration": duration, "data": data,
        }
        found.append(uri)
        if duration is None or duration > limits.max_segment_duration:
            failures.append(f"segment duration out of bounds: {uri}={duration}")
        if len(data) > limits.max_segment_bytes:
            failures.append(
                f"segment too large: {uri}={len(data)} bytes "
                f"(limit {limits.max_segment_bytes})"
            )
        if errors != {"sync": 0, "transport": 0, "continuity": 0}:
            failures.append(f"transport errors in {uri}: {errors}")
    return found


def sample_host(base_url, server_pid, samples, failures, api_version="v1"):
    try:
        samples["process"].append(process_group_sample(server_pid))
    except (OSError, subprocess.SubprocessError) as error:
        failures.append(f"could not sample process group: {error}")
    gpu = gpu_sample()
    if gpu is not None:
        samples["gpu"].append(gpu)
    try:
        status_path = "/api/v1/status" if api_version == "v1" \
            else "/api/status"
        status = http_json(base_url, status_path, timeout=2)
        health = (status.get("stream") or {}).get("health") \
            if api_version == "v1" else status.get("stream_health")
        if health and not health.get("ok", True):
            failures.append(f"server safety trip: {health.get('reason')}")
    except (OSError, ValueError, urllib.error.URLError) as error:
        failures.append(f"status sampling failed: {error}")


def tune_and_observe(base_url, channel, args, seen_segments, samples,
                     failures):
    started = time.monotonic()
    cable = "cable_channel" in channel
    if args.api_version == "v1":
        payload = {
            "source": "cable" if cable else "digital",
            "quality": args.quality,
        }
        payload["channel" if cable else "channel_id"] = (
            channel["cable_channel"] if cable else channel["id"]
        )
        tune = http_json(
            base_url, "/api/v1/tune", method="POST",
            payload=payload,
            timeout=10,
        )
    elif cable:
        tune = http_json(
            base_url, "/api/cable/tune", method="POST",
            payload={
                "channel": channel["cable_channel"],
                "quality": args.quality,
            },
            timeout=10,
        )
    else:
        tune = http_json(
            base_url, "/api/tune", method="POST",
            payload={"channel_id": channel["id"], "quality": args.quality},
            timeout=10,
        )
    if not tune.get("ok"):
        error = tune.get("error", "tune rejected")
        if isinstance(error, dict):
            error = error.get("message", "tune rejected")
        raise RuntimeError(error)
    if not cable and tune.get("stream_reused"):
        failures.append("server reused an encoder across channel timestamps")
    stream_id = (tune.get("stream") or {}).get("id") \
        if args.api_version == "v1" else tune.get("stream_id")
    if not stream_id:
        failures.append("tune response omitted stream_id")

    first_segment = None
    deadline = started + args.max_request_seconds
    next_sample = 0.0
    while time.monotonic() < deadline and first_segment is None:
        found = collect_playlist(base_url, seen_segments, failures, args)
        if found:
            first_segment = found[0]
            break
        if time.monotonic() >= next_sample:
            sample_host(
                base_url, args.server_pid, samples, failures,
                args.api_version,
            )
            next_sample = time.monotonic() + 1
        time.sleep(0.1)
    latency = time.monotonic() - started
    if first_segment is None:
        failures.append(
            f"{channel['number']} produced no segment within "
            f"{args.max_request_seconds:g}s"
        )
        return stream_id, latency, 0

    round_start_count = len(seen_segments)
    dwell_deadline = time.monotonic() + args.dwell_seconds
    while time.monotonic() < dwell_deadline:
        collect_playlist(base_url, seen_segments, failures, args)
        sample_host(
            base_url, args.server_pid, samples, failures,
            args.api_version,
        )
        time.sleep(1)
    round_segments = len(seen_segments) - round_start_count + 1
    return stream_id, latency, round_segments


def roku_round(base_url, target, token, args, seen_segments, samples,
               failures):
    """Navigate the Roku to one target and watch its lane.

    The Roku's capture never restarts for a navigation, so a round measures
    request-to-verified (the navigation) rather than request-to-first-segment,
    and requires the capture generation to stay the same throughout.
    """
    source, _, channel_id = target.partition(":")
    started = time.monotonic()
    before = http_json(base_url, "/api/v1/status")["lanes"]["roku"]
    payload = {"source": source, "channel_id": channel_id, "token": token,
               "force": True}
    if source not in ("youtube-tv", "roku-manual"):
        payload["date"] = time.strftime("%Y-%m-%d")
    tune = http_json(base_url, "/api/v1/tune", method="POST",
                     payload=payload, timeout=20)
    if not tune.get("ok"):
        error = tune.get("error", "tune rejected")
        raise RuntimeError(error.get("message") if isinstance(error, dict)
                           else error)
    capture_id = (tune.get("stream") or {}).get("id")
    deadline = started + args.max_request_seconds
    verified, lane, next_sample = False, before, 0.0
    while time.monotonic() < deadline:
        collect_playlist(base_url, seen_segments, failures, args,
                         playlist="roku.m3u8")
        lane = http_json(base_url, "/api/v1/status", timeout=3)["lanes"]["roku"]
        operation = (lane.get("roku") or {}).get("operation") or {}
        if operation.get("state") == "succeeded":
            verified = True
            break
        if operation.get("state") in ("failed", "cancelled"):
            failures.append(f"{target}: navigation {operation['state']}: "
                            f"{operation.get('message')}")
            break
        if time.monotonic() >= next_sample:
            sample_host(base_url, args.server_pid, samples, failures)
            next_sample = time.monotonic() + 1
        time.sleep(0.5)
    latency = time.monotonic() - started
    if not verified and latency >= args.max_request_seconds:
        failures.append(f"{target} was not verified within "
                        f"{args.max_request_seconds:g}s")
    selection = lane.get("selection") or {}
    if verified and str(selection.get("channel_id", "")).casefold() \
            != channel_id.casefold():
        failures.append(f"{target}: lane shows {selection.get('channel_id')}")
    if lane["stream"].get("id") != capture_id:
        failures.append(f"{target}: the capture restarted during navigation "
                        f"({capture_id} -> {lane['stream'].get('id')})")
    if lane["stream"].get("generation") == before["stream"].get("generation"):
        failures.append(f"{target}: the generation did not change")

    dwelt = []
    dwell_deadline = time.monotonic() + args.dwell_seconds
    while time.monotonic() < dwell_deadline:
        dwelt += collect_playlist(base_url, seen_segments, failures, args,
                                  playlist="roku.m3u8")
        sample_host(base_url, args.server_pid, samples, failures)
        time.sleep(1)
    return capture_id, latency, dwelt


# The Roku lane's limits, set by the owner on 2026-10-06 when the Roku moved
# from vizelVision into TV Thing -- a deliberate policy for a different
# stream, not a relaxation of the tuner lanes' limits:
#   * the HD PVR encodes CBR 6.5 Mbit/s H.264 + AAC itself and TV Thing copies
#     it; its logo templates were calibrated at that bitrate, so it is not
#     lowered to fit the 4 Mbit/s tuner cap: media and wall rate <= 8 Mbit/s,
#     segments <= 1.5 MB and <= 3 s (0.5 s HLS on a 0.533 s GOP);
#   * a YouTube TV navigation took 39-124 s in vizelVision's history, so the
#     request limit is request-to-verified <= 150 s, not request-to-segment.
ROKU_DEFAULTS = {
    "max_request_seconds": 150, "max_media_mbps": 8, "max_wall_mbps": 8,
    "max_segment_bytes": 1_500_000, "max_segment_duration": 3,
    # The tuner lane's packager, the Roku capture, and the verifier while
    # the Roku navigates.
    "max_ffmpeg_processes": 3,
}


def mean(values):
    return sum(values) / len(values) if values else 0.0


def decode_generation(stream_id, seen_segments, segment_prefix="segment",
                      uris=None):
    """Decode one collected HLS generation (or the given segments of it) in
    memory and return stderr."""
    prefix = f"{segment_prefix}_{stream_id}_"
    payload = b"".join(
        item["data"] for uri, item in sorted(seen_segments.items())
        if uri.startswith(prefix) and (uris is None or uri in uris)
    )
    if not payload:
        return False, "no media collected for generation"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-xerror", "-f", "mpegts", "-i", "pipe:0",
                "-map", "0:v:0", "-f", "null", "-",
            ],
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        stderr = result.stderr.decode(errors="replace").strip()
        return result.returncode == 0, stderr
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8099")
    parser.add_argument("--server-pid", type=int, required=True)
    parser.add_argument(
        "--api-version", choices=("v1", "legacy"), default="v1",
        help="tuning/status API exercised by the gate (default: v1)",
    )
    parser.add_argument("--quality", choices=("low", "medium", "high"),
                        default="low")
    parser.add_argument(
        "--channel-id", action="append", default=[],
        help="channel id to exercise (repeat twice); defaults to two services",
    )
    parser.add_argument(
        "--cable-channel", action="append", type=int, default=[],
        help="analog cable channel to exercise",
    )
    parser.add_argument("--retunes", type=int, default=3)
    parser.add_argument("--dwell-seconds", type=float, default=10)
    # Tune request to first HLS segment. Raised from 8 s on 2026-09-23 at the
    # owner's call: WJLA (7.1) settles in 4-5 s, but WHUT (32.1), on the same
    # RF 7 multiplex, measured 6.3-8.1 s across that evening's gates and tipped
    # an otherwise clean run over 8 s. 10 s still catches a tune that never
    # produces video.
    parser.add_argument("--max-request-seconds", type=float, default=10)
    parser.add_argument("--max-segment-duration", type=float, default=3)
    parser.add_argument("--max-segment-bytes", type=int, default=1_000_000)
    parser.add_argument("--max-media-mbps", type=float, default=4)
    parser.add_argument("--max-wall-mbps", type=float, default=4)
    parser.add_argument("--max-cpu-average", type=float, default=200)
    # Peak is measured with ps %CPU, which is cputime/elapsed -- so a
    # multi-threaded x264 init that burns ~1s of CPU across cores in its
    # first ~0.3s of life reads ~300%, reproducibly, without being sustained
    # load. Measured on viviserv: 153% steady state, 110% peak for a single
    # tune, 200-300% only across a retune. 350 clears that transient while
    # staying under the unit's own CPUQuota=400%, so a genuine runaway still
    # trips this.
    parser.add_argument("--max-cpu-peak", type=float, default=350)
    parser.add_argument("--max-rss-mib", type=float, default=512)
    parser.add_argument("--max-gpu-average", type=float, default=85)
    # Raised from 80 C by the owner on 2026-10-06. The Quadro K4200 slows down at
    # 97 C and shuts down at 102 C (nvidia-smi -q -d TEMPERATURE); 80 C is only its
    # rated "max operating" figure. vizelVision's master encodes on NVENC and holds
    # the card near 86 C on its own, which failed a gate at 0% TV Thing GPU use.
    parser.add_argument("--max-gpu-temp", type=float, default=90)
    parser.add_argument("--require-encoder")
    parser.add_argument(
        "--max-ffmpeg-processes", type=int, default=2,
        help="Encoders allowed at once. Two lanes may stream concurrently, "
             "so the default tolerates the other lane's encoder.",
    )
    parser.add_argument("--min-segments-per-round", type=int, default=3)
    parser.add_argument(
        "--roku-target", action="append", default=[],
        help="gate the Roku lane: SOURCE:ID, e.g. youtube-tv:NBC (repeat). "
             "Applies the Roku lane's own limits (ROKU_DEFAULTS) to any "
             "limit not given explicitly, and holds the roku lock while it runs.",
    )
    parser.add_argument("--lock-owner", default="stream gate")
    explicit = {
        action.dest for action in parser._actions
        if any(option in (argv if argv is not None else sys.argv[1:])
               for option in action.option_strings)
    }
    args = parser.parse_args(argv)
    if args.roku_target:
        for key, value in ROKU_DEFAULTS.items():
            if key not in explicit:
                setattr(args, key, value)

    started = time.monotonic()
    failures = []
    seen_segments = {}
    samples = {"process": [], "gpu": []}
    latencies = []
    stream_ids = []
    round_counts = []
    # The Roku keeps one capture through every navigation: each round's dwell
    # (about ten seconds of 720p60) is decoded, not the whole session.
    roku_samples = []
    # Per-lane segment prefixes: each lane writes its own playlist.
    segment_prefix = "analogseg" if args.cable_channel else "digitalseg"
    roku_token = None

    try:
        if args.roku_target:
            segment_prefix = "rokuseg"
            if args.channel_id or args.cable_channel:
                raise RuntimeError("--roku-target gates the Roku lane alone")
            claim = http_json(args.url, "/api/v1/tuner/lock", method="POST",
                              payload={"lane": "roku", "owner": args.lock_owner,
                                       "duration_seconds": 1800,
                                       "reason": "real-stream gate"})
            roku_token = claim.get("token")
            if not roku_token:
                raise RuntimeError(f"could not lock the Roku: {claim.get('error')}")
            targets = args.roku_target * max(1, -(-args.retunes // len(args.roku_target)))
            for index in range(args.retunes):
                target = targets[index]
                print(f"[{index + 1}/{args.retunes}] navigating the Roku to "
                      f"{target}", flush=True)
                capture_id, latency, dwelt = roku_round(
                    args.url, target, roku_token, args, seen_segments,
                    samples, failures)
                stream_ids.append(capture_id)
                latencies.append(latency)
                round_counts.append(len(dwelt))
                roku_samples.append((capture_id, set(dwelt)))
            selected = []
        elif args.channel_id and args.cable_channel:
            raise RuntimeError(
                "--channel-id and --cable-channel are mutually exclusive"
            )
        if args.cable_channel:
            if any(not 1 <= channel <= 135 for channel in args.cable_channel):
                raise RuntimeError("cable channels must be between 1 and 135")
            selected = [
                {
                    "number": str(channel),
                    "name": f"Cable {channel}",
                    "cable_channel": channel,
                }
                for channel in args.cable_channel
            ]
        else:
            if args.api_version == "v1":
                channel_payload = http_json(
                    args.url, "/api/v1/sources/digital"
                )
                channels = preferred_channels(channel_payload.get("items", []))
            else:
                channels = preferred_channels(
                    http_json(args.url, "/api/channels")
                )
            if len(channels) < 2:
                raise RuntimeError("at least two saved channels are required")
            if args.channel_id:
                selected = [
                    next((channel for channel in channels
                          if channel["id"] == channel_id), None)
                    for channel_id in args.channel_id
                ]
                if any(channel is None for channel in selected):
                    raise RuntimeError("a requested --channel-id was not found")
            else:
                # Prefer two services on one multiplex. This exercises
                # timestamp replacement without making RF reception the
                # dominant variable.
                first = channels[0]
                sibling = next(
                    (channel for channel in channels[1:]
                     if channel["frequency"] == first["frequency"]),
                    channels[1],
                )
                selected = [first, sibling]
        if len(selected) < 2:
            selected = selected * 2
        for index in range(args.retunes if not args.roku_target else 0):
            channel = selected[index % len(selected)]
            print(
                f"[{index + 1}/{args.retunes}] tuning "
                f"{channel['number']} {channel['name']}",
                flush=True,
            )
            stream_id, latency, count = tune_and_observe(
                args.url, channel, args, seen_segments, samples, failures
            )
            stream_ids.append(stream_id)
            latencies.append(latency)
            round_counts.append(count)
    except Exception as error:
        failures.append(str(error))
    finally:
        if roku_token:
            try:
                http_json(args.url, "/api/v1/tuner/lock/release", method="POST",
                          payload={"lane": "roku", "token": roku_token})
            except Exception as error:   # noqa: BLE001 - report, do not mask
                failures.append(f"could not release the Roku lock: {error}")

    elapsed = max(time.monotonic() - started, 0.001)
    total_bytes = sum(item["bytes"] for item in seen_segments.values())
    total_duration = sum(
        item["duration"] or 0 for item in seen_segments.values()
    )
    media_mbps = (total_bytes * 8 / total_duration / 1_000_000
                  if total_duration else float("inf"))
    wall_mbps = total_bytes * 8 / elapsed / 1_000_000
    cpu_values = [item["cpu_percent"] for item in samples["process"]]
    rss_values = [item["rss_mib"] for item in samples["process"]]
    gpu_values = [item["util_percent"] for item in samples["gpu"]]
    temp_values = [item["temp_c"] for item in samples["gpu"]]
    ffmpeg_counts = [item["ffmpeg_count"] for item in samples["process"]]
    source_counts = [item["source_count"] for item in samples["process"]]

    status = {}
    try:
        status = http_json(
            args.url,
            "/api/v1/status" if args.api_version == "v1" else "/api/status",
        )
    except Exception as error:
        failures.append(f"final status unavailable: {error}")

    decoded_generations = 0
    decode_sets = roku_samples if args.roku_target \
        else [(stream_id, None) for stream_id in stream_ids]
    for stream_id, uris in decode_sets:
        if not stream_id:
            continue
        decoded, decode_error = decode_generation(
            stream_id, seen_segments, segment_prefix, uris
        )
        if decoded:
            decoded_generations += 1
        else:
            failures.append(
                f"generation {stream_id} failed software decode: "
                f"{decode_error[:300]}"
            )

    checks = [
        (latencies and max(latencies) <= args.max_request_seconds,
         f"request-to-{'verified' if args.roku_target else 'stream'} max "
         f"{max(latencies, default=float('inf')):.3f}s"),
        (round_counts and min(round_counts) >= args.min_segments_per_round,
         f"segments per round {round_counts}"),
        (media_mbps <= args.max_media_mbps,
         f"encoded media rate {media_mbps:.3f} Mbit/s"),
        (wall_mbps <= args.max_wall_mbps,
         f"wall-clock write rate {wall_mbps:.3f} Mbit/s"),
        (mean(cpu_values) <= args.max_cpu_average,
         f"process CPU average {mean(cpu_values):.1f}%"),
        (max(cpu_values, default=0) <= args.max_cpu_peak,
         f"process CPU peak {max(cpu_values, default=0):.1f}%"),
        (max(rss_values, default=0) <= args.max_rss_mib,
         f"process RSS peak {max(rss_values, default=0):.1f} MiB"),
        # One encoder per *active lane*, not one overall: the digital and
        # analog lanes run concurrently by design. This gate exercises a
        # single lane, so anything beyond one extra long-lived encoder from
        # the other lane means a leaked generation.
        (max(ffmpeg_counts, default=0) <= args.max_ffmpeg_processes,
         f"concurrent ffmpeg processes max {max(ffmpeg_counts, default=0)} "
         f"(limit {args.max_ffmpeg_processes})"),
        # At most one DVB source at a time. Testing the analog lane does not
        # imply zero: the always-on digital lane keeps running alongside it.
        # Two or more means a previous tune leaked its source.
        (max(source_counts, default=0) <= 1,
         f"concurrent DVB sources max {max(source_counts, default=0)}"),
        (mean(gpu_values) <= args.max_gpu_average,
         f"GPU utilization average {mean(gpu_values):.1f}%"),
        (max(temp_values, default=0) <= args.max_gpu_temp,
         f"GPU temperature peak {max(temp_values, default=0):.1f} C"),
    ]
    if args.roku_target:
        checks += [
            (len({item for item in stream_ids if item}) == 1,
             f"one continuous Roku capture across navigations "
             f"({len(set(stream_ids))} seen)"),
            (decoded_generations == args.retunes,
             f"clean software-decodable dwell samples "
             f"{decoded_generations}/{args.retunes}"),
        ]
    else:
        checks += [
            (len({item for item in stream_ids if item}) == args.retunes,
             f"fresh stream generations {len(set(stream_ids))}/{args.retunes}"),
            (decoded_generations == args.retunes,
             f"clean software-decodable generations "
             f"{decoded_generations}/{args.retunes}"),
        ]
    if args.cable_channel:
        source_mode = (status.get("source") or {}).get("kind") \
            if args.api_version == "v1" else status.get("mode")
        # v1 reports the lane id ("analog"); the legacy view still says
        # "cable" for the same stream.
        checks.append((
            source_mode in ("analog", "cable"),
            f"final source mode {source_mode} (required analog/cable)",
        ))
    if args.require_encoder:
        encoder_name = (status.get("encoder") or {}).get("name") \
            if args.api_version == "v1" else status.get("video_encoder")
        checks.append((
            encoder_name == args.require_encoder,
            f"encoder {encoder_name} "
            f"(required {args.require_encoder})",
        ))
    for passed, description in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {description}")
        if not passed:
            failures.append(description)

    report = {
        "elapsed_seconds": round(elapsed, 3),
        "segments": len(seen_segments),
        "max_segment_bytes": max(
            (item["bytes"] for item in seen_segments.values()), default=0
        ),
        "request_to_stream_seconds": [round(value, 3) for value in latencies],
        "media_mbps": round(media_mbps, 3),
        "wall_mbps": round(wall_mbps, 3),
        "cpu_average_percent": round(mean(cpu_values), 1),
        "cpu_peak_percent": round(max(cpu_values, default=0), 1),
        "rss_peak_mib": round(max(rss_values, default=0), 1),
        "gpu_average_percent": round(mean(gpu_values), 1),
        "gpu_peak_temp_c": round(max(temp_values, default=0), 1),
        "encoder": (
            (status.get("encoder") or {}).get("name")
            if args.api_version == "v1"
            else status.get("video_encoder")
        ),
        "decoded_generations": decoded_generations,
        "failures": sorted(set(failures)),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    if failures:
        print("LIVE STREAM RESOURCE GATE: FAILED", file=sys.stderr)
        return 1
    print("LIVE STREAM RESOURCE GATE: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
