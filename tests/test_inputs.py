"""GET /api/v1/inputs (the Inputs page): the services as systemd sees them, the web streams
and where each comes from, and the disks. Never runs systemctl or touches a service."""
import subprocess

import internet_streams
import server
import web_basic
from test_api_contract import api  # noqa: F401 - the HTTP fixture

SHOWN = """Id=tv-thing-feed.service
ActiveState=active
SubState=running
ActiveEnterTimestamp=@1793115495
NRestarts=2
MainPID=1760617
MemoryCurrent=263151616
MemoryMax=1887436800
CPUUsageNSec=74400803331000
CPUQuotaPerSecUSec=8s
TasksCurrent=32

Id=tv-thing-web-basic.service
ActiveState=inactive
SubState=dead
ActiveEnterTimestamp=
NRestarts=0
MainPID=0
MemoryCurrent=[not set]
MemoryMax=infinity
CPUUsageNSec=[not set]
CPUQuotaPerSecUSec=infinity
TasksCurrent=[not set]

Id=somebody-else.service
ActiveState=active
"""


def fresh_cache(monkeypatch):
    monkeypatch.setattr(server, "_systemd_cache", {"at": None, "units": {}})


def test_a_quota_is_the_cpu_time_allowed_each_second():
    assert server._quota_percent("8s") == 800.0
    assert server._quota_percent("800ms") == 80.0
    assert server._quota_percent("infinity") is None
    assert server._quota_percent("") is None
    assert server._quota_percent(None) is None


def test_systemd_units_reads_systemctl_show_and_asks_once_in_five_seconds(monkeypatch):
    fresh_cache(monkeypatch)
    calls, now = [], [100.0]

    def run(command, **_):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=SHOWN)

    units = ("tv-thing-feed", "tv-thing-web-basic")
    found = server.systemd_units(units, run=run, clock=lambda: now[0])
    assert calls[0][:3] == ["systemctl", "show", "--timestamp=unix"]
    assert calls[0][-2:] == list(units)
    assert set(found) == set(units)               # a unit it was not asked about is left out
    feed = found["tv-thing-feed"]
    assert feed["state"] == "active" and feed["since"] == 1793115495 and feed["restarts"] == 2
    assert feed["pid"] == 1760617 and feed["memory_max_bytes"] == 1887436800
    assert feed["read_at"] > 0
    assert round(feed["cpu_seconds"], 3) == 74400.803 and feed["cpu_quota_percent"] == 800.0
    stopped = found["tv-thing-web-basic"]
    assert stopped["state"] == "inactive" and stopped["since"] is None and stopped["pid"] is None
    assert stopped["memory_bytes"] is None and stopped["memory_max_bytes"] is None
    assert stopped["cpu_quota_percent"] is None
    now[0] += 4.9
    assert server.systemd_units(units, run=run, clock=lambda: now[0]) is found
    now[0] += 0.2
    server.systemd_units(units, run=run, clock=lambda: now[0])
    assert len(calls) == 2


def test_systemd_units_is_empty_without_systemctl(monkeypatch):
    fresh_cache(monkeypatch)

    def run(command, **_):
        raise FileNotFoundError("systemctl")

    assert server.systemd_units(("tv-thing",), run=run, clock=lambda: 0.0) == {}


def test_a_stream_from_this_machine_or_the_lan_is_internal():
    def stream(url):
        return internet_streams.Stream("x", "X", "url", {"url": url})

    assert server._upstream(stream("http://127.0.0.1:8791/cab/index.m3u8")) == ("127.0.0.1:8791", "internal")
    assert server._upstream(stream("http://192.168.1.98:6970/a.m3u8")) == ("192.168.1.98:6970", "internal")
    assert server._upstream(stream("http://localhost/a.m3u8")) == ("localhost", "internal")
    assert server._upstream(stream("https://cdn.example.com/live/a.m3u8")) == ("cdn.example.com", "external")
    looked_up = internet_streams.Stream("p", "P", "yle-areena", {"program": "1-78104441"})
    assert server._upstream(looked_up) == ("areena.yle.fi", "external")


def test_disks_are_one_entry_a_filesystem_and_skip_folders_not_made(monkeypatch, tmp_path):
    (tmp_path / "streams").mkdir()
    (tmp_path / "recordings").mkdir()
    monkeypatch.setattr(server, "STREAMS_DIR", tmp_path / "streams")
    monkeypatch.setattr(web_basic, "STREAMS_DIR", tmp_path / "streams-web")   # not made yet
    monkeypatch.setattr(server, "RECORDINGS_DIR", tmp_path / "recordings")
    disks = server.disk_inputs()
    assert len(disks) == 1
    assert disks[0]["names"] == ["streams", "recordings"]
    assert disks[0]["paths"] == [str(tmp_path / "streams"), str(tmp_path / "recordings")]
    assert 0 < disks[0]["free_bytes"] <= disks[0]["total_bytes"]


def test_inputs_lists_the_services_and_the_web_streams_in_number_order(api, monkeypatch):  # noqa: F811
    request, _ = api
    monkeypatch.setattr(server, "systemd_units", lambda units: {
        "tv-thing": {"state": "active", "since": 1, "restarts": 0}})
    code, inputs = request("/api/v1/inputs")
    assert code == 200
    assert [service["id"] for service in inputs["services"]] == ["feed", "control", "web-basic"]
    control = inputs["services"][1]
    assert control["unit"] == "tv-thing" and control["answering"] is True
    assert control["systemd"]["state"] == "active"
    assert inputs["services"][0]["systemd"] is None           # a unit systemd does not know
    web = inputs["web"]
    assert [entry["number"] for entry in web] == sorted(entry["number"] for entry in web)
    assert {entry["kind"] for entry in web} == {"vizelvision", "slides", "internet"}
    by_id = {entry["id"]: entry for entry in web}
    assert by_id["internet/cabride"]["origin"] == "internal"
    assert by_id["internet/cabride"]["service"] == "feed" and by_id["internet/cabride"]["listings"] is True
    assert by_id["internet/pentulive"]["origin"] == "external"
    assert by_id["internet/pentulive"]["service"] == "web-basic"
    assert by_id["vizelvision/sports-guide"]["service"] == "web-basic"
    assert by_id["vizelvision/sports-guide"]["page"] == "sports-guide.html"
    assert by_id["vizelvision"]["service"] == "vizelvision"
    assert isinstance(inputs["disks"], list)
    code, discovery = request("/api/v1")
    assert discovery["endpoints"]["inputs"]["path"] == "/api/v1/inputs"
