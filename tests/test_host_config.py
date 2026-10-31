"""This host's settings and hardware (host_config.py), on a pretend viviserv and viviserv2."""
import json
import os
from pathlib import Path

import pytest

import feed_node
import host_config

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = feed_node.load_config(ROOT / "feed.json")
HD_PVR = "/dev/v4l/by-id/usb-AMBA_Hauppauge_HD_PVR_00A4E5AC-video-index0"


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def link(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, path)


@pytest.fixture
def viviserv2(tmp_path):
    """The NixOS box: a WinTV-dualHD (two ATSC tuners on one USB interface), Intel graphics,
    no analog tuner, no HD PVR."""
    root = tmp_path / "root"
    for adapter in (0, 1):
        write(root / f"dev/dvb/adapter{adapter}/frontend0")
        write(root / f"dev/dvb/adapter{adapter}/demux0")
    usb = root / "sys/devices/pci0000:00/0000:00:14.0/usb1/1-4"
    for name, value in (("idVendor", "2040"), ("idProduct", "826d"), ("manufacturer", "HCW"),
                        ("product", "dualHD")):
        write(usb / name, value + "\n")
    interface = usb / "1-4:1.0"
    link(interface / "driver", "../../../../../../bus/usb/drivers/em28xx")
    write(interface / "i2c-5/5-0059/name", "lgdt3306a\n")
    write(interface / "i2c-5/5-0060/name", "si2157\n")
    write(interface / "i2c-6/6-000e/name", "lgdt3306a\n")
    for adapter in (0, 1):
        link(root / f"sys/class/dvb/dvb{adapter}.frontend0/device", interface)
    gpu = root / "sys/devices/pci0000:00/0000:00:02.0"
    write(gpu / "vendor", "0x8086\n")
    link(gpu / "driver", "../../../bus/pci/drivers/i915")
    link(root / "sys/class/drm/renderD128/device", gpu)
    return root


@pytest.fixture
def viviserv(tmp_path):
    """The production box: an HVR-1600 (cx18) and the HD PVR."""
    root = tmp_path / "root"
    write(root / "dev/dvb/adapter0/frontend0")
    card = root / "sys/devices/pci0000:00/0000:07:00.0"
    for name, value in (("vendor", "0x14f1"), ("device", "0x5b7a"), ("subsystem_vendor", "0x0070")):
        write(card / name, value + "\n")
    link(card / "driver", "../../../bus/pci/drivers/cx18")
    write(card / "i2c-1/1-004c/name", "cs5345\n")
    link(root / "sys/class/dvb/dvb0.frontend0/device", card)
    write(root / "sys/class/video4linux/video0/name", "cx18-0 encoder MPEG\n")
    link(root / "sys/class/video4linux/video0/device", card)
    write(root / "sys/class/video4linux/video32/name", "cx18-0 encoder YUV\n")
    link(root / "sys/class/video4linux/video32/device", card)
    usb = root / "sys/devices/pci0000:00/0000:00:14.0/usb2/2-3"
    for name, value in (("idVendor", "2040"), ("idProduct", "4902"), ("manufacturer", "AMBA"),
                        ("product", "Hauppauge HD PVR")):
        write(usb / name, value + "\n")
    link(usb / "2-3:1.0/driver", "../../../../../../bus/usb/drivers/hdpvr")
    write(root / "sys/class/video4linux/video1/name", "Hauppauge HD PVR\n")
    link(root / "sys/class/video4linux/video1/device", usb / "2-3:1.0")
    write(root / "dev/video1")
    link(root / HD_PVR.lstrip("/"), "../../video1")
    (root / "mnt/sda1/media").mkdir(parents=True)
    return root


def host(root, hostname, tmp_path, environ=None, settings=None):
    path = tmp_path / "host.json"
    if settings is not None:
        path.write_text(json.dumps(settings))
    return host_config.Host(environ=environ or {}, path=path, hostname=hostname, root=root)


# -- viviserv: every setting as before ------------------------------------------------------
def test_viviserv_with_no_settings_is_the_primary_and_changes_nothing(viviserv, tmp_path):
    here = host(viviserv, "viviserv", tmp_path)
    assert here.value("role") == "primary"
    assert here.value("node") == "viviserv"
    assert here.value("roku") == "on"
    assert here.value("yttv_guide") == "on"
    assert here.value("digital_adapter") == 0
    assert here.value("video_encoder") == "cpu"
    assert here.value("recordings_dir") == Path("/mnt/sda1/media/TV Recordings")
    assert here.primary_url(1615, "/cairo.mp3") is None
    # feed.json as written: the very object, names and descriptions untouched.
    assert here.feed_config(SHIPPED) is SHIPPED


