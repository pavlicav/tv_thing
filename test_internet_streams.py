"""Internet streams: the config, the Yle Areena resolver and the relay."""
import io
import json
from urllib.error import HTTPError

import pytest

import internet_streams
from internet_streams import (InternetStreams, StreamConfigError, StreamUnavailable,
                              parse_streams, rewrite_master, rewrite_media)

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:LANGUAGE="fin",TYPE=AUDIO,URI="hdntl=exp=1~hmac=aa/index_6.m3u8",GROUP-ID="a",DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=7978575,RESOLUTION=1920x1080,AUDIO="a"
hdntl=exp=1~hmac=aa/index_1.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4094887,RESOLUTION=1280x720,AUDIO="a"
hdntl=exp=1~hmac=aa/index_2.m3u8
"""
CDN = "https://cdn.example/live/world/abc/hls/"


def media(count, sequence=100, duration=4.0, extra=None):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4",
             f"#EXT-X-MEDIA-SEQUENCE:{sequence}"]
    for n in range(sequence, sequence + count):
        lines += (extra or {}).get(n, [])
        lines += [f"#EXTINF:{duration},", f"seg_{n}.ts"]
    return "\n".join(lines) + "\n"


def test_streams_are_validated():
    streams = parse_streams({"streams": [
        {"id": "pentulive", "name": "Pentulive", "resolver": "yle-areena", "program": "1-78104441"},
        {"id": "cam", "resolver": "url", "url": "https://example.com/live.m3u8", "note": "a cam"}]})
    assert list(streams) == ["pentulive", "cam"]
    assert streams["pentulive"].params == {"program": "1-78104441"}
    assert streams["cam"].name == "cam" and streams["cam"].note == "a cam"
    for bad in ({"id": "Bad Id", "resolver": "url", "url": "https://x"},
                {"id": "a", "resolver": "rtsp"},
                {"id": "a", "resolver": "yle-areena"},
                {"id": "a", "resolver": "url", "url": "file:///etc/passwd"}):
        with pytest.raises(StreamConfigError):
            parse_streams({"streams": [bad]})
    with pytest.raises(StreamConfigError, match="twice"):
        parse_streams({"streams": [{"id": "a", "resolver": "url", "url": "https://x"}] * 2})


def test_the_master_names_its_renditions_by_position():
    rewritten = rewrite_master(MASTER)
    assert 'URI="0.m3u8"' in rewritten
    assert rewritten.splitlines()[4] == "1.m3u8" and rewritten.splitlines()[6] == "2.m3u8"
    assert "hdntl" not in rewritten
    assert internet_streams._rendition_uris(MASTER)[1] == "hdntl=exp=1~hmac=aa/index_1.m3u8"


def test_a_live_playlist_keeps_its_live_edge_with_absolute_segments():
    out = rewrite_media(media(3600), CDN + "hdntl=x/index_1.m3u8", window=120).splitlines()
    segments = [line for line in out if not line.startswith("#")]
    assert len(segments) == 30
    assert segments[-1] == CDN + "hdntl=x/seg_3699.ts"
    assert "#EXT-X-MEDIA-SEQUENCE:3670" in out


def test_trimming_carries_the_key_and_counts_discontinuities():
    extra = {101: ['#EXT-X-KEY:METHOD=AES-128,URI="key1"'], 102: ["#EXT-X-DISCONTINUITY"]}
    out = rewrite_media(media(10, extra=extra), CDN + "index.m3u8", window=8).splitlines()
    assert "#EXT-X-MEDIA-SEQUENCE:108" in out
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in out
    first_segment = out.index(next(line for line in out if line.startswith("#EXTINF")))
    assert out[first_segment - 1] == f'#EXT-X-KEY:METHOD=AES-128,URI="{CDN}key1"'


def test_relayed_segments_are_named_below_their_rendition():
    playlist = media(3, extra={100: ['#EXT-X-MAP:URI="init.mp4"']}) + "#EXTINF:4.0,\nhttps://other.example/ad.ts\n"
    out = rewrite_media(playlist, CDN + "hdntl=x/index_1.m3u8", window=0, relay="1").splitlines()
    assert "1/hdntl=x/seg_100.ts" not in out and "1/seg_100.ts" in out
    assert '#EXT-X-MAP:URI="1/init.mp4"' in out
    assert out[-1] == "https://other.example/ad.ts"    # Another host: the client fetches it.


def test_a_finished_playlist_is_not_trimmed():
    out = rewrite_media(media(100) + "#EXT-X-ENDLIST\n", CDN + "index.m3u8", window=8)
    assert out.count("#EXTINF") == 100 and out.endswith("#EXT-X-ENDLIST\n")


class FakeCdn:
    """Yle's preview API and a CDN whose token the test can expire."""

    def __init__(self, preview=None):
        self.token = "t1"
        self.calls = []
        self.preview = preview

    def __call__(self, url, accept="*/*", headers=None):
        self.calls.append(url)
        if url.startswith("https://player.api.yle.fi/"):
            if (headers or {}).get("Origin") != "https://areena.yle.fi":
                return url, json.dumps({"data": {"not_allowed": {}}})
            data = self.preview if self.preview is not None else {"ongoing_event": {
                "manifest_url": f"{CDN}index.m3u8?hdnts={self.token}"}}
            return url, json.dumps({"data": data})
        if f"hdnts={self.token}" in url or f"hdntl={self.token}" in url:
            if url.split("?")[0].endswith("/index.m3u8"):
                return url, MASTER.replace("hdntl=exp=1~hmac=aa", f"hdntl={self.token}")
            return url, media(60)
        raise HTTPError(url, 403, "Forbidden", {}, None)

    def open_url(self, url, accept="*/*", headers=None):
        self.calls.append(url)
        if f"hdntl={self.token}/" not in url:
            raise HTTPError(url, 403, "Forbidden", {}, None)
        return io.BytesIO(url.encode())


