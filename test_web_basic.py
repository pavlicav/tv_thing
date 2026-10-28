"""The web-basic service (web_basic.py), Cairo Radio's card (cairo_radio_video.py) and what
control reads of them. Simulated streams only: no ffmpeg runs."""
import io
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import cairo_radio_video
import internet_streams
import lineup
import server
import web_basic


# -- the Sports Guide's video (moved from server.py) --------------------------------------

def test_the_sports_guide_encoder_is_one_bounded_libx264_writing_hls_in_its_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(web_basic, "SPORTS_GUIDE_MUSIC", tmp_path / "no-music.m4a")
    cmd = web_basic.build_sports_guide_command("gen1", tmp_path)
    joined = " ".join(cmd)
    assert cmd[0] == "ffmpeg" and "-nostdin" in cmd
    # Raw frames from the renderer, silence from /dev/zero (no lavfi filter graph).
    assert "-f rawvideo -pix_fmt rgb24 -video_size 1280x720 -framerate 30" in joined
    assert "pipe:0" in cmd and "/dev/zero" in cmd and "lavfi" not in cmd and "-shortest" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264" and cmd[cmd.index("-threads") + 1] == "2"
    assert cmd[cmd.index("-filter_threads") + 1] == "1"
    assert "zerolatency" in cmd[cmd.index("-tune") + 1]
    assert cmd[cmd.index("-colorspace") + 1] == "bt709"
    assert cmd[cmd.index("-hls_time") + 1] == "2" and cmd[cmd.index("-g") + 1] == "60"
    assert "delete_segments" in cmd[cmd.index("-hls_flags") + 1]
    assert cmd[cmd.index("-hls_segment_filename") + 1] == str(tmp_path / "sportsguide_gen1_%05d.ts")
    assert cmd[-1] == str(tmp_path / "index.m3u8")
    renderer = web_basic.sports_guide_renderer_command("http://127.0.0.1:8087")
    assert renderer[1].endswith("sports_guide_video.py")
    assert renderer[renderer.index("--url") + 1] == "http://127.0.0.1:8087/api/v1/sports-guide"
    # The one process builds the same streams, its renderer asking its own API.
    built = server.SLIDE_STREAMS["sports-guide"]
    assert built.renderer_command()[built.renderer_command().index("--url") + 1] == (
        f"http://127.0.0.1:{server.HTTP_PORT}/api/v1/sports-guide")
    assert built.env["OMP_NUM_THREADS"] == built.env["MKL_NUM_THREADS"] == "1"


def test_the_sports_guide_plays_its_music_from_the_wall_clocks_place_in_it(tmp_path, monkeypatch):
    music = tmp_path / "sports-guide-music.m4a"
    monkeypatch.setattr(web_basic, "SPORTS_GUIDE_MUSIC", music)
    # No file, or no length beside it: silence, as before.
    assert web_basic.sports_guide_music() is None
    music.write_bytes(b"aac")
    assert web_basic.sports_guide_music() is None
    music.with_suffix(".json").write_text('{"seconds": 300.0}')
    assert web_basic.sports_guide_music(now=1_000_000_123.5) == (music, 1_000_000_123.5 % 300)
    cmd = web_basic.build_sports_guide_command("gen1", tmp_path, now=1_000_000_123.5)
    joined = " ".join(cmd)
    # Looped, sought to where a channel that never stopped would be; no /dev/zero.
    assert f"-stream_loop -1 -ss {(1_000_000_123.5 % 300):.3f} -thread_queue_size 8 -i {music}" in joined
    assert "/dev/zero" not in cmd and "-shortest" in cmd and "lavfi" not in cmd
    assert cmd[cmd.index("-map") + 1] == "0:v:0" and "1:a:0" in cmd
    assert cmd[cmd.index("-b:a") + 1] == "96k"
    # The music that ships with it.
    shipped = web_basic.BASE_DIR / "static" / "sports-guide-music.m4a"
    assert shipped.is_file() and float(json.loads(shipped.with_suffix(".json").read_text())["seconds"]) > 200


# -- Cairo Radio -------------------------------------------------------------------------------

