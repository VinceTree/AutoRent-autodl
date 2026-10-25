"""Multi-model selection, physical-region boundaries, images, and stock races."""
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

from autodl_quick.core import (APIError, AutoDL, Candidate, Engine, Settings, RentalJournal,
                          candidates, image_for_candidate)
from autodl_quick.catalog import Catalog, parse_region_stock
from test_autodl_quick import host, settings


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.load()

    def test_website_region_expands_all_physical_rooms(self):
        self.assertEqual(set(self.catalog.resolve_regions(["西北B区"])), {"west-B", "west-C", "west-D", "west-E"})
        self.assertEqual(set(self.catalog.resolve_regions(["beijingDC2"])), {"bj-B1", "bj-B2", "bj-B3"})

    def test_public_scope_includes_special_zones_excludes_enterprise(self):
        signs = self.catalog.resolve_regions()
        self.assertIn("beijing-C", signs)
        self.assertIn("nm-A1", signs)
        self.assertIn("sh-A1", signs)
        self.assertNotIn("west-X", signs)

    def test_physical_room_is_not_expanded_unexpectedly(self):
        self.assertEqual(self.catalog.resolve_regions(["bj-B2"]), ["bj-B2"])

    def test_bad_region_fails_instead_of_querying_everywhere(self):
        with self.assertRaises(ValueError):
            self.catalog.resolve_regions(["not-a-region"])

    def test_combined_regions_keep_order_and_deduplicate(self):
        signs = self.catalog.resolve_regions(["北京B区", "西北B区", "bj-B1"])
        self.assertEqual(signs[:3], ["bj-B1", "bj-B2", "bj-B3"])
        self.assertEqual(len(signs), 7)

    def test_full_catalog_contains_cpu_musa_and_special_models(self):
        names = self.catalog.gpu_names(include_cpu=True)
        for name in ("RTX 5090", "RTX 5090 D", "MTT S4000", "CPU", "CPU-close-HT", "V100-32GB", "A800-80GB"):
            self.assertIn(name, names)
        self.assertNotIn("CPU", self.catalog.gpu_names())
        self.assertEqual(self.catalog.model("MTT S4000")["chip"], "mthreads")

    def test_new_models_are_not_limited_to_a_hardcoded_list(self):
        catalog = Catalog.from_responses([], [{"gpu_name": "Future GPU", "gpu_memory": 128 * 1024 ** 3, "chip_corp": "new-chip", "cpu_arch": "arm"}])
        self.assertEqual(catalog.gpu_names(), ["Future GPU"])
        self.assertEqual(catalog.models[0]["memory_gb"], 128)

    def test_region_stock_format_and_missing_fields(self):
        stock = parse_region_stock([{"RTX 5090": {"gpu_idle_num": 2, "gpu_num": 100}}, {"MTT S4000": {"idle_gpu_num": 5, "total_gpu_num": 20}}])
        self.assertEqual(stock["RTX 5090"], {"idle": 2, "total": 100})
        with self.assertRaises(ValueError):
            parse_region_stock([{"RTX 5090": {"new_price": 42}}])


