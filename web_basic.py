#!/usr/bin/env python3
"""TV Thing's web-basic service: the simple web streams, in a process of their own.

It makes the station's slide channels as video -- the Sports Guide (channel 6)
and Cairo Radio (channel 197) -- and relays the simple web streams
(internet_streams.json's streams with ``"service": "web-basic"``, such as
pen2live). Each runs only while someone watches.

They run here rather than in the feed, so a web stream that misbehaves spends
this service's CPU and memory (tv-thing-web-basic.service's limits), never the
tuners'. They are AGENTS.md's web streams stack: a change is checked by
scripts/web_stream_check.py (CPU stays bounded, memory does not grow, it stops
when nobody watches), not by the tuners' gate. Restarting it drops only its own
streams. Trains (Cab Ride) stays the feed's.

Apache's front door sends it their /stream/ paths (deploy/apache/tv-thing-routes.conf).
It listens on 127.0.0.1 alone (:8085 unless PORT says otherwise) and answers
/web/v1/status, to this host only, for control's status.

server.py's one-process build (the rollback) and its feed role build the same
slide streams from here; split, the feed's are never asked for.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import host_config
import internet_streams
import slide_stream

BASE_DIR = Path(__file__).resolve().parent
SERVICE = "web-basic"
PORT = int(os.environ.get("PORT", "8085"))
# Control's API: the Sports Guide's listings, and the URL a resolved relay plays.
CONTROL_URL = os.environ.get("TV_THING_CONTROL_URL", "http://127.0.0.1:8087")
# Its own folder, apart from the feed's streams: each stream writes into <here>/<name>/.
STREAMS_DIR = Path(os.environ.get("TV_THING_WEB_STREAMS_DIR", str(BASE_DIR / "streams-web")))
INTERNET_STREAMS_FILE = BASE_DIR / "internet_streams.json"
IDLE_SECONDS = float(os.environ.get("TV_THING_SLIDES_IDLE_SECONDS", "60"))
START_TIMEOUT = float(os.environ.get("TV_THING_SLIDES_START_TIMEOUT", "8"))

# -- the Sports Guide (channel 6) ------------------------------------------------------
# sports_guide_video.py draws the slides at 30 fps 1280x720 into one libx264 on two threads,
# 2 s segments with a key frame each. Measured on viviserv (2026-10-19): about 11% of a core
# to draw, 33% to encode, 0.6 Mbit/s; with the music (2026-10-21) 12% and 45%.
SLIDE_FPS = 30
SLIDE_SEGMENT_SECONDS = 2
SLIDE_THREADS = "2"
# Its music (sports_guide_music.py makes it): smooth jazz, as a listings channel had. Its
# length is in the .json beside it. Without it, silence.
SPORTS_GUIDE_MUSIC = BASE_DIR / "static" / "sports-guide-music.m4a"
# One thread each for numpy's libraries (see sports_guide_video.py), and frame-sized buffers
# handed back when freed: glibc otherwise raises its mmap threshold after the first and keeps
# ~50 MB it no longer uses.
RENDERER_ENV = {"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                "MALLOC_MMAP_THRESHOLD_": "131072"}


def sports_guide_music(now=None):
    """``(path, seconds in)`` for the guide's music, or None when it is missing.

    It plays from wherever the wall clock says a channel that never stopped would
    be, so tuning in lands mid-tune, as on cable (the page does the same)."""
    try:
        seconds = float(json.loads(SPORTS_GUIDE_MUSIC.with_suffix(".json").read_text())["seconds"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if seconds <= 0 or not SPORTS_GUIDE_MUSIC.is_file():
        return None
    return SPORTS_GUIDE_MUSIC, (time.time() if now is None else now) % seconds


def build_sports_guide_command(generation, directory, now=None):
    """The Sports Guide's encoder: raw frames on stdin to HLS in ``directory``."""
    directory = Path(directory)
    music = sports_guide_music(now)
    if music:
        # The music, looped, from the wall clock's place in it.
        sound = ["-stream_loop", "-1", "-ss", f"{music[1]:.3f}", "-thread_queue_size", "8",
                 "-i", str(music[0])]
    else:
        # Silence, as raw zeros: some players refuse a stream with no sound
        # track. (lavfi's anullsrc ran a filter graph of 28 threads.)
        sound = ["-f", "s16le", "-ar", "48000", "-ac", "2", "-thread_queue_size", "8", "-i", "/dev/zero"]
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-filter_threads", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size", "1280x720",
        # Eight queued frames: each is 2.7 MB, and the renderer runs ahead.
        "-framerate", str(SLIDE_FPS), "-thread_queue_size", "8", "-i", "pipe:0",
        *sound,
        "-map", "0:v:0", "-map", "1:a:0", "-shortest",
        # HD is BT.709; say so, or a player may assume it of untagged 601.
        "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-color_range", "tv",
        # zerolatency drops the lookahead: 125 MB rather than 205 for veryfast's.
        "-c:v", "libx264", "-preset", "superfast", "-tune", "stillimage,zerolatency",
        "-profile:v", "high", "-level:v", "4.0", "-threads", SLIDE_THREADS,
        # Still pages cost almost nothing; a transition is capped.
        "-crf", "23", "-maxrate", "2000k", "-bufsize", "4000k",
        "-g", str(SLIDE_FPS * SLIDE_SEGMENT_SECONDS),
        "-keyint_min", str(SLIDE_FPS * SLIDE_SEGMENT_SECONDS), "-sc_threshold", "0",
        "-force_key_frames", f"expr:gte(t,n_forced*{SLIDE_SEGMENT_SECONDS})",
        "-c:a", "aac", "-b:a", "96k" if music else "48k", "-ac", "2", "-ar", "48000",
        "-max_interleave_delta", "500000", "-flush_packets", "1",
        "-f", "hls", "-hls_time", str(SLIDE_SEGMENT_SECONDS), "-hls_list_size", "8",
        "-hls_allow_cache", "0",
        "-hls_flags", "delete_segments+omit_endlist+independent_segments+temp_file",
        "-hls_segment_filename", str(directory / f"sportsguide_{generation}_%05d.ts"),
        str(directory / slide_stream.PLAYLIST),
    ]


