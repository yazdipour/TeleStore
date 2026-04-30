from contextlib import asynccontextmanager
import asyncio
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
from src.settings import APP_PORT, load_settings, normalize_channel
from src.source_builder import build_source
from src.telegram_client import TelegramService


settings = load_settings()
telegram = TelegramService(settings)
source_caches: dict[str, dict[str, object]] = {
    source.slug: {"expires_at": 0.0, "value": None} for source in settings.sources
}
source_icon_caches: dict[str, dict[str, object]] = {
    source.slug: {"expires_at": 0.0, "value": None} for source in settings.sources
}
ipa_cache_locks: dict[str, asyncio.Lock] = {}
ipa_cache_lock_refs: dict[str, int] = {}
ipa_cache_global_semaphore = asyncio.Semaphore(settings.ipa_cache_global_workers)
sources_by_slug = {source.slug: source for source in settings.sources}
SOURCE_ICON_PATHS = (
    Path("/app/imgs/ICON-120-blue.png"),
    Path("imgs/ICON-120-blue.png"),
)
logger = logging.getLogger("uvicorn.error")
PAGE_TEMPLATE_PATH = Path(__file__).with_name("templates") / "page.html"
PAGE_BODY_MARKER = "<!--BODY-->"


def _html_page(body: str, status_code: int = 200) -> Response:
    html = PAGE_TEMPLATE_PATH.read_text(encoding="utf-8").replace(PAGE_BODY_MARKER, body)
    return Response(
        html,
        status_code=status_code,
        media_type="text/html",
    )


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode()
    return {key: values[-1] for key, values in parse_qs(body).items()}


def _runtime_refresh() -> None:
    global settings, source_caches, source_icon_caches, sources_by_slug, ipa_cache_global_semaphore
    settings_module.reload_config()
    settings = load_settings()
    telegram.settings = settings
    telegram._channel_entities.clear()
    source_caches = {source.slug: {"expires_at": 0.0, "value": None} for source in settings.sources}
    source_icon_caches = {source.slug: {"expires_at": 0.0, "value": None} for source in settings.sources}
    sources_by_slug = {source.slug: source for source in settings.sources}
    ipa_cache_global_semaphore = asyncio.Semaphore(settings.ipa_cache_global_workers)


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
        if not isinstance(raw_source, str) or not normalize_channel(raw_source):
            continue
        if source_index >= len(settings.sources):
            break
        rows.append((config_index, settings.sources[source_index]))
        source_index += 1
    return rows


def _config_page_body(error: str = "", authorized: bool = False) -> str:
    status = ""
    if error:
        status = f'<div class="notice error">{escape(error)}</div>'

    rows = []
    for config_index, source in _source_config_rows():
        url = _source_url(source)
        rows.append(
            f'<li class="source-card" style="--source-tint: {escape(source.tint_color)}">'
            f'<img class="source-icon" src="{escape(_source_icon_url(source))}" alt="">'
            "<div>"
            f'<div class="source-name">{escape(source.name)}</div>'
            f'<div class="source-meta">@{escape(source.channel)}</div>'
            f'<a class="source-url" href="{escape(url)}"><code>{escape(url)}</code></a>'
            "</div>"
            '<div class="source-actions">'
            f'<button type="button" onclick="copySourceUrl(this, {escape(json.dumps(url))})">Copy</button>'
            '<form method="post" action="/config/channels/remove">'
            f'<input type="hidden" name="index" value="{config_index}">'
            '<button class="remove-button" type="submit">Remove</button>'
            "</form>"
            "</div>"
            "</li>"
        )

    channel_list = f'<ul class="source-list">{"".join(rows)}</ul>' if rows else '<div class="empty">No channels configured.</div>'
    
    template = (PAGE_TEMPLATE_PATH.with_name("config.html")).read_text(encoding="utf-8")
    return template.replace("<!--STATUS-->", status).replace("<!--COUNT-->", str(len(rows))).replace("<!--CHANNEL_LIST-->", channel_list)


def _channel_entry(data: dict[str, str]) -> str:
    channel = normalize_channel(data.get("channel", ""))
    if not channel:
        raise ValueError("Channel is required.")
    return channel


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


def _file_chunks(path: Path, start: int, content_length: int, chunk_size: int = 1024 * 1024):
    remaining = content_length
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _cache_key(source_slug: str, message_id: int, filename: str) -> str:
    safe_name = sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-") or "app.ipa"
    return f"{source_slug}/{message_id}-{safe_name[:180]}"


def _ipa_cache_path(source_slug: str, message_id: int, filename: str) -> Path | None:
    if not settings.ipa_cache_dir:
        return None
    return Path(settings.ipa_cache_dir) / _cache_key(source_slug, message_id, filename)


