#!/usr/bin/env python3
"""This host's settings, and what its hardware is (the Config page).

One checkout runs on more than one server. viviserv is the primary node: feed.json, roku.json
and the lineup describe its tuners, its HD PVR and the house Roku. A second node (viviserv2)
runs the same code on its own hardware. What differs between them lives in one small file
outside git, ~/.config/tv-thing/host.json (or TV_THING_HOST_CONFIG), which the Config page
writes. Each setting comes from, first that has it:

1. the service's environment (a unit's Environment=), exactly as before the file existed;
2. the settings file;
3. "auto": what this host's name and hardware say.

With neither, viviserv resolves every setting to what the code did before the file existed:
it is the node feed.json names, so it is the primary, keeps feed.json's name and descriptions,
runs the Roku lane roku.json describes and tunes feed.json's adapter.

A second node never drives the primary's Roku: the house Roku answers from anywhere on the
LAN, and keys pressed from a second node would move what viviserv and VizelVision are airing.
Its Roku lane runs only when roku.json's own capture device is plugged into it, or when asked
for in so many words.

Detection reads /sys, /dev and /proc only. It opens no device, so control may ask it while the
feed holds the tuners. The list of encoders and tools runs ffmpeg and nvidia-smi, and only when
the Config page asks for it.
"""
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

BASE_DIR = Path(__file__).parent
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")    # feed_node's, for a node's name
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}|\[[0-9A-Fa-f:.]{2,45}\]")
# viviserv's recordings disk. Where it is mounted, it is still the default.
VIVISERV_RECORDINGS = Path("/mnt/sda1/media/TV Recordings")


def settings_file(environ=os.environ):
    return Path(environ.get("TV_THING_HOST_CONFIG")
                or Path.home() / ".config" / "tv-thing" / "host.json")


def feed_config_file(environ=os.environ):
    """server.py's FEED_CONFIG_FILE."""
    return Path(environ.get("TV_THING_FEED_CONFIG", str(BASE_DIR / "feed.json")))


def roku_config_file(environ=os.environ):
    """server.py's ROKU_CONFIG_FILE."""
    return Path(environ.get("TV_THING_ROKU_CONFIG", str(BASE_DIR / "roku.json")))


@dataclass(frozen=True)
class Setting:
    id: str
    label: str
    env: str
    choices: tuple       # the values the file may hold; () for free text
    services: tuple      # the services that read it, each when it starts
    help: str
    default: str = "auto"   # the value that is no setting at all


SETTINGS = (
    Setting("role", "Node role", "TV_THING_NODE_ROLE", ("auto", "primary", "second"),
            ("feed", "control", "web-basic"),
            "The primary node is the one feed.json describes: its tuners, its HD PVR and the "
            "house Roku. A second node runs the same channels on its own hardware, describes "
            "that hardware itself, and leaves the primary's Roku alone. Auto: primary when this "
            "host's name is feed.json's node, a second node otherwise."),
    Setting("node", "Node name", "TV_THING_NODE", (), ("feed", "control"),
            "What the API and these pages call this node. Auto: feed.json's on the primary, "
            "this host's name on a second node."),
    Setting("roku", "Roku lane", "TV_THING_ROKU", ("auto", "on", "off"), ("feed", "control"),
            "Navigating the source Roku and capturing it through the HD PVR: YouTube TV, Sunday "
            "Ticket, MLB and Prime Video. Off: those channels do not tune, and nothing here "
            "presses a key on any Roku. Auto: on on the primary; on a second node, only when "
            "roku.json's capture device is plugged into it."),
    Setting("yttv_guide", "YouTube TV guide", "TV_THING_YTTV_GUIDE", ("auto", "off"), ("control",),
            "The daily read of YouTube TV's listings from the web. Auto: whenever the Roku "
            "lane runs."),
    Setting("digital_adapter", "Digital tuner", "TV_THING_DIGITAL_ADAPTER", (), ("feed", "control"),
            "The DVB adapter (/dev/dvb/adapterN) the digital lane, the scan, the DX meter and the "
            "guide read use. Auto: feed.json's, or else the first one this host has."),
    Setting("video_encoder", "Video encoder", "TV_THING_VIDEO_ENCODER",
            ("cpu", "auto", "nvenc", "vaapi", "qsv"), ("feed",),
            "The live packagers' H.264 encoder. cpu is libx264, and the default: the packager's "
            "temperature trip and the gate's GPU checks read NVIDIA GPUs only. auto tries NVENC, "
            "Quick Sync and VA-API in turn, then falls back to libx264.", default="cpu"),
    Setting("recordings_dir", "Recordings folder", "TV_THING_RECORDINGS_DIR", (), ("feed", "control"),
            f"Where the VCR files its recordings. Auto: {VIVISERV_RECORDINGS} where that disk is "
            "mounted, else ~/tv-recordings."),
    Setting("primary_host", "Primary's address", "TV_THING_PRIMARY_HOST", (), ("control", "web-basic"),
            "A second node's way to the primary's own services: Cairo Radio's audio and song "
            "information, and VizelVision's media. Empty: this host's own, which a second node "
            "does not have."),
)
BY_ID = {setting.id: setting for setting in SETTINGS}