def test_cairo_radio_is_one_cheap_libx264_frame_a_second_under_the_stations_audio(tmp_path):
    cmd = web_basic.build_cairo_radio_command("gen1", tmp_path, audio_url="http://radio.test/cairo.mp3")
    joined = " ".join(cmd)
    assert cmd[0] == "ffmpeg" and "-nostdin" in cmd and "-shortest" in cmd
    assert "-f rawvideo -pix_fmt rgb24 -video_size 1280x720 -framerate 1" in joined
    # The station's stream, reconnected when it drops and given up on when silent.
    assert "-reconnect 1 -reconnect_streamed 1" in joined and cmd[cmd.index("-rw_timeout") + 1] == "10000000"
    assert cmd[cmd.index("-i", cmd.index("pipe:0")) + 1] == "http://radio.test/cairo.mp3"
    assert cmd[cmd.index("-threads") + 1] == "1" and cmd[cmd.index("-filter_threads") + 1] == "1"
    assert cmd[cmd.index("-tune") + 1] == "stillimage" and cmd[cmd.index("-g") + 1] == "2"
    assert cmd[cmd.index("-c:a") + 1] == "aac" and cmd[cmd.index("-hls_time") + 1] == "2"
    assert cmd[cmd.index("-hls_segment_filename") + 1] == str(tmp_path / "cairoradio_gen1_%05d.ts")
    assert cmd[-1] == str(tmp_path / "index.m3u8")
    renderer = web_basic.cairo_radio_renderer_command("http://radio.test")
    assert renderer[1].endswith("cairo_radio_video.py") and renderer[renderer.index("--fps") + 1] == "1"


def test_the_cards_lines_are_vizelvisions_words():
    music = {"now": {"title": "Enceladus", "artist": "NASA · Cassini RPWS", "kind": "music"}}
    assert cairo_radio_video.describe(music, "Night") == {
        "show": "NIGHT SHOW", "playing": "NASA · CASSINI RPWS — ENCELADUS"}
    assert cairo_radio_video.describe(music, "Morning Show")["show"] == "MORNING SHOW"
    talk = {"now": {"title": "Station ID", "artist": "Cairo", "kind": "log"}}
    assert cairo_radio_video.describe(talk, None) == {"show": None, "playing": "STATION ID"}
    assert cairo_radio_video.describe({"now": {"title": "x", "kind": "spotify buffering"}}, None)["playing"] is None
    long_credits = {"now": {"title": "T" * 30, "artist": "A" * 30 + ", B" * 20, "kind": "music"}}
    assert cairo_radio_video.describe(long_credits, None)["playing"] == "A" * 30 + " — " + "T" * 30


def test_the_show_comes_from_the_log_else_the_weekly_grid():
    answers = {"/api/state": {"now": {"log_id": 7, "title": "Song", "artist": "Band", "kind": "music"}},
               "/api/log": {"entries": [{"id": 7, "source_slot": "Night"}, {"id": 8, "source_slot": None}]},
               "/api/grid": [{"dow": 1, "hour": 22, "name": "Late"}]}
    asked = []

    def opener(url, timeout):
        path = url.removeprefix("http://radio.test")
        asked.append(path)
        return io.BytesIO(json.dumps(answers[path]).encode())

    station = cairo_radio_video.Station("http://radio.test/", opener=opener, clock=lambda: 100.0,
                                        local_now=lambda: datetime(2026, 10, 27, 22, 5))
    station.refresh()
    assert station.info() == {"show": "NIGHT SHOW", "playing": "BAND — SONG"}
    answers["/api/state"]["now"]["log_id"] = 8      # in the log, with no clock: the grid's
    station.refresh()
    assert station.info()["show"] == "LATE SHOW"
    assert asked.count("/api/grid") == 1


def test_a_silent_station_leaves_the_card_on_the_air():
    clock = [0.0]
    station = cairo_radio_video.Station("http://radio.test", opener=None, clock=lambda: clock[0])
    station._info, station._at = {"show": "NIGHT SHOW", "playing": "X"}, 0.0
    assert station.info()["playing"] == "X"
    clock[0] = cairo_radio_video.STALE_SECONDS + 1
    assert station.info() == {"show": None, "playing": None}
    frame = cairo_radio_video.card(station.info(), datetime(2026, 10, 27, 22, 5))
    assert frame.shape == (720, 1280, 3) and frame.dtype.name == "uint8"


def test_a_long_title_wraps_to_two_lines_and_is_cut():
    face = cairo_radio_video.guide.font(40)
    lines = cairo_radio_video.wrap("WORD " * 60, face)
    assert len(lines) == 2 and lines[-1].endswith("…")
    assert all(face.getlength(line) <= cairo_radio_video.TEXT_WIDTH for line in lines)
    assert cairo_radio_video.wrap("SHORT", face) == ["SHORT"]


