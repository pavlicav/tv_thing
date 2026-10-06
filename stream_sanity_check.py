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


def mean(values):
    return sum(values) / len(values) if values else 0.0


def decode_generation(stream_id, seen_segments, segment_prefix="segment"):
    """Decode one collected HLS generation in memory and return stderr."""
    prefix = f"{segment_prefix}_{stream_id}_"
    payload = b"".join(
        item["data"] for uri, item in sorted(seen_segments.items())
        if uri.startswith(prefix)
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
    parser.add_argument("--max-gpu-temp", type=float, default=80)
    parser.add_argument("--require-encoder")
    parser.add_argument(
        "--max-ffmpeg-processes", type=int, default=2,
        help="Encoders allowed at once. Two lanes may stream concurrently, "
             "so the default tolerates the other lane's encoder.",
    )
    parser.add_argument("--min-segments-per-round", type=int, default=3)
    args = parser.parse_args(argv)

    started = time.monotonic()
    failures = []
    seen_segments = {}
    samples = {"process": [], "gpu": []}
    latencies = []
    stream_ids = []
    round_counts = []
    # Per-lane segment prefixes: each lane writes its own playlist.
    segment_prefix = "analogseg" if args.cable_channel else "digitalseg"

    try:
        if args.channel_id and args.cable_channel:
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
        for index in range(args.retunes):
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
    for stream_id in stream_ids:
        if not stream_id:
            continue
        decoded, decode_error = decode_generation(
            stream_id, seen_segments, segment_prefix
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
         f"request-to-stream max {max(latencies, default=float('inf')):.3f}s"),
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
