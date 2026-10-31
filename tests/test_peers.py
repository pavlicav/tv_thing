"""The peer client (peers.py) against a pretend peer node on loopback."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import peers


class FakePeer:
    """A peer's front door: /api/v1/tuners and the change calls, answering what it is told."""

    def __init__(self):
        self.tuners = [
            {"id": "viviserv2:digital", "node": "viviserv2", "lane": "digital", "local": True,
             "available": True, "reachable": True, "lock": None,
             "stream": {"id": "g1", "ready": True, "playlist_path": "/stream/digital.m3u8"}},
            # The peer's view of the asking node's own tuner, which it must not take as the peer's.
            {"id": "viviserv:digital", "node": "viviserv", "lane": "digital", "local": False},
        ]
        self.posted, self.status = [], 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if fake.status != 200:
                    return self.answer(fake.status, {})
                assert self.path == "/api/v1/tuners?local=1"
                self.answer(200, {"node": "viviserv2", "tuners": fake.tuners})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.posted.append((self.path, body))
                if self.path == "/api/v1/tune":
                    return self.answer(200, {"ok": True, "lane": "digital", "stream": {
                        "id": "g2", "playlist_path": "/stream/digital.m3u8",
                        "playback": [{"protocol": "hls", "url": "stream/digital.m3u8"}]}})
                self.answer(423, {"ok": False, "error": {"code": "locked", "message": "locked"}})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    peer = FakePeer()
    yield peer
    peer.close()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_peers_own_tuners_are_read_with_their_streams_on_the_relay(fake):
    client = peers.PeerClient(fake.url)
    assert client.refresh() and client.reachable and client.node() == "viviserv2"
    [tuner] = client.tuners()
    assert tuner["id"] == "viviserv2:digital" and tuner["local"] is False and tuner["available"]
    assert tuner["stream"]["playlist_path"] == "/stream/peer/digital.m3u8"


def test_a_read_that_goes_stale_makes_every_tuner_unknown(fake):
    clock = Clock()
    client = peers.PeerClient(fake.url, clock=clock, max_age=6.0)
    client.refresh()
    fake.tuners[0]["lock"] = {"lane": "digital", "owner": "VCR"}
    client.refresh()
    assert client.tuners()[0]["lock"]["lane"] == "viviserv2:digital"   # released by tuner id
    fake.status = 503
    assert not client.refresh() and client.last_error
    clock.now += 7
    [tuner] = client.tuners()
    assert not client.reachable
    assert tuner["available"] is False and tuner["reachable"] is False and tuner["state"] == "unknown"
    assert tuner["stream"]["ready"] is False


def test_a_peer_never_read_has_no_tuners():
    client = peers.PeerClient("http://127.0.0.1:9", timeout=0.3)
    assert not client.refresh() and client.tuners() == [] and not client.reachable


def test_a_tune_asks_the_peer_for_its_own_tuner_and_comes_back_relayed(fake):
    client = peers.PeerClient(fake.url)
    payload, status = client.tune({"source": "digital", "channel_id": "wusa_hd_9_1"})
    assert status == 200
    assert fake.posted == [("/api/v1/tune", {"source": "digital", "channel_id": "wusa_hd_9_1",
                                             "local_only": True})]
    assert payload["stream"]["playlist_path"] == "/stream/peer/digital.m3u8"
    assert payload["stream"]["playback"][0]["url"] == "stream/peer/digital.m3u8"


def test_a_refusal_comes_back_as_it_is_and_silence_as_unavailable(fake):
    client = peers.PeerClient(fake.url)
    payload, status = client.lock({"lane": "digital", "owner": "me"})
    assert status == 423 and payload["error"]["code"] == "locked"
    with pytest.raises(peers.PeerUnavailable):
        peers.PeerClient("http://127.0.0.1:9", command_timeout=0.3).tune({})


def test_the_relay_path():
    assert peers.stream_path("/stream/digital.m3u8") == "/stream/peer/digital.m3u8"
    assert peers.stream_path("/stream/digital-b.m3u8") == "/stream/peer/digital-b.m3u8"
    assert peers.stream_path(None) is None
