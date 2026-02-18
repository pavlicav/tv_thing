#!/usr/bin/env python3
"""TV Thing - ATSC broadcast streaming server."""

import json
import os
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
        transcode_opts += f",height={preset['scale']},vfilter=canvas{{width=0,height={preset['scale']}}}"

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

        if parsed.path == "/api/rescan":
            def do_scan():
                try:
                    # Stop any active stream so the DVB device is free
                    with lock:
                        stop_streaming()
                    time.sleep(2)
                    # Run VCT scan (captures from each freq, parses PSIP)
                    result = subprocess.run(
                        ["python3", str(BASE_DIR / "scan_vct.py")],
                        capture_output=True, text=True, timeout=600,
                    )
                    print(result.stdout)
                    if result.stderr:
                        print(result.stderr)
                    # Re-parse with VCT data
                    subprocess.run(
                        ["python3", str(BASE_DIR / "parse_channels.py")],
                        capture_output=True, timeout=30,
                    )
                    channels = load_channels()
                    return len(channels)
                except Exception as e:
                    return str(e)

            count = do_scan()
            if isinstance(count, int):
                self.send_json({"ok": True, "count": count})
            else:
                self.send_json({"error": count}, status=500)
            return

        self.send_error(404)

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
