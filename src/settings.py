import os
from dataclasses import dataclass
from pathlib import Path
from re import sub
from typing import Any
from urllib.parse import urlparse

import yaml


SOURCE_ICON_URL = ""
SOURCE_TINT_COLOR = "#1D9BF0"
APP_PORT = 8080


def config_path() -> Path:
    return Path(os.getenv("CONFIG_FILE", "config.yml")).expanduser()


def _load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        raise RuntimeError(f"Missing configuration file: {path}")

    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Configuration file must contain a YAML object: {path}")
    return data


CONFIG = _load_config()


def reload_config() -> dict[str, Any]:
    global CONFIG
    CONFIG = _load_config()
    return CONFIG


def save_config(data: dict[str, Any]) -> None:
    global CONFIG
    path = config_path()
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    CONFIG = data


def _get_config(path: str) -> Any:
    value: Any = CONFIG
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _setting(path: str, default: Any = None) -> Any:
    value = _get_config(path)
    return default if value is None else value


def _required(path: str) -> str:
    value = str(_setting(path, "")).strip()
    if not value:
        raise RuntimeError(f"Missing required setting: {path}")
    return value


def _bool_setting(path: str, default: bool = False) -> bool:
    value = _setting(path, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def normalize_channel(value: Any) -> str:
    channel = str(value or "").strip()
    parsed = urlparse(channel if "://" in channel else f"//{channel}")
    if parsed.netloc.lower() in {"t.me", "www.t.me"}:
        channel = parsed.path.strip("/").split("/", 1)[0]
    return channel.strip().lstrip("@")


@dataclass(frozen=True)
class SourceConfig:
    channel: str
    slug: str
    name: str
    subtitle: str
    description: str
    tint_color: str
    icon: str


@dataclass(frozen=True)
class Settings:
    telegram_api_id: int
    telegram_api_hash: str
    sources: tuple[SourceConfig, ...]
    telegram_limit: int
    telegram_session: str
    base_url: str
    source_icon_url: str
    source_cache_seconds: int
    host: str
    ui_config: bool
    ipa_cache_dir: str
    ipa_cache_workers: int
    ipa_cache_global_workers: int
    ipa_cache_part_size: int


def _slug(value: str) -> str:
    slug = sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-").lower()
    return slug or "source"


def _dedupe_slug(slug: str, used: set[str]) -> str:
    candidate = slug
    index = 2
    while candidate in used:
        candidate = f"{slug}-{index}"
        index += 1
    used.add(candidate)
    return candidate


def _source_value(source_data: dict, key: str) -> str:
    return str(source_data.get(key) or "").strip()


def _load_sources() -> tuple[SourceConfig, ...]:
    channels = _get_config("channels")
    if not isinstance(channels, list) or not channels:
        raise RuntimeError("channels must be a non-empty YAML list")

    default_name = str(_setting("source.name", "TeleStore")).strip()
    default_subtitle = str(_setting("source.subtitle", "Telegram-backed IPA source")).strip()
    default_description = str(
        _setting(
            "source.description",
            "Self-hosted AltStore source that streams IPA files from Telegram.",
        )
    ).strip()
    default_tint_color = str(_setting("source.tint_color", SOURCE_TINT_COLOR)).strip()

    sources: list[SourceConfig] = []
    used_slugs: set[str] = set()
    for index, raw_source in enumerate(channels):
        if isinstance(raw_source, dict):
            source_data = raw_source
            raw_channel = source_data.get("channel", "")
        else:
            source_data = {}
            raw_channel = raw_source

        channel = normalize_channel(raw_channel)
        if not channel:
            continue

        name = _source_value(source_data, "name") or (default_name if index == 0 else channel)
        slug = _dedupe_slug(_slug(_source_value(source_data, "slug") or name), used_slugs)
        subtitle = _source_value(source_data, "subtitle") or default_subtitle
        description = _source_value(source_data, "description") or default_description
        tint_color = _source_value(source_data, "tint_color") or default_tint_color
        icon = _source_value(source_data, "icon")

        sources.append(
            SourceConfig(
                channel=channel,
                slug=slug,
                name=name,
                subtitle=subtitle,
                description=description,
                tint_color=tint_color,
                icon=icon,
            )
        )

    if not sources:
        raise RuntimeError("At least one channel must be configured")
    return tuple(sources)


def load_settings() -> Settings:
    return Settings(
        telegram_api_id=int(_required("telegram.api_id")),
        telegram_api_hash=_required("telegram.api_hash"),
        sources=_load_sources(),
        telegram_limit=int(_setting("telegram.limit", "100")),
        telegram_session=str(_setting("telegram.session", "/data/telegram.session")).strip(),
        base_url=_base_url(str(_setting("server.base_url", "http://localhost:8080"))),
        source_icon_url=str(_setting("source.icon_url", SOURCE_ICON_URL)).strip(),
        source_cache_seconds=int(_setting("source.cache_seconds", "600")),
        host=str(_setting("server.host", "0.0.0.0")).strip(),
        ui_config=_bool_setting("server.ui_config", False),
        ipa_cache_dir=str(_setting("server.ipa_cache_dir", "/data/ipa-cache")).strip(),
        ipa_cache_workers=max(int(_setting("server.ipa_cache_workers", "4")), 1),
        ipa_cache_global_workers=max(int(_setting("server.ipa_cache_global_workers", "8")), 1),
        ipa_cache_part_size=max(int(_setting("server.ipa_cache_part_size", str(8 * 1024 * 1024))), 512 * 1024),
    )
