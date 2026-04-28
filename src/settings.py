import os
from dataclasses import dataclass
from pathlib import Path
from re import sub
from typing import Any

import yaml


SOURCE_ICON_URL = ""
SOURCE_TINT_COLOR = "#1D9BF0"
APP_PORT = 8080


def _load_config() -> dict[str, Any]:
    config_path = Path(os.getenv("CONFIG_FILE", "config.yml")).expanduser()
    if not config_path.exists():
        raise RuntimeError(f"Missing configuration file: {config_path}")

    data = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Configuration file must contain a YAML object: {config_path}")
    return data


CONFIG = _load_config()


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


def _base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


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

        channel = str(raw_channel).strip().lstrip("@")
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
    )
