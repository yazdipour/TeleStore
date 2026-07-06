"""Parallel media downloads over extra MTProto connections.

Telegram throttles throughput per connection, so one Telethon client tops out
around 1-2 MB/s. This module keeps a small pool of extra authorized senders
(the FastTelethon technique) and fetches file parts across them concurrently
while yielding bytes in order.
"""

import asyncio
import contextlib
from typing import AsyncIterator

from telethon import TelegramClient, utils
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InitConnectionRequest, InvokeWithLayerRequest
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest
from telethon.tl.functions.upload import GetFileRequest
from telethon.tl.types.upload import File

# upload.getFile requires: offset/limit 4 KB aligned and limit divides 1 MB.
PART_SIZE = 512 * 1024


class SenderPool:
    """Pool of extra MTProto connections, reused across downloads.

    acquire() never blocks: it hands out idle or newly created senders up to
    the global cap, possibly none at all. Callers must handle an empty result
    by falling back to the shared client connection.
    """

    def __init__(self, client: TelegramClient, max_connections: int):
        self._client = client
        self._max = max_connections
        self._idle: dict[int, list[MTProtoSender]] = {}
        self._counts: dict[int, int] = {}
        self._auth_keys: dict[int, object] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._max > 1

    async def acquire(self, dc_id: int, want: int) -> list[MTProtoSender]:
        want = min(want, self._max)
        senders: list[MTProtoSender] = []
        async with self._lock:
            idle = self._idle.setdefault(dc_id, [])
            while idle and len(senders) < want:
                sender = idle.pop()
                if sender.is_connected():
                    senders.append(sender)
                else:
                    self._counts[dc_id] -= 1
                    with contextlib.suppress(Exception):
                        await sender.disconnect()
            spawn = min(want - len(senders), self._max - sum(self._counts.values()))
            self._counts[dc_id] = self._counts.get(dc_id, 0) + max(spawn, 0)
        # ponytail: sequential connects, ~0.2s each; parallelize if cold-start latency matters
        for _ in range(max(spawn, 0)):
            try:
                senders.append(await self._connect(dc_id))
            except Exception:
                async with self._lock:
                    self._counts[dc_id] -= 1
                break
        return senders

    async def release(self, dc_id: int, senders: list[MTProtoSender], *, broken: bool = False) -> None:
        async with self._lock:
            for sender in senders:
                if broken or not sender.is_connected():
                    self._counts[dc_id] -= 1
                    with contextlib.suppress(Exception):
                        await sender.disconnect()
                else:
                    self._idle.setdefault(dc_id, []).append(sender)

    async def close(self) -> None:
        async with self._lock:
            for idle in self._idle.values():
                for sender in idle:
                    with contextlib.suppress(Exception):
                        await sender.disconnect()
            self._idle.clear()
            self._counts.clear()

    async def _connect(self, dc_id: int) -> MTProtoSender:
        client = self._client
        auth_key = self._auth_keys.get(dc_id)
        if auth_key is None and client.session.dc_id == dc_id:
            auth_key = client.session.auth_key
        dc = await client._get_dc(dc_id)
        sender = MTProtoSender(auth_key, loggers=client._log)
        await sender.connect(
            client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=client._log,
                proxy=client._proxy,
                local_addr=client._local_addr,
            )
        )
        if auth_key is None:
            auth = await client(ExportAuthorizationRequest(dc_id))
            init = client._init_request
            await sender.send(
                InvokeWithLayerRequest(
                    LAYER,
                    InitConnectionRequest(
                        api_id=client.api_id,
                        device_model=init.device_model,
                        system_version=init.system_version,
                        app_version=init.app_version,
                        system_lang_code=init.system_lang_code,
                        lang_pack=init.lang_pack,
                        lang_code=init.lang_code,
                        proxy=init.proxy,
                        params=init.params,
                        query=ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes),
                    ),
                )
            )
        self._auth_keys[dc_id] = sender.auth_key
        return sender


class _StridedRequest:
    """Fetches every Nth part of a file on one dedicated sender."""

    def __init__(self, client, sender, location, offset: int, stride: int, count: int):
        self._client = client
        self._sender = sender
        self._request = GetFileRequest(location, offset=offset, limit=PART_SIZE)
        self._stride = stride
        self.remaining = count

    async def next(self) -> bytes:
        result = await self._client._call(self._sender, self._request)
        if not isinstance(result, File):
            raise RuntimeError("Unsupported upload.getFile result (CDN redirect)")
        self.remaining -= 1
        self._request.offset += self._stride
        return result.bytes


async def try_parallel_download(
    client: TelegramClient,
    pool: SenderPool,
    media,
    *,
    offset: int,
    limit: int,
) -> AsyncIterator[bytes] | None:
    """Return an ordered chunk iterator using pooled connections, or None
    when parallelism is unavailable (pool disabled/exhausted, tiny range,
    or media without a direct file location)."""
    if not pool.enabled or limit <= PART_SIZE:
        return None
    try:
        dc_id, location = utils.get_input_location(media)
    except Exception:
        return None
    aligned = offset - offset % PART_SIZE
    part_count = -(-(offset + limit - aligned) // PART_SIZE)
    if part_count < 2:
        return None
    senders = await pool.acquire(dc_id, part_count)
    if not senders:
        return None
    return _chunks(client, pool, dc_id, senders, location, aligned, offset, limit, part_count)


async def _chunks(client, pool, dc_id, senders, location, aligned, offset, limit, part_count):
    stride = len(senders) * PART_SIZE
    requests = [
        _StridedRequest(
            client,
            sender,
            location,
            aligned + index * PART_SIZE,
            stride,
            part_count // len(senders) + (1 if index < part_count % len(senders) else 0),
        )
        for index, sender in enumerate(senders)
    ]
    broken = False
    try:
        part = 0
        skip = offset - aligned
        remaining = limit
        while part < part_count and remaining > 0:
            tasks = [
                asyncio.ensure_future(request.next())
                for request in requests
                if request.remaining > 0
            ]
            try:
                for task in tasks:
                    data = await task
                    part += 1
                    if part < part_count and len(data) != PART_SIZE:
                        raise RuntimeError(
                            f"Short read from Telegram: part {part}/{part_count}, {len(data)} bytes"
                        )
                    if skip:
                        data = data[skip:]
                        skip = 0
                    if len(data) > remaining:
                        data = data[:remaining]
                    remaining -= len(data)
                    yield data
                    if remaining <= 0:
                        break
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        # ponytail: any abort (error or client disconnect) discards the borrowed
        # senders instead of proving they are still clean; reconnects are cheap
        broken = True
        raise
    finally:
        await pool.release(dc_id, senders, broken=broken)
