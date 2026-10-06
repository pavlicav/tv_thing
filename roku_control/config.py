from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError


_VALID_KEYS = {
    "Home",
    "Rev",
    "Fwd",
    "Play",
    "Select",
    "Left",
    "Right",
    "Down",
    "Up",
    "Back",
    "InstantReplay",
    "Info",
    "Backspace",
    "Search",
    "Enter",
}


def normalize_channel_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


@dataclass(frozen=True)
class RokuConfig:
    url: str | None = None
    discovery_address: str = "0.0.0.0"
    app_name: str = "YouTube TV"
    app_id: str | None = None
    request_timeout_seconds: float = 5.0
    launch_timeout_seconds: float = 40.0
    playback_timeout_seconds: float = 35.0
    poll_interval_seconds: float = 1.0


@dataclass(frozen=True)
class PrimeVideoConfig:
    # Only this visibly focused adult profile may be selected automatically.
    # Empty disables profile selection and leaves it to the operator.
    profile_name: str = "Vivian"


@dataclass(frozen=True)
class MlbConfig:
    app_name: str = "MLB"
    app_id: str | None = "14"
    schedule_url: str = "https://statsapi.mlb.com/api/v1/schedule"
    request_timeout_seconds: float = 10.0
    cache_seconds: float = 60.0
    launch_settle_seconds: float = 12.0
    games_settle_seconds: float = 7.0
    scan_settle_seconds: float = 1.2
    max_cards: int = 24


@dataclass(frozen=True)
class NavigationConfig:
    reset_back_presses: int = 8
    reset_key_delay_seconds: float = 1.0
    post_exit_settle_seconds: float = 4.0
    launch_settle_seconds: float = 10.0
    key_delay_seconds: float = 0.9
    guide_settle_seconds: float = 5.0
    play_settle_seconds: float = 1.5
    guide_scan_lookbehind: int = 2
    guide_scan_max_rows: int = 14
    # A clean YouTube TV launch focuses Home on the supported UI. Right selects
    # Live; Select opens it; two Downs move through ON NOW to the first guide row.
    open_live_guide_keys: tuple[str, ...] = ("Right", "Select", "Down", "Down")


@dataclass(frozen=True)
class ChannelConfig:
    name: str
    aliases: tuple[str, ...] = ()
    guide_index: int = 0
    template: str | None = None
    network_page: str | None = None
    anchor_safe: bool = True


@dataclass(frozen=True)
class FeedbackConfig:
    # "lane": frames come from TV Thing's Roku lane and every observation is
    # newer than the request. "snapshot": frames handed in (tests, offline
    # calibration), with no freshness guarantee.
    backend: str = "snapshot"
    device: str | None = None
    input_format: str = "mpegts"
    source_name: str = "Roku capture"
    video_standard: str | None = None
    input_index: int | None = None
    ffmpeg_path: str = "/usr/bin/ffmpeg"
    capture_timeout_seconds: float = 15.0
    focus_luma_threshold: int = 145
    template_difference_threshold: float = 12.0
    template_margin: float = 1.5
    guide_correlation_threshold: float = 0.86
    guide_correlation_margin: float = 0.005
    network_template_margin: float = 1.0
    verify_attempts: int = 3
    verify_retry_seconds: float = 2.0
    template_dir: Path | None = None
    profile_template: str = "profile-vivian.json"
    ocr_enabled: bool = True
    ocr_required: bool = True
    tesseract_path: str = "auto"
    ocr_scale: int = 3
    ocr_min_confidence: float = 20.0
    ocr_timeout_seconds: float = 15.0
    network_page_settle_seconds: float = 7.0
    network_page_fallback: bool = False

    @property
    def fresh_frames(self) -> bool:
        return self.backend == "lane"


@dataclass(frozen=True)
class AppConfig:
    roku: RokuConfig
    mlb: MlbConfig
    navigation: NavigationConfig
    feedback: FeedbackConfig
    channels: tuple[ChannelConfig, ...]
    # Only its directory is used: operation history lives beside it.
    state_file: Path
    config_path: Path
    _channel_lookup: dict[str, int] = field(repr=False, compare=False)
    prime_video: PrimeVideoConfig = field(default_factory=PrimeVideoConfig)

    def resolve_channel(self, requested: str) -> tuple[int, ChannelConfig]:
        key = normalize_channel_name(requested)
        index = self._channel_lookup.get(key)
        if index is None:
            from .errors import ChannelNotFoundError

            raise ChannelNotFoundError(
                f"Unknown YouTube TV channel: {requested!r}",
                details={"available": [channel.name for channel in self.channels]},
            )
        return index, self.channels[index]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a JSON object")
    return value


