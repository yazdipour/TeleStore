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
        main_module.sources_by_slug = {source.slug: source for source in sources}

    async def test_reused_slug_does_not_inherit_previous_channel_icon(self):
        old, new = _source("oldchannel"), _source("newchannel")
        fake = FakeTelegram({"oldchannel": b"OLD", "newchannel": b"NEW"})
        with patch.object(main_module, "telegram", fake):
            self._use(old)
            self.assertEqual(await main_module._source_channel_icon(old), b"OLD")
            # Same channel again: served from cache, no second download.
            self.assertEqual(await main_module._source_channel_icon(old), b"OLD")
            self.assertEqual(fake.calls, 1)
            self.assertTrue(main_module._icon_cache_path("oldchannel").is_file())

            # Slug reassigned to another channel: must download, not reuse OLD.
            self._use(new)
            self.assertEqual(await main_module._source_channel_icon(new), b"NEW")
            self.assertEqual(fake.calls, 2)

    async def test_case_change_reuses_cached_icon(self):
        fake = FakeTelegram({"Example": b"ICON", "example": b"OTHER"})
        with patch.object(main_module, "telegram", fake):
            self._use(_source("Example"))
            self.assertEqual(await main_module._source_channel_icon(_source("Example")), b"ICON")
            self._use(_source("example"))
            self.assertEqual(await main_module._source_channel_icon(_source("example")), b"ICON")
        self.assertEqual(fake.calls, 1)

    async def test_download_error_falls_back_and_backs_off(self):
        source = _source("example")
        self._use(source)

        class Failing(FakeTelegram):
            async def download_channel_photo(self, source):
                self.calls += 1
                raise ConnectionError("telegram down")

        fake = Failing({})
        with patch.object(main_module, "telegram", fake):
            self.assertIsNone(await main_module._source_channel_icon(source))
            self.assertIsNone(await main_module._source_channel_icon(source))
        self.assertEqual(fake.calls, 1)

    async def test_icon_for_reassigned_slug_is_not_served_or_recorded(self):
        old, new = _source("oldchannel"), _source("newchannel")
        self._use(old)
        test = self

        class SlowTelegram(FakeTelegram):
            async def download_channel_photo(self, source):
                self.calls += 1
                test._use(new)  # config changes while Telegram is answering
                return self.icons.get(source.channel)

        with patch.object(main_module, "telegram", SlowTelegram({"oldchannel": b"OLD"})):
            self.assertIsNone(await main_module._source_channel_icon(old))
        self.assertEqual(main_module.source_icon_caches, {})
        self.assertEqual(main_module.source_icon_retry_after, {})

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
        self._use(_source("other", slug="other"))
        with patch.object(main_module, "telegram", fake):
            self.assertIsNone(await main_module._source_channel_icon(source))
        self.assertEqual(main_module.source_icon_caches, {})
        self.assertEqual(list(Path(self.tmpdir.name).iterdir()), [])

    def test_prune_removes_only_icon_files_of_unconfigured_channels(self):
        self._use(_source("keep"))
        cache_dir = Path(self.tmpdir.name)
        kept = main_module._icon_cache_path("keep")
        kept.write_bytes(b"K")
        main_module._icon_cache_path("drop").write_bytes(b"D")
        (cache_dir / "legacy-slug.img").write_bytes(b"L")
        (cache_dir / ".legacy-slug.img.tmp").write_bytes(b"T")
        unrelated = [cache_dir / "telegram.session", cache_dir / "notes.txt"]
        for path in unrelated:
            path.write_bytes(b"keep me")
        main_module._prune_icon_cache()
        self.assertEqual(sorted(cache_dir.iterdir()), sorted([kept, *unrelated]))

    def test_refresh_drops_locks_of_removed_channels(self):
        self._use(_source("keep"))
        main_module.source_icon_locks[main_module._icon_key("keep")] = asyncio.Lock()
        main_module.source_icon_locks[main_module._icon_key("gone")] = asyncio.Lock()
        with patch.object(main_module.settings_module, "reload_config"), patch.object(
            main_module, "load_settings", return_value=main_module.settings
        ):
            main_module._runtime_refresh()
        self.assertEqual(list(main_module.source_icon_locks), [main_module._icon_key("keep")])

    def test_write_failure_with_failing_cleanup_does_not_raise(self):
        path = Path(self.tmpdir.name) / "icon.img"
        with patch.object(Path, "write_bytes", side_effect=OSError("disk full")) as write, patch.object(
            Path, "unlink", side_effect=OSError("busy")
        ) as unlink:
            main_module._write_icon_cache(path, b"ICON")
        write.assert_called_once_with(b"ICON")
        unlink.assert_called_once()
        self.assertFalse(path.exists())

    def test_write_stores_icon(self):
        path = Path(self.tmpdir.name) / "sub" / "icon.img"
        main_module._write_icon_cache(path, b"ICON")
        self.assertEqual(path.read_bytes(), b"ICON")

    def test_icon_url_changes_when_slug_moves_to_another_channel(self):
        old_url = main_module._source_icon_url(_source("oldchannel"))
        new_url = main_module._source_icon_url(_source("newchannel"))
        self.assertNotEqual(old_url, new_url)
        self.assertTrue(old_url.split("?")[0] == new_url.split("?")[0])


class DuplicateChannelTests(TestCase):
    def test_duplicate_channels_produce_one_source(self):
        original = settings_module.CONFIG
        settings_module.CONFIG = {**original, "channels": ["ethModsGroup", "other", "@ethmodsgroup"]}
        try:
            sources = settings_module._load_sources()
        finally:
            settings_module.CONFIG = original
        self.assertEqual([source.channel for source in sources], ["ethModsGroup", "other"])

    def test_invite_link_forms_are_one_channel(self):
        self.assertEqual(
            settings_module.channel_identity("+AbC123"), settings_module.channel_identity("joinchat/AbC123")
        )
        self.assertNotEqual(
            settings_module.channel_identity("+AbC123"), settings_module.channel_identity("+abc123")
        )


class RemoveChannelTests(IsolatedAsyncioTestCase):
    async def test_remove_drops_all_duplicates_of_only_channel_pair(self):
        saved = []
        original = settings_module.CONFIG
        settings_module.CONFIG = {**original, "channels": ["dup", "@DUP", "other"]}

        class FakeRequest:
            async def body(self):
                return b"index=0"

        try:
            with patch.object(main_module, "_save_channels", side_effect=saved.append), patch.object(
                main_module, "_config_ui_enabled"
            ):
                response = await main_module.config_remove_channel(FakeRequest())
        finally:
            settings_module.CONFIG = original
        self.assertEqual(response.status_code, 303)
        self.assertEqual(saved, [["other"]])

    async def test_remove_last_distinct_channel_is_refused(self):
        saved = []
        original = settings_module.CONFIG
        settings_module.CONFIG = {**original, "channels": ["dup", "dup"]}

        class FakeRequest:
            async def body(self):
                return b"index=0"

        async def authorized():
            return True

        try:
            with patch.object(main_module, "_save_channels", side_effect=saved.append), patch.object(
                main_module, "_config_ui_enabled"
            ), patch.object(main_module.telegram, "is_authorized", authorized):
                response = await main_module.config_remove_channel(FakeRequest())
        finally:
            settings_module.CONFIG = original
        self.assertEqual(response.status_code, 400)
        self.assertEqual(saved, [])
