import asyncio
from dataclasses import replace
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from tests.test_telegram_client import _write_test_config

_write_test_config()

import src.main as main_module  # noqa: E402
import src.settings as settings_module  # noqa: E402
from src.settings import SourceConfig  # noqa: E402


def _source(channel: str, slug: str = "example") -> SourceConfig:
    return SourceConfig(channel=channel, slug=slug, name=channel, tint_color="#1D9BF0")


class FakeTelegram:
    def __init__(self, icons):
        self.icons = icons
        self.calls = 0

    async def is_authorized(self):
        return True

    async def download_channel_photo(self, source):
        self.calls += 1
        await asyncio.sleep(0)
        return self.icons.get(source.channel)


class IconCacheTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.orig_settings = main_module.settings
        main_module.source_icon_caches.clear()
        main_module.source_icon_locks.clear()
        main_module.source_icon_retry_after.clear()

    def tearDown(self):
        main_module.settings = self.orig_settings
        main_module.source_icon_caches.clear()
        main_module.source_icon_locks.clear()
        main_module.source_icon_retry_after.clear()
        self.tmpdir.cleanup()

    def _use(self, *sources):
        main_module.settings = replace(
            self.orig_settings, icon_cache_dir=self.tmpdir.name, sources=tuple(sources)
        )

    async def test_reused_slug_does_not_inherit_previous_channel_icon(self):
        old, new = _source("oldchannel"), _source("newchannel")
        fake = FakeTelegram({"oldchannel": b"OLD", "newchannel": b"NEW"})
        with patch.object(main_module, "telegram", fake):
            self._use(old)
            self.assertEqual(await main_module._source_channel_icon(old), b"OLD")
            self._use(new)
            self.assertEqual(await main_module._source_channel_icon(new), b"NEW")

    async def test_concurrent_misses_share_one_download(self):
        source = _source("example")
        fake = FakeTelegram({"example": b"ICON"})
        self._use(source)
        with patch.object(main_module, "telegram", fake):
            results = await asyncio.gather(*(main_module._source_channel_icon(source) for _ in range(5)))
        self.assertEqual(results, [b"ICON"] * 5)
        self.assertEqual(fake.calls, 1)

    async def test_failed_fetch_is_not_retried_immediately(self):
        source = _source("example")
        fake = FakeTelegram({})
        self._use(source)
        with patch.object(main_module, "telegram", fake):
            self.assertIsNone(await main_module._source_channel_icon(source))
            self.assertIsNone(await main_module._source_channel_icon(source))
        self.assertEqual(fake.calls, 1)

    async def test_icon_for_removed_channel_is_not_cached(self):
        source = _source("gone")
        fake = FakeTelegram({"gone": b"ICON"})
        self._use(_source("other"))
        with patch.object(main_module, "telegram", fake):
            self.assertEqual(await main_module._source_channel_icon(source), b"ICON")
        self.assertNotIn("gone", main_module.source_icon_caches)
        self.assertEqual(list(Path(self.tmpdir.name).iterdir()), [])

    def test_prune_removes_icons_of_unconfigured_channels(self):
        keep, drop = _source("keep"), _source("drop")
        self._use(keep)
        cache_dir = Path(self.tmpdir.name)
        kept = main_module._icon_cache_path("keep")
        kept.write_bytes(b"K")
        main_module._icon_cache_path("drop").write_bytes(b"D")
        (cache_dir / "legacy-slug.img").write_bytes(b"L")
        main_module._prune_icon_cache()
        self.assertEqual(list(cache_dir.iterdir()), [kept])

    def test_write_failure_with_failing_cleanup_does_not_raise(self):
        path = Path(self.tmpdir.name) / "icon.img"
        with patch.object(Path, "write_bytes", side_effect=OSError("disk full")), patch.object(
            Path, "unlink", side_effect=OSError("busy")
        ):
            main_module._write_icon_cache(path, b"ICON")


class DuplicateChannelTests(TestCase):
    def test_duplicate_channels_produce_one_source(self):
        original = settings_module.CONFIG
        settings_module.CONFIG = {**original, "channels": ["ethModsGroup", "other", "@ethmodsgroup"]}
        try:
            sources = settings_module._load_sources()
        finally:
            settings_module.CONFIG = original
        self.assertEqual([source.channel for source in sources], ["ethModsGroup", "other"])