def sports_guide_renderer_command(control_url):
    return [sys.executable, str(BASE_DIR / "sports_guide_video.py"),
            "--url", f"{control_url}/api/v1/sports-guide", "--fps", str(SLIDE_FPS)]


# -- Cairo Radio (channel 197) ------------------------------------------------------------
# hazel's station: its audio, and what it plays now (Apache's /radio/, its master control).
# A second node reaches the primary's, when its settings know the primary's address.
_HOST = host_config.load()
CAIRO_RADIO_AUDIO = os.environ.get("TV_THING_CAIRO_RADIO_AUDIO", _HOST.primary_url(1615, "/cairo.mp3")
                                   or "http://127.0.0.1:1615/cairo.mp3")
CAIRO_RADIO_INFO = os.environ.get("TV_THING_CAIRO_RADIO_INFO", _HOST.primary_url(80, "/radio")
                                  or "http://127.0.0.1/radio")
# A title card that changes with the song: one frame a second is all it needs.
CAIRO_FPS = 1


def build_cairo_radio_command(generation, directory, audio_url=CAIRO_RADIO_AUDIO):
    """Cairo Radio's encoder: the card's frames on stdin and the station's MP3 to HLS in
    ``directory``. One frame a second, a key frame every two, the audio as AAC. When either
    input ends (-shortest), so does the encoder, and slide_stream starts the pair afresh."""
    directory = Path(directory)
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-filter_threads", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size", "1280x720",
        "-framerate", str(CAIRO_FPS), "-thread_queue_size", "4", "-i", "pipe:0",
        # The station's stream, reconnected when it drops, given up when silent for 10 s. It
        # sends a burst of buffered audio on connect; 512 packets (a few seconds) take it.
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4",
        "-rw_timeout", "10000000", "-thread_queue_size", "512", "-i", audio_url,
        "-map", "0:v:0", "-map", "1:a:0", "-shortest",
        "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-color_range", "tv",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
        "-profile:v", "main", "-level:v", "4.0", "-threads", "1",
        "-crf", "26", "-maxrate", "400k", "-bufsize", "800k",
        "-g", str(CAIRO_FPS * SLIDE_SEGMENT_SECONDS), "-keyint_min", str(CAIRO_FPS * SLIDE_SEGMENT_SECONDS),
        "-sc_threshold", "0", "-force_key_frames", f"expr:gte(t,n_forced*{SLIDE_SEGMENT_SECONDS})",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100",
        "-max_interleave_delta", "500000", "-flush_packets", "1",
        "-f", "hls", "-hls_time", str(SLIDE_SEGMENT_SECONDS), "-hls_list_size", "8",
        "-hls_allow_cache", "0",
        "-hls_flags", "delete_segments+omit_endlist+independent_segments+temp_file",
        "-hls_segment_filename", str(directory / f"cairoradio_{generation}_%05d.ts"),
        str(directory / slide_stream.PLAYLIST),
    ]


