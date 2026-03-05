#!/usr/bin/env python3
"""TV Thing - ATSC broadcast streaming server."""

import json
import os
import re
import select
import signal
import subprocess
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).parent
STREAMS_DIR = BASE_DIR / "streams"
CHANNELS_FILE = BASE_DIR / "channels.json"
WSCAN_OUTPUT = BASE_DIR / "w_scan_output.txt"

# ATSC frequency ranges
ATSC_FREQUENCIES = []
for _ch in range(2, 7):     # VHF-Lo: ch 2-6
    ATSC_FREQUENCIES.append((_ch, ((_ch - 2) * 6 + 54) * 1_000_000))
for _ch in range(7, 14):    # VHF-Hi: ch 7-13
    ATSC_FREQUENCIES.append((_ch, ((_ch - 7) * 6 + 174) * 1_000_000))
for _ch in range(14, 37):   # UHF: ch 14-36
    ATSC_FREQUENCIES.append((_ch, ((_ch - 14) * 6 + 470) * 1_000_000))

# Quality presets: name -> (video_bitrate, audio_bitrate, scale)
QUALITY_PRESETS = {
    "high":   {"vb": 2000, "ab": 128, "scale": None},
    "medium": {"vb": 1000, "ab": 96,  "scale": "720"},
    "low":    {"vb": 500,  "ab": 64,  "scale": "480"},
}

# Tuner state
procs = {"vlc": None}
current_channel = None
current_quality = "medium"
lock = threading.Lock()
scanning = False  # True while w_scan is running; blocks /api/tune


def load_channels():
    if CHANNELS_FILE.exists():
        with open(CHANNELS_FILE) as f:
            return json.load(f)
    return []


def kill_process(proc):
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def stop_streaming():
    global current_channel
    kill_process(procs["vlc"])
    procs["vlc"] = None
    current_channel = None
    for f in STREAMS_DIR.glob("*"):
        f.unlink()


