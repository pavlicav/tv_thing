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
    monkeypatch.setattr(server, 'VIZELVISION_MAX_MEDIA_BYTES', 3)
    monkeypatch.setattr(server, 'urlopen', lambda *a, **kw: BytesIO(b'1234'))
    h = handler('/stream/vizelvision/segment.mp4')
    h.do_GET()
    assert h.sent['status'] == 502