def test_viviserv_units_environment_still_wins(viviserv, tmp_path):
    environ = {"TV_THING_VIDEO_ENCODER": "CPU", "TV_THING_RECORDINGS_DIR": "/mnt/sda1/media/TV Recordings",
               "TV_THING_YTTV_GUIDE": "0"}
    here = host(viviserv, "viviserv", tmp_path, environ,
                settings={"video_encoder": "nvenc", "recordings_dir": "/srv/elsewhere"})
    assert here.get("video_encoder") == host_config.Resolved("cpu", "environment", "set")
    assert here.value("recordings_dir") == Path("/mnt/sda1/media/TV Recordings")
    assert here.value("yttv_guide") == "off"


def test_viviserv_sees_its_cards(viviserv, tmp_path):
    [adapter] = host_config.dvb_adapters(viviserv)
    assert adapter["adapter"] == 0 and adapter["driver"] == "cx18"
    assert adapter["description"] == "Hauppauge card (cx18, PCI 14f1:5b7a)"   # its audio chip is no tuner
    video = host_config.video_devices(viviserv)
    assert [(v["device"], v["kind"]) for v in video] == [
        ("/dev/video0", "analog"), ("/dev/video1", "capture"), ("/dev/video32", "other")]
    assert video[1]["by_id"] == HD_PVR
    capture = host(viviserv, "viviserv", tmp_path).capture_device()
    assert capture == {"path": HD_PVR, "name": "Hauppauge HD PVR", "named": True}


# -- viviserv2: a second node, worked out from its name and hardware ------------------------
def test_viviserv2_is_a_second_node_without_the_house_roku(viviserv2, tmp_path):
    here = host(viviserv2, "viviserv2", tmp_path)
    assert here.get("role").value == "second"
    assert "feed.json describes viviserv" in here.get("role").reason
    assert here.value("node") == "viviserv2"
    roku = here.get("roku")
    assert roku.value == "off" and roku.source == "auto"
    assert "capture device" in roku.reason
    assert here.value("yttv_guide") == "off"
    assert here.value("digital_adapter") == 0
    assert here.value("video_encoder") == "cpu"


def test_viviserv2_describes_its_own_hardware(viviserv2, tmp_path):
    config = host(viviserv2, "viviserv2", tmp_path).feed_config(SHIPPED)
    assert config.node == "viviserv2"
    devices = {device.id: device for device in config.devices}
    assert devices["digital"].detail == \
        "Hauppauge dualHD (em28xx, USB 2040:826d): LGDT3306A + Si2157 via /dev/dvb/adapter0"
    assert devices["digital"].adapter == 0
    assert devices["analog"].detail == "Not fitted on viviserv2: no analog TV tuner"
    assert devices["roku"].detail == "Not fitted on viviserv2: no HD PVR"
    lanes = {lane.id: lane for lane in config.lanes}
    assert lanes["digital"].description == "ATSC digital television (LGDT3306A + Si2157)"
    assert lanes["analog"].description == "Analog NTSC cable television (not fitted on viviserv2)"
    assert lanes["roku"].description == "Source Roku via an HD PVR (not on viviserv2)"
    # Everything else about the lanes is feed.json's: their files, processes and devices.
    assert [(l.id, l.device, l.playlist, l.process) for l in config.lanes] == \
        [(l.id, l.device, l.playlist, l.process) for l in SHIPPED.lanes]


def test_viviserv2_sees_both_tuners_and_may_use_the_second(viviserv2, tmp_path):
    assert [entry["adapter"] for entry in host_config.dvb_adapters(viviserv2)] == [0, 1]
    here = host(viviserv2, "viviserv2", tmp_path, settings={"digital_adapter": 1})
    assert here.get("digital_adapter") == host_config.Resolved(1, "file", "set")
    config = here.feed_config(SHIPPED)
    digital = config.device("digital")
    assert digital.adapter == 1 and digital.detail.endswith("via /dev/dvb/adapter1")


def test_a_second_node_reaches_the_primarys_services(viviserv2, tmp_path):
    here = host(viviserv2, "viviserv2", tmp_path, settings={"primary_host": "10.0.0.98"})
    assert here.primary_url(1615, "/cairo.mp3") == "http://10.0.0.98:1615/cairo.mp3"
    assert here.primary_url(80, "/radio") == "http://10.0.0.98/radio"
    # The primary never points at another.
    primary = host(viviserv2, "viviserv2", tmp_path, {"TV_THING_NODE_ROLE": "primary"},
                   settings={"primary_host": "10.0.0.98"})
    assert primary.primary_url(1615, "/cairo.mp3") is None


def test_a_second_node_runs_the_roku_lane_only_with_its_capture_device_or_when_told(viviserv2, tmp_path):
    write(viviserv2 / HD_PVR.lstrip("/"))
    assert host(viviserv2, "viviserv2", tmp_path).value("roku") == "on"
    (viviserv2 / HD_PVR.lstrip("/")).unlink()
    told = host(viviserv2, "viviserv2", tmp_path, settings={"roku": "on"})
    assert told.get("roku") == host_config.Resolved("on", "file", "set")
    assert host(viviserv2, "viviserv2", tmp_path, {"TV_THING_ROKU": "0"}).value("roku") == "off"


