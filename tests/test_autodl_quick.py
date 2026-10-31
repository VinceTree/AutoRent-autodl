"""Offline tests. All HTTP requests and all paid creation calls are mocked."""

from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from autodl_quick.core import (AutoDL, APIError, Cancelled, CreateUncertain, Engine,
                          LoginRequired, QuickError, ReadUnavailable, RentalJournal,
                          SameOriginRedirect, Settings, amount, candidates,
                          create_payload, exact_gpu)


def host(**changes):
    row = {"machine_id": "test-host", "gpu_name": "RTX 5090", "gpu_idle_num": 2,
           "on_line": 2, "gpu_memory": 32 * 1024 ** 3,
           "max_instance_num": 10, "binding_instance_num": 2,
           "highest_cuda_version": "12.8", "region_name": "测试地区", "region_sign": "test-region",
           "machine_sku_info": [{"type": "payg", "current_price": 4000}]}
    row.update(changes)
    return row


def settings(**changes):
    values = {"max_price": "4.00", "image_uuid": "base-image-test", "image_cuda": "12.8",
              "gpu_names": ["RTX 5090"], "min_cuda": "12.8", "auto_image": False}
    values.update(changes)
    return Settings(**values)


class NoWait(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


class StopAtWait(threading.Event):
    def wait(self, timeout=None):
        self.set()
        return True


@contextmanager
def response(data):
    item = MagicMock()
    item.read.return_value = json.dumps(data).encode()
    yield item


class FilteringTests(unittest.TestCase):
    def test_exact_5090_excludes_d_and_other_gpus(self):
        for value in ("RTX 5090", " NVIDIA GeForce RTX 5090 ", "NVIDIA RTX 5090"):
            self.assertTrue(exact_gpu(value))
        for value in ("RTX 5090 D", "RTX 5090D", "RTX 4090", "vGPU-32GB", "5090-p"):
            self.assertFalse(exact_gpu(value))

    def test_stock_driver_instance_slots_and_maintenance(self):
        for change in ({"gpu_idle_num": 0}, {"on_line": 4}, {"on_line": None},
                       {"max_instance_num": 2}, {"highest_cuda_version": "12.7"},
                       {"gpu_name": "RTX 5090 D"}, {"machine_id": ""},
                       {"machine_sku_info": []}):
            with self.subTest(change=change):
                self.assertEqual(candidates([host(**change)], settings()), [])
        self.assertEqual(len(candidates([host()], settings())), 1)

    def test_regions_and_sorted_prices(self):
        rows = [host(machine_id="expensive"), host(machine_id="cheap", machine_sku_info=[{"type": "payg", "current_price": 3000}])]
        self.assertEqual([r.machine_id for r in candidates(rows, settings())], ["cheap", "expensive"])
        self.assertEqual(candidates(rows, settings(regions=["other"])), [])

    def test_decimal_conversion_and_nonfinite_prices(self):
        self.assertEqual(amount(1970, "price"), Decimal("1.970"))
        for value in ("NaN", "Infinity", 0, -5, True, None):
            with self.subTest(value=value), self.assertRaises(QuickError):
                amount(value, "price")

    def test_validations(self):
        for change in ({"max_price": "NaN"}, {"max_price": "-1"}, {"poll_seconds": 0.1},
                       {"wait_minutes": 0}, {"image_cuda": "bad"}, {"regions": "test"},
                       {"min_cuda": "bad"}, {"image_uuid": ""}, {"poll_seconds": True}):
            with self.subTest(change=change), self.assertRaises(QuickError):
                settings(**change).validate(renting=True)

    def test_single_card_payload_no_expansion_or_coupon(self):
        payload = create_payload("test-host", settings(), "quick-5090-test")
        self.assertEqual(payload["instance_info"]["req_gpu_amount"], 1)
        self.assertEqual(payload["instance_info"]["expand_data_disk"], 0)
        self.assertEqual(payload["price_info"]["num"], 1)
        self.assertEqual(payload["price_info"]["coupon_id_list"], [])
        self.assertEqual(payload["instance_info"]["base_image_uuid"], "base-image-test")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = RentalJournal(Path(self.temp.name))
        self.api = MagicMock(spec=AutoDL)
        self.api.machines.return_value = [host()]
        self.api.machine_batches.side_effect = lambda s, c: [self.api.machines(s, c)]
        self.api.quote.return_value = Decimal("3.50")
        self.api.online.return_value = True
        self.api.create.return_value = "test-instance-001"
        self.api.snapshot.return_value = {"ssh_command": "ssh -p 12345 root@test.invalid"}
        self.engine = Engine(self.api, journal=self.journal)

    def test_rehearsal_never_creates_or_writes_journal(self):
        result = self.engine.wait_for_match(settings(image_uuid=""), threading.Event())
        self.assertEqual(result["status"], "rehearsed")
        self.api.create.assert_not_called()
        self.api.snapshot.assert_not_called()
        self.assertFalse(self.journal.path.exists())

    def test_success_creates_exactly_once_and_survives_restart(self):
        result = self.engine.rent(settings(), threading.Event())
        self.assertEqual(result["instance_uuid"], "test-instance-001")
        self.api.create.assert_called_once()
        self.assertEqual(self.api.quote.call_count, 1)
        self.assertEqual(self.journal.read()["status"], "created")
        with self.assertRaises(QuickError):
            self.engine.rent(settings(), threading.Event())
        self.api.create.assert_called_once()

    def test_pending_marker_precedes_create(self):
        def create(_):
            self.assertEqual(self.journal.read()["status"], "submitting")
            return "test-instance-001"
        self.api.create.side_effect = create
        self.engine.rent(settings(), threading.Event())

    def test_uncertain_create_never_retries_even_if_read_error_type(self):
        self.api.create.side_effect = ReadUnavailable("unexpected transport failure")
        with self.assertRaises(CreateUncertain):
            self.engine.rent(settings(), NoWait())
        self.api.create.assert_called_once()
        self.assertEqual(self.journal.read()["status"], "submitting")
        with self.assertRaises(QuickError):
            self.engine.rent(settings(), NoWait())
        self.api.create.assert_called_once()

    def test_api_rejection_preserves_record_and_stops(self):
        self.api.create.side_effect = APIError("UnknownBusinessFailure", "拒绝")
        with self.assertRaisesRegex(CreateUncertain, "拒绝.*UnknownBusinessFailure") as failure:
            self.engine.rent(settings(), NoWait())
        self.api.create.assert_called_once()
        self.assertEqual(self.journal.read()["status"], "submitting")
        self.assertEqual(self.journal.read()["error_code"], "UnknownBusinessFailure")
        self.assertIn("拒绝", self.journal.read()["error_message"])
        self.assertEqual(failure.exception.reason_code, "UnknownBusinessFailure")
        with self.assertRaisesRegex(QuickError, "上次失败原因：.*UnknownBusinessFailure"):
            self.engine.rent(settings(), NoWait())
        self.api.create.assert_called_once()

    def test_transport_failure_preserves_http_status_and_redacts_token(self):
        self.api._token = "sensitive-local-token"
        self.api.create.side_effect = CreateUncertain("HTTP 503 sensitive-local-token", reason_code="http_error", http_status=503)
        logs = []
        self.engine.emit = lambda kind, value: logs.append(str(value))
        with self.assertRaises(CreateUncertain) as failure:
            self.engine.rent(settings(), NoWait())
        record = self.journal.read()
        self.assertEqual(record["http_status"], 503)
        self.assertEqual(failure.exception.http_status, 503)
        self.assertNotIn(self.api._token, json.dumps(record) + str(logs) + str(failure.exception))
        self.api.create.assert_called_once()

    def test_rehearsal_does_not_clear_existing_journal(self):
        self.journal.write(status="created", instance_uuid="already-rented")
        self.engine.wait_for_match(settings(), threading.Event())
        self.assertEqual(self.journal.read()["instance_uuid"], "already-rented")
        self.api.create.assert_not_called()

    def test_empty_stock_waits_then_cancels(self):
        self.api.machines.return_value = []
        with self.assertRaises(Cancelled):
            self.engine.rent(settings(), StopAtWait())
        self.api.create.assert_not_called()
        self.assertFalse(self.journal.path.exists())

    def test_price_above_cap_prevents_create(self):
        self.api.quote.return_value = Decimal("4.001")
        with self.assertRaises(Cancelled):
            self.engine.rent(settings(), StopAtWait())
        self.api.create.assert_not_called()

    def test_price_increases_during_final_check(self):
        # Inventory lists a price within the cap; the single final account quote
        # after the online check is higher and must prevent submission.
        self.api.quote.return_value = Decimal("5.00")
        with self.assertRaises(Cancelled):
            self.engine.rent(settings(), StopAtWait())
        self.api.create.assert_not_called()
        self.assertFalse(self.journal.path.exists())

    def test_fresh_quote_is_used_instead_of_inventory_price(self):
        self.api.machines.return_value = [host(machine_sku_info=[{"type": "payg", "current_price": 9000}])]
        self.engine.rent(settings(), threading.Event())
        self.api.create.assert_called_once()

    def test_cancel_during_final_quote_prevents_dispatch(self):
        cancel = threading.Event()
        def quote(_):
            cancel.set()
            return Decimal("3.50")
        self.api.quote.side_effect = quote
        with self.assertRaises(Cancelled):
            self.engine.rent(settings(), cancel)
        self.api.create.assert_not_called()

    def test_driver_too_old_for_selected_image(self):
        with self.assertRaises(Cancelled):
            self.engine.rent(settings(image_cuda="13.0"), StopAtWait())
        self.api.create.assert_not_called()

    def test_offline_machine_skipped(self):
        self.api.online.return_value = False
        with self.assertRaises(Cancelled):
            self.engine.rent(settings(), StopAtWait())
        self.api.create.assert_not_called()

    def test_login_failure_not_retried(self):
        self.api.machines.side_effect = LoginRequired("expired")
        with self.assertRaises(LoginRequired):
            self.engine.rent(settings(), NoWait())
        self.assertEqual(self.api.machines.call_count, 1)
        self.api.create.assert_not_called()

    def test_five_read_failures_stop(self):
        self.api.machines.side_effect = ReadUnavailable("offline")
        with self.assertRaises(QuickError):
            self.engine.rent(settings(), NoWait())
        self.assertEqual(self.api.machines.call_count, 5)
        self.api.create.assert_not_called()

    def test_snapshot_failure_cannot_trigger_second_create(self):
        self.api.snapshot.side_effect = ReadUnavailable("not ready")
        result = self.engine.rent(settings(), threading.Event())
        self.assertEqual(result["status"], "created")
        self.api.create.assert_called_once()

    def test_process_lock_prevents_concurrent_rent(self):
        with self.journal, self.assertRaises(QuickError):
            self.engine.rent(settings(), threading.Event())
        self.api.create.assert_not_called()

    def test_separate_journal_cannot_acquire_same_lock(self):
        with self.journal, self.assertRaises(QuickError):
            with RentalJournal(Path(self.temp.name)):
                self.fail("second lock should not succeed")

    def test_corrupt_journal_blocks_create(self):
        self.journal.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(QuickError):
            self.engine.rent(settings(), threading.Event())
        self.api.create.assert_not_called()


class APITests(unittest.TestCase):
    def setUp(self):
        self.api = AutoDL("test-secret-token")

    def test_login_required_response(self):
        self.api.opener.open = MagicMock(return_value=response({"code": "AuthorizeFailed", "data": None}))
        with self.assertRaises(LoginRequired):
            self.api.quote("test-host")

    def test_read_only_session_blocks_mutations_before_network(self):
        api = AutoDL("fake-test-token", read_only=True)
        api.opener.open = MagicMock()
        for path in ("/api/v1/order/instance/create/payg", "/api/v1/instance/power_off",
                     "/api/v1/instance/release", "/api/v1/order/pay"):
            with self.subTest(path=path), self.assertRaises(QuickError):
                api.request("POST", path, {})
        api.opener.open.assert_not_called()
        self.assertEqual(api.audit, [])

    def test_read_only_session_allows_quote_and_audits_only_path(self):
        api = AutoDL("fake-test-token", read_only=True)
        api.opener.open = MagicMock(return_value=response({"code": "Success", "data": {"origin_price": 3500}}))
        self.assertEqual(api.quote("test-host"), Decimal("3.50"))
        self.assertEqual(api.audit, [{"method": "POST", "path": "/api/v1/order/price/preview"}])
        self.assertNotIn("fake-test-token", json.dumps(api.audit))

    def test_quote_uses_milliyuan_without_discount_assumptions(self):
        self.api.opener.open = MagicMock(return_value=response({"code": "Success", "data": {"origin_price": 3500, "discount_list": []}}))
        self.assertEqual(self.api.quote("test-host"), Decimal("3.50"))

    def test_missing_quote_stops(self):
        self.api.opener.open = MagicMock(return_value=response({"code": "Success", "data": {"some_new_price": 3500}}))
        with self.assertRaises(QuickError):
            self.api.quote("test-host")

    def test_timeout_creation_is_uncertain(self):
        self.api.opener.open = MagicMock(side_effect=TimeoutError())
        with self.assertRaisesRegex(CreateUncertain, "TimeoutError") as failure:
            self.api.create({})
        self.assertEqual(failure.exception.reason_code, "TimeoutError")
        self.api.opener.open.assert_called_once()

    def test_http_creation_failure_keeps_status_without_retry(self):
        error = urllib.error.HTTPError("https://www.autodl.com", 503, "unavailable", {}, None)
        self.addCleanup(error.close)
        self.api.opener.open = MagicMock(side_effect=error)
        with self.assertRaises(CreateUncertain) as failure:
            self.api.create({})
        self.assertEqual(failure.exception.http_status, 503)
        self.assertEqual(failure.exception.reason_code, "http_error")
        self.api.opener.open.assert_called_once()

    def test_timeout_query_can_retry(self):
        self.api.opener.open = MagicMock(side_effect=TimeoutError())
        with patch("autodl_quick.core.time.sleep"), self.assertRaises(ReadUnavailable):
            self.api.quote("test-host")
        self.assertEqual(self.api.opener.open.call_count, 3)

    def test_quote_prefers_current_account_deal_price(self):
        self.api.opener.open = MagicMock(return_value=response({"code": "Success", "data": {"origin_price": 3030, "deal_price": 2880}}))
        self.assertEqual(self.api.quote("test-host"), Decimal("2.88"))

    def test_payg_price_is_used_when_deal_price_is_zero_placeholder(self):
        self.api.opener.open = MagicMock(return_value=response({"code": "Success", "data": {"origin_price": 1240, "deal_price": 0, "payg_price": 1180}}))
        self.assertEqual(self.api.quote("test-host"), Decimal("1.18"))

    def test_quote_falls_back_to_account_discount(self):
        self.api.opener.open = MagicMock(return_value=response({"code": "Success", "data": {"origin_price": 3030, "discount_list": [{"product_type": "instance", "origin_price": 3030, "total_reduction": 150}]}}))
        self.assertEqual(self.api.quote("test-host"), Decimal("2.88"))

    def test_secret_is_not_in_api_error(self):
        self.api.opener.open = MagicMock(return_value=response({"code": "BadToken", "msg": "test-secret-token invalid"}))
        with self.assertRaises(APIError) as error:
            self.api.quote("test-host")
        self.assertNotIn("test-secret-token", str(error.exception))

    def test_snapshot_whitelists_connection_info(self):
        self.api.opener.open = MagicMock(return_value=response({"code": "Success", "data": {
            "ssh_command": "ssh root@test.invalid", "root_password": "secret", "jupyter_token": "secret2"}}))
        self.assertEqual(self.api.snapshot("test-instance"), {"ssh_command": "ssh root@test.invalid"})

    def test_unexpected_create_response_does_not_retry(self):
        self.api.opener.open = MagicMock(return_value=response({"code": "Success", "data": {"new_uuid_field": "test-id"}}))
        with self.assertRaises(CreateUncertain):
            self.api.create({})
        self.api.opener.open.assert_called_once()

    def test_images_flatten_cuda_and_chip_filters(self):
        data = [{"label_name": "PyTorch", "children": [{"label_name": "2.8.0", "children": [
            {"label_name": "3.12", "children": [
                {"label_name": {"uuid": "new", "v": "12.8", "chip_corp": "nvidia", "cpu_arch": "x86"}},
                {"label_name": {"uuid": "old", "v": "12.6", "chip_corp": "nvidia", "cpu_arch": "x86"}},
                {"label_name": {"uuid": "arm", "v": "13.0", "chip_corp": "nvidia", "cpu_arch": "arm"}},
            ]}]}]}]
        self.api.request = MagicMock(return_value=data)
        self.assertEqual([i["uuid"] for i in self.api.images()], ["new", "old"])

    def test_pagination_and_deduplication(self):
        self.api.request = MagicMock(side_effect=[
            {"list": [host()], "max_page": 2, "result_total": 2},
            {"list": [host(), host(machine_id="second")], "max_page": 2, "result_total": 2},
        ])
        rows = self.api.machines(settings(), threading.Event())
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.api.request.call_count, 2)

    def test_redirect_does_not_forward_token_to_another_host(self):
        import urllib.request
        request = urllib.request.Request("https://www.autodl.com/api/v1/test", headers={"Authorization": "test-secret-token"})
        with self.assertRaises(QuickError):
            SameOriginRedirect().redirect_request(request, None, 302, "", {}, "https://other.invalid/")


if __name__ == "__main__":
    unittest.main()