def check(setting_id, value, saving=False):
    """The value the settings file keeps for ``setting_id`` (None: back to auto), or ValueError.
    ``saving`` also asks that a folder exist: one read back is kept even while its disk is away,
    so a recording fails loudly rather than landing somewhere else."""
    setting = BY_ID.get(setting_id)
    if setting is None:
        raise ValueError(f"no setting called {setting_id!r}")
    if value is None or value == "" or value == setting.default:
        return None
    if setting.choices:
        if value not in setting.choices:
            raise ValueError(f"{setting.label} must be one of {', '.join(setting.choices)}")
        return value
    if setting_id == "node":
        if not isinstance(value, str) or len(value) > 40 or not _NAME.fullmatch(value):
            raise ValueError("A node's name is letters, digits, '.', '_' and '-', at most 40")
        return value
    if setting_id == "digital_adapter":
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 31:
            raise ValueError("The digital tuner is an adapter number, 0 to 31")
        return value
    if setting_id == "recordings_dir":
        path = Path(str(value))
        if "\0" in str(value) or len(str(value)) > 300 or not path.is_absolute():
            raise ValueError("The recordings folder must be an absolute path")
        if saving and (not path.is_dir() or not os.access(path, os.W_OK | os.X_OK)):
            raise ValueError(f"{path} is not a folder this server can write to")
        return str(path)
    if setting_id == "primary_host":
        if not isinstance(value, str) or not _HOST.fullmatch(value):
            raise ValueError("The primary's address is a host name or an IP address, no scheme or port")
        return value
    raise ValueError(f"{setting.label} cannot be set")


def read_settings(path):
    """(settings, problems) from the settings file; a missing file is no settings."""
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}, []
    except (OSError, ValueError) as error:
        return {}, [f"{path}: {error}"]
    if not isinstance(raw, dict):
        return {}, [f"{path}: not a JSON object"]
    settings, problems = {}, []
    for key, value in raw.items():
        if key.startswith("_"):
            continue                # a comment
        try:
            kept = check(key, value)
        except ValueError as error:
            problems.append(f"{key}: {error}")
            continue
        if kept is not None:
            settings[key] = kept
    return settings, problems


