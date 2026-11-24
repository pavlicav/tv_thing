"""The peer's streams at /stream/peer/: web-basic relays the other box's /stream/ with its
segments fetched ahead (web_basic.send_peer, hls_relay.py), every stream alike."""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import hls_relay
import web_basic

ROOT = Path(__file__).resolve().parent.parent

# A tuner's playlist as the feed writes it: 0.6 s segments, 20 of them (12 s).
TUNER = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:100\n"
         + "".join(f"#EXTINF:0.600600,\ndigitalseg_g7-2_{n:09d}.ts\n" for n in range(100, 120))).encode()
# An internet stream's rendition, its segments in a folder below it, with the upstream's query.
RENDITION = ("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:7\n"
             + "".join(f"#EXTINF:2.0,\n0/g9e-{n:06d}.ts?t=abc\n" for n in range(7, 12))
             + "#EXTINF:2.0,\nhttps://cdn.test/elsewhere.ts\n").encode()


class Peer:
    """The other box's front door: its /stream/ files, and every path asked for."""

    def __init__(self):
        self.files = {"/stream/digital.m3u8": TUNER, "/stream/internet/cabride/0.m3u8": RENDITION}
        self.asked, self.lock = [], threading.Lock()
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                with peer.lock:
                    peer.asked.append(self.path)
                path = self.path.split("?", 1)[0]
                body = peer.files.get(path)
                if body is None and path.endswith(".ts"):
                    body = path.encode()        # every segment is its own path
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl" if path.endswith(".m3u8")
                                 else "video/mp2t")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/stream/"

    def paths(self):
        with self.lock:
            return list(self.asked)


def settle(relay):
    with relay.lock:
        pending = list(relay.segments.values())
    for future in pending:
        try:
            future.result(timeout=5)
        except Exception:
            pass


@pytest.fixture
def peer():
    fake = Peer()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


@pytest.fixture
def served(peer, tmp_path):
    """web-basic, its relay pointed at the pretend peer: ``(service, get)``."""
    basic = web_basic.WebBasic(tmp_path / "streams", "http://127.0.0.1:1", tmp_path / "none.json", peer=peer.url)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_basic.handler_for(basic))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path):
        try:
            response = urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}{path}", timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, dict(response.headers), response.read()
    yield basic, get
    httpd.shutdown()
    httpd.server_close()


def test_a_tuners_playlist_fetches_its_newest_segments_and_they_are_answered_from_memory(peer, served):
    basic, get = served
    code, headers, body = get("/stream/peer/digital.m3u8")
    assert code == 200 and headers["Content-Type"] == "application/vnd.apple.mpegurl"
    assert headers["Cache-Control"] == "no-store"
    settle(basic.peer)
    # Its newest 5 (3 s), as many as last longer than a fetch from the other box takes.
    assert {f"/stream/digitalseg_g7-2_{n:09d}.ts" for n in range(115, 120)} <= set(peer.paths())
    assert "/stream/digitalseg_g7-2_000000114.ts" not in peer.paths()
    code, headers, body = get("/stream/peer/digitalseg_g7-2_000000119.ts")
    assert (code, headers["Content-Type"], body) == (200, "video/mp2t", b"/stream/digitalseg_g7-2_000000119.ts")
    assert peer.paths().count("/stream/digitalseg_g7-2_000000119.ts") == 1


def test_a_tuners_playlist_starts_players_half_its_length_back(peer, served):
    basic, get = served
    get("/stream/peer/digital.m3u8")
    settle(basic.peer)
    body = get("/stream/peer/digital.m3u8")[2]
    # All 20 in memory now: 12 s listed, so 6 s back, not the 10 s a 30 s window gets.
    assert body.startswith(b"#EXTM3U\n#EXT-X-START:TIME-OFFSET=-6.0\n#EXT-X-VERSION:3\n")
    assert body.count(b"#EXTINF") == 20


def test_a_segment_request_fetches_the_next_ones(peer, served):
    basic, get = served
    get("/stream/peer/digital.m3u8")
    get("/stream/peer/digitalseg_g7-2_000000105.ts")
    settle(basic.peer)
    assert {f"/stream/digitalseg_g7-2_{n:09d}.ts" for n in (106, 107, 108)} <= set(peer.paths())
    assert "/stream/digitalseg_g7-2_000000109.ts" not in peer.paths()


def test_a_stream_in_a_folder_names_its_segments_below_its_playlist_with_their_query(peer, served):
    basic, get = served
    code, _, body = get("/stream/peer/internet/cabride/0.m3u8")
    assert code == 200
    settle(basic.peer)
    assert {"/stream/internet/cabride/0/g9e-000010.ts?t=abc", "/stream/internet/cabride/0/g9e-000011.ts?t=abc"} \
        <= set(peer.paths())
    assert not any("elsewhere" in path for path in peer.paths())    # the player's to fetch
    # Listed after the two still on their way, it waited with them; now it is passed on as it is.
    assert b"elsewhere" not in body
    assert b"\nhttps://cdn.test/elsewhere.ts\n" in get("/stream/peer/internet/cabride/0.m3u8")[2]
    code, _, body = get("/stream/peer/internet/cabride/0/g9e-000011.ts?t=abc")
    assert (code, body) == (200, b"/stream/internet/cabride/0/g9e-000011.ts")
    assert peer.paths().count("/stream/internet/cabride/0/g9e-000011.ts?t=abc") == 1


