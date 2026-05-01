import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase


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
    os.environ["CONFIG_FILE"] = str(path)


_write_test_config()

from src.settings import Settings, SourceConfig, normalize_channel  # noqa: E402
from src.source_builder import _app_from_message, _dedupe_latest, _is_ipa_message  # noqa: E402


def make_settings() -> Settings:
    return Settings(
        telegram_api_id=123,
        telegram_api_hash="hash",
        sources=(),
        telegram_limit=100,
        telegram_session="/tmp/session",
        base_url="https://apps.example.test",
        cache_seconds=600,
        host="0.0.0.0",
        ui_config=True,
        ipa_cache_dir="/tmp/cache",
        ipa_cache_workers=4,
        ipa_cache_global_workers=8,
        ipa_cache_part_size=8 * 1024 * 1024,
    )


def make_source() -> SourceConfig:
    return SourceConfig(
        channel="example",
        slug="example",
        name="Example",
        tint_color="#1D9BF0",
    )


def make_message(*, filename="Example_App.ipa", text="", size=1234):
    return SimpleNamespace(
        date=datetime(2026, 4, 1, 12, 30, tzinfo=UTC),
        file=SimpleNamespace(name=filename, size=size),
        message=text,
    )


class SourceBuilderTests(TestCase):
    def test_normalize_channel_accepts_handles_and_tme_urls(self):
        self.assertEqual(normalize_channel("@ExampleChannel"), "ExampleChannel")
        self.assertEqual(normalize_channel("https://t.me/example/123"), "example")
        self.assertEqual(normalize_channel("www.t.me/example"), "example")

    def test_is_ipa_message_accepts_filename_or_required_caption_fields(self):
        self.assertTrue(_is_ipa_message(make_message(filename="App.ipa")))
        self.assertTrue(
            _is_ipa_message(
                make_message(
                    filename="document.bin",
                    text="Bundle ID: com.example.app\nUpdated To: 2.0",
                )
            )
        )
        self.assertFalse(_is_ipa_message(make_message(filename="document.bin", text="hello")))

    def test_app_from_message_parses_altstore_metadata(self):
        app = _app_from_message(
            make_settings(),
            make_source(),
            55,
            make_message(
                text=(
                    "Bundle ID: com.example.app\n"
                    "Updated To: 2.3.4\n"
                    "Minimum iOS: 16.0\n"
                    "Modifier: Tweaked"
                )
            ),
        )

        version = app["versions"][0]
        self.assertEqual(app["name"], "Example App")
        self.assertEqual(app["bundleIdentifier"], "com.example.app")
        self.assertEqual(app["subtitle"], "Tweaked")
        self.assertEqual(version["version"], "2.3.4")
        self.assertEqual(version["minOSVersion"], "16.0")
        self.assertEqual(version["downloadURL"], "https://apps.example.test/ipa/example/55/Example_App.ipa")
        self.assertEqual(app["iconURL"], "https://apps.example.test/icon/example/55.jpg")

    def test_dedupe_latest_keeps_newest_version_for_each_bundle(self):
        old = {"bundleIdentifier": "com.example.app", "versions": [{"version": "1.9", "date": "2026-01-01"}]}
        new = {"bundleIdentifier": "com.example.app", "versions": [{"version": "2.0", "date": "2026-01-02"}]}
        other = {"bundleIdentifier": "com.example.other", "versions": [{"version": "1.0", "date": "2026-01-01"}]}

        self.assertEqual(_dedupe_latest([old, other, new]), [new, other])