def write_settings(path, settings):
    """Replace the settings file in one step: a reader sees the old file or the new one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"_comment": "This host's TV Thing settings (host_config.py), written by its "
                            "Config page. A service reads them when it starts.",
                **{key: settings[key] for key in BY_ID if key in settings}}
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


@dataclass(frozen=True)
class Resolved:
    value: object
    source: str          # "environment", "file" or "auto"
    reason: str


class Host:
    """One process's view of this host: its environment and settings file, read once, and
    what each setting resolves to. Every path and root can be pointed elsewhere for a test."""

    def __init__(self, environ=None, path=None, hostname=None, root=Path("/"), saved=None):
        self.environ = dict(os.environ if environ is None else environ)
        self.path = Path(path) if path is not None else settings_file(self.environ)
        # ``saved``: settings already read (what a service started with), not the file's now.
        self.saved, self.problems = (dict(saved), []) if saved is not None else read_settings(self.path)
        self.hostname = hostname or socket.gethostname().split(".")[0]
        self.root = Path(root)
        self._cache = {}

    def _under(self, path):
        return self.root / str(path).lstrip("/")

    # -- what the files say ---------------------------------------------------------
    def _feed_json(self):
        if "feed_json" not in self._cache:
            try:
                self._cache["feed_json"] = json.loads(feed_config_file(self.environ).read_text())
            except (OSError, ValueError):
                self._cache["feed_json"] = {}
        return self._cache["feed_json"]

    def primary_node(self):
        """The node feed.json describes (viviserv)."""
        node = self._feed_json().get("node")
        return node if isinstance(node, str) else None

    def _feed_adapter(self):
        data = self._feed_json()
        digital = next((lane.get("device") for lane in data.get("lanes") or []
                        if isinstance(lane, dict) and lane.get("id") == "digital"), "digital")
        device = next((device for device in data.get("devices") or []
                       if isinstance(device, dict) and device.get("id") == digital), {})
        adapter = device.get("adapter")
        return adapter if isinstance(adapter, int) else 0

    @staticmethod
    def roku_capture_device(config_file):
        """The capture device the Roku config names (its feedback.device), or None."""
        try:
            device = json.loads(Path(config_file).read_text())["feedback"]["device"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return device if isinstance(device, str) and device else None

    # -- each setting ---------------------------------------------------------------
    def _given(self, setting_id):
        """(value, source) from the environment or the file, or (None, None) for auto."""
        setting = BY_ID[setting_id]
        if setting.env in self.environ:
            return self.environ[setting.env], "environment"
        if setting_id in self.saved:
            return self.saved[setting_id], "file"
        return None, None

    def get(self, setting_id):
        if setting_id not in self._cache:
            self._cache[setting_id] = getattr(self, f"_resolve_{setting_id}")()
        return self._cache[setting_id]

    def value(self, setting_id):
        return self.get(setting_id).value

    def _resolve_role(self):
        value, source = self._given("role")
        if source == "environment":
            value = value.strip().lower()
            value = {"secondary": "second"}.get(value, value)
        if value in ("primary", "second"):
            return Resolved(value, source, "set")
        primary = self.primary_node()
        if primary is None or self.hostname.lower() == primary.lower():
            return Resolved("primary", "auto", f"this host is {self.hostname}, the node feed.json describes")
        return Resolved("second", "auto", f"this host is {self.hostname}; feed.json describes {primary}")

    @property
    def primary(self):
        return self.value("role") == "primary"

    def _resolve_node(self):
        value, source = self._given("node")
        if value is not None and _NAME.fullmatch(str(value)):
            return Resolved(str(value), source, "set")
        if self.primary:
            return Resolved(self.primary_node() or self.hostname, "auto", "feed.json's node")
        name = re.sub(r"[^A-Za-z0-9_.-]", "-", self.hostname).lstrip("_.-") or "node"
        return Resolved(name, "auto", "this host's name")

    def roku(self, config_file=None):
        """Whether the Roku lane runs from ``config_file`` (server.py's ROKU_CONFIG_FILE)."""
        config_file = Path(config_file) if config_file is not None else roku_config_file(self.environ)
        key = ("roku", str(config_file))
        if key not in self._cache:
            self._cache[key] = self._resolve_roku_for(config_file)
        return self._cache[key]

    def _resolve_roku(self):
        return self.roku()

    def _resolve_roku_for(self, config_file):
        value, source = self._given("roku")
        if source == "environment":
            value = {"1": "on", "0": "off", "true": "on", "false": "off"}.get(value.strip().lower(),
                                                                          value.strip().lower())
        if not config_file.is_file():
            return Resolved("off", source or "auto", f"there is no {config_file.name}")
        if value == "off":
            return Resolved("off", source, "set")
        if value == "on":
            return Resolved("on", source, "set")
        if self.primary:
            return Resolved("on", "auto", f"the primary node runs the Roku lane {config_file.name} describes")
        device = self.roku_capture_device(config_file)
        if device and self._under(device).exists():
            return Resolved("on", "auto", f"{config_file.name}'s capture device {device} is on this host")
        return Resolved("off", "auto", f"a second node, without {config_file.name}'s capture device "
                                       f"({device or 'none named'}): its Roku is the primary's")

    def _resolve_yttv_guide(self):
        value, source = self._given("yttv_guide")
        if source == "environment":
            # As before the settings file: "0" turns it off, anything else leaves it on.
            value = "off" if value.strip() == "0" else "auto"
        if value == "off":
            return Resolved("off", source, "set")
        roku = self.roku().value
        return Resolved(roku, source or "auto", f"follows the Roku lane, {roku}")

    def _resolve_digital_adapter(self):
        value, source = self._given("digital_adapter")
        if value is not None:
            try:
                return Resolved(int(value), source, "set")
            except (TypeError, ValueError):
                pass
        configured = self._feed_adapter()
        present = [entry["adapter"] for entry in dvb_adapters(self.root)]
        if configured in present or not present:
            return Resolved(configured, "auto", "feed.json's" + ("" if present else
                                                                 "; this host has no DVB adapter"))
        return Resolved(present[0], "auto", f"this host's first; feed.json's adapter{configured} is absent")

    def _resolve_video_encoder(self):
        value, source = self._given("video_encoder")
        if value is not None:
            # The environment's value is taken as it is, as before (server.select_video_encoder).
            return Resolved(str(value).lower(), source, "set")
        return Resolved("cpu", "auto", "libx264: the GPU checks read NVIDIA GPUs only")

    def _resolve_recordings_dir(self):
        value, source = self._given("recordings_dir")
        if value is not None:
            return Resolved(Path(value), source, "set")
        if self._under(VIVISERV_RECORDINGS.parent).is_dir():
            return Resolved(VIVISERV_RECORDINGS, "auto", "viviserv's recordings disk is mounted")
        return Resolved(Path.home() / "tv-recordings", "auto",
                        f"no {VIVISERV_RECORDINGS.parent} on this host")

    def _resolve_primary_host(self):
        value, source = self._given("primary_host")
        if value:
            return Resolved(str(value), source, "set")
        return Resolved("", source or "auto", "this host's own services")

    # -- what follows from them -----------------------------------------------------
    def primary_url(self, port, path=""):
        """A primary's service as a second node reaches it, or None to keep this host's own."""
        host = self.value("primary_host")
        if self.primary or not host:
            return None
        return f"http://{host}{'' if port == 80 else f':{port}'}{path}"

    def feed_config(self, base):
        """feed.json (``base``, a feed_node.FeedConfig) as this host has it: its node's name,
        the digital lane's adapter, and on a second node the hardware it really has."""
        node = self.value("node")
        adapter = self.value("digital_adapter")
        digital = next((lane.device for lane in base.lanes if lane.id == "digital"), None)
        devices = []
        for device in base.devices:
            changes = {}
            if device.id == digital and (0 if device.adapter is None else device.adapter) != adapter:
                changes["adapter"] = adapter
            if not self.primary:
                changes["detail"] = self.describe_device(device.kind, adapter if device.id == digital
                                                         else device.adapter, node)
            devices.append(replace(device, **changes) if changes else device)
        lanes = base.lanes
        if not self.primary:
            lanes = tuple(replace(lane, description=self.describe_lane(lane, node, adapter))
                          for lane in base.lanes)
        if node == base.node and tuple(devices) == base.devices and lanes == base.lanes:
            return base
        return replace(base, node=node, devices=tuple(devices), lanes=lanes)

    def describe_device(self, kind, adapter, node):
        if kind == "dvb":
            adapter = 0 if adapter is None else adapter
            found = next((entry for entry in dvb_adapters(self.root) if entry["adapter"] == adapter), None)
            if found is None:
                return f"Not fitted on {node}: no /dev/dvb/adapter{adapter}"
            return f"{found['description']} via /dev/dvb/adapter{adapter}"
        if kind == "analog":
            found = next((entry for entry in video_devices(self.root) if entry["kind"] == "analog"), None)
            if found is None:
                return f"Not fitted on {node}: no analog TV tuner"
            return f"{found['name']} ({found['driver']}) via {found['device']}"
        if kind == "capture":
            capture = self.capture_device()
            if capture is None:
                return f"Not fitted on {node}: no HD PVR"
            off = "" if self.roku().value == "on" else "; the Roku lane is off here"
            return f"{capture['name']} via {capture['path']}{off}"
        return ""

    def describe_lane(self, lane, node, adapter):
        if lane.backend == "linux-dvb":
            found = next((entry for entry in dvb_adapters(self.root) if entry["adapter"] == adapter), None)
            what = ((" + ".join(found["chips"]) or f"{found['driver'] or 'DVB'} on adapter{adapter}")
                    if found else f"not fitted on {node}")
            return f"ATSC digital television ({what})"
        if lane.backend == "analog-v4l2":
            found = next((entry for entry in video_devices(self.root) if entry["kind"] == "analog"), None)
            return f"Analog NTSC cable television ({found['name'] if found else f'not fitted on {node}'})"
        if lane.backend == "hdpvr-copy" and (self.capture_device() is None or self.roku().value != "on"):
            return f"Source Roku via an HD PVR (not on {node})"
        return lane.description

    def capture_device(self):
        """The Roku's capture here: roku.json's own device, or else any HD PVR, or None."""
        named = self.roku_capture_device(roku_config_file(self.environ))
        devices = video_devices(self.root)
        if named and self._under(named).exists():
            target = os.path.realpath(self._under(named))
            found = next((entry for entry in devices
                          if os.path.realpath(self._under(entry["device"])) == target), None)
            return {"path": named, "name": found["name"] if found else "Capture device", "named": True}
        found = next((entry for entry in devices if entry["kind"] == "capture"), None)
        if found is None:
            return None
        return {"path": found["device"], "name": found["name"], "named": False}

    def with_environment(self, environ, saved=None):
        """This host as a service with ``environ`` sees it (its unit's Environment=)."""
        return Host(environ=environ, path=self.path, hostname=self.hostname, root=self.root,
                    saved=self.saved if saved is None else saved)


# -- detection: /sys and /dev, never a device opened --------------------------------------
VENDORS = {"0070": "Hauppauge", "2040": "Hauppauge", "14f1": "Conexant", "10de": "NVIDIA",
           "8086": "Intel", "1002": "AMD", "1131": "Philips", "185b": "Compro", "0ccd": "TerraTec"}
# DVB demodulators and tuners as sysfs names their I2C clients; other chips on a card (its audio
# decoder, say) are left out of what it is called.
CHIPS = {"lgdt3306a": "LGDT3306A", "lgdt3305": "LGDT3305", "lgdt330x": "LGDT330x",
         "s5h1409": "S5H1409", "s5h1411": "S5H1411", "au8522": "AU8522", "si2168": "Si2168",
         "si2165": "Si2165", "si2183": "Si2183", "mxl692": "MxL692", "tda10048": "TDA10048",
         "si2157": "Si2157", "si2141": "Si2141", "tda18271": "TDA18271", "xc5000": "XC5000",
         "mxl5007t": "MxL5007T", "mt2131": "MT2131"}
# Drivers of cards with an analog TV tuner (the analog lane's kind of device).
ANALOG_TV_DRIVERS = {"cx18", "ivtv", "cx23885", "cx88", "cx8800", "saa7134", "bttv", "cx231xx",
                     "pvrusb2", "tm6000", "saa7164"}


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _driver(device_dir):
    try:
        return os.path.basename(os.readlink(Path(device_dir) / "driver"))
    except OSError:
        return ""


def _number(name, prefix):
    rest = name[len(prefix):]
    return int(rest) if name.startswith(prefix) and rest.isdigit() else None


def hardware(device_dir):
    """What sysfs says a device is: bus, ids, maker, product, driver and its I2C chips."""
    device_dir = Path(device_dir)
    try:
        device_dir = device_dir.resolve()
    except OSError:
        return {}
    info = {"driver": _driver(device_dir), "bus": None, "id": None, "maker": "", "product": "",
            "chips": []}
    usb = device_dir if (device_dir / "idVendor").exists() else device_dir.parent
    if (usb / "idVendor").exists():
        vendor = _read(usb / "idVendor")
        info.update(bus="usb", id=f"{vendor}:{_read(usb / 'idProduct')}",
                    maker=VENDORS.get(vendor, _read(usb / "manufacturer")), product=_read(usb / "product"))
    elif (device_dir / "vendor").exists():
        vendor = _read(device_dir / "vendor").removeprefix("0x")
        subvendor = _read(device_dir / "subsystem_vendor").removeprefix("0x")
        info.update(bus="pci", id=f"{vendor}:{_read(device_dir / 'device').removeprefix('0x')}",
                    maker=VENDORS.get(subvendor) or VENDORS.get(vendor, ""))
    chips = []
    for name_file in sorted(device_dir.glob("i2c-*/*-*/name")) + sorted(device_dir.glob("*/i2c-*/*-*/name")):
        chip = CHIPS.get(_read(name_file))
        if chip and chip not in chips:
            chips.append(chip)
    info["chips"] = chips
    return info


def _describe(info, fallback):
    maker, product = info.get("maker") or "", info.get("product") or ""
    if not product:
        words = f"{maker} card" if maker else fallback
    else:
        words = product if maker and product.startswith(maker) else f"{maker} {product}".strip()
    extra = ", ".join(part for part in (info.get("driver"),
                                        f"{info['bus'].upper()} {info['id']}" if info.get("bus") else "")
                      if part)
    chips = " + ".join(info.get("chips") or [])
    return f"{words}{f' ({extra})' if extra else ''}{f': {chips}' if chips else ''}"


def dvb_adapters(root=Path("/")):
    """Each /dev/dvb/adapterN with a front-end, and what it is."""
    root = Path(root)
    out = []
    for path in sorted((root / "dev/dvb").glob("adapter*"), key=lambda p: _number(p.name, "adapter") or 0):
        adapter = _number(path.name, "adapter")
        frontends = sorted(n for n in (_number(f.name, "frontend") for f in path.glob("frontend*"))
                           if n is not None)
        if adapter is None or not frontends:
            continue
        info = hardware(root / f"sys/class/dvb/dvb{adapter}.frontend{frontends[0]}/device")
        out.append({"adapter": adapter, "frontends": frontends, "driver": info.get("driver", ""),
                    "bus": info.get("bus"), "id": info.get("id"), "chips": info.get("chips", []),
                    "description": _describe(info, "DVB adapter")})
    return out


def video_devices(root=Path("/")):
    """Each V4L2 capture node: its name and driver, and whether it is an analog TV tuner's
    encoder ("analog"), an HD PVR ("capture") or something else ("other")."""
    root = Path(root)
    by_id = {}
    for link in sorted((root / "dev/v4l/by-id").glob("*")):
        by_id.setdefault(os.path.basename(os.path.realpath(link)), f"/dev/v4l/by-id/{link.name}")
    out = []
    entries = sorted((root / "sys/class/video4linux").glob("video*"),
                     key=lambda p: _number(p.name, "video") if _number(p.name, "video") is not None else 1 << 20)
    for entry in entries:
        name, info = _read(entry / "name"), hardware(entry / "device")
        driver = info.get("driver", "")
        if driver == "hdpvr":
            kind = "capture"
        elif driver in ANALOG_TV_DRIVERS and ("MPEG" in name or driver not in ("cx18", "ivtv")):
            kind = "analog"
        else:
            kind = "other"
        out.append({"device": f"/dev/{entry.name}", "by_id": by_id.get(entry.name), "name": name,
                    "driver": driver, "bus": info.get("bus"), "id": info.get("id"), "kind": kind})
    # One card shows several nodes; the analog lane wants its first.
    seen, unique = set(), []
    for entry in out:
        key = (entry["kind"], entry["driver"], entry["id"]) if entry["kind"] == "analog" else entry["device"]
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def gpus(root=Path("/")):
    root = Path(root)
    out = []
    for card in sorted((root / "sys/class/drm").glob("renderD*")):
        info = hardware(card / "device")
        out.append({"render_node": f"/dev/dri/{card.name}", "driver": info.get("driver", ""),
                    "id": info.get("id"), "maker": info.get("maker", "")})
    return out


TOOLS = ("ffmpeg", "ffprobe", "v4l2-ctl", "dvbv5-scan", "w_scan", "nvidia-smi", "vainfo",
         "tesseract", "tectonic", "node")
ENCODERS = ("libx264", "h264_nvenc", "h264_qsv", "h264_vaapi")


def _run(command, timeout):
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def encoders(run=_run):
    """The H.264 encoders this host's ffmpeg was built with (whether each works is the feed's
    probe's business, at its first tune)."""
    listing = run(["ffmpeg", "-hide_banner", "-encoders"], 5)
    if listing is None:
        return None
    names = {line.split()[1] for line in listing.splitlines() if len(line.split()) > 1}
    return [name for name in ENCODERS if name in names]


def system(root=Path("/")):
    root = Path(root)
    cpu = next((line.split(":", 1)[1].strip() for line in _read(root / "proc/cpuinfo").splitlines()
                if line.startswith("model name")), "")
    memory = next((int(line.split()[1]) * 1024 for line in _read(root / "proc/meminfo").splitlines()
                   if line.startswith("MemTotal:")), None)
    os_name = next((line.split("=", 1)[1].strip('"') for line in _read(root / "etc/os-release").splitlines()
                    if line.startswith("PRETTY_NAME=")), "")
    return {"cpu": cpu, "threads": os.cpu_count(), "memory_bytes": memory, "os": os_name,
            "kernel": os.uname().release, "python": sys.version.split()[0],
            "python_path": sys.executable}


def detect(host, run=_run):
    """Everything the Config page shows about this host's hardware and tools."""
    capture = host.capture_device()
    gpu_list = gpus(host.root)
    if any(gpu["driver"] == "nvidia" for gpu in gpu_list):
        names = (run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], 3) or "").splitlines()
        for gpu, name in zip((gpu for gpu in gpu_list if gpu["driver"] == "nvidia"), names):
            gpu["name"] = name.strip()
    return {
        "hostname": host.hostname,
        "system": system(host.root),
        "dvb": dvb_adapters(host.root),
        "video": video_devices(host.root),
        "roku_capture": capture,
        "gpus": gpu_list,
        "encoders": encoders(run),
        "tools": {tool: shutil.which(tool) for tool in TOOLS},
    }


def tool(name, usual):
    """A tool's usual path where it is (Debian's /usr/bin), else wherever PATH finds it: NixOS
    has no /usr/bin."""
    return usual if Path(usual).exists() else (shutil.which(name) or usual)


def load(environ=None, path=None):
    return Host(environ=environ, path=path)
