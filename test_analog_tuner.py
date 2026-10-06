"""Fast tests for fixed NTSC cable channels and V4L2 analog tuning."""

import ctypes
import errno

from pathlib import Path

import analog_tuner


def test_linux_v4l2_analog_abi_sizes_and_requests():
    assert ctypes.sizeof(analog_tuner.V4L2Input) == 80
    assert ctypes.sizeof(analog_tuner.V4L2Tuner) == 84
    assert ctypes.sizeof(analog_tuner.V4L2Frequency) == 44
    assert analog_tuner.VIDIOC_ENUMINPUT == 0xC050561A
    assert analog_tuner.VIDIOC_G_TUNER == 0xC054561D
    assert analog_tuner.VIDIOC_S_FREQUENCY == 0x402C5639


def test_us_cable_std_channel_plan_has_fixed_channels_1_through_135():
    channels = analog_tuner.cable_channels()

    assert [row["number"] for row in channels] == list(range(1, 136))
    assert analog_tuner.cable_frequency_hz(1) == 73_250_000
    assert analog_tuner.cable_frequency_hz(4) == 67_250_000
    assert analog_tuner.cable_frequency_hz(14) == 121_250_000
    assert analog_tuner.cable_frequency_hz(23) == 217_250_000
    assert analog_tuner.cable_frequency_hz(94) == 643_250_000
    assert analog_tuner.cable_frequency_hz(95) == 91_250_000
    assert analog_tuner.cable_frequency_hz(100) == 649_250_000
    assert analog_tuner.cable_frequency_hz(135) == 859_250_000


def test_out_of_range_cable_channel_is_rejected():
    for channel in (0, 136):
        try:
            analog_tuner.cable_frequency_hz(channel)
        except ValueError as error:
            assert "between 1 and 135" in str(error)
        else:
            raise AssertionError(f"channel {channel} should be invalid")


def test_tuner_input_enumeration_keeps_only_analog_tuner(monkeypatch):
    monkeypatch.setattr(analog_tuner.os, "open", lambda *_args: 12)
    closed = []
    monkeypatch.setattr(analog_tuner.os, "close", closed.append)

    def ioctl(_fd, request, entry):
        assert request == analog_tuner.VIDIOC_ENUMINPUT
        if entry.index == 0:
            entry.name = b"Tuner 1"
            entry.type = analog_tuner.V4L2_INPUT_TYPE_TUNER
            entry.tuner = 2
            entry.std = analog_tuner.V4L2_STD_NTSC_M
            return 0
        if entry.index == 1:
            entry.name = b"Composite"
            entry.type = 2
            return 0
        raise OSError(errno.EINVAL, "end")

    monkeypatch.setattr(analog_tuner.fcntl, "ioctl", ioctl)
    inputs = analog_tuner.enumerate_tuner_inputs("/dev/video0")

    assert inputs == [{
        "input_index": 0,
        "input_name": "Tuner 1",
        "tuner_index": 2,
        "standards": analog_tuner.V4L2_STD_NTSC_M,
    }]
    assert closed == [12]


def test_configure_tuner_converts_channel_frequency_to_driver_units(monkeypatch):
    monkeypatch.setattr(analog_tuner.os, "open", lambda *_args: 8)
    monkeypatch.setattr(analog_tuner.os, "close", lambda _fd: None)
    requests = []

    def ioctl(_fd, request, value):
        requests.append((request, value))
        if request == analog_tuner.VIDIOC_G_TUNER:
            value.name = b"NTSC tuner"
            value.type = analog_tuner.V4L2_TUNER_ANALOG_TV
            value.capability = analog_tuner.V4L2_TUNER_CAP_LOW
            value.rangelow = 700_000
            value.rangehigh = 14_000_000
            value.signal = 32768
        return 0

    monkeypatch.setattr(analog_tuner.fcntl, "ioctl", ioctl)
    status = analog_tuner.configure_tuner(
        "/dev/video0", input_index=0, tuner_index=0,
        frequency_hz=analog_tuner.cable_frequency_hz(4),
    )

    assert [request for request, _value in requests] == [
        analog_tuner.VIDIOC_S_INPUT,
        analog_tuner.VIDIOC_S_STD,
        analog_tuner.VIDIOC_G_TUNER,
        analog_tuner.VIDIOC_S_FREQUENCY,
    ]
    frequency = requests[-1][1]
    assert frequency.frequency == 1_076_000  # 67.25 MHz / 62.5 Hz
    assert status == {"name": "NTSC tuner", "signal": 50}


def test_discovery_prefers_the_cx18_raw_yuv_node(monkeypatch):
    caps = {
        "/dev/video0": {"driver": "cx18", "card": "Hauppauge HVR-1600",
                        "bus_info": "PCI:0000:07:00.0"},
        "/dev/video32": {"driver": "cx18", "card": "Hauppauge HVR-1600",
                         "bus_info": "PCI:0000:07:00.0"},
    }
    names = {"video0": "cx18-0 encoder MPEG", "video32": "cx18-0 encoder YUV"}

    monkeypatch.setattr(
        analog_tuner, "query_capability", lambda device: caps.get(device)
    )
    monkeypatch.setattr(
        analog_tuner, "enumerate_tuner_inputs",
        lambda _device: [{
            "input_index": 0, "input_name": "Tuner 1",
            "tuner_index": 0, "standards": analog_tuner.V4L2_STD_NTSC_M,
        }],
    )
    monkeypatch.setattr(
        analog_tuner, "_read_text",
        lambda path: names.get(path.parent.name, "")
        if path.name == "name" else "",
    )
    monkeypatch.setattr(analog_tuner, "_hardware_root", lambda _path: None)
    monkeypatch.setattr(
        analog_tuner, "_tuner_audio_device", lambda *_a: "hw:2,0"
    )

    class FakeDevRoot:
        def glob(self, _pattern):
            return [Path("/dev/video0"), Path("/dev/video32")]

    tuners = analog_tuner.discover_analog_tuners(
        dev_root=FakeDevRoot(),
        sys_video=Path("/sys/class/video4linux"),
        sys_sound=Path("/sys/class/sound"),
    )

    # The raw YUV node is preferred over the hardware MPEG node.
    assert tuners[0]["video_node"] == "video32"
    assert tuners[0]["audio_device"] == "hw:2,0"
    assert tuners[0]["input_args"] == [
        "-standard", "NTSC-M", "-channel", "0",
        "-input_format", "uyvy422", "-video_size", "720x480",
    ]


def test_discovery_excludes_capture_cards_without_a_tuner(monkeypatch):
    """A USB grabber has camera inputs but no tuner, so it never appears."""
    monkeypatch.setattr(
        analog_tuner, "query_capability",
        lambda device: {"driver": "em28xx", "card": "Pinnacle Dazzle DVC100",
                        "bus_info": "usb-0000:00:14.0-12"},
    )
    # enumerate_tuner_inputs keeps only V4L2_INPUT_TYPE_TUNER entries, so a
    # composite/S-video grabber yields an empty list.
    monkeypatch.setattr(
        analog_tuner, "enumerate_tuner_inputs", lambda _device: []
    )
    monkeypatch.setattr(analog_tuner, "_read_text", lambda _path: "")
    monkeypatch.setattr(analog_tuner, "_hardware_root", lambda _path: None)

    class FakeDevRoot:
        def glob(self, _pattern):
            return [Path("/dev/video1")]

    assert analog_tuner.discover_analog_tuners(
        dev_root=FakeDevRoot(),
        sys_video=Path("/sys/class/video4linux"),
        sys_sound=Path("/sys/class/sound"),
    ) == []
