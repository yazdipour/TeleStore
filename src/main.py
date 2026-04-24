from contextlib import asynccontextmanager
from email.utils import formatdate
from html import escape
from urllib.parse import parse_qs
from urllib.parse import unquote

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from src.settings import load_settings
from src.source_builder import build_source
from src.telegram_client import TelegramService


settings = load_settings()
telegram = TelegramService(settings)


def _html_page(body: str, status_code: int = 200) -> Response:
    return Response(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram Login</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 520px; margin: 48px auto; padding: 0 20px; line-height: 1.5; }}
    input, button {{ font: inherit; width: 100%; box-sizing: border-box; padding: 10px 12px; margin: 6px 0 14px; }}
    button {{ cursor: pointer; }}
    code {{ background: #eee; padding: 2px 4px; }}
    .error {{ color: #b00020; }}
  </style>
</head>
<body>{body}</body>
</html>""",
        status_code=status_code,
        media_type="text/html",
    )


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode()
    return {key: values[-1] for key, values in parse_qs(body).items()}


def _parse_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if not value:
        return 0, max(size - 1, 0), False
    if not value.startswith("bytes="):
        raise HTTPException(status_code=400, detail="Invalid Range header")
    spec = value.removeprefix("bytes=").split(",", 1)[0].strip()
    start_s, _, end_s = spec.partition("-")
    if start_s:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    else:
        suffix = int(end_s)
        if suffix <= 0:
            raise HTTPException(status_code=416, detail="Invalid Range header")
        start = max(size - suffix, 0)
        end = size - 1
    if start < 0 or end >= size or start > end:
        raise HTTPException(status_code=416, detail="Range not satisfiable")
    return start, end, True


@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram.start()
    try:
        yield
    finally:
        await telegram.stop()


app = FastAPI(title="LiveBlatant", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "Content-Range", "Content-Disposition"],
)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "authorized": await telegram.is_authorized(),
        "channel": settings.telegram_channel,
    }


@app.get("/")
async def home():
    if await telegram.is_authorized():
        return _html_page(
            f"<h1>{escape(settings.source_name)}</h1>"
            '<p>Telegram login ready.</p>'
            '<p>AltStore source: <a href="/source.json"><code>/source.json</code></a></p>'
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
            '<p>AltStore source: <a href="/source.json"><code>/source.json</code></a></p>'
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
        '<p>AltStore source: <a href="/source.json"><code>/source.json</code></a></p>'
    )


@app.get("/source.json")
async def source_json():
    if not await telegram.is_authorized():
        raise HTTPException(status_code=401, detail="Open /login to authenticate Telegram")
    return JSONResponse(await build_source(settings, telegram))


@app.api_route("/ipa/{message_id}/{filename:path}", methods=["GET", "HEAD"])
async def ipa(message_id: int, filename: str, request: Request):
    if not await telegram.is_authorized():
        raise HTTPException(status_code=401, detail="Open /login to authenticate Telegram")
    try:
        message = await telegram.get_message(message_id)
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
        "Content-Disposition": f'attachment; filename="{safe_filename}"',
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


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port)
