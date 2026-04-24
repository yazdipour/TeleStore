import os
from typing import AsyncIterator

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import Message

from src.settings import Settings


class TelegramService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = TelegramClient(
            settings.telegram_session,
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        self._channel_entity = None
        self._pending_phone: str | None = None

    async def start(self) -> None:
        os.makedirs(os.path.dirname(self.settings.telegram_session), exist_ok=True)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            print("")
            print("Telegram login required. Open /login in browser.")

    async def stop(self) -> None:
        await self.client.disconnect()

    async def is_authorized(self) -> bool:
        return await self.client.is_user_authorized()

    async def send_login_code(self, phone: str) -> None:
        self._pending_phone = phone.strip()
        await self.client.send_code_request(self._pending_phone)

    async def complete_login(self, code: str, password: str | None = None) -> None:
        if not self._pending_phone:
            raise RuntimeError("No pending phone login. Request a code first.")
        try:
            await self.client.sign_in(phone=self._pending_phone, code=code.strip())
        except SessionPasswordNeededError:
            if not password:
                raise RuntimeError("Two-step password required")
            await self.client.sign_in(password=password)
        self._pending_phone = None

    async def channel(self):
        if self._channel_entity is None:
            self._channel_entity = await self.client.get_entity(self.settings.telegram_channel)
        return self._channel_entity

    async def get_message(self, message_id: int) -> Message:
        message = await self.client.get_messages(await self.channel(), ids=message_id)
        if not message or not message.media:
            raise FileNotFoundError(f"Telegram message {message_id} has no media")
        return message

    async def download_thumbnail(self, message: Message) -> bytes | None:
        try:
            data = await self.client.download_media(message, file=bytes, thumb=-1)
        except Exception:
            return None
        if isinstance(data, bytes) and data:
            return data
        return None

    async def iter_recent_messages(self, limit: int) -> AsyncIterator[Message]:
        async for message in self.client.iter_messages(await self.channel(), limit=limit):
            if message and message.media:
                yield message

    async def stream_media(
        self,
        message: Message,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 512 * 1024,
    ) -> AsyncIterator[bytes]:
        sent = 0
        async for chunk in self.client.iter_download(
            message.media,
            offset=offset,
            chunk_size=chunk_size,
            request_size=chunk_size,
        ):
            if limit is not None:
                remaining = limit - sent
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
            sent += len(chunk)
            yield chunk
