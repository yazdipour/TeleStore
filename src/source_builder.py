import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import yaml
from telethon.tl.types import Message

from src.settings import Settings
from src.telegram_client import TelegramService


FIELD_PATTERNS = {
    "bundleIdentifier": re.compile(r"bundle\s*id\s*:\s*([^\s]+)", re.I),
    "version": re.compile(r"(?:updated\s*to|version)\s*:\s*([^\s]+)", re.I),
    "minOSVersion": re.compile(r"minimum\s*ios\s*:\s*([^\s]+)", re.I),
    "modifier": re.compile(r"modifier\s*:\s*(.+)", re.I),
}


def _load_manual_apps(path: str) -> list[dict]:
    if not path:
        return []
    config_path = Path(path)
    if not config_path.exists():
        return []
    data = yaml.safe_load(config_path.read_text()) or {}
    apps = data.get("apps", [])
    if not isinstance(apps, list):
        raise ValueError("manual app config must contain an apps list")
    return [dict(app) for app in apps]


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


def _app_download_url(settings: Settings, message_id: int, filename: str) -> str:
    return f"{settings.base_url}/ipa/{message_id}/{quote(filename)}"


def _icon_url(settings: Settings) -> str:
    return settings.source_icon_url or f"{settings.base_url}/source-icon.png"


def _message_icon_url(settings: Settings, message_id: int) -> str:
    return f"{settings.base_url}/icon/{message_id}.jpg"


def _message_date(message: Message) -> str:
    msg_date = message.date.astimezone(UTC) if message.date else datetime.now(UTC)
    return msg_date.isoformat().replace("+00:00", "Z")


def _message_size(message: Message) -> int:
    return int(getattr(getattr(message, "file", None), "size", None) or 0)


def _version_sort_key(app: dict) -> tuple[tuple[int | str, ...], str]:
    version = app["versions"][0]
    parsed: list[int | str] = []
    for part in re.split(r"[._\-\s]+", str(version.get("version", ""))):
        if not part:
            continue
        parsed.append(int(part) if part.isdigit() else part.lower())
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


def _app_from_message(settings: Settings, message_id: int, message: Message) -> dict:
    text = _message_text(message)
    filename = _message_filename(message, f"{message_id}.ipa")
    name = _clean_name(filename)
    bundle_id = _parse_field(text, "bundleIdentifier") or f"telegram.blatants.{message_id}"
    version = _parse_field(text, "version") or "1.0"
    min_os = _parse_field(text, "minOSVersion")
    modifier = _parse_field(text, "modifier")
    description = _caption_description(text) or f"Install from Telegram post {message_id}."

    version_entry = {
        "version": version,
        "buildVersion": version,
        "date": _message_date(message),
        "downloadURL": _app_download_url(settings, message_id, filename),
        "localizedDescription": description,
        "size": _message_size(message),
    }
    if min_os:
        version_entry["minOSVersion"] = min_os

    return {
        "name": name,
        "bundleIdentifier": bundle_id,
        "developerName": "Blatants",
        "subtitle": modifier or "Telegram IPA",
        "localizedDescription": description,
        "iconURL": _message_icon_url(settings, message_id),
        "tintColor": settings.source_tint_color,
        "versions": [version_entry],
    }


def _is_ipa_message(message: Message) -> bool:
    filename = _message_filename(message, "")
    if filename.lower().endswith(".ipa"):
        return True
    text = _message_text(message).lower()
    return "bundle id:" in text and "updated to:" in text


async def _manual_app(settings: Settings, telegram: TelegramService, raw_app: dict) -> dict:
    app = dict(raw_app)
    message_id = int(app["message_id"])
    message = await telegram.get_message(message_id)
    filename = app.get("filename") or _message_filename(message, f"{message_id}.ipa")
    text = _message_text(message)

    name = app.get("name") or _clean_name(filename)
    bundle_id = app.get("bundleIdentifier") or _parse_field(text, "bundleIdentifier")
    version = app.get("version") or _parse_field(text, "version") or "1.0"
    min_os = app.get("minOSVersion") or _parse_field(text, "minOSVersion")
    description = app.get("localizedDescription") or _caption_description(text)
    if not bundle_id:
        bundle_id = f"telegram.blatants.{message_id}"

    version_entry = {
        "version": str(version),
        "buildVersion": str(app.get("buildVersion") or version),
        "date": str(app.get("date") or _message_date(message)),
        "downloadURL": _app_download_url(settings, message_id, filename),
        "localizedDescription": description,
        "size": int(app.get("size") or _message_size(message)),
    }
    if min_os:
        version_entry["minOSVersion"] = str(min_os)

    source_app = {
        "name": name,
        "bundleIdentifier": bundle_id,
        "developerName": app.get("developerName", "Blatants"),
        "subtitle": app.get("subtitle") or app.get("modifier") or "Telegram IPA",
        "localizedDescription": description,
        "iconURL": app.get("iconURL") or _message_icon_url(settings, message_id),
        "tintColor": app.get("tintColor", settings.source_tint_color),
        "versions": [version_entry],
    }
    return source_app


async def build_source(settings: Settings, telegram: TelegramService) -> dict:
    source: dict = {
        "name": settings.source_name,
        "subtitle": settings.source_subtitle,
        "description": settings.source_description,
        "iconURL": _icon_url(settings),
        "tintColor": settings.source_tint_color,
        "apps": [],
    }

    manual_apps = _load_manual_apps(settings.apps_config)
    manual_ids = {int(app["message_id"]) for app in manual_apps if app.get("message_id")}

    for app in manual_apps:
        if app.get("message_id"):
            source["apps"].append(await _manual_app(settings, telegram, app))

    async for message in telegram.iter_recent_messages(settings.telegram_limit):
        if not message.id or message.id in manual_ids:
            continue
        if not _is_ipa_message(message):
            continue
        source["apps"].append(_app_from_message(settings, int(message.id), message))

    source["apps"] = _dedupe_latest(source["apps"])
    return source
