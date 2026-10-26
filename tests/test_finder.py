from decimal import Decimal
import gc
import json
from pathlib import Path
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from autodl_quick.core import APIError, AutoDL, Engine, RentalJournal, Settings
from test_autodl_quick import host


def api_fixture():
    api = MagicMock(spec=AutoDL)
    api.audit = []
    api.region_stock.return_value = {"RTX 5090": {"idle": 1, "total": 100}, "RTX 3090": {"idle": 0, "total": 100}, "CPU": {"idle": 10, "total": 100}}
    api.machine_batches.return_value = [[host(region_sign="west-B", cpu_per_gpu=16, mem_per_gpu=80 * 1024 ** 3,
                                              driver_version="580.1", max_instance_disk_size=100 * 1024 ** 3,
                                              machine_base_info={"cpu_name": "Test CPU"})]]
    api.quote.return_value = Decimal("2.88")
    api.online.return_value = True
    api.images.return_value = [{"uuid": "auto-image", "cuda": "12.8", "kind": "base", "chip": "nvidia", "arch": "x86", "label": "PyTorch / CUDA 12.8"}]
    api.create.return_value = "test-instance"
    api.snapshot.return_value = {}
    return api


class FinderEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = api_fixture()
        self.engine = Engine(self.api, journal=RentalJournal(Path(self.temp.name)))

    def test_default_auto_mode_needs_no_manual_model_price_region_or_image(self):
        selected = Settings()
        selected.validate(renting=True)
        result = self.engine.auto_wait(selected, threading.Event())
        self.assertEqual(result["gpu"], "RTX 5090")
        self.assertEqual(result["current_price"], "2.88")
        self.assertEqual(result["image_uuid"], "auto-image")
        self.api.create.assert_not_called()

    def test_only_models_with_remaining_gpu_are_queried(self):
        self.engine.discover(Settings(), threading.Event())
        selected = self.api.machine_batches.call_args.args[0]
        self.assertEqual(selected.gpu_names, ["RTX 5090"])

    def test_empty_stock_does_not_scan_thousands_of_busy_hosts(self):
        self.api.region_stock.return_value = {"RTX 5090": {"idle": 0, "total": 100}}
        self.assertEqual(self.engine.discover(Settings(), threading.Event()), [])
        self.api.machine_batches.assert_not_called()
        self.api.quote.assert_not_called()
        self.api.create.assert_not_called()

    def test_card_hardware_and_image_are_loaded_together(self):
        result = self.engine.discover(Settings(), threading.Event())[0]
        self.assertEqual(result.cpu_cores, 16)
        self.assertEqual(result.ram_gb, 80)
        self.assertEqual(result.memory_gb, 32)
        self.assertEqual(result.disk_gb, 100)
        self.assertEqual(result.driver, "580.1")
        self.assertEqual(result.image_uuid, "auto-image")
        self.assertEqual(result.image_label, "PyTorch / CUDA 12.8")

    def test_explicit_real_auto_mode_uses_auto_image_and_creates_once(self):
        result = self.engine.auto_wait(Settings(), threading.Event(), execute=True)
        self.assertEqual(result["instance_uuid"], "test-instance")
        self.api.create.assert_called_once()
        self.assertEqual(self.api.create.call_args.args[0]["instance_info"]["base_image_uuid"], "auto-image")
        self.api.images.assert_called_once_with("nvidia", "x86")

    def test_real_auto_mode_does_not_wait_for_the_rest_of_the_market(self):
        def pages(*_):
            yield [host(region_sign="west-B")]
            self.fail("The first ready card should be submitted before scanning more pages")
        self.api.machine_batches.side_effect = pages
        self.engine.auto_wait(Settings(), threading.Event(), execute=True)
        self.api.create.assert_called_once()

    def test_v100_automatically_avoids_cuda13_image(self):
        self.api.region_stock.return_value = {"V100-32GB": {"idle": 1, "total": 100}}
        self.api.machine_batches.return_value = [[host(gpu_name="V100-32GB", highest_cuda_version="13.0")]]
        self.api.images.return_value = [
            {"uuid": "wrong-new", "cuda": "13.0", "kind": "base", "label": "PyTorch / CUDA 13.0"},
            {"uuid": "right-legacy", "cuda": "12.6", "kind": "base", "label": "PyTorch / CUDA 12.6"}]
        result = self.engine.discover(Settings(), threading.Event())[0]
        self.assertEqual(result.image_uuid, "right-legacy")

    def test_musa_image_is_selected_without_cuda_assumption(self):
        self.api.region_stock.return_value = {"MTT S4000": {"idle": 1, "total": 192}}
        self.api.machine_batches.return_value = [[host(gpu_name="MTT S4000", chip_corp="mthreads", highest_cuda_version="", region_sign="sh-A1")]]
        self.api.images.return_value = [{"uuid": "musa-auto", "cuda": "1.3", "kind": "base", "chip": "mthreads", "arch": "x86", "label": "PyTorch / MUSA 1.3"}]
        result = self.engine.discover(Settings(), threading.Event())[0]
        self.assertEqual(result.image_uuid, "musa-auto")
        self.api.images.assert_called_once_with("mthreads", "x86")

    def test_configured_optional_preference_is_still_respected(self):
        self.assertEqual(self.engine.discover(Settings(gpu_names=["RTX 3090"]), threading.Event()), [])
        self.api.machine_batches.assert_not_called()


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "GUI requires a display")
class FinderWindowTests(unittest.TestCase):
    def setUp(self):
        # Dispose of destroyed Tk cycles on the UI thread before starting workers.
        gc.collect()
        import tkinter as tk
        from autodl_quick.ui.main import App
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, Settings(), read_only=True)
        self.addCleanup(self.app.close)

    def test_main_window_requires_no_model_region_or_price_input(self):
        self.assertFalse(hasattr(self.app, "price"))
        self.assertFalse(hasattr(self.app, "model_table"))
        chosen = self.app.settings()
        self.assertEqual(chosen.gpu_names, [])
        self.assertEqual(chosen.max_price, "")
        self.assertTrue(chosen.auto_image)
        self.assertEqual(len(chosen.regions), 18)
        self.assertIn("disabled", self.app.rent_button.state())

    def test_main_selectors_expand_region_and_preserve_exact_model(self):
        self.app.region.set("北京B区")
        self.app.region_changed()
        self.app.gpu.set("RTX 5090 D")
        self.app.gpu_changed()
        selected = self.app.settings()
        self.assertEqual(selected.region_groups, ["北京B区"])
        self.assertEqual(selected.regions, ["bj-B1", "bj-B2", "bj-B3"])
        self.assertEqual(selected.gpu_names, ["RTX 5090 D"])
        self.assertEqual(selected.max_price, "")
        self.assertTrue(selected.auto_image)

    def test_switching_region_rejects_late_old_region_stock(self):
        old_generation = self.app._stock_generation
        self.app.region.set("北京B区")
        self.app.region_changed()
        self.app.events.put(("scope_stock", (old_generation, {"MTT S4000": {"idle": 9, "total": 9}})))
        self.app.drain()
        self.assertIsNone(self.app.stock)
        self.app.events.put(("scope_stock", (self.app._stock_generation, {"RTX 5090": {"idle": 0, "total": 10}})))
        self.app.drain()
        self.assertNotIn("MTT S4000", self.app.gpu_combo.cget("values"))
        self.assertIn("RTX 5090", self.app.gpu_combo.cget("values"))

    def test_zero_stock_or_missing_model_never_expands_the_requested_scope(self):
        self.app.region.set("西北B区")
        self.app.region_changed()
        self.app.gpu.set("RTX 5090")
        self.app.gpu_changed()
        self.app.events.put(("scope_stock", (self.app._stock_generation, {"RTX 5090": {"idle": 0, "total": 10}})))
        self.app.drain()
        self.assertIn("0 / 10", self.app.stock_info.get())
        self.app.region.set("摩尔线程专区")
        self.app.region_changed()
        self.app.events.put(("scope_stock", (self.app._stock_generation, {"MTT S4000": {"idle": 8, "total": 10}})))
        self.app.drain()
        self.assertEqual(self.app.settings().gpu_names, ["RTX 5090"])
        self.assertEqual(self.app.settings().regions, ["sh-A1"])
        self.assertIn("本地区未提供", self.app.stock_info.get())
        self.assertEqual(self.app.gpu.get(), "RTX 5090")

    def test_scope_change_clears_previous_hosts_and_details(self):
        card = Engine(api_fixture()).discover(Settings(), threading.Event())[0]
        self.app.show_cards([card])
        self.app.region.set("北京B区")
        self.app.region_changed()
        self.assertEqual(self.app.rows, [])
        self.assertFalse(self.app.table.get_children())
        self.assertNotIn(card.machine_id, self.app.details.get())

    def test_advanced_multiple_regions_and_models_are_preserved(self):
        self.app.preferences = Settings(region_groups=["北京B区", "西北B区"], gpu_names=["RTX 5090", "RTX 4090"])
        self.app.sync_selectors()
        self.assertIn("高级偏好", self.app.region.get())
        self.assertIn("高级偏好", self.app.gpu.get())
        self.assertEqual(len(self.app.settings().regions), 7)
        self.assertEqual(self.app.settings().gpu_names, ["RTX 5090", "RTX 4090"])
        self.app.region.set("重庆A区")
        self.app.region_changed()
        self.assertEqual(self.app.settings().regions, ["cq-A1"])
        self.assertEqual(self.app.settings().gpu_names, ["RTX 5090", "RTX 4090"])

    def test_inventory_refresh_queries_only_selected_region_through_read_only_transport(self):
        self.app.region.set("北京B区")
        self.app.region_changed()
        self.app.token.set("fake-test-token")
        api = api_fixture()
        with patch("autodl_quick.ui.main.AutoDL", return_value=api) as constructor:
            self.app.refresh_stock()
            deadline = time.monotonic() + 3
            while self.app.stock is None and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
        constructor.assert_called_once_with("fake-test-token", read_only=True)
        api.region_stock.assert_called_once_with(["bj-B1", "bj-B2", "bj-B3"])
        api.create.assert_not_called()

    def test_token_change_rejects_pending_inventory_from_previous_login(self):
        old_generation = self.app._stock_generation
        self.app.token.set("different-fake-token")
        self.app.events.put(("scope_stock", (old_generation, {"RTX 5090": {"idle": 8, "total": 10}})))
        self.app.drain()
        self.assertIsNone(self.app.stock)

    def test_read_only_window_cannot_rent_even_through_direct_callback(self):
        with patch("autodl_quick.ui.main.AutoDL") as api:
            self.app.rent_button.invoke()
            self.app.rent()
        api.assert_not_called()
        self.assertFalse(self.app.busy)
        self.assertIn("只读", self.app.status.get())

    def test_normal_window_rents_only_after_explicit_rent_button_click(self):
        import tkinter as tk
        from autodl_quick.ui.main import App
        window = tk.Toplevel(self.root)
        window.withdraw()
        app = App(window, Settings())
        self.addCleanup(app.close)
        self.assertNotIn("disabled", app.rent_button.state())
        app.token.set("fake-test-token")
        app.region.set("西北B区")
        app.region_changed()
        app.gpu.set("RTX 5090")
        app.gpu_changed()
        api = api_fixture()
        api.machine_batches.return_value[0][:0] = [host(machine_id="wrong-region", region_sign="bj-B1"), host(machine_id="wrong-model", region_sign="west-B", gpu_name="RTX 3090")]
        def finish():
            deadline = time.monotonic() + 5
            while app.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertFalse(app.busy)
        with tempfile.TemporaryDirectory() as temp, patch("autodl_quick.ui.main.AutoDL", return_value=api), patch("autodl_quick.ui.main.ROOT", Path(temp)), patch("autodl_quick.ui.main.Catalog.refresh", return_value=app.catalog), patch("autodl_quick.ui.main.Engine", side_effect=lambda client, emit: Engine(client, emit, journal=RentalJournal(Path(temp) / "rental"))):
            api.create.assert_not_called()
            app.scan_button.invoke()
            finish()
            api.create.assert_not_called()
            app.wait_button.invoke()
            finish()
            api.create.assert_not_called()
            app.rent_button.invoke()
            self.assertIn("disabled", app.rent_button.state())
            self.assertIn("disabled", app.region_combo.state())
            self.assertIn("disabled", app.gpu_combo.state())
            finish()
            api.create.assert_called_once()
            self.assertEqual(api.create.call_args.args[0]["instance_info"]["machine_id"], "test-host")
            requested = api.machine_batches.call_args.args[0]
            self.assertEqual(requested.regions, ["west-B", "west-C", "west-D", "west-E"])
            self.assertEqual(requested.gpu_names, ["RTX 5090"])
            self.assertIn("已租用一台", app.status.get())
            self.assertNotIn("disabled", app.rent_button.state())
            self.assertIn("readonly", app.region_combo.state())
            self.assertIn("readonly", app.gpu_combo.state())

    def test_busy_window_does_not_start_another_job(self):
        self.app.busy = True
        try:
            with patch("autodl_quick.ui.main.AutoDL") as api:
                self.app.scan()
            api.assert_not_called()
        finally:
            self.app.busy = False

    def test_creation_failure_is_visible_and_survives_another_click(self):
        import tkinter as tk
        from autodl_quick.ui.main import App
        window = tk.Toplevel(self.root)
        window.withdraw()
        app = App(window, Settings(regions=["west-B"], gpu_names=["RTX 5090"]))
        self.addCleanup(app.close)
        app.token.set("fake-test-token")
        api = api_fixture()
        api.create.side_effect = APIError("UnknownBusinessFailure", "平台返回具体原因")
        with tempfile.TemporaryDirectory() as temp, patch("autodl_quick.ui.main.AutoDL", return_value=api), patch("autodl_quick.ui.main.ROOT", Path(temp)), patch("autodl_quick.ui.main.Catalog.refresh", return_value=app.catalog), patch("autodl_quick.ui.main.Engine", side_effect=lambda client, emit: Engine(client, emit, journal=RentalJournal(Path(temp) / "rental"))):
            for attempt in range(2):
                app.rent()
                deadline = time.monotonic() + 5
                while app.busy and time.monotonic() < deadline:
                    self.root.update()
                    time.sleep(0.01)
                self.assertFalse(app.busy)
                self.assertIn("平台返回具体原因", app.status.get())
                self.assertIn("UnknownBusinessFailure", app.status.get())
                self.assertEqual(app.activity.cget("text"), "需要处理")
            api.create.assert_called_once()
            reports = [json.loads(path.read_text(encoding="utf-8")) for path in (Path(temp) / ".state" / "runs").glob("*.json")]
            self.assertEqual(len(reports), 2)
            self.assertTrue(any(report["error_code"] == "UnknownBusinessFailure" for report in reports))
            self.assertNotIn("fake-test-token", json.dumps(reports))

    def test_scan_automatically_displays_first_card_details_without_choice(self):
        api = api_fixture()
        self.app.token.set("fake-test-token")
        with tempfile.TemporaryDirectory() as temp, patch("autodl_quick.ui.main.AutoDL", return_value=api), patch("autodl_quick.ui.main.ROOT", Path(temp)), patch("autodl_quick.ui.main.Catalog.refresh", return_value=self.app.catalog):
            self.app.scan()
            deadline = time.monotonic() + 5
            while self.app.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertFalse(self.app.busy)
            self.assertEqual(len(self.app.rows), 1)
            self.assertIn("RTX 5090", self.app.details.get())
            self.assertIn("Test CPU", self.app.details.get())
            self.assertIn("auto-image", self.app.details.get())
            self.assertIn("2.88", self.app.details.get())
        api.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