def test_the_roku_lane_can_be_turned_off_on_the_primary(viviserv, tmp_path):
    here = host(viviserv, "viviserv", tmp_path, settings={"roku": "off"})
    assert here.get("roku") == host_config.Resolved("off", "file", "set")
    assert here.value("yttv_guide") == "off"


def test_no_roku_config_is_no_roku_lane_whatever_the_setting(viviserv, tmp_path):
    here = host(viviserv, "viviserv", tmp_path, {"TV_THING_ROKU": "on"})
    assert here.roku(tmp_path / "no-roku.json").value == "off"


def test_role_and_name_can_be_set(viviserv2, tmp_path):
    here = host(viviserv2, "viviserv", tmp_path, {"TV_THING_NODE_ROLE": "secondary"},
                settings={"node": "den"})
    assert here.value("role") == "second" and here.value("node") == "den"


def test_recordings_go_home_where_viviservs_disk_is_absent(viviserv2, tmp_path):
    assert host(viviserv2, "viviserv2", tmp_path).value("recordings_dir") == Path.home() / "tv-recordings"


# -- the settings file ------------------------------------------------------------------------
def test_the_file_round_trips_and_keeps_only_what_is_set(tmp_path):
    path = tmp_path / "config" / "host.json"
    host_config.write_settings(path, {"roku": "off", "digital_adapter": 1, "primary_host": "viviserv"})
    document = json.loads(path.read_text())
    assert list(document) == ["_comment", "roku", "digital_adapter", "primary_host"]
    assert host_config.read_settings(path) == ({"roku": "off", "digital_adapter": 1,
                                                "primary_host": "viviserv"}, [])
    assert not [name for name in os.listdir(path.parent) if name.startswith(".")]


def test_a_bad_entry_is_reported_and_left_out(tmp_path):
    path = tmp_path / "host.json"
    path.write_text(json.dumps({"roku": "maybe", "digital_adapter": "1", "nonsense": 3, "_note": "x"}))
    settings, problems = host_config.read_settings(path)
    assert settings == {"digital_adapter": 1}
    assert len(problems) == 2 and any("roku" in p for p in problems)
    path.write_text("{not json")
    assert host_config.read_settings(path)[0] == {}
    assert host_config.read_settings(tmp_path / "absent.json") == ({}, [])


def test_saving_checks_each_value(tmp_path):
    assert host_config.check("roku", "auto") is None
    assert host_config.check("video_encoder", "cpu") is None          # its default
    assert host_config.check("video_encoder", "auto") == "auto"       # a real choice: probe the GPUs
    assert host_config.check("digital_adapter", "2") == 2
    assert host_config.check("primary_host", "10.0.0.98") == "10.0.0.98"
    for setting_id, value in (("role", "boss"), ("node", "two words"), ("digital_adapter", 99),
                              ("primary_host", "http://viviserv"), ("recordings_dir", "relative"),
                              ("nothing", "x")):
        with pytest.raises(ValueError):
            host_config.check(setting_id, value)
    # A folder must exist to be saved, but one read back is kept while its disk is away: a
    # recording should fail loudly, not land somewhere else.
    with pytest.raises(ValueError):
        host_config.check("recordings_dir", str(tmp_path / "absent"), saving=True)
    assert host_config.check("recordings_dir", str(tmp_path / "absent")) == str(tmp_path / "absent")
    assert host_config.check("recordings_dir", str(tmp_path), saving=True) == str(tmp_path)


def test_a_service_sees_the_host_through_its_own_environment(viviserv, tmp_path):
    here = host(viviserv, "viviserv", tmp_path, settings={"video_encoder": "auto"})
    feed = here.with_environment({"TV_THING_VIDEO_ENCODER": "cpu"})
    assert here.value("video_encoder") == "auto" and feed.value("video_encoder") == "cpu"
    assert feed.with_environment({}, saved={}).value("video_encoder") == "cpu"


def test_detect_never_needs_the_tools(viviserv2, tmp_path):
    calls = []

    def run(command, timeout):
        calls.append(command[0])
        return " V..... libx264   H.264\n V..... h264_vaapi   VA-API\n" if command[0] == "ffmpeg" else None
    found = host_config.detect(host(viviserv2, "viviserv2", tmp_path), run=run)
    assert found["encoders"] == ["libx264", "h264_vaapi"]
    assert [gpu["driver"] for gpu in found["gpus"]] == ["i915"]
    assert calls == ["ffmpeg"]          # nvidia-smi only where there is an NVIDIA GPU
    assert found["roku_capture"] is None and found["hostname"] == "viviserv2"
    assert host_config.encoders(run=lambda command, timeout: None) is None