class GeneralFilteringTests(unittest.TestCase):
    def test_all_gpu_pool_allows_old_cuda_and_musa(self):
        rows = [host(), host(machine_id="old", gpu_name="V100-32GB", highest_cuda_version="11.4"),
                host(machine_id="musa", gpu_name="MTT S4000", chip_corp="mthreads", highest_cuda_version="")]
        self.assertEqual(len(candidates(rows, Settings())), 3)

    def test_cpu_is_opt_in(self):
        cpu = host(gpu_name="CPU", gpu_memory=0, highest_cuda_version="")
        self.assertEqual(candidates([cpu], Settings()), [])
        result = candidates([cpu], Settings(gpu_names=["CPU"]))
        self.assertEqual(result[0].chip, "cpu")

    def test_down_and_maintenance_hosts_are_never_candidates(self):
        for status in (0, 1, 3, 4, 5, 6, 999):
            with self.subTest(status=status):
                self.assertEqual(candidates([host(on_line=status)], Settings()), [])

    def test_selected_model_is_exact_including_d_variants(self):
        rows = [host(), host(machine_id="d", gpu_name="RTX 5090 D")]
        result = candidates(rows, Settings(gpu_names=["RTX 5090 D"]))
        self.assertEqual([r.machine_id for r in result], ["d"])

    def test_minimum_memory_excludes_small_cards(self):
        rows = [host(), host(machine_id="small", gpu_name="RTX 3060", gpu_memory=12 * 1024 ** 3)]
        self.assertEqual([r.machine_id for r in candidates(rows, Settings(min_vram_gb=24))], ["test-host"])

    def test_physical_region_filter_blocks_extra_api_results(self):
        rows = [host(region_sign="west-B"), host(machine_id="foreign", region_sign="bj-B1")]
        self.assertEqual([r.machine_id for r in candidates(rows, Settings(regions=["west-B"]))], ["test-host"])

    def test_model_priority_overrides_price_within_page(self):
        rows = [host(), host(machine_id="3090", gpu_name="RTX 3090")]
        chosen = Settings(gpu_names=["RTX 3090", "RTX 5090"], strategy="gpu_priority")
        self.assertEqual(candidates(rows, chosen)[0].gpu, "RTX 3090")

    def test_region_priority_uses_room_union_order(self):
        rows = [host(region_sign="west-B"), host(machine_id="bj", region_sign="bj-B1")]
        chosen = Settings(regions=Catalog.load().resolve_regions(["北京B区", "西北B区"]), strategy="region_priority")
        self.assertEqual(candidates(rows, chosen)[0].region_sign, "bj-B1")


class ImageRoutingTests(unittest.TestCase):
    def test_vendor_mismatch_is_never_submitted(self):
        card = Candidate("musa", "摩尔线程专区", "sh-A1", 1, "", "MTT S4000", Decimal("1"), chip="mthreads")
        self.assertIsNone(image_for_candidate(settings(), card))

    def test_musa_uses_musa_image_without_cuda_constraint(self):
        card = Candidate("musa", "摩尔线程专区", "sh-A1", 1, "", "MTT S4000", Decimal("1"), chip="mthreads")
        chosen = Settings(image_profiles={"mthreads:x86": {"uuid": "musa-image", "version": "1.3", "kind": "base", "chip": "mthreads", "arch": "x86"}})
        self.assertEqual(image_for_candidate(chosen, card).image_uuid, "musa-image")

    def test_blackwell_rejects_older_cuda_image(self):
        card = Candidate("5090", "西北", "west-B", 1, "13.2", "RTX 5090", Decimal("3"))
        self.assertIsNone(image_for_candidate(settings(image_cuda="11.8"), card))

    def test_legacy_gpu_rejects_cuda13_even_if_driver_reports_support(self):
        card = Candidate("v100", "V100专区", "beijing-C", 1, "13.0", "V100-32GB", Decimal("1"))
        self.assertIsNone(image_for_candidate(settings(image_cuda="13.0"), card))

    def test_per_model_image_overrides_platform_image(self):
        card = Candidate("v100", "V100专区", "beijing-C", 1, "11.8", "V100-32GB", Decimal("1"))
        chosen = settings(image_profiles={"gpu:V100-32GB": {"uuid": "old-cuda-image", "version": "11.4", "kind": "private", "chip": "nvidia", "arch": "x86"}})
        self.assertEqual(image_for_candidate(chosen, card).image_uuid, "old-cuda-image")
        self.assertEqual(image_for_candidate(chosen, card).image_kind, "private")

    def test_profile_vendor_and_arch_are_checked(self):
        card = Candidate("5090", "西北", "west-B", 1, "13.2", "RTX 5090", Decimal("3"))
        chosen = settings(image_profiles={"gpu:RTX 5090": {"uuid": "wrong", "version": "12.8", "chip": "mthreads", "arch": "x86"}})
        self.assertIsNone(image_for_candidate(chosen, card))


class GrabFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = MagicMock(spec=AutoDL)
        self.api.quote.return_value = Decimal("3")
        self.api.online.return_value = True
        self.api.snapshot.return_value = {}
        self.engine = Engine(self.api, journal=RentalJournal(Path(self.temp.name)))

    def test_first_available_page_is_used_before_loading_rest(self):
        def pages(*_):
            yield [host()]
            self.fail("Should not fetch another page after finding a matching card")
        self.api.machine_batches.side_effect = pages
        result = self.engine.wait_for_match(settings(), threading.Event())
        self.assertEqual(result["status"], "rehearsed")
        self.api.create.assert_not_called()

    def test_no_price_cap_uses_platform_quote_for_rehearsal(self):
        self.api.machine_batches.return_value = [[host()]]
        self.api.quote.return_value = Decimal("12.34")
        result = self.engine.wait_for_match(settings(max_price=""), threading.Event())
        self.assertEqual(result["quoted_yuan_per_hour"], "12.34")
        self.api.create.assert_not_called()

    def test_no_price_cap_is_allowed_for_explicit_real_mode(self):
        self.api.machine_batches.return_value = [[host()]]
        self.api.create.return_value = "test-instance"
        result = self.engine.rent(settings(max_price=""), threading.Event())
        self.assertIsNone(result["max_price"])
        self.api.create.assert_called_once()

    def test_explicit_stock_rejection_falls_back_to_next_host(self):
        self.api.machine_batches.return_value = [[host(machine_id="a"), host(machine_id="b")]]
        self.api.create.side_effect = [APIError("NoStock", "被占用"), "test-instance"]
        result = self.engine.rent(settings(), threading.Event())
        self.assertEqual(result["instance_uuid"], "test-instance")
        self.assertEqual(self.api.create.call_count, 2)
        payloads = [call.args[0] for call in self.api.create.call_args_list]
        self.assertEqual([p["instance_info"]["machine_id"] for p in payloads], ["a", "b"])

    def test_model_priority_queries_one_model_at_a_time(self):
        self.api.machine_batches.side_effect = [[], [[host()]]]
        chosen = settings(gpu_names=["RTX 3090", "RTX 5090"], strategy="gpu_priority")
        result = self.engine.wait_for_match(chosen, threading.Event())
        self.assertEqual(result["gpu"], "RTX 5090")
        calls = self.api.machine_batches.call_args_list
        self.assertEqual(calls[0].args[0].gpu_names, ["RTX 3090"])
        self.assertEqual(calls[1].args[0].gpu_names, ["RTX 5090"])

    def test_region_priority_queries_each_whole_website_region(self):
        self.api.machine_batches.side_effect = [[], [[host(region_sign="west-C")]]]
        chosen = settings(region_groups=["beijingDC2", "westDC3"], strategy="region_priority")
        self.engine.wait_for_match(chosen, threading.Event())
        calls = self.api.machine_batches.call_args_list
        self.assertEqual(set(calls[0].args[0].regions), {"bj-B1", "bj-B2", "bj-B3"})
        self.assertEqual(set(calls[1].args[0].regions), {"west-B", "west-C", "west-D", "west-E"})

    def test_wrong_platform_is_skipped_before_quoting(self):
        self.api.machine_batches.return_value = [[host(machine_id="musa", gpu_name="MTT S4000", chip_corp="mthreads"), host()]]
        self.api.create.return_value = "test-instance"
        self.engine.rent(settings(gpu_names=[]), threading.Event())
        self.assertTrue(all(call.args[0] == "test-host" for call in self.api.quote.call_args_list))


if __name__ == "__main__":
    unittest.main()
