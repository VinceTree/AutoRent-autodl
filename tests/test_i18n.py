"""English presentation must preserve exact filters, units and read-only behavior."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
from string import Formatter
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from autodl_quick.core import Engine, GIB, RentalJournal, Settings, main
from autodl_quick.i18n import Translator, english_catalog
from test_disk import disk_api


class TranslationTests(unittest.TestCase):
    def test_formats_and_units_are_preserved_in_every_translation(self):
        def fields(text):
            return {field for _, field, _, _ in Formatter().parse(text) if field is not None}
        for source, target in english_catalog().items():
            with self.subTest(source=source):
                self.assertEqual(fields(source), fields(target))

    def test_dynamic_backend_errors_are_translated_with_parameters(self):
        self.assertEqual(Translator("en")("当前无满足条件的剩余卡，30 秒后自动重查"),
                         "No matching GPU stock; check again in 30 seconds")
        self.assertEqual(Translator("en")("查询暂时失败，继续等待：网络或响应异常，可稍后重试查询"),
                         "Query temporarily failed; keep waiting: Network or response error; retry the query later")

    def test_chinese_and_english_translators_do_not_share_window_language(self):
        self.assertEqual(Translator("en")("扫描当前余量"), "Scan stock")
        self.assertEqual(Translator("zh")("扫描当前余量"), "扫描当前余量")
        self.assertEqual(Translator("zh")("发现 {p0} 台有余量的主机。", p0=2), "发现 2 台有余量的主机。")

    def test_official_identifiers_are_unchanged(self):
        for name in ("北京B区", "RTX 5090 D", "west-B", "sh-A1", "MTT S4000"):
            self.assertEqual(Translator("en")(name), name)

    def test_english_cli_cannot_rent_in_read_only_mode(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["autodl_quick", "rent", "--read-only", "--execute", "--lang", "en"]), patch("autodl_quick.core.Settings.load", return_value=Settings()), patch("autodl_quick.core.AutoDL") as api, redirect_stdout(output):
            self.assertEqual(main(), 1)
        api.assert_not_called()
        self.assertIn("Read-only mode blocks renting", output.getvalue())

    def test_cli_passes_english_to_gui_without_starting_any_network_call(self):
        chosen = Settings()
        with patch.object(sys, "argv", ["autodl_quick", "gui", "--demo", "--lang", "en"]), patch("autodl_quick.core.Settings.load", return_value=chosen), patch("autodl_quick.ui.run") as run:
            self.assertEqual(main(), 0)
        run.assert_called_once_with(chosen, demo=True, read_only=False, language="en")

    def test_cli_defaults_to_chinese_without_language_flag(self):
        chosen = Settings()
        with patch.object(sys, "argv", ["autodl_quick", "gui", "--demo"]), patch("autodl_quick.core.Settings.load", return_value=chosen), patch("autodl_quick.ui.run") as run:
            self.assertEqual(main(), 0)
        run.assert_called_once_with(chosen, demo=True, read_only=False, language="zh")

@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "GUI requires a display")
class EnglishWindowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from autodl_quick.ui.main import App
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, read_only=True, language="en")
        self.addCleanup(self.app.close)

    def test_controls_and_all_options_are_english(self):
        self.assertEqual(self.app.rent_button.cget("text"), "Start auto-rent (billed)")
        self.assertEqual(self.app.region.get(), "All public regions")
        self.assertEqual(self.app.gpu.get(), "All GPU models")
        self.assertIn("disabled", self.app.rent_button.state())
        self.assertEqual(self.app.settings().gpu_names, [])
        self.assertEqual(len(self.app.settings().regions), 18)

    def test_default_windows_remain_chinese_next_to_an_english_window(self):
        import tkinter as tk
        from autodl_quick.ui.main import App
        from autodl_quick.ui.advanced import App as AdvancedApp
        window = tk.Toplevel(self.root)
        window.withdraw()
        chinese = App(window, read_only=True)
        self.addCleanup(chinese.close)
        self.assertEqual(chinese.region.get(), "全部公开地区")
        self.assertEqual(chinese.rent_button.cget("text"), "开始自动租卡（计费）")
        advanced_window = tk.Toplevel(self.root)
        advanced_window.withdraw()
        advanced = AdvancedApp(advanced_window, Settings(), read_only=True)
        self.addCleanup(advanced.close)
        self.assertEqual(advanced.rent_button.cget("text"), "开始库存演练")
        self.assertEqual(self.app.rent_button.cget("text"), "Start auto-rent (billed)")

    def test_region_model_and_disk_selection_keep_exact_backend_values(self):
        self.app.region.set("北京B区")
        self.app.region_changed()
        self.app.gpu.set("RTX 5090 D")
        self.app.gpu_changed()
        self.app.disk.set("100")
        chosen = self.app.settings()
        self.assertEqual(chosen.regions, ["bj-B1", "bj-B2", "bj-B3"])
        self.assertEqual(chosen.gpu_names, ["RTX 5090 D"])
        self.assertEqual(chosen.expand_disk_gb, 100)

    def test_english_log_translates_errors_and_still_redacts_token(self):
        self.app.token.set("fake-test-token")
        self.app.write_log("当前无满足条件的剩余卡，30 秒后自动重查")
        self.app.write_log("Server message containing fake-test-token")
        log = self.app.log.get("1.0", "end")
        self.assertIn("No matching GPU stock", log)
        self.assertNotIn("fake-test-token", log)
        self.assertIn("[redacted]", log)

    def test_english_advanced_preferences_keep_canonical_platform_and_strategy(self):
        import tkinter as tk
        from autodl_quick.ui.advanced import App
        window = tk.Toplevel(self.root)
        window.withdraw()
        editor = App(window, Settings(), read_only=True, language="en")
        self.addCleanup(editor.close)
        editor.platform.set("Moore Threads / MUSA")
        editor.platform_changed()
        editor.strategy.set("GPU selection order")
        chosen = editor.get_settings()
        self.assertEqual(chosen.image_chip, "mthreads")
        self.assertEqual(chosen.strategy, "gpu_priority")
        editor.launch(lambda: None)
        deadline = time.monotonic() + 3
        while editor.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertIn("Query / rehearsal complete", editor.status.get())

    def test_english_rent_button_uses_same_exact_scope_and_expansion(self):
        import tkinter as tk
        from autodl_quick.ui.main import App
        window = tk.Toplevel(self.root)
        window.withdraw()
        app = App(window, language="en")
        self.addCleanup(app.close)
        app.region.set("西北B区")
        app.region_changed()
        app.gpu.set("RTX 5090")
        app.gpu_changed()
        app.disk.set("100")
        app.token.set("fake-test-token")
        api = disk_api()
        with tempfile.TemporaryDirectory() as temp, patch("autodl_quick.ui.main.AutoDL", return_value=api), patch("autodl_quick.ui.main.ROOT", Path(temp)), patch("autodl_quick.ui.main.Catalog.refresh", return_value=app.catalog), patch("autodl_quick.ui.main.Engine", side_effect=lambda client, emit: Engine(client, emit, journal=RentalJournal(Path(temp) / "rental"))):
            app.rent_button.invoke()
            deadline = time.monotonic() + 5
            while app.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertFalse(app.busy)
            api.create.assert_called_once()
            payload = api.create.call_args.args[0]
            self.assertEqual(payload["instance_info"]["machine_id"], "test-host")
            self.assertEqual(payload["instance_info"]["expand_data_disk"], 100 * GIB)
            scope = api.machine_batches.call_args.args[0]
            self.assertEqual(scope.gpu_names, ["RTX 5090"])
            self.assertEqual(scope.regions, ["west-B", "west-C", "west-D", "west-E"])
            self.assertIn("Rented one instance and stopped", app.status.get())
            self.assertIn("Data disk: 50 free + 100 added = 150 GB", app.details.get())
            self.assertIn("CNY 0.67/day", app.details.get())


if __name__ == "__main__":
    unittest.main()