def start_streaming(channel, quality="medium"):
    global current_channel, current_quality

    stop_streaming()
    STREAMS_DIR.mkdir(exist_ok=True)

    freq = channel["frequency"]
    name = channel["name"]
    service_id = channel.get("service_id", 1)
    preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["medium"])
    current_quality = quality

    hls_path = str(STREAMS_DIR / "live.m3u8")
    segment_pattern = str(STREAMS_DIR / "segment_###.ts")

    # Build transcode string with optional scaling
    transcode_opts = f"vcodec=h264,vb={preset['vb']},acodec=aac,ab={preset['ab']},channels=2"
    if preset["scale"]:
        transcode_opts += f",height={preset['scale']}"

    # cvlc handles everything: DVB tuning, TS demux, transcode, HLS output
    vlc_cmd = [
        "cvlc",
        f"atsc://frequency={freq}",
        "--dvb-adapter=0",
        f"--program={service_id}",
        "--no-sout-all",
        "--sout",
        f"#transcode{{{transcode_opts}}}"
        f":std{{access=livehttp{{seglen=4,delsegs=true,numsegs=5,"
        f"index={hls_path},index-url=segment_###.ts}},"
        f"mux=ts{{use-key-frames}},dst={segment_pattern}}}",
    ]

    vlc_proc = subprocess.Popen(
        vlc_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    procs["vlc"] = vlc_proc
    current_channel = channel

    def monitor():
        vlc_proc.wait()
        print(f"VLC exited with code {vlc_proc.returncode}")
        stderr = vlc_proc.stderr.read().decode() if vlc_proc.stderr else ""
        if stderr:
            for line in stderr.strip().split('\n')[-5:]:
                print(f"  vlc: {line}")

    threading.Thread(target=monitor, daemon=True).start()
    print(f"Streaming: {name} ({freq/1e6:.0f} MHz, program {service_id}, quality={quality})")
    return True


class TVHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/channels":
            self.send_json(load_channels())
            return

        if path == "/api/status":
            vlc = procs["vlc"]
            self.send_json({
                "channel": current_channel,
                "streaming": vlc is not None and vlc.poll() is None,
                "quality": current_quality,
            })
            return

        if path.startswith("/stream/"):
            filename = path[len("/stream/"):]
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
                self.wfile.write(content)
                return
            self.send_error(404)
            return

        if path == "/api/rescan/stream":
            self.handle_rescan_stream()
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
            }.get(filepath.suffix, "application/octet-stream")
            self.send_header("Content-Type", ct)
            self.end_headers()
            self.wfile.write(content)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/tune":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            channel_id = body.get("channel_id")
            quality = body.get("quality", "medium")
            if quality not in QUALITY_PRESETS:
                quality = "medium"

            channels = load_channels()
            channel = next((c for c in channels if c["id"] == channel_id),
                           None)
            if not channel:
                self.send_json({"error": "Channel not found"}, status=404)
                return

            with lock:
                if scanning:
                    self.send_json({"error": "Channel scan in progress"}, status=409)
                    return
                ok = start_streaming(channel, quality)

            if ok:
                self.send_json({"ok": True, "channel": channel})
            else:
                self.send_json({"error": "Failed to tune"}, status=500)
            return

        if parsed.path == "/api/stop":
            with lock:
                stop_streaming()
            self.send_json({"ok": True})
            return

        self.send_error(404)

    def handle_rescan_stream(self):
        """Run w_scan and stream results as SSE events."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def send_sse(event, data):
            try:
                payload = f"event: {event}\ndata: {json.dumps(data) if not isinstance(data, str) else data}\n\n"
                self.wfile.write(payload.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                raise

        try:
            send_sse("status", "Stopping active streams...")
            with lock:
                stop_streaming()
            # VLC can be slow to release the DVB device after termination.
            # Wait and verify no vlc processes remain.
            for _ in range(10):
                time.sleep(1)
                result = subprocess.run(
                    ["pgrep", "-x", "vlc"],
                    capture_output=True,
                )
                if result.returncode != 0:
                    break
            time.sleep(1)

            send_sse("status", "Starting channel scan...")

            global scanning
            with lock:
                scanning = True

            # Run w_scan: -fa = ATSC, -c US = country, -X = output format
            # Note: w_scan buffers all stdout until exit, so channels
            # only appear after the process completes
            proc = subprocess.Popen(
                ["w_scan", "-fa", "-c", "US", "-X"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            found_channels = []
            current_freq_khz = None
            current_rf = None
            got_signal = False
            past_ch36 = False
            # Scan range for progress bar: 54 MHz to ~608 MHz (ch 36 upper edge)
            SCAN_MIN_KHZ = 54000
            SCAN_MAX_KHZ = 608000

            def freq_khz_to_rf(freq_khz):
                """Match w_scan's center frequency (kHz) to ATSC RF channel.
                w_scan uses center freqs (e.g. 57 MHz for ch 2),
                our table has lower-edge (54 MHz). Allow 4 MHz tolerance."""
                for ch_num, ch_freq_hz in ATSC_FREQUENCIES:
                    if abs(ch_freq_hz - freq_khz * 1000) < 4_000_000:
                        return ch_num
                return 0

            def parse_stdout_line(line):
                """Parse a channels.conf line from w_scan stdout."""
                parts = line.split(":")
                if len(parts) >= 6 and not line.startswith(";"):
                    name = parts[0].strip()
                    try:
                        freq = int(parts[1])
                    except ValueError:
                        return None
                    return {"line": line, "name": name, "frequency": freq}
                return None

            while True:
                # Read stderr for progress (w_scan writes status to stderr)
                rlist, _, _ = select.select(
                    [proc.stderr, proc.stdout], [], [], 0.5
                )

                for stream in rlist:
                    line = stream.readline()
                    if not line:
                        continue

                    line = line.strip()
                    if not line:
                        continue

                    if stream == proc.stderr:
                        # w_scan stderr format: "57000: 8VSB(time: 00:00.196)"
                        freq_match = re.match(r'(\d+):\s*8VSB', line)
                        if freq_match:
                            freq_khz = int(freq_match.group(1))

                            if freq_khz > SCAN_MAX_KHZ:
                                if not past_ch36:
                                    past_ch36 = True
                                    send_sse("progress", "99")
                                    send_sse("status", "Finishing up...")
                                continue

                            # Update progress based on frequency position
                            pct = min(99, (freq_khz - SCAN_MIN_KHZ) / (SCAN_MAX_KHZ - SCAN_MIN_KHZ) * 100)
                            send_sse("progress", f"{pct:.0f}")

                            rf = freq_khz_to_rf(freq_khz)
                            if rf > 0 and rf != current_rf:
                                # Mark previous freq as no-signal if it never locked
                                if current_freq_khz and not got_signal:
                                    send_sse("lock", {
                                        "frequency": current_freq_khz * 1000,
                                        "frequency_mhz": f"{current_freq_khz / 1000:.0f}",
                                        "rf_channel": current_rf,
                                        "locked": False,
                                    })
                                current_rf = rf
                                current_freq_khz = freq_khz
                                got_signal = False
                                send_sse("frequency", {
                                    "frequency": freq_khz * 1000,
                                    "frequency_mhz": f"{freq_khz / 1000:.0f}",
                                    "rf_channel": rf,
                                })
                                send_sse("status", f"Scanning RF {rf} ({freq_khz / 1000:.0f} MHz)...")

                        if not past_ch36 and 'signal ok' in line.lower():
                            got_signal = True
                            if current_freq_khz:
                                send_sse("lock", {
                                    "frequency": current_freq_khz * 1000,
                                    "frequency_mhz": f"{current_freq_khz / 1000:.0f}",
                                    "rf_channel": current_rf,
                                    "locked": True,
                                })

                    elif stream == proc.stdout:
                        ch = parse_stdout_line(line)
                        if ch and ch["frequency"] <= SCAN_MAX_KHZ * 1000:
                            found_channels.append(ch["line"])
                            send_sse("channel", {
                                "frequency": ch["frequency"],
                                "name": ch["name"],
                            })

                if proc.poll() is not None:
                    # Process ended — drain remaining stdout
                    for line in proc.stdout:
                        line = line.strip()
                        ch = parse_stdout_line(line)
                        if ch and ch["frequency"] <= SCAN_MAX_KHZ * 1000:
                            found_channels.append(ch["line"])
                            send_sse("channel", {
                                "frequency": ch["frequency"],
                                "name": ch["name"],
                            })
                    break

            if found_channels:
                with open(WSCAN_OUTPUT, "w") as f:
                    for line in found_channels:
                        f.write(line + "\n")

                # Re-parse channels
                import parse_channels
                parse_channels.main()
                channels = load_channels()
                send_sse("done", {"ok": True, "count": len(channels)})
            else:
                send_sse("done", {"ok": False, "error": f"w_scan found no channels (exit code {proc.returncode})"})

        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                send_sse("done", {"ok": False, "error": str(e)})
            except Exception:
                pass
        finally:
            with lock:
                scanning = False

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}" if args else "")


def main():
    STREAMS_DIR.mkdir(exist_ok=True)
    for f in STREAMS_DIR.glob("*"):
        f.unlink()

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    port = int(os.environ.get("PORT", 8080))
    server = ThreadedHTTPServer(("0.0.0.0", port), TVHandler)
    print(f"TV Thing server running on http://localhost:{port}")

    channels = load_channels()
    print(f"Loaded {len(channels)} channels")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        stop_streaming()
        server.server_close()


if __name__ == "__main__":
    main()
