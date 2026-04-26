import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv


load_dotenv()

SOURCE_ICON_URL = "https://raw.githubusercontent.com/yazdipour/LiveBlatant/refs/heads/master/ShaFace.png"
SOURCE_TINT_COLOR = "#1D9BF0"


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _base_url_with_port(base_url: str, port: int) -> str:
    url = base_url.strip().rstrip("/")
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return f"{url}:{port}"

    hostname = parts.hostname
    host = f"[{hostname}]" if ":" in hostname else hostname
    return urlunsplit((parts.scheme, f"{host}:{port}", parts.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class Settings:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_channel: str
    telegram_limit: int
    telegram_session: str
    base_url: str
    source_name: str
    source_subtitle: str
    source_description: str
    source_icon_url: str
    source_tint_color: str
    source_cache_seconds: int
    apps_config: str
    host: str
    port: int


def load_settings() -> Settings:
    port = int(os.getenv("PORT", "8080"))
    return Settings(
        telegram_api_id=int(_required("TELEGRAM_API_ID")),
        telegram_api_hash=_required("TELEGRAM_API_HASH"),
        telegram_channel=os.getenv("TELEGRAM_CHANNEL", "blatants").strip().lstrip("@"),
        telegram_limit=int(os.getenv("TELEGRAM_LIMIT", "100")),
        telegram_session=os.getenv("TELEGRAM_SESSION", "/data/telegram.session").strip(),
        base_url=_base_url_with_port(os.getenv("BASE_URL", "http://localhost"), port),
        source_name=os.getenv("SOURCE_NAME", "LiveBlatant").strip(),
        source_subtitle=os.getenv("SOURCE_SUBTITLE", "Telegram-backed IPA source").strip(),
        source_description=os.getenv(
            "SOURCE_DESCRIPTION",
            "Self-hosted AltStore source that streams IPA files from Telegram.",
        ).strip(),
        source_icon_url=SOURCE_ICON_URL,
        source_tint_color=SOURCE_TINT_COLOR,
        source_cache_seconds=int(os.getenv("SOURCE_CACHE_SECONDS", "600")),
        apps_config=os.getenv("APPS_CONFIG", "").strip(),
        host=os.getenv("HOST", "0.0.0.0").strip(),
        port=port,
    )