def test_the_card_is_drawn_again_only_when_it_changes():
    station = cairo_radio_video.Station("http://radio.test", opener=None)
    frames = cairo_radio_video.frames(station, clock=lambda: 1_800_000_000)
    first, second = next(frames), next(frames)
    assert first is second


# -- the service ----------------------------------------------------------------------------

def test_its_slide_streams_are_the_lineups_each_in_its_own_folder(tmp_path):
    streams = web_basic.slide_streams(tmp_path, "http://127.0.0.1:8087")
    assert set(streams) == set(lineup.SLIDES) == {"sports-guide", "cairo-radio"}
    assert {name: stream.directory for name, stream in streams.items()} == {
        "sports-guide": tmp_path / "sports-guide", "cairo-radio": tmp_path / "cairo-radio"}
    assert streams["cairo-radio"].startup_seconds == 20.0
    assert all(stream.env["OPENBLAS_NUM_THREADS"] == "1" for stream in streams.values())


class _FakeSlides:
    def __init__(self, directory, ready=True):
        self.directory, self.ready, self.demanded, self.touched = directory, ready, [], 0

    def demand(self, wait=0.0):
        self.demanded.append(wait)
        return self.ready

    def touch(self):
        self.touched += 1

    def status(self):
        return {"running": self.ready}

    def check(self):
        pass

    def stop(self, reason=""):
        pass


@pytest.fixture
def service(tmp_path):
    streams_file = tmp_path / "internet_streams.json"
    streams_file.write_text(json.dumps({"streams": [
        {"id": "pups", "name": "Pups", "resolver": "url", "url": "http://cdn.test/pups.m3u8", "service": "web-basic"},
        {"id": "trains", "name": "Trains", "resolver": "url", "url": "http://cdn.test/trains.m3u8"}]}))
    basic = web_basic.WebBasic(tmp_path / "streams", "http://127.0.0.1:1", streams_file)
    (tmp_path / "cairo").mkdir()
    (tmp_path / "cairo" / "index.m3u8").write_text("#EXTM3U\n#EXTINF:2.0,\ncairoradio_g_00000.ts\n")
    (tmp_path / "cairo" / "cairoradio_g_00000.ts").write_bytes(b"\x47" * 188)
    basic.slides = {"sports-guide": _FakeSlides(tmp_path, ready=False), "cairo-radio": _FakeSlides(tmp_path / "cairo")}
    basic.relays.playlist = lambda stream_id, name: f"#EXTM3U\n# {stream_id}/{name}\n"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_basic.handler_for(basic))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path):
        try:
            response = urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}{path}", timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, dict(response.headers), response.read()
    yield basic, get
    httpd.shutdown()
    httpd.server_close()


def test_it_serves_its_slide_channels_video(service):
    basic, get = service
    code, headers, body = get("/stream/cairo-radio/index.m3u8")
    assert code == 200 and body.startswith(b"#EXTM3U") and headers["Content-Type"] == "application/vnd.apple.mpegurl"
    assert basic.slides["cairo-radio"].demanded == [web_basic.START_TIMEOUT]
    code, headers, body = get("/stream/cairo-radio/cairoradio_g_00000.ts")
    assert (code, headers["Content-Type"], len(body), basic.slides["cairo-radio"].touched) == (200, "video/mp2t", 188, 1)
    assert headers["Access-Control-Allow-Origin"] == "*"
    code, headers, body = get("/stream/sports-guide/index.m3u8")
    assert code == 503 and headers["Retry-After"] == "2" and b"starting" in body
    assert get("/stream/cairo-radio/..%2Fweb_basic.py")[0] == 400
    assert get("/stream/cairo-radio/gone.ts")[0] == 404


def test_it_relays_only_its_own_web_streams(service):
    basic, get = service
    assert basic.relays_here() == ["pups"]
    code, _, body = get("/stream/internet/pups/index.m3u8")
    assert code == 200 and b"pups/index.m3u8" in body
    # Trains stays the feed's.
    code, _, body = get("/stream/internet/trains/index.m3u8")
    assert code == 404 and b"trains" in body
    code, _, body = get("/stream/digital.m3u8")
    assert code == 404 and json.loads(body)["code"] == "web_basic_only"


