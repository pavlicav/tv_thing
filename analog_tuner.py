#!/usr/bin/env python3
"""Analog NTSC cable tuning through the Linux V4L2 API."""

import ctypes
import errno
import fcntl
import hashlib
import os
import re
from pathlib import Path


V4L2_CAP_DEVICE_CAPS = 0x80000000
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
V4L2_INPUT_TYPE_TUNER = 1
V4L2_TUNER_ANALOG_TV = 2
V4L2_TUNER_CAP_LOW = 0x0001
V4L2_TUNER_CAP_1HZ = 0x1000
V4L2_STD_NTSC_M = 0x00001000


class V4L2Capability(ctypes.Structure):
    """``struct v4l2_capability`` (104 bytes on the Linux V4L2 ABI)."""

    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


class V4L2Input(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
        ("type", ctypes.c_uint32),
        ("audioset", ctypes.c_uint32),
        ("tuner", ctypes.c_uint32),
        ("std", ctypes.c_uint64),
        ("status", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


class V4L2Tuner(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
        ("type", ctypes.c_uint32),
        ("capability", ctypes.c_uint32),
        ("rangelow", ctypes.c_uint32),
        ("rangehigh", ctypes.c_uint32),
        ("rxsubchans", ctypes.c_uint32),
        ("audmode", ctypes.c_uint32),
        ("signal", ctypes.c_int32),
        ("afc", ctypes.c_int32),
        ("reserved", ctypes.c_uint32 * 4),
    ]


class V4L2Frequency(ctypes.Structure):
    _fields_ = [
        ("tuner", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("frequency", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 8),
    ]


def _ioc(direction, type_char, number, size):
    return (
        (direction << 30)
        | (ord(type_char) << 8)
        | number
        | (size << 16)
    )


VIDIOC_S_STD = _ioc(1, "V", 24, ctypes.sizeof(ctypes.c_uint64))
VIDIOC_ENUMINPUT = _ioc(3, "V", 26, ctypes.sizeof(V4L2Input))
VIDIOC_G_TUNER = _ioc(3, "V", 29, ctypes.sizeof(V4L2Tuner))
VIDIOC_S_INPUT = _ioc(3, "V", 39, ctypes.sizeof(ctypes.c_uint32))
VIDIOC_S_FREQUENCY = _ioc(1, "V", 57, ctypes.sizeof(V4L2Frequency))
VIDIOC_QUERYCAP = _ioc(2, "V", 0, ctypes.sizeof(V4L2Capability))


def _decode(value):
    return bytes(value).split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def cable_frequency_hz(channel):
    """Return the NTSC STD cable video-carrier frequency for channel 1-135."""
    channel = int(channel)
    if channel == 1:
        mhz = 73.25
    elif 2 <= channel <= 4:
        mhz = 55.25 + (channel - 2) * 6
    elif 5 <= channel <= 6:
        mhz = 77.25 + (channel - 5) * 6
    elif 7 <= channel <= 13:
        mhz = 175.25 + (channel - 7) * 6
    elif 14 <= channel <= 22:
        mhz = 121.25 + (channel - 14) * 6
    elif 23 <= channel <= 94:
        mhz = 217.25 + (channel - 23) * 6
    elif 95 <= channel <= 99:
        mhz = 91.25 + (channel - 95) * 6
    elif 100 <= channel <= 135:
        mhz = 649.25 + (channel - 100) * 6
    else:
        raise ValueError("Cable channel must be between 1 and 135")
    return int(mhz * 1_000_000)


def cable_channels():
    return [
        {
            "number": channel,
            "frequency_hz": cable_frequency_hz(channel),
            "frequency_mhz": cable_frequency_hz(channel) / 1_000_000,
        }
        for channel in range(1, 136)
    ]


def enumerate_tuner_inputs(device):
    """Return the analog tuner inputs exposed by one V4L2 video node."""
    fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    inputs = []
    try:
        for index in range(32):
            entry = V4L2Input()
            entry.index = index
            try:
                fcntl.ioctl(fd, VIDIOC_ENUMINPUT, entry)
            except OSError as exc:
                if exc.errno == errno.EINVAL:
                    break
                raise
            if entry.type == V4L2_INPUT_TYPE_TUNER:
                inputs.append({
                    "input_index": entry.index,
                    "input_name": _decode(entry.name),
                    "tuner_index": entry.tuner,
                    "standards": entry.std,
                })
    finally:
        os.close(fd)
    return inputs


def _read_text(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _hardware_root(device_link):
    """Return the PCI function or USB device owning a sysfs node."""
    try:
        resolved = device_link.resolve(strict=True)
    except OSError:
        return None
    for candidate in (resolved, *resolved.parents):
        if ((candidate / "idVendor").is_file()
                and (candidate / "idProduct").is_file()):
            return candidate
        try:
            subsystem = (candidate / "subsystem").resolve(strict=True).name
        except OSError:
            subsystem = ""
        if subsystem == "pci":
            return candidate
    return None


def _tuner_audio_device(video_entry, sys_sound):
    """Find the ALSA capture PCM belonging to the same tuner hardware."""
    video_root = _hardware_root(video_entry / "device")
    if video_root is None:
        return None

    pattern = re.compile(r"pcmC(\d+)D(\d+)c$")
    matches = []
    try:
        pcm_entries = sys_sound.glob("pcmC*D*c")
    except OSError:
        return None
    for pcm in pcm_entries:
        match = pattern.fullmatch(pcm.name)
        if not match or _hardware_root(pcm / "device") != video_root:
            continue
        card, pcm_device = match.groups()
        matches.append((int(card), int(pcm_device)))
    if not matches:
        return None
    card, pcm_device = min(matches)
    return f"hw:{card},{pcm_device}"


def query_capability(device):
    """Return V4L2 identity for a video-capture node, else ``None``."""
    fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    try:
        capability = V4L2Capability()
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, capability)
    finally:
        os.close(fd)

    effective = capability.device_caps \
        if capability.capabilities & V4L2_CAP_DEVICE_CAPS \
        else capability.capabilities
    if not effective & (V4L2_CAP_VIDEO_CAPTURE
                        | V4L2_CAP_VIDEO_CAPTURE_MPLANE):
        return None
    return {
        "driver": _decode(capability.driver),
        "card": _decode(capability.card),
        "bus_info": _decode(capability.bus_info),
    }


def _device_sort_key(path):
    match = re.search(r"(\d+)$", path.name)
    return (int(match.group(1)) if match else 1 << 30, path.name)


def discover_analog_tuners(
        dev_root=Path("/dev"),
        sys_video=Path("/sys/class/video4linux"),
        sys_sound=Path("/sys/class/sound")):
    """Return the broadcast tuner nodes that can receive analog TV.

    Only nodes exposing a V4L2 *tuner* input qualify.  Generic capture
    hardware -- USB grabbers, webcams, composite/S-video dongles -- has
    camera inputs but no tuner, so it is excluded by construction rather
    than by name matching.
    """
    try:
        device_paths = sorted(dev_root.glob("video*"), key=_device_sort_key)
    except OSError:
        return []

    tuners = []
    for device_path in device_paths:
        if not re.fullmatch(r"video\d+", device_path.name):
            continue
        video_entry = sys_video / device_path.name
        try:
            capability = query_capability(str(device_path))
        except OSError:
            continue
        if capability is None:
            continue
        try:
            inputs = enumerate_tuner_inputs(str(device_path))
        except OSError:
            continue
        if not inputs:
            continue

        hardware_root = _hardware_root(video_entry / "device")
        identity = str(hardware_root) if hardware_root \
            else capability["bus_info"] or device_path.name
        index = _read_text(video_entry / "index")
        digest = hashlib.sha256(
            f"{identity}|{index}".encode()
        ).hexdigest()[:16]
        node_name = _read_text(video_entry / "name")

        for tuner_input in inputs:
            tuner = {
                "id": f"tuner:{digest}",
                "kind": "analog",
                "name": capability["card"] or node_name or device_path.name,
                "device": str(device_path),
                "video_node": device_path.name,
                "node_name": node_name or None,
                "driver": capability["driver"] or None,
                "bus_info": capability["bus_info"] or None,
                "audio_device": _tuner_audio_device(video_entry, sys_sound),
                "available": True,
                "error": None,
            }
            tuner.update(tuner_input)
            tuner["input_args"] = [
                "-standard", "NTSC-M",
                "-channel", str(tuner_input["input_index"]),
            ]
            if (tuner["driver"] == "cx18"
                    and "encoder yuv" in (node_name or "").lower()):
                tuner["input_args"] += [
                    "-input_format", "uyvy422",
                    "-video_size", "720x480",
                ]
            tuners.append(tuner)

    def preference(tuner):
        node = (tuner.get("node_name") or "").lower()
        return (
            0 if "encoder yuv" in node else 1,
            1 if "encoder mpeg" in node else 0,
            0 if tuner.get("driver") == "cx18" else 1,
            tuner["video_node"],
            tuner["input_index"],
        )

    return sorted(tuners, key=preference)


def frequency_units(frequency_hz, capability):
    """Convert Hz to the unit advertised by ``v4l2_tuner.capability``."""
    if capability & V4L2_TUNER_CAP_1HZ:
        return round(frequency_hz)
    if capability & V4L2_TUNER_CAP_LOW:
        return round(frequency_hz / 62.5)
    return round(frequency_hz / 62_500)


def configure_tuner(device, input_index, tuner_index, frequency_hz):
    """Select NTSC, choose the tuner input, and set one cable frequency."""
    fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    try:
        selected_input = ctypes.c_uint32(input_index)
        fcntl.ioctl(fd, VIDIOC_S_INPUT, selected_input)

        standard = ctypes.c_uint64(V4L2_STD_NTSC_M)
        fcntl.ioctl(fd, VIDIOC_S_STD, standard)

        tuner = V4L2Tuner()
        tuner.index = tuner_index
        fcntl.ioctl(fd, VIDIOC_G_TUNER, tuner)
        if tuner.type != V4L2_TUNER_ANALOG_TV:
            raise OSError(errno.ENODEV, "V4L2 input is not an analog TV tuner")

        frequency = V4L2Frequency()
        frequency.tuner = tuner_index
        frequency.type = V4L2_TUNER_ANALOG_TV
        frequency.frequency = frequency_units(
            frequency_hz, tuner.capability
        )
        if not tuner.rangelow <= frequency.frequency <= tuner.rangehigh:
            raise OSError(errno.ERANGE, "Channel is outside tuner range")
        fcntl.ioctl(fd, VIDIOC_S_FREQUENCY, frequency)
        return {
            "name": _decode(tuner.name),
            "signal": max(0, min(100, round(tuner.signal * 100 / 65535))),
        }
    finally:
        os.close(fd)
