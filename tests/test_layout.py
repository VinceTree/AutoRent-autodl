"""Package paths, runtime state, and Windows launchers after moving directories."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from autodl_quick import catalog
from autodl_quick.core import ROOT, RentalJournal


class RuntimePathTests(unittest.TestCase):
    def test_existing_rental_records_keep_the_same_location(self):
        project = Path(__file__).resolve().parents[1]
        self.assertEqual(ROOT, project)
        self.assertEqual(RentalJournal().path, project / ".state" / "rental.json")

    def test_missing_cache_loads_bundled_catalog(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(catalog, "CACHE", Path(temp) / "missing.json"):
            loaded = catalog.Catalog.load()
        self.assertIn("RTX 5090", loaded.gpu_names())
        self.assertTrue(loaded.resolve_regions())

    def test_local_cache_takes_priority_over_bundled_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "catalog.json"
            cache.write_text(json.dumps({"regions": [], "models": [{"name": "New GPU", "is_cpu": False}]}), encoding="utf-8")
            with patch.object(catalog, "CACHE", cache):
                self.assertEqual(catalog.Catalog.load().gpu_names(), ["New GPU"])

    def test_refresh_updates_runtime_cache_without_changing_bundled_snapshot(self):
        snapshot_before = hashlib.sha256(catalog.BUNDLED_CATALOG.read_bytes()).digest()
        responses = [
            {"code": "Success", "data": [{"data_center": "test-dc", "region_name": "测试区", "region_sign": "test-room"}]},
            {"code": "Success", "data": [{"gpu_name": "New GPU"}]},
        ]
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / ".state" / "catalog.json"
            with patch.object(catalog, "CACHE", cache), patch("autodl_quick.catalog.urllib.request.urlopen", side_effect=[io.BytesIO(json.dumps(item).encode()) for item in responses]):
                catalog.Catalog.refresh()
                self.assertEqual(catalog.Catalog.load().gpu_names(), ["New GPU"])
                self.assertTrue(cache.exists())
        self.assertEqual(hashlib.sha256(catalog.BUNDLED_CATALOG.read_bytes()).digest(), snapshot_before)


@unittest.skipUnless(sys.platform == "win32", "Windows launchers")
class WindowsLauncherTests(unittest.TestCase):
    def test_root_launcher_forwards_flags_and_uses_project_directory(self):
        with tempfile.TemporaryDirectory(prefix="autodl launcher ") as temp:
            work = Path(temp)
            fake_bin = work / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "python.cmd").write_text('@echo off\necho %CD%>"%AUTODL_LAUNCH_CWD%"\necho %*>"%AUTODL_LAUNCH_ARGS%"\nexit /b 0\n', encoding="utf-8")
            cwd_result = work / "cwd.txt"
            args_result = work / "args.txt"
            env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
                       AUTODL_LAUNCH_CWD=str(cwd_result), AUTODL_LAUNCH_ARGS=str(args_result))
            for language_args in ([], ["-Lang", "en"]):
                result = subprocess.run(["cmd.exe", "/d", "/c", str(ROOT / "start.cmd"), "-Demo", "-ReadOnly", *language_args],
                                        cwd=work, env=env, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(Path(cwd_result.read_text().strip()), ROOT)
                expected = ["-m", "autodl_quick", "gui", "--demo", "--read-only"] + (["--lang", "en"] if language_args else [])
                self.assertEqual(args_result.read_text().split(), expected)


if __name__ == "__main__":
    unittest.main()