def _valid_cached_file(path: Path | None, expected_size: int) -> bool:
    if path is None:
        return False
    try:
        return path.stat().st_size == expected_size
    except OSError:
        return False


def _cached_ipa_response(
    path: Path,
    *,
    start: int,
    content_length: int,
    partial: bool,
    headers: dict[str, str],
) -> StreamingResponse:
    return StreamingResponse(
        _file_chunks(path, start, content_length),
        status_code=206 if partial else 200,
        media_type="application/octet-stream",
        headers=headers,
    )


@asynccontextmanager
async def _cache_lock(path: Path):
    key = str(path)
    lock = ipa_cache_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        ipa_cache_locks[key] = lock
    ipa_cache_lock_refs[key] = ipa_cache_lock_refs.get(key, 0) + 1
    try:
        async with lock:
            yield
    finally:
        refs = ipa_cache_lock_refs.get(key, 1) - 1
        if refs <= 0:
            ipa_cache_lock_refs.pop(key, None)
            ipa_cache_locks.pop(key, None)
        else:
            ipa_cache_lock_refs[key] = refs


def _cache_ranges(expected_size: int) -> list[tuple[int, int]]:
    part_size = settings.ipa_cache_part_size
    return [
        (offset, min(part_size, expected_size - offset))
        for offset in range(0, expected_size, part_size)
    ]


async def _download_cache_range(message, temp_path: Path, offset: int, length: int) -> None:
    position = offset
    with temp_path.open("r+b") as handle:
        async with ipa_cache_global_semaphore:
            async for chunk in telegram.stream_media(message, offset=offset, limit=length):
                handle.seek(position)
                handle.write(chunk)
                position += len(chunk)

    if position - offset != length:
        raise RuntimeError(
            f"Telegram media cache part incomplete: offset {offset}, {position - offset}/{length} bytes"
        )


async def _download_cache_parallel(message, temp_path: Path, expected_size: int) -> None:
    ranges = _cache_ranges(expected_size)
    workers = min(settings.ipa_cache_workers, len(ranges))
    queue = asyncio.Queue()
    for item in ranges:
        queue.put_nowait(item)

    async def worker() -> None:
        while True:
            try:
                offset, length = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await _download_cache_range(message, temp_path, offset, length)
            finally:
                queue.task_done()

    with temp_path.open("wb") as handle:
        handle.truncate(expected_size)

    tasks = [asyncio.create_task(worker()) for _ in range(workers)]
    try:
        await asyncio.gather(*tasks)
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _download_cache_serial(message, temp_path: Path, expected_size: int) -> None:
    written = 0
    with temp_path.open("wb") as handle:
        async with ipa_cache_global_semaphore:
            async for chunk in telegram.stream_media(message, offset=0, limit=expected_size):
                handle.write(chunk)
                written += len(chunk)
    if written != expected_size:
        raise RuntimeError(f"Telegram media cache incomplete: {written}/{expected_size} bytes")


async def _download_cache_file(message, temp_path: Path, expected_size: int) -> None:
    if settings.ipa_cache_workers <= 1 or expected_size <= settings.ipa_cache_part_size:
        await _download_cache_serial(message, temp_path, expected_size)
        return
    await _download_cache_parallel(message, temp_path, expected_size)