def cairo_radio_renderer_command(info_url=CAIRO_RADIO_INFO):
    return [sys.executable, str(BASE_DIR / "cairo_radio_video.py"),
            "--url", info_url, "--fps", str(CAIRO_FPS)]


def slide_streams(directory, control_url, idle_seconds=IDLE_SECONDS):
    """The station's slide channels as video (lineup.SLIDES), each in ``directory/<name>``."""
    directory = Path(directory)
    env = {**os.environ, **RENDERER_ENV}
    return {
        "sports-guide": slide_stream.SlideStream(
            "sports-guide", directory / "sports-guide",
            lambda: sports_guide_renderer_command(control_url), build_sports_guide_command,
            idle_seconds=idle_seconds, env=env),
        "cairo-radio": slide_stream.SlideStream(
            "cairo-radio", directory / "cairo-radio",
            cairo_radio_renderer_command, build_cairo_radio_command,
            # Its first segment waits for two frames and the station's first audio.
            idle_seconds=idle_seconds, startup_seconds=20.0, env=env),
    }


# -- serving ----------------------------------------------------------------------------

def _send_body(handler, status, content_type, body, cache="no-cache"):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", cache)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def send_json(handler, payload, status=200):
    _send_body(handler, status, "application/json", json.dumps(payload).encode(), cache="no-store")


def send_slide(handler, name, stream, filename, start_timeout=START_TIMEOUT):
    """A slide channel's video. The playlist starts it and waits for its
    first segments; a segment keeps it running."""
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:m3u8|ts)", filename):
        send_json(handler, {"error": "Invalid slide media path"}, 400)
        return
    if filename.endswith(".m3u8"):
        if not stream.demand(wait=start_timeout):
            handler.send_response(503)
            handler.send_header("Retry-After", "2")
            handler.send_header("Content-Type", "application/json")
            body = json.dumps({"error": f"The {name} video is starting"}).encode()
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
    else:
        stream.touch()
    try:
        content = (stream.directory / filename).read_bytes()
    except OSError:
        handler.send_error(404)
        return
    _send_body(handler, 200, "application/vnd.apple.mpegurl" if filename.endswith(".m3u8")
               else "video/mp2t", content)


def send_relay(handler, relays, parsed, allowed=None):
    """Relay an internet stream (internet_streams.py): its playlists, and its segments as
    they stream in. No tuner, lock or encoder. ``allowed(stream_id)`` says which it relays."""
    parts = parsed.path.removeprefix("/stream/internet/").split("/", 2)
    if len(parts) < 2 or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", parts[0]):
        send_json(handler, {"error": "Invalid internet stream path"}, 400)
        return
    if allowed is not None and not allowed(parts[0]):
        send_json(handler, {"error": f"No internet stream {parts[0]!r} here"}, 404)
        return
    try:
        if len(parts) == 2:
            content = relays.playlist(*parts).encode()
            response = None
        else:
            response = relays.segment(parts[0], parts[1], parts[2], parsed.query)
    except internet_streams.StreamConfigError as error:
        send_json(handler, {"error": str(error)}, 500)
        return
    except internet_streams.StreamUnavailable as error:
        send_json(handler, {"error": str(error)}, error.status)
        return
    if response is None:
        _send_body(handler, 200, "application/vnd.apple.mpegurl", content, cache="no-store")
        return
    with response:
        length = response.headers.get("Content-Length")
        if length and int(length) > internet_streams.MAX_SEGMENT_BYTES:
            send_json(handler, {"error": "Segment too large"}, 502)
            return
        handler.send_response(200)
        handler.send_header("Content-Type", response.headers.get("Content-Type")
                            or "application/octet-stream")
        if length:
            handler.send_header("Content-Length", length)
        else:
            handler.close_connection = True
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        sent = 0
        try:
            while sent <= internet_streams.MAX_SEGMENT_BYTES:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError, TimeoutError):
            handler.close_connection = True


