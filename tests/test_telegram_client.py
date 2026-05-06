import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase


def _write_test_config() -> None:
    path = Path(tempfile.gettempdir()) / "telestore-test-config.yml"
    path.write_text(
        """
telegram:
  api_id: 123
  api_hash: test-hash
  session: /tmp/telestore-test.session
  limit: 100
server:
  base_url: http://localhost:8080
channels:
  - example
""",
        encoding="utf-8",
    )
    import os

    os.environ["CONFIG_FILE"] = str(path)


_write_test_config()

from src.settings import SourceConfig  # noqa: E402
from src.telegram_client import TelegramService  # noqa: E402


class AsyncSequence:
    def __init__(self, items, exc_at: int | None = None):
        self.items = list(items)
        self.exc_at = exc_at

    async def __aiter__(self):
        for index, item in enumerate(self.items):
            if self.exc_at == index:
                raise ConnectionError("disconnected")
            yield item
        if self.exc_at == len(self.items):
            raise ConnectionError("disconnected")


class FakeTelegramClient:
    def __init__(self):
        self.connected = False
        self.connect_count = 0
        self.disconnect_count = 0
        self.entities = {}
        self.messages = {}
        self.iter_message_calls = []
        self.iter_message_results = []
        self.iter_download_calls = []
        self.iter_download_results = []

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True
        self.connect_count += 1

    async def disconnect(self):
        self.connected = False
        self.disconnect_count += 1

    async def get_entity(self, channel):
        return self.entities[channel]

    async def get_messages(self, entity, ids):
        return self.messages[(entity, ids)]

    def iter_messages(self, entity, *, limit, offset_id=0):
        self.iter_message_calls.append((entity, limit, offset_id))
        return self.iter_message_results.pop(0)

    def iter_download(self, media, *, offset, chunk_size, request_size):
        self.iter_download_calls.append((media, offset, chunk_size, request_size))
        return self.iter_download_results.pop(0)


def make_service(client: FakeTelegramClient) -> TelegramService:
    service = TelegramService.__new__(TelegramService)
    service.client = client
    service._channel_entities = {}
    service._pending_phone = None
    service._connect_lock = asyncio.Lock()
    service.settings = SimpleNamespace()
    return service


def make_source() -> SourceConfig:
    return SourceConfig(
        channel="channel-name",
        slug="channel-name",
        name="Channel Name",
        tint_color="#1D9BF0",
    )


def make_message(message_id: int, *, media=True):
    return SimpleNamespace(id=message_id, media=object() if media else None)


class TelegramServiceTests(IsolatedAsyncioTestCase):
    async def test_channel_connects_and_caches_entity(self):
        client = FakeTelegramClient()
        client.entities["channel-name"] = "entity"
        service = make_service(client)
        source = make_source()

        first = await service.channel(source)
        second = await service.channel(source)

        self.assertEqual(first, "entity")
        self.assertEqual(second, "entity")
        self.assertEqual(client.connect_count, 1)

    async def test_get_message_raises_when_message_has_no_media(self):
        client = FakeTelegramClient()
        client.entities["channel-name"] = "entity"
        client.messages[("entity", 42)] = make_message(42, media=False)
        service = make_service(client)

        with self.assertRaises(FileNotFoundError):
            await service.get_message(make_source(), 42)

    async def test_iter_recent_messages_resumes_after_consumed_message_on_reconnect(self):
        client = FakeTelegramClient()
        client.connected = True
        client.entities["channel-name"] = "entity"
        client.iter_message_results = [
            AsyncSequence([make_message(10), make_message(9, media=False)], exc_at=2),
            AsyncSequence([make_message(8), make_message(7), make_message(6, media=False)]),
        ]
        service = make_service(client)

        ids = [message.id async for message in service.iter_recent_messages(make_source(), limit=5)]

        self.assertEqual(ids, [10, 8, 7])
        self.assertEqual(
            client.iter_message_calls,
            [("entity", 5, 0), ("entity", 3, 9)],
        )

    async def test_stream_media_retries_from_last_yielded_offset(self):
        client = FakeTelegramClient()
        client.connected = True
        client.iter_download_results = [
            AsyncSequence([b"abc"], exc_at=1),
            AsyncSequence([b"de"]),
        ]
        service = make_service(client)
        message = SimpleNamespace(media="media")

        chunks = [
            chunk
            async for chunk in service.stream_media(
                message,
                offset=100,
                limit=5,
                chunk_size=8,
            )
        ]

        self.assertEqual(chunks, [b"abc", b"de"])
        self.assertEqual(
            client.iter_download_calls,
            [("media", 100, 8, 8), ("media", 103, 8, 8)],
        )
        self.assertEqual(client.disconnect_count, 1)


class TestConfigImport(TestCase):
    def test_config_file_is_set_for_import_time_settings(self):
        import os

        self.assertTrue(Path(os.environ["CONFIG_FILE"]).exists())
