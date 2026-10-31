"""The Config page's API, and what the server takes from this host's settings."""
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import feed_node
import host_config
import server


# -- the shipped primary: what this branch must not change on viviserv ----------------------
def test_the_primary_runs_feed_json_as_written():
    assert server.HOST.value("role") == "primary"
    assert server.FEED_CONFIG == feed_node.load_config(server.FEED_CONFIG_FILE)
    assert server.FEED_CONFIG.node == "viviserv"
    assert server.DIGITAL_ADAPTER == 0


def test_the_digital_lanes_source_still_tunes_adapter_zero():
    channel = {"frequency": 177_000_000, "service_id": 3, "video_pid": 49, "audio_pid": 52}
    command = server.build_tv_source_command(channel, udp_port=18320)
    assert command[command.index("--adapter") + 1] == "0"


def test_the_constants_resolve_as_they_did_before_the_settings_file():
    assert server.VIDEO_ENCODER == os.environ.get("TV_THING_VIDEO_ENCODER", "cpu").lower()
    assert server.YTTV_GUIDE_ENABLED is (os.environ.get("TV_THING_YTTV_GUIDE", "1") != "0")
    if "TV_THING_RECORDINGS_DIR" in os.environ:
        assert str(server.RECORDINGS_DIR) == os.environ["TV_THING_RECORDINGS_DIR"]
    elif host_config.VIVISERV_RECORDINGS.parent.is_dir():      # viviserv
        assert server.RECORDINGS_DIR == host_config.VIVISERV_RECORDINGS
    assert server.VIZELVISION_MEDIA_URL == os.environ.get(
        "TV_THING_VIZELVISION_MEDIA_URL", "http://127.0.0.1:6970/v1/master/media/")
    assert server.HOST.roku(server.ROKU_CONFIG_FILE).value == "on"


# -- the Roku lane, off -------------------------------------------------------------------------
def test_a_host_with_the_roku_lane_off_never_builds_it(monkeypatch, tmp_path, capsys):
    off = host_config.Host(environ={"TV_THING_ROKU": "off"}, path=tmp_path / "host.json")
    monkeypatch.setattr(server, "HOST", off)
    monkeypatch.setattr(server, "roku", None)
    monkeypatch.setattr(server.roku_lane, "load", lambda *a, **k: pytest.fail("built the Roku lane"))
    server.start_roku_lane()
    assert server.roku is None
    assert "Roku lane off on this host" in capsys.readouterr().out
    assert server.roku_unavailable()[0]["error"]["message"].startswith("The Roku lane is off on viviserv")

    from roku_control import config as roku_config
    monkeypatch.setattr(roku_config, "load_config", lambda *a: pytest.fail("read the capture"))
    monkeypatch.setattr(server, "roku_feedback", None)
    server.start_roku_capture_lane()
    assert server.roku_feedback is None


def test_the_roku_lane_off_says_so_in_the_sports_guide(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "HOST", host_config.Host(environ={"TV_THING_ROKU": "off"},
                                                         path=tmp_path / "host.json"))
    assert server.roku_unavailable_message().startswith("The Roku lane is off on viviserv (set)")


# -- GET and POST /api/v1/config --------------------------------------------------------------
@pytest.fixture
def api(monkeypatch, tmp_path):
    path = tmp_path / "tv-thing" / "host.json"
    here = host_config.Host(environ={}, path=path, hostname="viviserv")
    monkeypatch.setattr(server, "HOST", here)
    environments = {"feed": {"TV_THING_VIDEO_ENCODER": "cpu"}, "control": {}, "web-basic": {}}
    monkeypatch.setattr(server, "config_environments", lambda: environments)
    monkeypatch.setattr(server, "config_started", lambda: {"feed": 0, "control": 0, "web-basic": 0})
    monkeypatch.setattr(server.host_config, "detect", lambda host: {
        "hostname": host.hostname, "system": {}, "dvb": [{"adapter": 0, "frontends": [0],
                                                          "description": "a tuner", "chips": []}],
        "video": [], "roku_capture": None, "gpus": [], "encoders": ["libx264"], "tools": {}})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.TVHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(method, payload=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/v1/config", method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    yield request, path
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


def test_the_page_reads_every_setting_and_why(api):
    request, path = api
    status, body = request("GET")
    assert status == 200
    assert body["node"] == "viviserv" and body["node_role"] == "primary"
    assert body["file"] == {"path": str(path), "exists": False, "saved_at": None, "problems": []}
    settings = {item["id"]: item for item in body["settings"]}
    assert list(settings) == [setting.id for setting in host_config.SETTINGS]
    assert settings["roku"]["effective"] == "on" and settings["roku"]["source"] == "auto"
    encoder = settings["video_encoder"]
    assert encoder["editable"] is False
    assert encoder["environment"] == [{"service": "feed", "unit": "tv-thing-feed",
                                       "name": "TV_THING_VIDEO_ENCODER", "value": "cpu"}]
    assert body["restart"] == [] and body["hardware"]["encoders"] == ["libx264"]


def test_saving_writes_the_file_and_names_the_restarts(api):
    request, path = api
    status, body = request("POST", {"settings": {"roku": "off", "primary_host": "10.0.0.98"}})
    assert status == 200 and body["ok"] is True
    assert json.loads(path.read_text())["roku"] == "off"
    settings = {item["id"]: item for item in body["settings"]}
    assert settings["roku"]["saved"] == "off" and settings["roku"]["effective"] == "off"
    assert settings["roku"]["waiting_for"] == ["feed", "control"]
    assert [item["unit"] for item in body["restart"]] == ["tv-thing-feed", "tv-thing", "tv-thing-web-basic"]
    # "auto" takes a setting back out of the file.
    status, body = request("POST", {"settings": {"roku": "auto"}})
    assert status == 200 and "roku" not in json.loads(path.read_text())


def test_saving_refuses_bad_values_and_what_the_environment_sets(api):
    request, path = api
    status, body = request("POST", {"settings": {"roku": "maybe", "video_encoder": "nvenc", "bogus": 1}})
    assert status == 400 and body["error"]["code"] == "invalid_settings"
    assert set(body["fields"]) == {"roku", "video_encoder", "bogus"}
    assert "tv-thing-feed" in body["fields"]["video_encoder"]
    assert not path.exists()            # nothing saved when anything is refused
    assert request("POST", {"settings": {}})[0] == 400
    assert request("POST", ["roku"])[0] == 400