def loopback(address):
    return bool(re.fullmatch(r"127\.\d+\.\d+\.\d+|::1|::ffff:127\.\d+\.\d+\.\d+", address or ""))


class WebBasic:
    """The service's streams: the slide channels' video and its relays."""

    def __init__(self, directory=STREAMS_DIR, control_url=CONTROL_URL, streams_file=INTERNET_STREAMS_FILE,
                 idle_seconds=IDLE_SECONDS):
        self.slides = slide_streams(directory, control_url, idle_seconds)
        # Its relays find a stream's URL as the feed's do: a fixed one from the file, any
        # other through control (internal_resolve).
        self.relays = internet_streams.InternetStreams(
            streams_file, resolver=lambda stream: (
                internet_streams.resolve_stream(stream)
                if internet_streams.RESOLVERS[stream.resolver].fixed
                else internet_streams.resolve_through(control_url, stream)))

    def relays_here(self):
        """The ids of the internet streams this service relays."""
        try:
            return [stream.id for stream in self.relays.streams().values() if stream.service == SERVICE]
        except internet_streams.StreamConfigError:
            return []

    def status(self):
        return {"service": SERVICE, "slides": {name: stream.status() for name, stream in self.slides.items()},
                "relays": self.relays_here()}

    def supervise(self, stopping):
        """Stop video nobody watches; restart video that died or stalled."""
        while not stopping.wait(1):
            for stream in self.slides.values():
                try:
                    stream.check()
                except Exception as error:  # noqa: BLE001 - the supervisor must not die
                    print(f"[{stream.name}] supervisor error: {error!r}", flush=True)

    def stop(self):
        for stream in self.slides.values():
            stream.stop("service stopping")


class StatusClient:
    """Control's view of web-basic: its slide channels' video, as /api/v1/status reports it.
    Read at most once a second; when web-basic does not answer, each slide channel says so
    rather than the status failing."""

    def __init__(self, base_url, names, *, timeout=1.0, max_age=1.0, opener=urlopen, clock=time.monotonic):
        self.url = f"{base_url.rstrip('/')}/web/v1/status"
        self.names, self.timeout, self.max_age = tuple(names), timeout, max_age
        self.opener, self.clock = opener, clock
        self._lock = threading.Lock()
        self._cached = (None, None)   # (read at, slides)

    def slides(self):
        with self._lock:
            at, slides = self._cached
            if at is not None and self.clock() - at < self.max_age:
                return slides
            try:
                with self.opener(self.url, timeout=self.timeout) as response:
                    slides = json.loads(response.read(1024 * 1024))["slides"]
            except (URLError, OSError, TimeoutError, ValueError, KeyError, TypeError) as error:
                slides = {name: {"running": False, "ready": False, "generation": None,
                                 "playlist_path": f"/stream/{name}/{slide_stream.PLAYLIST}",
                                 "unavailable": f"web-basic is not answering: {error}"}
                          for name in self.names}
            self._cached = (self.clock(), slides)
            return slides


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass   # every segment request would be a line

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/web/v1/status":
                if not loopback(self.client_address[0]):
                    send_json(self, {"error": "web-basic's status answers this host only"}, 403)
                else:
                    send_json(self, service.status())
                return
            parts = path.split("/")
            if len(parts) == 4 and parts[1] == "stream" and parts[2] in service.slides:
                send_slide(self, parts[2], service.slides[parts[2]], parts[3])
                return
            if path.startswith("/stream/internet/"):
                send_relay(self, service.relays, parsed, allowed=lambda stream_id: stream_id in service.relays_here())
                return
            send_json(self, {"error": "This is TV Thing's web-basic service: the slide channels' "
                                      "video and the simple web relays", "code": "web_basic_only"}, 404)

    return Handler


def main():
    # systemd stops it with SIGTERM: stop the video on the way out, not just the server.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    service = WebBasic()
    STREAMS_DIR.mkdir(parents=True, exist_ok=True)
    for stream in service.slides.values():
        stream.clear()   # a predecessor's segments; its processes went with its cgroup
    stopping = threading.Event()
    threading.Thread(target=service.supervise, args=(stopping,), daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), handler_for(service))
    httpd.daemon_threads = True
    print(f"TV Thing web-basic on http://127.0.0.1:{PORT}: slides {', '.join(service.slides)}; "
          f"relays {', '.join(service.relays_here()) or 'none'}; control at {CONTROL_URL}", flush=True)
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        stopping.set()
        service.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
