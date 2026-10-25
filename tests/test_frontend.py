"""Check the desktop redesign's layout and visible task lifecycle offline."""

import os
import sys
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from autodl_quick.core import Settings
from autodl_quick.ui.main import App


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "GUI requires a display")
class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, Settings(expand_disk_gb=100), demo=True)
        self.addCleanup(self.app.close)

    def test_small_window_keeps_actions_inventory_and_log_visible_in_both_languages(self):
        for language in ("zh", "en"):
            with self.subTest(language=language):
                window = tk.Toplevel(self.root)
                app = App(window, demo=True, language=language)
                try:
                    window.geometry("1040x740")
                    window.update()
                    bottom = window.winfo_rooty() + window.winfo_height()
                    for widget in (app.scan_button, app.wait_button, app.rent_button, app.stop_button, app.log):
                        self.assertTrue(widget.winfo_ismapped())
                        self.assertGreaterEqual(widget.winfo_rooty(), window.winfo_rooty())
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom)
                    self.assertGreaterEqual(app.table.winfo_height(), 120)
                    self.assertGreater(app.log.winfo_height(), 40)
                finally:
                    app.close()

    def test_empty_results_and_filter_changes_do_not_keep_previous_configuration(self):
        self.root.deiconify()
        self.root.update()
        self.app.show_cards([])
        self.root.update()
        self.assertTrue(self.app.empty_state.winfo_ismapped())
        self.assertFalse(self.app.detail_content.winfo_ismapped())
        self.assertIn("disabled", self.app.copy_button.state())
        self.assertIn("暂无", self.app.result_count.get())
        self.app.disk.set("200")
        self.assertEqual(self.app.rows, [])
        self.assertNotIn("demo-host", self.app.details.get())
        self.assertIn("等待扫描", self.app.result_count.get())

    def test_copy_exports_selected_host_configuration_without_credentials(self):
        self.app.token.set("offline-test-secret")
        self.app.table.selection_set("1")
        self.app.show_selected()
        with patch.object(self.root, "clipboard_clear"), patch.object(self.root, "clipboard_append") as copy:
            self.app.copy_button.invoke()
        exported = copy.call_args.args[0]
        self.assertIn("RTX 5090", exported)
        self.assertIn("demo-host-5090", exported)
        self.assertIn("扩容 100", exported)
        self.assertNotIn("offline-test-secret", exported)

    def test_stop_completion_releases_controls_and_finishes_stopping_state(self):
        self.app.busy = True
        self.app.stop()
        self.app.events.put(("done", "cancelled"))
        self.app.drain()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.activity.cget("text"), "已停止")
        self.assertIn("已停止查找", self.app.status.get())
        self.assertNotIn("disabled", self.app.rent_button.state())
        self.assertIn("disabled", self.app.stop_button.state())

    def test_empty_scan_finishes_with_completion_and_waiting_guidance(self):
        self.app.events.put(("found_cards", []))
        self.app.events.put(("done", "complete"))
        self.app.drain()
        self.assertEqual(self.app.activity.cget("text"), "查询完成")
        self.assertIn("当前没有", self.app.status.get())
        self.assertIn("持续自动找卡", self.app.empty_hint.get())

    def test_advanced_window_preserves_main_window_styles(self):
        from autodl_quick.ui.advanced import App as AdvancedApp

        style = ttk.Style(self.root)
        before = [style.lookup(name, option) for name, option in (
            ("Heading.TLabel", "font"), ("Treeview", "rowheight"), ("Accent.TButton", "background"),
        )]
        window = tk.Toplevel(self.root)
        window.withdraw()
        editor = AdvancedApp(window, Settings(), demo=True, read_only=True, language="en")
        try:
            after = [style.lookup(name, option) for name, option in (
                ("Heading.TLabel", "font"), ("Treeview", "rowheight"), ("Accent.TButton", "background"),
            )]
            self.assertEqual(before, after)
        finally:
            editor.close()


if __name__ == "__main__":
    unittest.main()
