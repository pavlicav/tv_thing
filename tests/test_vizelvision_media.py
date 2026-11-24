"""Watch-only station HLS proxy: safe paths, nested playlists, ranges, outages."""
from io import BytesIO
from urllib.parse import urlparse
from urllib.error import URLError

import pytest
import server


def handler(path, headers=None):
    h = object.__new__(server.TVHandler)
    h.path = path
    h.headers = headers or {}
    h.wfile = BytesIO()
    h.sent = {}
    h.send_response = lambda code: h.sent.update(status=code)
    h.send_header = lambda name, value: h.sent.update({name: value})
    h.end_headers = lambda: None
    return h


@pytest.mark.parametrize('asset,content,code', [
    ('index.m3u8', b'#EXTM3U\nvideo1_stream.m3u8?session=abc\n', 200),
    ('video1_stream.m3u8?session=abc', b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\nseg1.mp4\n', 200),
    ('init.mp4?session=abc', b'ftyp', 206),
])
def test_relay_preserves_assets_queries_and_ranges(monkeypatch, asset, content, code):
    seen = []
    def upstream(request, timeout):
        seen.append((request.full_url, request.get_header('Range'), timeout))
        response = BytesIO(content)
        response.status = code
        response.headers = {'Content-Type': 'video/mp4', 'Content-Range': 'bytes 0-3/100'}
        return response
    monkeypatch.setattr(server, 'urlopen', upstream)
    h = handler('/stream/vizelvision/' + asset, {'Range': 'bytes=0-3'})
    h.do_GET()
    assert seen == [(server.VIZELVISION_MEDIA_URL + asset, 'bytes=0-3', 5)]
    assert h.wfile.getvalue() == content
    assert h.sent['status'] == code
    assert h.sent['Content-Range'] == 'bytes 0-3/100'
    assert h.sent['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('path', ['../secret.mp4', '%2e%2e/secret.ts', 'http://example.com/a.ts', 'a/b.ts', 'config.json'])
def test_rejects_non_media_or_nested_paths(monkeypatch, path):
    monkeypatch.setattr(server, 'urlopen', lambda *a, **kw: pytest.fail('contacted upstream'))
    h = handler('/stream/vizelvision/' + path)
    h.do_GET()
    assert h.sent['status'] == 400


def test_unavailable_station_is_a_bounded_error(monkeypatch):
    def unavailable(*args, **kwargs):
        raise URLError('offline')
    monkeypatch.setattr(server, 'urlopen', unavailable)
    h = handler('/stream/vizelvision/index.m3u8')
    h.do_GET()
    assert h.sent['status'] == 502
    assert b'VizelVision stream unavailable' in h.wfile.getvalue()


def test_oversized_station_response_is_not_forwarded(monkeypatch):
    monkeypatch.setattr(server.VIZELVISION_RELAY, 'MAX_BYTES', 3)
    monkeypatch.setattr(server, 'urlopen', lambda *a, **kw: BytesIO(b'1234'))
    h = handler('/stream/vizelvision/segment.mp4')
    h.do_GET()
    assert h.sent['status'] == 502


# --- Segments fetched ahead (hls_relay.py) --------------------------------------

import threading
from urllib.error import HTTPError

import hls_relay

MEDIA = ('#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:10\n#EXT-X-MAP:URI="ab_video1_init.mp4?session=s"\n'
         + ''.join(f'#EXTINF:1.0,\nab_video1_seg{n}.mp4?session=s\n' for n in range(10, 20))).encode()


class Station:
    """The primary: records each URL asked for; a segment can be made to fail once."""
    def __init__(self, failing=()):
        self.asked, self.failing, self.lock = [], set(failing), threading.Lock()

    def __call__(self, request, timeout):
        url = request.full_url
        with self.lock:
            self.asked.append(url)
            name = url.rsplit('/', 1)[1].split('?')[0]
            if name in self.failing:
                self.failing.discard(name)
                raise HTTPError(url, 404, 'gone', {}, None)
        body = MEDIA if '.m3u8' in url else name.encode()
        response = BytesIO(body)
        response.status, response.headers = 200, {'Content-Type': 'video/mp4'}
        return response

    def names(self):
        with self.lock:
            return [url.rsplit('/', 1)[1].split('?')[0] for url in self.asked]


def settle(relay):
    with relay.lock:
        pending = list(relay.segments.values())
    for future in pending:
        try:
            future.result(timeout=5)
        except Exception:
            pass


def test_a_media_playlist_starts_its_init_and_newest_segments():
    station = Station()
    relay = hls_relay.Relay('http://primary/v1/master/media/', station)
    status, _, body = relay.get('video1_stream.m3u8?session=s')
    assert status == 200
    settle(relay)
    assert sorted(station.names()) == sorted(['video1_stream.m3u8', 'ab_video1_init.mp4', 'ab_video1_seg18.mp4',
                                              'ab_video1_seg19.mp4'])
    assert relay.get('ab_video1_seg19.mp4?session=s')[2] == b'ab_video1_seg19.mp4'
    settle(relay)
    assert station.names().count('ab_video1_seg19.mp4') == 1          # answered from memory


def test_a_segment_request_starts_the_next_ones():
    station = Station()
    relay = hls_relay.Relay('http://primary/v1/master/media/', station)
    relay.get('video1_stream.m3u8?session=s')
    assert relay.get('ab_video1_seg12.mp4?session=s')[2] == b'ab_video1_seg12.mp4'
    settle(relay)
    assert {'ab_video1_seg13.mp4', 'ab_video1_seg14.mp4', 'ab_video1_seg15.mp4'} <= set(station.names())
    assert 'ab_video1_seg16.mp4' not in station.names()
    for n in (13, 14, 15):
        relay.get(f'ab_video1_seg{n}.mp4?session=s')
    settle(relay)
    assert all(station.names().count(f'ab_video1_seg{n}.mp4') == 1 for n in range(12, 19))


def test_one_fetch_serves_every_session_and_keeps_the_query_upstream():
    station = Station()
    relay = hls_relay.Relay('http://primary/v1/master/media/', station)
    relay.get('ab_video1_seg30.mp4?session=a&quality=low')
    relay.get('ab_video1_seg30.mp4?session=b&quality=low')
    assert station.asked == ['http://primary/v1/master/media/ab_video1_seg30.mp4?session=a&quality=low']


def test_a_failed_fetch_is_tried_again_and_its_error_passes_through():
    station = Station(failing={'ab_video1_seg40.mp4'})
    relay = hls_relay.Relay('http://primary/v1/master/media/', station)
    with pytest.raises(HTTPError):
        relay.get('ab_video1_seg40.mp4')
    settle(relay)
    assert relay.get('ab_video1_seg40.mp4')[2] == b'ab_video1_seg40.mp4'
    assert station.names().count('ab_video1_seg40.mp4') == 2


def test_byte_ranges_go_straight_through_and_memory_is_bounded():
    station = Station()
    relay = hls_relay.Relay('http://primary/v1/master/media/', station)
    relay.KEEP = 5
    relay.get('ab_video1_seg50.mp4', 'bytes=0-3')
    relay.get('ab_video1_seg50.mp4', 'bytes=0-3')
    assert station.names().count('ab_video1_seg50.mp4') == 2
    for n in range(60, 72):
        relay.get(f'ab_video1_seg{n}.mp4')
    assert len(relay.segments) <= 5


def test_the_handler_answers_a_segment_from_the_relay(monkeypatch):
    station = Station()
    monkeypatch.setattr(server, 'urlopen', station)
    monkeypatch.setattr(server, 'VIZELVISION_RELAY', hls_relay.Relay(
        server.VIZELVISION_MEDIA_URL, opener=lambda request, timeout: server.urlopen(request, timeout=timeout)))
    h = handler('/stream/vizelvision/ab_video1_seg77.mp4?session=s')
    h.do_GET()
    assert (h.sent['status'], h.wfile.getvalue(), h.sent['Content-Type']) == (200, b'ab_video1_seg77.mp4', 'video/mp4')


def test_the_newest_segments_are_left_out_until_they_are_in_memory():
    gate = threading.Event()
    station = Station()

    def slow(request, timeout):
        if 'seg19' in request.full_url:
            gate.wait(5)                     # the newest segment is still on its way
        return station(request, timeout)
    relay = hls_relay.Relay('http://primary/v1/master/media/', slow)
    held = relay.get('video1_stream.m3u8?session=s')[2].decode()
    assert held.rstrip().endswith('ab_video1_seg18.mp4?session=s') or held.rstrip().endswith('ab_video1_seg17.mp4?session=s')
    assert 'seg19' not in held
    assert held.startswith('#EXTM3U\n#EXT-X-START:TIME-OFFSET=-10.0\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:10\n')
    relay.segments['ab_video1_seg18.mp4'].result(timeout=5)
    held = relay.get('video1_stream.m3u8?session=s')[2].decode()
    assert held.rstrip().endswith('ab_video1_seg18.mp4?session=s')
    gate.set()
    settle(relay)
    assert relay.get('video1_stream.m3u8?session=s')[2] == MEDIA.replace(   # all in memory: whole
        b'#EXTM3U\n', b'#EXTM3U\n#EXT-X-START:TIME-OFFSET=-10.0\n', 1)


def test_at_most_two_segments_are_held_back():
    relay = hls_relay.Relay('http://primary/v1/master/media/', lambda request, timeout: pytest.fail('fetched'))
    relay._listed = lambda name, playlist: True
    held = relay._held(MEDIA).decode()   # nothing in memory: only the newest two go
    assert held.rstrip().endswith('ab_video1_seg17.mp4?session=s')
    assert held.count('#EXTINF') == 8


def test_a_start_of_its_own_and_multivariant_playlists_are_left_alone():
    relay = hls_relay.Relay('http://primary/v1/master/media/', Station())
    own = b'#EXTM3U\n#EXT-X-START:TIME-OFFSET=-4.0\n#EXTINF:1.0,\nseg1.mp4\n'
    assert relay._started(own) is own
    multivariant = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=9329480\nvideo1_stream.m3u8?session=s\n'

    def station(request, timeout):
        response = BytesIO(multivariant)
        response.status, response.headers = 200, {}
        return response
    relay = hls_relay.Relay('http://primary/v1/master/media/', station)
    assert relay.get('index.m3u8')[2] == multivariant


def test_a_player_starts_at_most_half_the_playlist_back():
    """A 10 s window and a 10 s start would leave the player at the oldest segment."""
    relay = hls_relay.Relay('http://primary/v1/master/media/', Station(), start_within=0.5)
    assert relay._started(MEDIA).startswith(b'#EXTM3U\n#EXT-X-START:TIME-OFFSET=-5.0\n')
    thirty = MEDIA + b''.join(b'#EXTINF:1.0,\nab_video1_seg%d.mp4\n' % n for n in range(20, 40))
    assert relay._started(thirty).startswith(b'#EXTM3U\n#EXT-X-START:TIME-OFFSET=-10.0\n')
    assert server.VIZELVISION_RELAY.start_within == 0.5