async def _ensure_ipa_cached(message, cache_path: Path, expected_size: int) -> None:
    async with _cache_lock(cache_path):
        if _valid_cached_file(cache_path, expected_size):
            return

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cache_path.with_name(f".{cache_path.name}.tmp")
        try:
            await _download_cache_file(message, temp_path, expected_size)
            temp_path.replace(cache_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise


async def _stream_ipa_with_cache(message, cache_path: Path, expected_size: int):
    async with _cache_lock(cache_path):
        if _valid_cached_file(cache_path, expected_size):
            for chunk in _file_chunks(cache_path, 0, expected_size):
                yield chunk
            return

        handle = None
        temp_path = cache_path.with_name(f".{cache_path.name}.tmp")
        written = 0
        cache_failed = False
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            handle = temp_path.open("wb")
        except Exception as exc:
            cache_failed = True
            logger.warning("IPA cache write unavailable, streaming without cache: %s", exc)

        try:
            async with ipa_cache_global_semaphore:
                async for chunk in telegram.stream_media(message, offset=0, limit=expected_size):
                    if handle is not None:
                        try:
                            handle.write(chunk)
                        except Exception as exc:
                            cache_failed = True
                            logger.warning("IPA cache write failed, streaming without cache: %s", exc)
                            handle.close()
                            handle = None
                            temp_path.unlink(missing_ok=True)
                        else:
                            written += len(chunk)
                    yield chunk
        finally:
            if handle is not None:
                handle.close()

        if not cache_failed:
            if written == expected_size:
                temp_path.replace(cache_path)
            else:
                temp_path.unlink(missing_ok=True)


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


def _icon_response(icon: bytes, request: Request) -> Response:
    media_type = _image_media_type(icon)
    headers = {
        "Cache-Control": "public, max-age=86400",
        "Content-Length": str(len(icon)),
    }
    if request.method == "HEAD":
        return Response(media_type=media_type, headers=headers)
    return Response(content=icon, media_type=media_type, headers=headers)


async def _source_channel_icon(source) -> bytes | None:
    cache = source_icon_caches.setdefault(source.slug, {"expires_at": 0.0, "value": None})
    now = monotonic()
    if float(cache["expires_at"]) > now:
        value = cache["value"]
        return value if isinstance(value, bytes) else None
    icon = None
    if not await telegram.is_authorized():
        return None
    icon = await telegram.download_channel_photo(source)
    cache["value"] = icon
    cache["expires_at"] = now + 86400
    return icon


def _source_url(source) -> str:
    return f"{settings.base_url}/{source.slug}.json"


def _source_icon_url(source) -> str:
    return f"{settings.base_url}/{source.slug}-icon.png"


def _source_links(include_channel: bool = False) -> str:
    rows = []
    for source in settings.sources:
        url = _source_url(source)
        channel = f'<div class="source-meta">@{escape(source.channel)}</div>' if include_channel else ""
        rows.append(
            f'<li class="source-card" style="--source-tint: {escape(source.tint_color)}">'
            f'<img class="source-icon" src="{escape(_source_icon_url(source))}" alt="">'
            "<div>"
            f'<div class="source-name">{escape(source.name)}</div>'
            f"{channel}"
            f'<a class="source-url" href="{escape(url)}"><code>{escape(url)}</code></a>'
            "</div>"
            f'<button type="button" onclick="copySourceUrl(this, {escape(json.dumps(url))})">Copy</button>'
            "</li>"
        )
    return f'<ul class="source-list">{"".join(rows)}</ul>'


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
    channel_icon = await _source_channel_icon(source)
    if channel_icon:
        return _icon_response(channel_icon, request)
    return await source_icon_png(request)


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


@app.get("/")
async def home():
    if settings.ui_config:
        return _html_page(_config_page_body(authorized=await telegram.is_authorized()))
    if await telegram.is_authorized():
        return _html_page(
            '<h1><a href="https://github.com/yazdipour/telestore">TeleStore</a></h1>'
            '<p>Telegram login ready.</p>'
            f"{_source_links(include_channel=True)}"
        )
    return _html_page(
        '<h1>Telegram Login Required</h1>'
        '<p>Open <a href="/login"><code>/login</code></a> to save Telegram session.</p>'
    )


@app.get("/login")
async def login_page():
    if await telegram.is_authorized():
        return _html_page(
            '<h1>Logged In</h1><p>Telegram session saved.</p>'
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
    return _html_page(_config_page_body(authorized=await telegram.is_authorized()))


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
        return _html_page(_config_page_body(error=str(exc), authorized=await telegram.is_authorized()), 400)

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
            if isinstance(item, str) and normalize_channel(item)
        ]
        if len(configured_channels) <= 1:
            raise ValueError("At least one channel must remain configured.")
        removed = channels[index]
        next_channels = list(channels)
        del next_channels[index]
        _save_channels(next_channels)
    except Exception as exc:
        return _html_page(_config_page_body(error=str(exc), authorized=await telegram.is_authorized()), 400)

    return _config_redirect()


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
    source_cache["expires_at"] = now + max(settings.cache_seconds, 0)
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

    cache_path = _ipa_cache_path(source.slug, message_id, safe_filename)
    if _valid_cached_file(cache_path, size):
        return _cached_ipa_response(
            cache_path,
            start=start,
            content_length=content_length,
            partial=partial,
            headers=headers,
        )

    if cache_path is not None:
        try:
            if partial:
                await _ensure_ipa_cached(message, cache_path, size)
                return _cached_ipa_response(
                    cache_path,
                    start=start,
                    content_length=content_length,
                    partial=partial,
                    headers=headers,
                )

            return StreamingResponse(
                _stream_ipa_with_cache(message, cache_path, size),
                status_code=200,
                media_type="application/octet-stream",
                headers=headers,
            )
        except Exception as exc:
            logger.warning("IPA cache unavailable, streaming from Telegram: %s", exc)

    return StreamingResponse(
        telegram.stream_media(message, offset=start, limit=content_length),
        status_code=206 if partial else 200,
        media_type="application/octet-stream",
        headers=headers,
    )

if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=APP_PORT)
