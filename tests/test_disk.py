"""Disk capacity, units, separate daily billing, and simulated creation."""
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from autodl_quick.core import (AutoDL, Engine, GIB, PriceQuote, QuickError, RentalJournal,
                          Settings, candidates, create_payload, price_payload)
from test_autodl_quick import host
from test_finder import api_fixture


def disk_host(**changes):
    values = dict(region_sign="west-B", max_instance_disk_size=50 * GIB,
                  max_data_disk_expand_size=100 * GIB)
    values.update(changes)
    return host(**values)


def disk_api():
    api = api_fixture()
    api.machine_batches.return_value = [[disk_host()]]
    api.quote_detail.return_value = PriceQuote(Decimal("2.88"), Decimal("0.67"))
    return api


class DiskCapacityTests(unittest.TestCase):
    def test_expansion_is_additional_to_free_capacity_and_exact_boundary_is_allowed(self):
        card = candidates([disk_host()], Settings(expand_disk_gb=100))[0]
        self.assertEqual(card.disk_gb, 50)
        self.assertEqual(card.disk_expandable_gb, 100)
        self.assertEqual(card.display()["disk_total_gb"], 150)

    def test_insufficient_or_unknown_capacity_is_excluded_when_expansion_requested(self):
        for raw in (99 * GIB, 100 * GIB - 1, None, "unknown", -1, False, float("inf")):
            with self.subTest(capacity=raw):
                self.assertEqual(candidates([disk_host(max_data_disk_expand_size=raw)], Settings(expand_disk_gb=100)), [])

    def test_zero_expansion_does_not_require_capacity_metadata(self):
        self.assertEqual(len(candidates([host()], Settings())), 1)

    def test_disk_filter_keeps_region_and_exact_model_constraints(self):
        rows = [disk_host(machine_id="wrong-region", region_sign="bj-B1"),
                disk_host(machine_id="wrong-model", gpu_name="RTX 5090 D"),
                disk_host(machine_id="small", max_data_disk_expand_size=99 * GIB), disk_host()]
        chosen = Settings(regions=["west-B"], gpu_names=["RTX 5090"], expand_disk_gb=100)
        self.assertEqual([card.machine_id for card in candidates(rows, chosen)], ["test-host"])

    def test_invalid_expansion_settings_are_rejected(self):
        for value in (-1, True, 1.5, "100", None):
            with self.subTest(value=value), self.assertRaises(QuickError):
                Settings(expand_disk_gb=value).validate()

    def test_config_round_trip_keeps_expansion_and_old_config_defaults_to_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text('{"gpu_names": []}', encoding="utf-8")
            self.assertEqual(Settings.load(path).expand_disk_gb, 0)
            Settings(expand_disk_gb=450).save(path)
            self.assertEqual(Settings.load(path).expand_disk_gb, 450)


class DiskQuoteTests(unittest.TestCase):
    def test_preview_and_create_use_the_same_byte_quantity(self):
        payload = create_payload("test-host", Settings(expand_disk_gb=100), "test")
        self.assertEqual(payload["instance_info"]["expand_data_disk"], 100 * GIB)
        self.assertEqual(payload["price_info"], price_payload("test-host", 100))
        self.assertEqual(payload["price_info"]["expand_data_disk"], 100 * GIB)
        self.assertEqual(payload["instance_info"]["req_gpu_amount"], 1)
        self.assertEqual(price_payload("test-host")["expand_data_disk"], 0)

    def test_live_response_shape_keeps_hourly_gpu_and_daily_disk_prices_separate(self):
        api = AutoDL("fake-test-token", read_only=True)
        api.request = MagicMock(return_value={"origin_price": 1660, "payg_price": 1580, "deal_price": 0,
                                               "data_disk_info": {"data_disk_deal_daily_price": 670}})
        result = api.quote_detail("test-host", 100)
        self.assertEqual(result.instance_hourly, Decimal("1.58"))
        self.assertEqual(result.disk_daily, Decimal("0.67"))
        self.assertEqual(api.request.call_args.args[2]["expand_data_disk"], 100 * GIB)

    def test_unknown_or_invalid_disk_price_stops_instead_of_assuming_free(self):
        api = AutoDL("fake-test-token")
        for value in (None, "NaN", "Infinity", -1, False):
            with self.subTest(value=value):
                api.request = MagicMock(return_value={"origin_price": 1660, "payg_price": 1580,
                                                       "data_disk_info": {"data_disk_deal_daily_price": value}})
                with self.assertRaises(QuickError):
                    api.quote_detail("test-host", 100)

    def test_explicit_zero_disk_fee_is_valid(self):
        api = AutoDL("fake-test-token")
        api.request = MagicMock(return_value={"origin_price": 1660, "payg_price": 1580,
                                               "data_disk_info": {"data_disk_deal_daily_price": 0}})
        self.assertEqual(api.quote_detail("test-host", 100).disk_daily, 0)


class DiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = disk_api()
        self.engine = Engine(self.api, journal=RentalJournal(Path(self.temp.name)))

    def test_read_only_discovery_filters_capacity_before_quoting_and_loads_daily_fee(self):
        self.api.machine_batches.return_value[0].insert(0, disk_host(machine_id="too-small", max_data_disk_expand_size=0))
        cards = self.engine.discover(Settings(expand_disk_gb=100), threading.Event())
        self.assertEqual(len(cards), 1)
        self.api.quote_detail.assert_called_once_with("test-host", 100)
        self.assertEqual(cards[0].disk_daily_price, Decimal("0.67"))
        self.assertEqual(json.loads(json.dumps(cards[0].display()))["disk_daily_price"], "0.67")
        self.api.create.assert_not_called()

    def test_simulated_rent_preserves_expansion_through_auto_image_and_final_quote(self):
        self.api.quote_detail.return_value = PriceQuote(Decimal("2.88"), Decimal("0.69"))
        result = self.engine.auto_wait(Settings(expand_disk_gb=100), threading.Event(), execute=True)
        self.api.create.assert_called_once()
        payload = self.api.create.call_args.args[0]
        self.assertEqual(payload["instance_info"]["expand_data_disk"], 100 * GIB)
        self.assertEqual(payload["price_info"]["expand_data_disk"], 100 * GIB)
        self.assertEqual(result["disk_daily_price"], "0.69")
        self.assertEqual(result["disk_total_gb"], 150)
        self.assertEqual(result["expand_disk_gb"], 100)
        self.api.quote_detail.assert_called_once_with("test-host", 100)
        self.api.quote.assert_not_called()

    def test_failed_disk_quote_cannot_reach_paid_creation(self):
        self.api.quote_detail.side_effect = QuickError("扩容数据盘日费格式异常")
        with self.assertRaises(QuickError):
            self.engine.auto_wait(Settings(expand_disk_gb=100), threading.Event(), execute=True)
        self.api.create.assert_not_called()


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "GUI requires a display")
class DiskWindowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from autodl_quick.ui.main import App
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, read_only=True)
        self.addCleanup(self.app.close)

    def test_expansion_remains_selected_when_region_or_model_changes(self):
        self.app.disk.set("100")
        self.app.region.set("西北B区")
        self.app.region_changed()
        self.app.gpu.set("RTX 5090")
        self.app.gpu_changed()
        self.assertEqual(self.app.settings().expand_disk_gb, 100)
        self.assertEqual(self.app.settings().gpu_names, ["RTX 5090"])

    def test_invalid_capacity_does_not_start_query(self):
        self.app.disk.set("-100")
        with patch("autodl_quick.ui.main.AutoDL") as api, patch("autodl_quick.ui.main.messagebox.showerror") as message:
            self.app.scan()
        api.assert_not_called()
        message.assert_called_once()
        self.assertFalse(self.app.busy)

    def test_scan_displays_target_capacity_and_daily_fee_without_renting(self):
        self.app.disk.set("100")
        self.app.token.set("fake-test-token")
        api = disk_api()
        with tempfile.TemporaryDirectory() as temp, patch("autodl_quick.ui.main.AutoDL", return_value=api), patch("autodl_quick.ui.main.ROOT", Path(temp)), patch("autodl_quick.ui.main.Catalog.refresh", return_value=self.app.catalog):
            self.app.scan()
            self.assertIn("disabled", self.app.disk_entry.state())
            deadline = time.monotonic() + 5
            while self.app.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertFalse(self.app.busy)
            values = self.app.table.item("0", "values")
            self.assertIn("50+100=150", values)
            self.assertIn("0.67", values)
            self.assertIn("费用 ¥0.67/日", self.app.details.get())
            self.assertNotIn("disabled", self.app.disk_entry.state())
        api.create.assert_not_called()

    def test_advanced_editor_preserves_disk_choice(self):
        import tkinter as tk
        from autodl_quick.ui.advanced import App
        window = tk.Toplevel(self.root)
        window.withdraw()
        editor = App(window, Settings(expand_disk_gb=100), read_only=True)
        self.addCleanup(editor.close)
        self.assertEqual(editor.get_settings().expand_disk_gb, 100)
        editor.disk.set("200")
        self.assertEqual(editor.get_settings().expand_disk_gb, 200)


if __name__ == "__main__":
    unittest.main()