def _number(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{key} must be a non-negative number")
    return float(value)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file does not exist: {config_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read configuration {config_path}: {exc}") from exc

    root = _mapping(raw, "configuration")
    roku_raw = _mapping(root.get("roku"), "roku")
    mlb_raw = _mapping(root.get("mlb"), "mlb")
    prime_raw = _mapping(root.get("prime_video"), "prime_video")
    prime_profile = prime_raw.get("profile_name", "Vivian")
    if not isinstance(prime_profile, str) or len(prime_profile) > 100:
        raise ConfigError("prime_video.profile_name must be a string of at most 100 characters")
    nav_raw = _mapping(root.get("navigation"), "navigation")

    roku_url = roku_raw.get("url")
    if roku_url is not None:
        if not isinstance(roku_url, str) or not roku_url.startswith("http://"):
            raise ConfigError("roku.url must be an http:// URL or null")
        roku_url = roku_url.rstrip("/")
    app_id = roku_raw.get("app_id")
    if app_id is not None:
        app_id = str(app_id)
    roku = RokuConfig(
        url=roku_url,
        discovery_address=str(roku_raw.get("discovery_address", "0.0.0.0")),
        app_name=str(roku_raw.get("app_name", "YouTube TV")),
        app_id=app_id,
        request_timeout_seconds=_number(roku_raw, "request_timeout_seconds", 5.0),
        launch_timeout_seconds=_number(roku_raw, "launch_timeout_seconds", 40.0),
        playback_timeout_seconds=_number(roku_raw, "playback_timeout_seconds", 35.0),
        poll_interval_seconds=_number(roku_raw, "poll_interval_seconds", 1.0),
    )

    mlb_app_id = mlb_raw.get("app_id", "14")
    if mlb_app_id is not None:
        mlb_app_id = str(mlb_app_id)
    schedule_url = str(
        mlb_raw.get(
            "schedule_url", "https://statsapi.mlb.com/api/v1/schedule"
        )
    )
    if not schedule_url.startswith(("https://", "http://")):
        raise ConfigError("mlb.schedule_url must be an http:// or https:// URL")
    max_cards = mlb_raw.get("max_cards", 24)
    if (
        isinstance(max_cards, bool)
        or not isinstance(max_cards, int)
        or not 1 <= max_cards <= 60
    ):
        raise ConfigError("mlb.max_cards must be from 1 through 60")
    mlb = MlbConfig(
        app_name=str(mlb_raw.get("app_name", "MLB")),
        app_id=mlb_app_id,
        schedule_url=schedule_url,
        request_timeout_seconds=_number(
            mlb_raw, "request_timeout_seconds", 10.0
        ),
        cache_seconds=_number(mlb_raw, "cache_seconds", 60.0),
        launch_settle_seconds=_number(
            mlb_raw, "launch_settle_seconds", 12.0
        ),
        games_settle_seconds=_number(
            mlb_raw, "games_settle_seconds", 7.0
        ),
        scan_settle_seconds=_number(
            mlb_raw, "scan_settle_seconds", 1.2
        ),
        max_cards=max_cards,
    )

    keys_raw = nav_raw.get(
        "open_live_guide_keys", ["Right", "Select", "Down", "Down"]
    )
    if not isinstance(keys_raw, list) or not keys_raw:
        raise ConfigError("navigation.open_live_guide_keys must be a non-empty array")
    keys: list[str] = []
    for key in keys_raw:
        if not isinstance(key, str) or key not in _VALID_KEYS:
            raise ConfigError(f"Unsupported Roku key in open_live_guide_keys: {key!r}")
        keys.append(key)
    reset_back_presses = nav_raw.get("reset_back_presses", 8)
    if (
        isinstance(reset_back_presses, bool)
        or not isinstance(reset_back_presses, int)
        or not 1 <= reset_back_presses <= 30
    ):
        raise ConfigError("navigation.reset_back_presses must be from 1 through 30")
    guide_scan_lookbehind = nav_raw.get("guide_scan_lookbehind", 2)
    if (
        isinstance(guide_scan_lookbehind, bool)
        or not isinstance(guide_scan_lookbehind, int)
        or not 0 <= guide_scan_lookbehind <= 20
    ):
        raise ConfigError("navigation.guide_scan_lookbehind must be from 0 through 20")
    guide_scan_max_rows = nav_raw.get("guide_scan_max_rows", 14)
    if (
        isinstance(guide_scan_max_rows, bool)
        or not isinstance(guide_scan_max_rows, int)
        or not 1 <= guide_scan_max_rows <= 100
    ):
        raise ConfigError("navigation.guide_scan_max_rows must be from 1 through 100")
    navigation = NavigationConfig(
        reset_back_presses=reset_back_presses,
        reset_key_delay_seconds=_number(nav_raw, "reset_key_delay_seconds", 1.0),
        post_exit_settle_seconds=_number(nav_raw, "post_exit_settle_seconds", 4.0),
        launch_settle_seconds=_number(nav_raw, "launch_settle_seconds", 10.0),
        key_delay_seconds=_number(nav_raw, "key_delay_seconds", 0.9),
        guide_settle_seconds=_number(nav_raw, "guide_settle_seconds", 5.0),
        play_settle_seconds=_number(nav_raw, "play_settle_seconds", 1.5),
        guide_scan_lookbehind=guide_scan_lookbehind,
        guide_scan_max_rows=guide_scan_max_rows,
        open_live_guide_keys=tuple(keys),
    )

    feedback_raw = _mapping(root.get("feedback"), "feedback")
    luma_threshold = feedback_raw.get("focus_luma_threshold", 145)
    if isinstance(luma_threshold, bool) or not isinstance(luma_threshold, int) or not 0 <= luma_threshold <= 255:
        raise ConfigError("feedback.focus_luma_threshold must be from 0 through 255")
    diff_threshold = _number(
        feedback_raw, "template_difference_threshold", 12.0
    )
    template_margin = _number(feedback_raw, "template_margin", 1.5)
    guide_correlation_threshold = _number(
        feedback_raw, "guide_correlation_threshold", 0.86
    )
    guide_correlation_margin = _number(
        feedback_raw, "guide_correlation_margin", 0.005
    )
    if guide_correlation_threshold > 1:
        raise ConfigError("guide_correlation_threshold must be from 0 through 1")
    if guide_correlation_margin > 1:
        raise ConfigError("guide_correlation_margin must be from 0 through 1")
    network_template_margin = _number(
        feedback_raw, "network_template_margin", 1.0
    )
    verify_attempts = feedback_raw.get("verify_attempts", 3)
    if (
        isinstance(verify_attempts, bool)
        or not isinstance(verify_attempts, int)
        or not 1 <= verify_attempts <= 10
    ):
        raise ConfigError("feedback.verify_attempts must be from 1 through 10")
    ocr_scale = feedback_raw.get("ocr_scale", 3)
    if (
        isinstance(ocr_scale, bool)
        or not isinstance(ocr_scale, int)
        or not 1 <= ocr_scale <= 6
    ):
        raise ConfigError("feedback.ocr_scale must be from 1 through 6")
    for boolean_key in ("ocr_enabled", "ocr_required", "network_page_fallback"):
        if boolean_key in feedback_raw and not isinstance(
            feedback_raw[boolean_key], bool
        ):
            raise ConfigError(f"feedback.{boolean_key} must be true or false")

    channels_raw = root.get("channels", [])
    if not isinstance(channels_raw, list) or not channels_raw:
        raise ConfigError("channels must be a non-empty array in exact Live-guide order")
    channels: list[ChannelConfig] = []
    lookup: dict[str, int] = {}
    for index, item in enumerate(channels_raw):
        item = _mapping(item, f"channels[{index}]")
        name = item.get("name")
        aliases = item.get("aliases", [])
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"channels[{index}].name must be a non-empty string")
        if not isinstance(aliases, list) or not all(isinstance(v, str) for v in aliases):
            raise ConfigError(f"channels[{index}].aliases must be an array of strings")
        guide_index = item.get("guide_index", index)
        if isinstance(guide_index, bool) or not isinstance(guide_index, int) or guide_index < 0:
            raise ConfigError(f"channels[{index}].guide_index must be a non-negative integer")
        template = item.get("template")
        if template is not None and (not isinstance(template, str) or not template):
            raise ConfigError(f"channels[{index}].template must be null or a non-empty string")
        network_page = item.get("network_page")
        if network_page is not None and (
            not isinstance(network_page, str) or not network_page.strip()
        ):
            raise ConfigError(
                f"channels[{index}].network_page must be null or a non-empty string"
            )
        anchor_safe = item.get("anchor_safe", True)
        if not isinstance(anchor_safe, bool):
            raise ConfigError(f"channels[{index}].anchor_safe must be true or false")
        channel = ChannelConfig(
            name=name.strip(),
            aliases=tuple(aliases),
            guide_index=guide_index,
            template=template,
            network_page=network_page.strip() if network_page else None,
            anchor_safe=anchor_safe,
        )
        channels.append(channel)
        for value in (channel.name, *channel.aliases):
            key = normalize_channel_name(value)
            if not key:
                raise ConfigError(f"Channel name or alias cannot normalize to empty: {value!r}")
            if key in lookup and lookup[key] != index:
                raise ConfigError(f"Duplicate channel name or alias: {value!r}")
            lookup[key] = index

    state_value = root.get("state_file", "roku_state/state.json")
    if not isinstance(state_value, str) or not state_value:
        raise ConfigError("state_file must be a non-empty path")
    state_file = Path(state_value)
    if not state_file.is_absolute():
        state_file = config_path.parent / state_file

    template_dir_value = feedback_raw.get("template_dir", "roku_state/templates")
    if not isinstance(template_dir_value, str) or not template_dir_value:
        raise ConfigError("feedback.template_dir must be a non-empty path")
    template_dir = Path(template_dir_value)
    if not template_dir.is_absolute():
        template_dir = config_path.parent / template_dir
    backend = feedback_raw.get("backend", "snapshot")
    if backend not in ("lane", "snapshot"):
        raise ConfigError("feedback.backend must be lane or snapshot")
    device = feedback_raw.get("device")
    if backend == "lane" and (
        not isinstance(device, str) or not device.startswith("/dev/")
        or ".." in Path(device).parts
    ):
        raise ConfigError("The Roku lane requires an absolute /dev/ capture device path")
    input_format = feedback_raw.get("input_format", "mpegts")
    if input_format != "mpegts":
        raise ConfigError("feedback.input_format must be mpegts (the HD PVR's transport stream)")
    source_name = feedback_raw.get("source_name", "Roku capture")
    if not isinstance(source_name, str) or not source_name.strip():
        raise ConfigError("feedback.source_name must be a non-empty string")
    video_standard = feedback_raw.get("video_standard")
    if video_standard not in (None, "NTSC-M", "PAL"):
        raise ConfigError("feedback.video_standard must be NTSC-M or PAL")
    input_index = feedback_raw.get("input_index")
    if input_index is not None and (type(input_index) is not int or not 0 <= input_index <= 31):
        raise ConfigError("feedback.input_index must be an integer from 0 through 31")
    feedback = FeedbackConfig(
        backend=backend,
        device=device,
        input_format=input_format,
        source_name=source_name.strip(),
        video_standard=video_standard,
        input_index=input_index,
        ffmpeg_path=str(feedback_raw.get("ffmpeg_path", "/usr/bin/ffmpeg")),
        capture_timeout_seconds=_number(
            feedback_raw, "capture_timeout_seconds", 15.0
        ),
        focus_luma_threshold=luma_threshold,
        template_difference_threshold=diff_threshold,
        template_margin=template_margin,
        guide_correlation_threshold=guide_correlation_threshold,
        guide_correlation_margin=guide_correlation_margin,
        network_template_margin=network_template_margin,
        verify_attempts=verify_attempts,
        verify_retry_seconds=_number(feedback_raw, "verify_retry_seconds", 2.0),
        template_dir=template_dir.resolve(),
        profile_template=str(
            feedback_raw.get("profile_template", "profile-vivian.json")
        ),
        ocr_enabled=feedback_raw.get("ocr_enabled", True),
        ocr_required=feedback_raw.get("ocr_required", True),
        tesseract_path=str(feedback_raw.get("tesseract_path", "auto")),
        ocr_scale=ocr_scale,
        ocr_min_confidence=_number(feedback_raw, "ocr_min_confidence", 20.0),
        ocr_timeout_seconds=_number(feedback_raw, "ocr_timeout_seconds", 15.0),
        network_page_settle_seconds=_number(
            feedback_raw, "network_page_settle_seconds", 7.0
        ),
        network_page_fallback=feedback_raw.get("network_page_fallback", False),
    )

    return AppConfig(
        roku=roku,
        mlb=mlb,
        prime_video=PrimeVideoConfig(profile_name=prime_profile.strip()),
        navigation=navigation,
        feedback=feedback,
        channels=tuple(channels),
        state_file=state_file.resolve(),
        config_path=config_path,
        _channel_lookup=lookup,
    )
