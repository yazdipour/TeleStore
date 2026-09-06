from dataclasses import replace
import os
import tempfile
import time
from pathlib import Path
from unittest import TestCase

from tests.test_telegram_client import _write_test_config

_write_test_config()

import src.main as main_module  # noqa: E402
from src.main import _sweep_ipa_cache_once  # noqa: E402


class CacheSweepTests(TestCase):
    def test_sweep_evicts_oldest_files_over_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_dir = main_module.settings.ipa_cache_dir
            orig_max = main_module.settings.ipa_cache_max_bytes

            # 150 bytes limit
            main_module.settings = replace(
                main_module.settings,
                ipa_cache_dir=tmpdir,
                ipa_cache_max_bytes=150,
            )

            try:
                # Create 3 files of 100 bytes each with different mtimes
                file1 = Path(tmpdir) / "file1.ipa"
                file2 = Path(tmpdir) / "file2.ipa"
                file3 = Path(tmpdir) / "file3.ipa"

                file1.write_bytes(b"A" * 100)
                file2.write_bytes(b"B" * 100)
                file3.write_bytes(b"C" * 100)

                now = time.time()
                os.utime(file1, (now - 300, now - 300))
                os.utime(file2, (now - 200, now - 200))
                os.utime(file3, (now - 100, now - 100))

                _sweep_ipa_cache_once()

                # file1 (oldest) and file2 should be evicted so total <= 150 (only file3 remains = 100 bytes)
                self.assertFalse(file1.exists())
                self.assertFalse(file2.exists())
                self.assertTrue(file3.exists())
            finally:
                main_module.settings = replace(
                    main_module.settings,
                    ipa_cache_dir=orig_dir,
                    ipa_cache_max_bytes=orig_max,
                )

    def test_sweep_proactively_evicts_for_incoming_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_dir = main_module.settings.ipa_cache_dir
            orig_max = main_module.settings.ipa_cache_max_bytes

            # 250 bytes limit
            main_module.settings = replace(
                main_module.settings,
                ipa_cache_dir=tmpdir,
                ipa_cache_max_bytes=250,
            )

            try:
                file1 = Path(tmpdir) / "file1.ipa"
                file2 = Path(tmpdir) / "file2.ipa"

                file1.write_bytes(b"A" * 100)
                file2.write_bytes(b"B" * 100)

                now = time.time()
                os.utime(file1, (now - 200, now - 200))
                os.utime(file2, (now - 100, now - 100))

                # Currently 200 bytes cached. Incoming file is 100 bytes.
                # Total would become 300 > 250 limit.
                # Sweeping with incoming_bytes=100 will make target_limit = 150, evicting file1.
                _sweep_ipa_cache_once(incoming_bytes=100)

                self.assertFalse(file1.exists())
                self.assertTrue(file2.exists())
            finally:
                main_module.settings = replace(
                    main_module.settings,
                    ipa_cache_dir=orig_dir,
                    ipa_cache_max_bytes=orig_max,
                )