@pytest.fixture
def relay(tmp_path):
    config = tmp_path / "internet_streams.json"
    config.write_text(json.dumps({"streams": [
        {"id": "pentulive", "name": "Pentulive", "resolver": "yle-areena", "program": "1-78104441"}]}))
    cdn = FakeCdn()
    clock = [0.0]
    return InternetStreams(config, fetch=cdn, clock=lambda: clock[0], open_url=cdn.open_url), cdn, clock


def test_the_relay_resolves_areena_and_serves_both_playlists(relay):
    streams, cdn, _ = relay
    assert 'URI="0.m3u8"' in streams.playlist("pentulive", "index.m3u8")
    variant = streams.playlist("pentulive", "1.m3u8").splitlines()
    assert variant[-1] == "1/seg_159.ts"
    assert streams.segment("pentulive", "1", "seg_159.ts").read().decode() == (
        f"{CDN}hdntl=t1/seg_159.ts")
    assert sum("player.api.yle.fi" in call for call in cdn.calls) == 1


def test_an_expired_token_is_resolved_again(relay):
    streams, cdn, _ = relay
    streams.playlist("pentulive", "1.m3u8")
    cdn.token = "t2"           # The CDN now refuses t1.
    # The client's segment path is the same; the token under it is new.
    assert streams.segment("pentulive", "1", "seg_159.ts").read().decode() == (
        f"{CDN}hdntl=t2/seg_159.ts")
    assert sum("player.api.yle.fi" in call for call in cdn.calls) == 2


def test_a_resolved_url_is_looked_up_again_after_a_while(relay):
    streams, cdn, clock = relay
    streams.playlist("pentulive", "index.m3u8")
    clock[0] += internet_streams.RESOLVE_SECONDS + 1
    streams.playlist("pentulive", "index.m3u8")
    assert sum("player.api.yle.fi" in call for call in cdn.calls) == 2


def test_unknown_streams_renditions_and_finished_events(relay, tmp_path):
    streams, cdn, _ = relay
    with pytest.raises(StreamUnavailable) as missing:
        streams.playlist("nope", "index.m3u8")
    assert missing.value.status == 404
    with pytest.raises(StreamUnavailable) as rendition:
        streams.playlist("pentulive", "9.m3u8")
    assert rendition.value.status == 404
    cdn.preview = {}
    streams.forget("pentulive")
    with pytest.raises(StreamUnavailable, match="nothing live") as over:
        streams.playlist("pentulive", "index.m3u8")
    assert over.value.status == 503


def test_segment_paths_stay_below_their_playlist(relay):
    streams, _, _ = relay
    for path in ("../index_2.m3u8", "a/../../x.ts", "/etc/passwd", "a//b.ts", ""):
        with pytest.raises(StreamUnavailable) as refused:
            streams.segment("pentulive", "1", path)
        assert refused.value.status == 400
    with pytest.raises(StreamUnavailable) as rendition:
        streams.segment("pentulive", "media", "seg_1.ts")
    assert rendition.value.status == 404


def test_no_config_file_means_no_streams(tmp_path):
    assert InternetStreams(tmp_path / "absent.json").streams() == {}
