"""Switch languages on a live window while preserving user input and work."""

import os
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

from autodl_quick.core import Settings
from autodl_quick.ui.main import App


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "GUI requires a display")
class LanguageSwitchTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, Settings(expand_disk_gb=100), demo=True, read_only=True)
        self.addCleanup(self.app.close)

    def test_top_buttons_switch_both_directions_without_losing_input_or_selection(self):
        app = self.app
        app.token.set("local-test-token")
        app.region.set("北京B区")
        app.region_changed()
        app.gpu.set("RTX 5090")
        app.gpu_changed()
        app.disk.set("150")
        conditions = app.settings()
        app.status.set(app.tr("演示数据 · 完全不联网"))
        self.assertEqual(app.language, "zh")
        self.assertEqual(app.language_switch.zh_button.cget("text"), "中文")
        self.assertEqual(app.language_switch.en_button.cget("text"), "en")
        for _ in range(2):
            app.language_switch.en_button.invoke()
            self.assertEqual(app.language, "en")
            self.assertEqual(app.scan_button.cget("text"), "Scan stock")
            self.assertIn("GPU finder", self.root.title())
            self.assertIn("No network requests", app.status.get())
            self.assertEqual(app.token.get(), "local-test-token")
            self.assertEqual(app.settings(), conditions)
            self.assertIn("disabled", app.rent_button.state())
            app.language_switch.zh_button.invoke()
            self.assertEqual(app.language, "zh")
            self.assertEqual(app.scan_button.cget("text"), "扫描当前余量")
            self.assertEqual(app.status.get(), "演示数据 · 完全不联网")
            self.assertEqual(app.settings(), conditions)

    def test_selected_host_details_and_logs_follow_language_without_replacing_widgets(self):
        app = self.app
        app.table.selection_set("1")
        app.show_selected()
        table, rows = app.table, app.rows
        app.write_log(app.tr("当前无满足条件的剩余卡，{p0} 秒后自动重查", p0=30))
        original_details = app.details.get()
        app.set_language("en")
        self.assertIs(app.table, table)
        self.assertIs(app.rows, rows)
        self.assertEqual(app.table.selection(), ("1",))
        self.assertEqual(app.table.heading("gpu", "text"), "Available GPU")
        self.assertIn("Beijing B (sample)", app.table.item("1", "values"))
        self.assertIn("Data disk:", app.details.get())
        self.assertIn("Beijing B (sample)", app.host_subtitle.get())
        self.assertIn("No matching GPU stock", app.log.get("1.0", "end"))
        app.show_selected()
        self.assertIn("Beijing B (sample)", app.host_subtitle.get())
        app.set_language("zh")
        self.assertEqual(app.details.get(), original_details)
        self.assertIn("西北B区（示例）", app.table.item("0", "values"))
        self.assertIn("30 秒后自动重查", app.log.get("1.0", "end"))

    def test_english_start_can_switch_back_to_chinese_without_changing_all_scope(self):
        window = tk.Toplevel(self.root)
        app = App(window, demo=True, language="en")
        try:
            conditions = app.settings()
            app.language_switch.zh_button.invoke()
            self.assertEqual(app.region.get(), "全部公开地区")
            self.assertEqual(app.gpu.get(), "全部 GPU 型号")
            self.assertEqual(app.settings(), conditions)
            self.assertIn("示例 CPU", app.details.get())
            self.assertIn("西北B区（示例）", app.host_subtitle.get())
            self.assertIn("扫描当前余量", app.scan_button.cget("text"))
        finally:
            app.close()

    def test_switch_during_work_preserves_worker_controls_and_incoming_messages(self):
        app = self.app
        app.busy = True
        app.scan_button.configure(state="disabled")
        app.stop_button.configure(state="normal")
        app.activity.configure(text=app.tr("自动租卡中"))
        original_queue, original_cancel = app.events, app.cancel
        try:
            app.set_language("en")
            self.assertTrue(app.busy)
            self.assertIs(app.events, original_queue)
            self.assertIs(app.cancel, original_cancel)
            self.assertFalse(app.cancel.is_set())
            self.assertIn("disabled", app.scan_button.state())
            self.assertNotIn("disabled", app.stop_button.state())
            self.assertEqual(app.activity.cget("text"), "Renting automatically")
            app.events.put(("created", {"instance_uuid": "sample-instance"}))
            app.events.put(("done", "complete"))
            app.drain()
            self.assertIn("Rented one instance and stopped", app.status.get())
            app.set_language("zh")
            self.assertEqual(app.status.get(), "已租用一台，自动停止：sample-instance")
            app.set_language("en")
            self.assertIn("Rented one instance and stopped", app.status.get())
        finally:
            app.busy = False

    def test_open_advanced_window_switches_with_main_and_retains_preferences(self):
        app = self.app
        with patch("autodl_quick.ui.advanced.AutoDL"):
            app.advanced_button.invoke()
        editor = app.advanced_editors[0]
        editor.price.set("4.50")
        editor.platform.set("摩尔线程 / MUSA")
        editor.platform_changed()
        editor.strategy.set("按型号勾选顺序")
        preferences = editor.get_settings()
        app.language_switch.en_button.invoke()
        self.assertEqual(editor.language, "en")
        self.assertEqual(editor.save_button.cget("text"), "Apply preferences")
        self.assertEqual(editor.platform.get(), "Moore Threads / MUSA")
        self.assertEqual(editor.strategy.get(), "GPU selection order")
        self.assertEqual(editor.get_settings(), preferences)
        editor.language_switch.zh_button.invoke()
        self.assertEqual(app.language, "zh")
        self.assertEqual(editor.language, "zh")
        self.assertEqual(editor.get_settings(), preferences)
        editor.close()
        app.set_language("en")  # A closed preference window must not prevent switching.


if __name__ == "__main__":
    unittest.main()
