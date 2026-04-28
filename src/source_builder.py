import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from telethon.tl.types import Message

from src.settings import Settings, SourceConfig
from src.telegram_client import TelegramService


FIELD_PATTERNS = {
    "bundleIdentifier": re.compile(r"bundle\s*id\s*:\s*([^\s]+)", re.I),
    "version": re.compile(r"(?:updated\s*to|version)\s*:\s*([^\s]+)", re.I),
    "minOSVersion": re.compile(r"minimum\s*ios\s*:\s*([^\s]+)", re.I),
    "modifier": re.compile(r"modifier\s*:\s*(.+)", re.I),
}


def _message_text(message: Message) -> str:
    return message.message or ""


def _message_filename(message: Message, fallback: str) -> str:
    name = getattr(getattr(message, "file", None), "name", None)
    return str(name or fallback)


def _clean_name(value: str) -> str:
    name = Path(value).name
    if name.lower().endswith(".ipa"):
        name = name[:-4]
    name = re.sub(r"[_\-.]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Telegram App"


def _parse_field(text: str, field: str) -> str | None:
    match = FIELD_PATTERNS[field].search(text)
    if not match:
        return None
    return match.group(1).strip(" .")


def _app_download_url(settings: Settings, source: SourceConfig, message_id: int, filename: str) -> str:
    return f"{settings.base_url}/ipa/{source.slug}/{message_id}/{quote(filename)}"


def _icon_url(settings: Settings) -> str:
    return settings.source_icon_url or f"{settings.base_url}/source-icon.png"


def _source_icon_url(settings: Settings, source: SourceConfig) -> str:
    if source.icon:
        return f"{settings.base_url}/{source.slug}-icon.png"
    return _icon_url(settings)


def _message_icon_url(settings: Settings, source: SourceConfig, message_id: int) -> str:
    return f"{settings.base_url}/icon/{source.slug}/{message_id}.jpg"


def _message_date(message: Message) -> str:
    msg_date = message.date.astimezone(UTC) if message.date else datetime.now(UTC)
    return msg_date.isoformat().replace("+00:00", "Z")


def _message_size(message: Message) -> int:
    return int(getattr(getattr(message, "file", None), "size", None) or 0)


def _version_sort_key(app: dict) -> tuple[tuple[tuple[int, int | str], ...], str]:
    version = app["versions"][0]
    parsed: list[tuple[int, int | str]] = []
    for part in re.findall(
        r"\d+|[A-Za-z]+|[^A-Za-z\d._\-\s]+",
        str(version.get("version", "")),
    ):
        if not part:
            continue
        parsed.append((1, int(part)) if part.isdigit() else (0, part.lower()))
    return tuple(parsed), str(version.get("date", ""))


def _dedupe_latest(apps: list[dict]) -> list[dict]:
    latest_by_bundle: dict[str, dict] = {}
    order: list[str] = []
    for app in apps:
        bundle_id = str(app["bundleIdentifier"])
        if bundle_id not in latest_by_bundle:
            latest_by_bundle[bundle_id] = app
            order.append(bundle_id)
            continue
        if _version_sort_key(app) >= _version_sort_key(latest_by_bundle[bundle_id]):
            latest_by_bundle[bundle_id] = app
    return [latest_by_bundle[bundle_id] for bundle_id in order]


def _caption_description(text: str) -> str:
    lines = [line.strip(" •\t") for line in text.splitlines() if line.strip(" •\t")]
    return "\n".join(lines)


def _app_from_message(settings: Settings, source: SourceConfig, message_id: int, message: Message) -> dict:
    text = _message_text(message)
    filename = _message_filename(message, f"{message_id}.ipa")
    name = _clean_name(filename)
    bundle_id = _parse_field(text, "bundleIdentifier") or f"telegram.{source.slug}.{message_id}"
    version = _parse_field(text, "version") or "1.0"
    min_os = _parse_field(text, "minOSVersion")
    modifier = _parse_field(text, "modifier")
    description = _caption_description(text) or f"Install from Telegram post {message_id}."

    version_entry = {
        "version": version,
        "buildVersion": version,
        "date": _message_date(message),
        "downloadURL": _app_download_url(settings, source, message_id, filename),
        "localizedDescription": description,
        "size": _message_size(message),
    }
    if min_os:
        version_entry["minOSVersion"] = min_os

    return {
        "name": name,
        "bundleIdentifier": bundle_id,
        "developerName": source.name,
        "subtitle": modifier or "Telegram IPA",
        "localizedDescription": description,
        "iconURL": _message_icon_url(settings, source, message_id),
        "tintColor": source.tint_color,
        "versions": [version_entry],
    }


def _is_ipa_message(message: Message) -> bool:
    filename = _message_filename(message, "")
    if filename.lower().endswith(".ipa"):
        return True
    text = _message_text(message).lower()
    return "bundle id:" in text and "updated to:" in text


async def build_source(settings: Settings, source_config: SourceConfig, telegram: TelegramService) -> dict:
    source: dict = {
        "name": source_config.name,
        "subtitle": source_config.subtitle,
        "description": source_config.description,
        "iconURL": _source_icon_url(settings, source_config),
        "tintColor": source_config.tint_color,
        "apps": [],
    }

    async for message in telegram.iter_recent_messages(source_config, settings.telegram_limit):
        if not message.id:
            continue
        if not _is_ipa_message(message):
            continue
        source["apps"].append(_app_from_message(settings, source_config, int(message.id), message))

    source["apps"] = _dedupe_latest(source["apps"])
    return source
