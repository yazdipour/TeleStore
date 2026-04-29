from contextlib import asynccontextmanager
from copy import deepcopy
from email.utils import formatdate
from html import escape
import json
import logging
from pathlib import Path
from re import sub
from time import monotonic
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import unquote

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from src.assets import DEFAULT_ICON_PNG
from src import settings as settings_module
from src.settings import APP_PORT, load_settings
from src.source_builder import build_source
from src.telegram_client import TelegramService


settings = load_settings()
telegram = TelegramService(settings)
source_caches: dict[str, dict[str, object]] = {
    source.slug: {"expires_at": 0.0, "value": None} for source in settings.sources
}
sources_by_slug = {source.slug: source for source in settings.sources}
default_source = settings.sources[0]
SOURCE_ICON_PATHS = (
    Path("/app/imgs/ICON-120-blue.png"),
    Path("imgs/ICON-120-blue.png"),
)
DEFAULT_SOURCE_TINT_COLOR = "#1D9BF0"
DEFAULT_SOURCE_ICON = "imgs/ICON-120-blue.png"
logger = logging.getLogger("uvicorn.error")


def _html_page(body: str, status_code: int = 200) -> Response:
    return Response(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TeleStore</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 520px; margin: 48px auto; padding: 0 20px; line-height: 1.5; }}
    input, button {{ font: inherit; width: 100%; box-sizing: border-box; padding: 10px 12px; margin: 6px 0 14px; }}
    button {{ cursor: pointer; }}
    code {{ background: #eee; padding: 2px 4px; overflow-wrap: anywhere; }}
    form {{ margin: 0; }}
    label {{ display: block; font-weight: 600; margin-top: 8px; }}
    ul {{ padding-left: 0; list-style: none; }}
    li {{ margin: 0 0 18px; }}
    .source-name {{ font-weight: 650; }}
    .source-url {{ display: block; margin: 6px 0 8px; }}
    .source-actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .copy-button, .remove-button {{ width: auto; min-width: 92px; margin: 0; padding: 8px 10px; }}
    .remove-button {{ color: #b00020; }}
    .error {{ color: #b00020; }}
    .config-card {{ border: 1px solid #ddd; padding: 14px; margin: 14px 0; }}
    .muted {{ color: #666; }}
  </style>
  <script>
    async function copySourceUrl(button, url) {{
      try {{
        await navigator.clipboard.writeText(url);
        button.textContent = "Copied";
      }} catch (error) {{
        const input = document.createElement("input");
        input.value = url;
        document.body.appendChild(input);
        input.select();
        document.execCommand("copy");
        input.remove();
        button.textContent = "Copied";
      }}
      setTimeout(() => button.textContent = "Copy", 1400);
    }}
  </script>
</head>
<body>{body}</body>
</html>""",
        status_code=status_code,
        media_type="text/html",
    )


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode()
    return {key: values[-1] for key, values in parse_qs(body).items()}


def _runtime_refresh() -> None:
    global settings, source_caches, sources_by_slug, default_source
    settings_module.reload_config()
    settings = load_settings()
    telegram.settings = settings
    telegram._channel_entities.clear()
    source_caches = {source.slug: {"expires_at": 0.0, "value": None} for source in settings.sources}
    sources_by_slug = {source.slug: source for source in settings.sources}
    default_source = settings.sources[0]


def _config_ui_enabled() -> None:
    if not settings.ui_config:
        raise HTTPException(status_code=404, detail="Not found")


def _source_config_rows() -> list[tuple[int, object]]:
    channels = settings_module.CONFIG.get("channels")
    if not isinstance(channels, list):
        return []

    rows = []
    source_index = 0
    for config_index, raw_source in enumerate(channels):
        raw_channel = raw_source.get("channel", "") if isinstance(raw_source, dict) else raw_source
        if not str(raw_channel).strip().lstrip("@"):
            continue
        if source_index >= len(settings.sources):
            break
        rows.append((config_index, settings.sources[source_index]))
        source_index += 1
    return rows


def _config_page_body(error: str = "") -> str:
    status = ""
    if error:
        status = f'<p class="error">{escape(error)}</p>'

    rows = []
    for config_index, source in _source_config_rows():
        url = _source_url(source)
        rows.append(
            '<li class="config-card">'
            f'<div class="source-name">{escape(source.name)} / @{escape(source.channel)}</div>'
            f'<a class="source-url" href="{escape(url)}"><code>{escape(url)}</code></a>'
            '<div class="source-actions">'
            f'<button class="copy-button" type="button" onclick="copySourceUrl(this, {escape(json.dumps(url))})">'
            "Copy URL</button>"
            '<form method="post" action="/config/channels/remove">'
            f'<input type="hidden" name="index" value="{config_index}">'
            '<button class="remove-button" type="submit">Remove</button>'
            "</form>"
            "</div>"
            "</li>"
        )

    return (
        "<h1>Channel Config</h1>"
        '<p><a href="/">Sources</a></p>'
        f"{status}"
        f"<ul>{''.join(rows)}</ul>"
        '<h2>Add Channel</h2>'
        '<form method="post" action="/config/channels">'
        "<label>Telegram channel name, without @</label>"
        '<input name="channel" placeholder="blatants" required>'
        "<label>Tint color</label>"
        f'<input name="tint_color" value="{DEFAULT_SOURCE_TINT_COLOR}" required>'
        "<label>Icon path</label>"
        f'<input name="icon" value="{DEFAULT_SOURCE_ICON}" required>'
        '<button type="submit">Add Channel</button>'
        "</form>"
        f'<p class="muted">Changes are saved to <code>{escape(str(settings_module.config_path()))}</code>.</p>'
    )


def _slug(value: str) -> str:
    slug = sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-").lower()
    return slug or "source"


def _channel_entry(data: dict[str, str]) -> dict[str, str]:
    channel = data.get("channel", "").strip().lstrip("@")
    if not channel:
        raise ValueError("Channel is required.")

    telegram_name = channel.split("/")[-1].strip() or channel
    return {
        "channel": channel,
        "name": telegram_name,
        "slug": _slug(telegram_name),
        "tint_color": data.get("tint_color", "").strip() or DEFAULT_SOURCE_TINT_COLOR,
        "icon": data.get("icon", "").strip() or DEFAULT_SOURCE_ICON,
    }


def _save_channels(channels: list) -> None:
    next_config = deepcopy(settings_module.CONFIG)
    next_config["channels"] = channels
    settings_module.save_config(next_config)
    _runtime_refresh()


def _config_redirect() -> RedirectResponse:
    return RedirectResponse("/config", status_code=303)


def _parse_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if not value:
        return 0, max(size - 1, 0), False
    if not value.startswith("bytes="):
        raise HTTPException(status_code=400, detail="Invalid Range header")
    spec = value.removeprefix("bytes=").split(",", 1)[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        else:
            suffix = int(end_s)
            if suffix <= 0:
                raise HTTPException(status_code=416, detail="Invalid Range header")
            start = max(size - suffix, 0)
            end = size - 1
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Range header") from exc
    if start < 0 or end >= size or start > end:
        raise HTTPException(status_code=416, detail="Range not satisfiable")
    return start, end, True


def _content_disposition(filename: str) -> str:
    fallback = "".join(
        char
        if char.isascii() and char not in {'"', "\\", "\r", "\n"} and ord(char) >= 32
        else "_"
        for char in filename
    ).strip()
    if not fallback:
        fallback = "app.ipa"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


def _image_media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _configured_icon_path(icon: str) -> Path | None:
    if not icon:
        return None
    path = Path(icon)
    candidates = [path] if path.is_absolute() else [Path("/app") / path, path]
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _icon_response(icon: bytes, request: Request) -> Response:
    media_type = _image_media_type(icon)
    headers = {
        "Cache-Control": "public, max-age=86400",
        "Content-Length": str(len(icon)),
    }
    if request.method == "HEAD":
        return Response(media_type=media_type, headers=headers)
    return Response(content=icon, media_type=media_type, headers=headers)


def _source_url(source) -> str:
    return f"{settings.base_url}/{source.slug}.json"


def _source_links(include_channel: bool = False) -> str:
    rows = []
    for source in settings.sources:
        url = _source_url(source)
        channel = f" / @{escape(source.channel)}" if include_channel else ""
        rows.append(
            "<li>"
            f'<div class="source-name">{escape(source.name)}{channel}</div>'
            f'<a class="source-url" href="{escape(url)}"><code>{escape(url)}</code></a>'
            f'<button class="copy-button" type="button" onclick="copySourceUrl(this, {escape(json.dumps(url))})">'
            "Copy</button>"
            "</li>"
        )
    return f"<ul>{''.join(rows)}</ul>"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram.start()
    if await telegram.is_authorized():
        for source in settings.sources:
            logger.info(
                "Service ready. %s repo link: %s",
                source.name,
                f"{settings.base_url}/{source.slug}.json",
            )
    try:
        yield
    finally:
        await telegram.stop()


app = FastAPI(title="TeleStore", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "Content-Range", "Content-Disposition"],
)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "authorized": await telegram.is_authorized(),
        "sources": [
            {
                "channel": source.channel,
                "name": source.name,
                "slug": source.slug,
                "url": _source_url(source),
            }
            for source in settings.sources
        ],
    }


@app.api_route("/icon.png", methods=["GET", "HEAD"])
async def icon_png(request: Request):
    headers = {
        "Cache-Control": "public, max-age=86400",
        "Content-Length": str(len(DEFAULT_ICON_PNG)),
    }
    if request.method == "HEAD":
        return Response(media_type="image/png", headers=headers)
    return Response(
        content=DEFAULT_ICON_PNG,
        media_type="image/png",
        headers=headers,
    )


@app.api_route("/source-icon.png", methods=["GET", "HEAD"])
async def source_icon_png(request: Request):
    icon_path = next((path for path in SOURCE_ICON_PATHS if path.exists()), None)
    if icon_path is None:
        return await icon_png(request)

    return _icon_response(icon_path.read_bytes(), request)


def _source_or_404(source_slug: str):
    source = sources_by_slug.get(source_slug)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_slug}")
    return source


@app.api_route("/{source_slug}-icon.png", methods=["GET", "HEAD"])
async def configured_source_icon(source_slug: str, request: Request):
    source = _source_or_404(source_slug)
    icon_path = _configured_icon_path(source.icon)
    if icon_path is None:
        return await source_icon_png(request)
    return _icon_response(icon_path.read_bytes(), request)


@app.api_route("/icon/{source_slug}/{message_id:int}.jpg", methods=["GET", "HEAD"])
async def telegram_icon(source_slug: str, message_id: int, request: Request):
    if not await telegram.is_authorized():
        raise HTTPException(status_code=401, detail="Open /login to authenticate Telegram")
    source = _source_or_404(source_slug)
    try:
        message = await telegram.get_message(source, message_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    thumbnail = await telegram.download_thumbnail(message)
    if not thumbnail:
        thumbnail = DEFAULT_ICON_PNG
    media_type = _image_media_type(thumbnail)

    headers = {
        "Cache-Control": "public, max-age=86400",
        "Content-Length": str(len(thumbnail)),
    }
    if request.method == "HEAD":
        return Response(media_type=media_type, headers=headers)
    return Response(content=thumbnail, media_type=media_type, headers=headers)


@app.api_route("/icon/{message_id:int}.jpg", methods=["GET", "HEAD"])
async def legacy_telegram_icon(message_id: int, request: Request):
    return await telegram_icon(default_source.slug, message_id, request)


@app.get("/")
async def home():
    if await telegram.is_authorized():
        config_link = '<p><a href="/config">Edit channels</a></p>' if settings.ui_config else ""
        return _html_page(
            "<h1>Telegram Sources</h1>"
            '<p>Telegram login ready.</p>'
            f"{config_link}"
            f"{_source_links(include_channel=True)}"
        )
    return _html_page(
        '<h1>Telegram Login Required</h1>'
        '<p>Open <a href="/login"><code>/login</code></a> to save Telegram session.</p>'
    )


@app.get("/login")
async def login_page():
    if await telegram.is_authorized():
        config_link = '<p><a href="/config">Edit channels</a></p>' if settings.ui_config else ""
        return _html_page(
            '<h1>Logged In</h1><p>Telegram session saved.</p>'
            f"{config_link}"
            f"{_source_links()}"
        )
    return _html_page(
        """<h1>Telegram Login</h1>
<form method="post" action="/login/send-code">
  <label>Phone number with country code</label>
  <input name="phone" placeholder="+491234567890" autocomplete="tel" required>
  <button type="submit">Send Code</button>
</form>"""
    )


@app.post("/login/send-code")
async def login_send_code(request: Request):
    data = await _form(request)
    phone = data.get("phone", "").strip()
    if not phone:
        return _html_page('<p class="error">Phone required.</p><p><a href="/login">Back</a></p>', 400)
    try:
        await telegram.send_login_code(phone)
    except Exception as exc:
        return _html_page(
            f'<p class="error">{escape(str(exc))}</p><p><a href="/login">Back</a></p>',
            400,
        )
    return _html_page(
        """<h1>Enter Code</h1>
<form method="post" action="/login/verify">
  <label>Telegram login code</label>
  <input name="code" autocomplete="one-time-code" required>
  <label>Two-step password, if enabled</label>
  <input name="password" type="password" autocomplete="current-password">
  <button type="submit">Save Session</button>
</form>"""
    )


@app.post("/login/verify")
async def login_verify(request: Request):
    data = await _form(request)
    try:
        await telegram.complete_login(data.get("code", ""), data.get("password") or None)
    except Exception as exc:
        return _html_page(
            f'<p class="error">{escape(str(exc))}</p><p><a href="/login">Try again</a></p>',
            400,
        )
    return _html_page(
        '<h1>Login Saved</h1><p>Telegram session saved in Docker volume.</p>'
        f"{_source_links()}"
    )


@app.get("/config")
async def config_page():
    _config_ui_enabled()
    return _html_page(_config_page_body())


@app.post("/config/channels")
async def config_add_channel(request: Request):
    _config_ui_enabled()
    data = await _form(request)
    try:
        entry = _channel_entry(data)
        channels = settings_module.CONFIG.get("channels")
        if not isinstance(channels, list):
            channels = []
        else:
            channels = list(channels)
        channels.append(entry)
        _save_channels(channels)
    except Exception as exc:
        return _html_page(_config_page_body(error=str(exc)), 400)

    return _config_redirect()


@app.post("/config/channels/remove")
async def config_remove_channel(request: Request):
    _config_ui_enabled()
    data = await _form(request)
    try:
        index = int(data.get("index", ""))
        channels = settings_module.CONFIG.get("channels")
        if not isinstance(channels, list):
            raise ValueError("channels must be a YAML list.")
        configured_channels = [
            item
            for item in channels
            if str(item.get("channel", item) if isinstance(item, dict) else item).strip().lstrip("@")
        ]
        if len(configured_channels) <= 1:
            raise ValueError("At least one channel must remain configured.")
        removed = channels[index]
        next_channels = list(channels)
        del next_channels[index]
        _save_channels(next_channels)
    except Exception as exc:
        return _html_page(_config_page_body(error=str(exc)), 400)

    return _config_redirect()


@app.get("/source.json")
async def source_json():
    return await named_source_json(default_source.slug)


@app.get("/{source_slug}.json")
async def named_source_json(source_slug: str):
    if not await telegram.is_authorized():
        raise HTTPException(status_code=401, detail="Open /login to authenticate Telegram")
    source_config = _source_or_404(source_slug)
    now = monotonic()
    source_cache = source_caches[source_config.slug]
    cached_source = source_cache["value"]
    if cached_source is not None and now < float(source_cache["expires_at"]):
        return JSONResponse(cached_source)

    source = await build_source(settings, source_config, telegram)
    source_cache["value"] = source
    source_cache["expires_at"] = now + max(settings.source_cache_seconds, 0)
    return JSONResponse(source)


@app.api_route("/ipa/{source_slug}/{message_id:int}/{filename:path}", methods=["GET", "HEAD"])
async def ipa(source_slug: str, message_id: int, filename: str, request: Request):
    if not await telegram.is_authorized():
        raise HTTPException(status_code=401, detail="Open /login to authenticate Telegram")
    source = _source_or_404(source_slug)
    try:
        message = await telegram.get_message(source, message_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    media = getattr(message, "file", None)
    size = int(getattr(media, "size", None) or 0)
    if size <= 0:
        raise HTTPException(status_code=404, detail="Telegram media size unavailable")

    start, end, partial = _parse_range(request.headers.get("range"), size)
    content_length = end - start + 1
    safe_filename = unquote(filename).split("/")[-1] or getattr(media, "name", None) or "app.ipa"

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": _content_disposition(safe_filename),
        "Cache-Control": "no-store",
        "Last-Modified": formatdate(message.date.timestamp(), usegmt=True),
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    if request.method == "HEAD":
        return Response(
            status_code=206 if partial else 200,
            media_type="application/octet-stream",
            headers=headers,
        )

    return StreamingResponse(
        telegram.stream_media(message, offset=start, limit=content_length),
        status_code=206 if partial else 200,
        media_type="application/octet-stream",
        headers=headers,
    )


@app.api_route("/ipa/{message_id:int}/{filename:path}", methods=["GET", "HEAD"])
async def legacy_ipa(message_id: int, filename: str, request: Request):
    return await ipa(default_source.slug, message_id, filename, request)


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=APP_PORT)