def test_its_status_answers_this_host(service):
    basic, get = service
    code, _, body = get("/web/v1/status")
    status = json.loads(body)
    assert code == 200 and status["service"] == "web-basic" and status["relays"] == ["pups"]
    assert status["slides"] == {"sports-guide": {"running": False}, "cairo-radio": {"running": True}}


def test_its_status_refuses_another_host():
    sent = {}
    handler_class = web_basic.handler_for(SimpleNamespace(status=lambda: {"secret": True}, slides={}))
    handler = handler_class.__new__(handler_class)
    handler.path, handler.client_address = "/web/v1/status", ("192.168.1.228", 5555)
    handler.wfile = io.BytesIO()
    handler.send_response = lambda code, *args: sent.setdefault("code", code)
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.do_GET()
    assert sent["code"] == 403


# -- what control reads of it -------------------------------------------------------------

def test_control_reads_the_slide_channels_video_from_web_basic():
    clock = [0.0]
    answers = [{"slides": {"sports-guide": {"running": True, "generation": "g1"}}}]

    def opener(url, timeout):
        assert url == "http://127.0.0.1:8085/web/v1/status"
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())

    client = web_basic.StatusClient("http://127.0.0.1:8085/", lineup.SLIDES, opener=opener, clock=lambda: clock[0])
    assert client.slides()["sports-guide"]["generation"] == "g1"
    assert client.slides()["sports-guide"]["generation"] == "g1"     # read once a second at most
    clock[0] = 2.0
    answers.append(urllib.error.URLError("refused"))
    slides = client.slides()
    assert set(slides) == set(lineup.SLIDES)
    assert slides["cairo-radio"]["running"] is False and "not answering" in slides["cairo-radio"]["unavailable"]
    assert slides["cairo-radio"]["playlist_path"] == "/stream/cairo-radio/index.m3u8"


def test_split_control_reports_web_basics_slides(monkeypatch):
    monkeypatch.setattr(server, "ROLE", "control")
    monkeypatch.setattr(server.web_basic_status, "slides", lambda: {"cairo-radio": {"running": True}})
    assert server._api_v1_status_payload()["slides"] == {"cairo-radio": {"running": True}}


def test_cairo_radio_is_a_slide_channel_whose_page_plays_its_video():
    pages = server.SLIDE_PAGES["cairo-radio"]
    assert pages == {"page_path": "cairo-radio.html", "data_path": None,
                     "stream_path": "/stream/cairo-radio/index.m3u8"}
    page = (server.BASE_DIR / "static" / "cairo-radio.html").read_text()
    assert 'stream/cairo-radio/index.m3u8' in page and "hls.min.js" in page


def test_a_stream_says_which_service_relays_it():
    streams = internet_streams.parse_streams({"streams": [
        {"id": "a", "resolver": "url", "url": "http://x/a.m3u8"},
        {"id": "b", "resolver": "url", "url": "http://x/b.m3u8", "service": "web-basic"}]})
    assert (streams["a"].service, streams["b"].service) == ("feed", "web-basic")
    with pytest.raises(internet_streams.StreamConfigError, match="service is one of feed, web-basic"):
        internet_streams.parse_streams({"streams": [
            {"id": "c", "resolver": "url", "url": "http://x/c.m3u8", "service": "tuner"}]})
    checked_in = internet_streams.parse_streams(json.loads((server.BASE_DIR / "internet_streams.json").read_text()))
    assert (checked_in["pentulive"].service, checked_in["cabride"].service) == ("web-basic", "feed")


def test_apaches_web_basic_route_lists_every_web_basic_stream():
    routes = (server.BASE_DIR / "deploy" / "apache" / "tv-thing-routes.conf").read_text()
    line = next(line for line in routes.splitlines() if line.startswith("ProxyPassMatch ^/stream/("))
    listed = set(line.split("(", 1)[1].split(")", 1)[0].split("|"))
    relays = {f"internet/{stream_id}" for stream_id, stream in internet_streams.parse_streams(
        json.loads((server.BASE_DIR / "internet_streams.json").read_text())).items() if stream.service == "web-basic"}
    assert listed == set(lineup.SLIDES) | relays
    # Before the feed's /stream/, which would otherwise take them.
    assert routes.index("ProxyPassMatch ^/stream/(") < routes.index("ProxyPass        /stream/")
