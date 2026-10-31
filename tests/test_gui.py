"""Exercise the local window with mocked API calls, including its default mode."""

from decimal import Decimal
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from autodl_quick.core import AutoDL, Settings


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "GUI requires a display")
class WindowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from autodl_quick.ui.advanced import App
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, Settings(max_price="4"))
        self.addCleanup(self.app.close)

    def test_default_is_rehearsal(self):
        self.assertFalse(self.app.real_rent.get())
        self.assertEqual(self.app.rent_button.cget("text"), "开始库存演练")

    def test_price_limit_is_optional(self):
        self.app.price.set("")
        chosen = self.app.get_settings()
        self.assertEqual(chosen.max_price, "")

    def test_polling_interval_accepts_fractional_seconds(self):
        self.app.interval.set("0.5")
        self.assertEqual(self.app.get_settings().poll_seconds, 0.5)

    def test_read_only_cannot_enable_paid_mode(self):
        self.app.read_only = True
        self.app.real_rent.set(True)
        self.app.mode_changed()
        self.assertFalse(self.app.real_rent.get())
        self.assertEqual(self.app.rent_button.cget("text"), "开始库存演练")

    def test_region_click_selects_website_group_then_allows_multiple(self):
        self.app.region_changed("westDC3")
        self.assertEqual(set(self.app.get_settings().regions), {"west-B", "west-C", "west-D", "west-E"})
        self.app.region_vars["beijingDC2"].set(True)
        self.app.region_changed("beijingDC2")
        self.assertEqual(len(self.app.get_settings().regions), 7)
        self.assertEqual(self.app.get_settings().region_groups, ["westDC3", "beijingDC2"])

    def test_empty_region_selection_cannot_become_unrestricted(self):
        from autodl_quick.core import QuickError
        self.app.all_regions.set(False)
        self.app.select_all_regions()
        with self.assertRaises(QuickError):
            self.app.get_settings()

    def test_obsolete_region_stock_response_is_ignored(self):
        self.app.region_generation = 4
        self.app.events.put(("region_stock", (3, {"RTX 5090": {"idle": 99, "total": 100}})))
        self.app.drain()
        self.assertIsNone(self.app.stock)
        self.app.events.put(("region_stock", (4, {"RTX 5090": {"idle": 1, "total": 100}})))
        self.app.drain()
        self.assertEqual(self.app.stock["RTX 5090"]["idle"], 1)

    def test_cpu_not_included_by_all_gpu_selection(self):
        self.app.select_all_gpus()
        self.assertEqual(self.app.model_table.item("CPU", "values")[0], "☐")
        self.assertEqual(self.app.model_table.item("MTT S4000", "values")[0], "☑")

    def test_cleared_model_selection_cannot_become_all_gpu(self):
        from autodl_quick.core import QuickError
        self.app.clear_gpus()
        with self.assertRaises(QuickError):
            self.app.get_settings()

    def test_multiple_platform_images_survive_platform_switch(self):
        self.app.image.set("nvidia-image")
        self.app.image_cuda.set("12.8")
        self.app.platform.set("摩尔线程 / MUSA")
        self.app.platform_changed()
        self.app.image.set("musa-image")
        self.app.image_cuda.set("1.3")
        self.app.platform.set("NVIDIA / CUDA")
        self.app.platform_changed()
        chosen = self.app.get_settings()
        self.assertEqual(chosen.image_profiles["nvidia:x86"]["uuid"], "nvidia-image")
        self.assertEqual(chosen.image_profiles["mthreads:x86"]["uuid"], "musa-image")
        self.assertEqual(self.app.image.get(), "nvidia-image")

    def test_rehearsal_worker_makes_no_create_calls(self):
        api = MagicMock(spec=AutoDL)
        api.machines.return_value = [{
            "machine_id": "test-ui", "gpu_name": "RTX 5090", "gpu_idle_num": 1,
            "on_line": 2, "max_instance_num": 3, "binding_instance_num": 1,
            "highest_cuda_version": "12.8", "region_sign": "west-B",
            "machine_sku_info": [{"type": "payg", "current_price": 3500}],
        }]
        api.quote.return_value = Decimal("3.50")
        api.online.return_value = True
        api.machine_batches.side_effect = lambda s, c: [api.machines(s, c)]
        with patch("autodl_quick.ui.advanced.AutoDL", return_value=api):
            self.app.rent()
            deadline = time.monotonic() + 5
            while self.app.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
        self.assertFalse(self.app.busy)
        api.create.assert_not_called()
        self.assertIn("演练命中", self.app.log.get("1.0", "end"))

    def test_demo_display_cannot_access_network(self):
        self.app.demo = True
        with patch("autodl_quick.ui.advanced.AutoDL") as api:
            self.app.scan()
        api.assert_not_called()
        self.app.drain()
        self.assertEqual(len(self.app.table.get_children()), 2)
        self.assertIn("演示数据", self.app.status.get())

    def test_log_remains_visible_at_minimum_window_size(self):
        self.root.geometry("900x820")
        self.root.deiconify()
        self.root.update()
        self.assertGreater(self.app.log.winfo_height(), 40)
        self.assertLess(self.app.log.winfo_y() + self.app.log.winfo_height(), 820)


if __name__ == "__main__":
    unittest.main()