def test_never_the_peers_own_relay_nor_a_path_outside_its_streams(peer, served):
    _, get = served
    for path in ("/stream/peer/peer/digital.m3u8", "/stream/peer/../server.py", "/stream/peer/.hidden.ts",
                 "/stream/peer/a//b.ts", "/stream/peer/%2e%2e/server.py"):
        code, _, body = get(path)
        assert code == 404 and b"Invalid peer stream path" in body, path
    assert peer.paths() == []


def test_the_peers_answer_passes_through_and_its_absence_is_a_bad_gateway(peer, served, tmp_path):
    basic, get = served
    assert get("/stream/peer/gone.m3u8")[0] == 404
    peer.server.shutdown()
    peer.server.server_close()
    code, _, body = get("/stream/peer/digital.m3u8")
    assert code == 502 and b"did not answer" in body
    alone = web_basic.WebBasic(tmp_path / "s", "http://127.0.0.1:1", tmp_path / "none.json", peer=None)
    assert alone.peer is None and alone.status()["peer"] is None


def test_its_status_says_what_the_relay_holds(peer, served):
    basic, get = served
    get("/stream/peer/digital.m3u8")
    settle(basic.peer)
    status = basic.status()["peer"]
    assert status["playlists"] == ["digital.m3u8"] and status["segments"] >= 2


def test_a_stream_that_starts_its_numbering_again_is_not_answered_from_before():
    """A source that restarts and reuses its names gets them fetched again."""
    asked = []

    class Response:
        def __init__(self, url, body):
            self.status, self.headers, self.body, self.url = 200, {"Content-Type": "video/mp2t"}, body, url

        def read(self, limit):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    playlists = iter([b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:40\n#EXTINF:2.0,\nseg1.ts\n#EXTINF:2.0,\nseg2.ts\n",
                      b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:2.0,\nseg1.ts\n#EXTINF:2.0,\nseg2.ts\n"])

    def opener(request, timeout):
        asked.append(request.full_url)
        if request.full_url.endswith(".m3u8"):
            return Response(request.full_url, next(playlists))
        return Response(request.full_url, f"{request.full_url} #{len(asked)}".encode())
    relay = hls_relay.Relay("http://peer/stream/", opener, by_name=False)
    relay.get("s/index.m3u8")
    settle(relay)
    first = relay.get("s/seg2.ts")[2]
    relay.get("s/index.m3u8")
    settle(relay)
    assert relay.get("s/seg2.ts")[2] != first
    assert asked.count("http://peer/stream/s/seg2.ts") == 2


def test_apache_sends_the_peers_streams_to_web_basics_relay():
    routes = (ROOT / "deploy" / "apache" / "tv-thing-routes.conf").read_text()
    block = routes[routes.index("<IfDefine TV_THING_PEER_URL>"):]
    block = block[:block.index("</IfDefine>")]
    assert "ProxyPass        /stream/peer/  http://127.0.0.1:${TV_THING_WEB_BASIC_PORT}/stream/peer/" in block
    assert "${TV_THING_PEER_URL}" not in block
    # The guard first: the first matching ProxyPass wins.
    assert block.index("/stream/peer/peer/ !") < block.index("ProxyPass        /stream/peer/  ")
    # Defined before it, and before the feed's /stream/, which would otherwise take it.
    assert routes.index("Define TV_THING_WEB_BASIC_PORT") < routes.index("<IfDefine TV_THING_PEER_URL>")
    assert routes.index("<IfDefine TV_THING_PEER_URL>") < routes.index("ProxyPass        /stream/  ")
    timeout = int(block.split("timeout=")[-1].split()[0])
    assert timeout > hls_relay.Relay.WAIT


def test_its_settings_are_inside_the_services_memory():
    unit = (ROOT / "deploy" / "systemd" / "tv-thing-web-basic.service").read_text()
    limit = int(unit.split("MemoryMax=")[1].split("M")[0])
    assert web_basic.PEER_KEEP * 0.6 < limit / 4      # ~600 KB a segment at high, a quarter of it


def test_a_tuners_newest_segments_wait_until_they_have_arrived():
    """Up to 5 left out while on their way: a 0.6 s segment's fetch can outlast two of them."""
    gate = threading.Event()

    class Response:
        def __init__(self, body):
            self.status, self.headers, self.body = 200, {}, body

        def read(self, limit):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        if request.full_url.endswith(".m3u8"):
            return Response(TUNER)
        if int(request.full_url[-12:-3]) >= 115:
            gate.wait(5)                     # the newest five are still on their way
        return Response(b"ts")
    relay = web_basic.peer_relay("http://peer/stream/")
    relay.opener = opener
    held = relay.get("digital.m3u8")[2].decode()
    assert held.count("#EXTINF") == 15 and held.rstrip().endswith("digitalseg_g7-2_000000114.ts")
    gate.set()
    settle(relay)
    assert relay.get("digital.m3u8")[2].count(b"#EXTINF") == 20
