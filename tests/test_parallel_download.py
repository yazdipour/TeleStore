import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from tests.test_telegram_client import _write_test_config

_write_test_config()

from telethon.tl.types.upload import File  # noqa: E402

from src import parallel_download  # noqa: E402
from src.parallel_download import PART_SIZE, SenderPool, try_parallel_download  # noqa: E402


def _content(size: int) -> bytes:
    return bytes(i % 251 for i in range(size))


class FakeCallClient:
    """Answers upload.GetFileRequest from an in-memory byte string."""

    def __init__(self, content: bytes, fail_at_offset: int | None = None):
        self.content = content
        self.fail_at_offset = fail_at_offset
        self.calls = []

    async def _call(self, sender, request):
        self.calls.append((sender, request.offset))
        if self.fail_at_offset is not None and request.offset == self.fail_at_offset:
            raise ConnectionError("boom")
        data = self.content[request.offset : request.offset + request.limit]
        return File(type=None, mtime=0, bytes=data)


class FakePool:
    def __init__(self, senders):
        self.senders = senders
        self.released = None

    @property
    def enabled(self):
        return True

    async def acquire(self, dc_id, want):
        return self.senders[:want]

    async def release(self, dc_id, senders, *, broken=False):
        self.released = (senders, broken)


class ParallelChunksTests(IsolatedAsyncioTestCase):
    async def _run(self, *, size, offset, limit, senders=3, fail_at_offset=None):
        content = _content(size)
        client = FakeCallClient(content, fail_at_offset=fail_at_offset)
        pool = FakePool([f"sender-{i}" for i in range(senders)])
        aligned = offset - offset % PART_SIZE
        part_count = -(-(offset + limit - aligned) // PART_SIZE)
        iterator = parallel_download._chunks(
            client, pool, 2, pool.senders, "location", aligned, offset, limit, part_count
        )
        chunks = [chunk async for chunk in iterator]
        return content, chunks, pool, client

    async def test_ordered_bytes_with_skip_and_trim(self):
        size = 3 * PART_SIZE + 1234
        offset = 1000
        limit = size - 2000
        content, chunks, pool, _ = await self._run(size=size, offset=offset, limit=limit)

        self.assertEqual(b"".join(chunks), content[offset : offset + limit])
        self.assertEqual(pool.released, (pool.senders, False))

    async def test_full_file_single_sender(self):
        size = 2 * PART_SIZE + 7
        content, chunks, pool, _ = await self._run(size=size, offset=0, limit=size, senders=1)

        self.assertEqual(b"".join(chunks), content)

    async def test_failure_marks_senders_broken(self):
        size = 4 * PART_SIZE
        with self.assertRaises(ConnectionError):
            await self._run(size=size, offset=0, limit=size, fail_at_offset=2 * PART_SIZE)

    async def test_failure_releases_broken(self):
        content = _content(4 * PART_SIZE)
        client = FakeCallClient(content, fail_at_offset=2 * PART_SIZE)
        pool = FakePool(["a", "b"])
        iterator = parallel_download._chunks(
            client, pool, 2, pool.senders, "location", 0, 0, 4 * PART_SIZE, 4
        )
        with self.assertRaises(ConnectionError):
            async for _ in iterator:
                pass
        self.assertEqual(pool.released, (pool.senders, True))


class TryParallelDownloadTests(IsolatedAsyncioTestCase):
    async def test_disabled_pool_returns_none(self):
        pool = SenderPool(client=None, max_connections=1)
        result = await try_parallel_download(
            None, pool, media=None, offset=0, limit=10 * PART_SIZE
        )
        self.assertIsNone(result)

    async def test_small_range_returns_none(self):
        pool = SenderPool(client=None, max_connections=8)
        result = await try_parallel_download(None, pool, media=None, offset=0, limit=1024)
        self.assertIsNone(result)

    async def test_unsupported_media_returns_none(self):
        pool = SenderPool(client=None, max_connections=8)
        result = await try_parallel_download(
            None, pool, media=object(), offset=0, limit=10 * PART_SIZE
        )
        self.assertIsNone(result)


class StreamMediaFallbackTests(IsolatedAsyncioTestCase):
    async def test_stream_media_falls_back_when_parallel_unavailable(self):
        from tests.test_telegram_client import AsyncSequence, FakeTelegramClient, make_service

        client = FakeTelegramClient()
        client.connected = True
        client.iter_download_results = [AsyncSequence([b"abcde"])]
        service = make_service(client)
        message = SimpleNamespace(media="media")

        chunks = [
            chunk
            async for chunk in service.stream_media(message, offset=0, limit=5, chunk_size=8)
        ]

        self.assertEqual(chunks, [b"abcde"])
        self.assertEqual(client.iter_download_calls, [("media", 0, 8, 8)])
