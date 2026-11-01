"""Automatic recovery of confirmed, shut-down rentals; all requests are mocked."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from autodl_quick.core import (APIError, AutoDL, Cancelled, Engine, LoginRequired,
                              QuickError, ReadUnavailable, RentalJournal, Settings)
from test_autodl_quick import response
from test_disk import disk_api


class RentalRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.journal = RentalJournal(self.directory)
        self.previous = {"status": "created", "instance_uuid": "old-instance", "operation": "old-operation",
                         "instance_name": "old-rental", "expand_disk_gb": 100}
        self.journal.write(**self.previous)
        self.original = self.journal.path.read_bytes()
        self.api = disk_api()
        self.api.instance_status.return_value = "shutdown"
        self.events = []
        self.engine = Engine(self.api, lambda *event: self.events.append(event), self.journal)
        self.cancel = threading.Event()

    def rent(self):
        return self.engine.auto_wait(Settings(expand_disk_gb=100), self.cancel, execute=True)

    def assert_blocked_without_archiving(self):
        self.assertEqual(self.journal.path.read_bytes(), self.original)
        self.assertEqual(list(self.directory.glob("rental-*.json")), [])
        self.api.machine_batches.assert_not_called()
        self.api.quote_detail.assert_not_called()
        self.api.create.assert_not_called()

    def test_verified_shutdown_is_archived_before_one_new_expanded_rental(self):
        def verify(instance_id, cancel):
            self.assertEqual(instance_id, "old-instance")
            self.assertIs(cancel, self.cancel)
            # Verification and archiving must hold the cross-process rental lock.
            with self.assertRaises(QuickError):
                with RentalJournal(self.directory):
                    self.fail("Another process acquired the rental lock during verification")
            return "shutdown"

        def create(payload):
            archives = list(self.directory.glob("rental-*.json"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(archives[0].read_bytes(), self.original)
            pending = self.journal.read()
            self.assertEqual(pending["status"], "submitting")
            self.assertNotEqual(pending["operation"], self.previous["operation"])
            self.assertEqual(payload["instance_info"]["expand_data_disk"], 100 * 1024 ** 3)
            return "new-instance"

        self.api.instance_status.side_effect = verify
        self.api.create.side_effect = create
        result = self.rent()
        self.assertEqual(result["instance_uuid"], "new-instance")
        self.api.instance_status.assert_called_once_with("old-instance", self.cancel)
        self.api.create.assert_called_once()
        self.api.quote_detail.assert_called_once_with("test-host", 100)
        self.assertTrue(any(kind == "log" and "已确认关机" in str(value) for kind, value in self.events))

    def test_legacy_rental_entry_also_recovers_the_stopped_instance(self):
        result = self.engine.rent(Settings(expand_disk_gb=100), self.cancel)
        self.assertEqual(result["instance_uuid"], "test-instance")
        self.assertEqual(len(list(self.directory.glob("rental-*.json"))), 1)
        self.api.instance_status.assert_called_once()
        self.api.create.assert_called_once()

    def test_explicit_shutdown_after_start_or_running_error_can_be_archived(self):
        for status in ("shutdown_by_starting_error", "shutdown_by_running_error"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                journal = RentalJournal(Path(directory))
                journal.write(**self.previous)
                api = disk_api()
                api.instance_status.return_value = status
                Engine(api, journal=journal).auto_wait(Settings(), threading.Event(), execute=True)
                self.assertEqual(len(list(Path(directory).glob("rental-*.json"))), 1)
                api.create.assert_called_once()

    def test_running_transitional_unknown_or_missing_states_preserve_the_record(self):
        for status in ("running", "starting", "shutting_down", "creating", "created", "restarting",
                       "unknown", "shutdown_pending", "SHUTDOWN", None):
            with self.subTest(status=status):
                self.api.reset_mock()
                self.api.instance_status.return_value = status
                with self.assertRaises(QuickError):
                    self.rent()
                self.assert_blocked_without_archiving()
                self.api.instance_status.assert_called_once()

    def test_uncertain_or_invalid_records_never_trigger_automatic_verification(self):
        changes = [
            {"status": "submitting"}, {"status": "failed"}, {"status": "unrecognized"},
            {"instance_uuid": ""}, {"instance_uuid": None}, {"instance_uuid": ["old-instance"]},
            {"instance_uuid": "old/instance"},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.api.reset_mock()
                self.journal.write(**dict(self.previous, **change))
                self.original = self.journal.path.read_bytes()
                with self.assertRaises(QuickError):
                    self.rent()
                self.assert_blocked_without_archiving()
                self.api.instance_status.assert_not_called()

    def test_verification_failure_never_archives_or_starts_inventory_requests(self):
        for error in (ReadUnavailable("offline"), LoginRequired("expired"), APIError("Denied", "denied"),
                      QuickError("invalid response")):
            with self.subTest(error=type(error).__name__):
                self.api.reset_mock()
                self.api.instance_status.side_effect = error
                with self.assertRaises(QuickError):
                    self.rent()
                self.assert_blocked_without_archiving()
                self.api.instance_status.assert_called_once()

    def test_cancel_before_verification_preserves_the_record_without_querying(self):
        self.cancel.set()
        with self.assertRaises(Cancelled):
            self.rent()
        self.assert_blocked_without_archiving()
        self.api.instance_status.assert_not_called()

    def test_cancel_during_verification_prevents_archiving(self):
        def verify(*_):
            self.cancel.set()
            return "shutdown"

        self.api.instance_status.side_effect = verify
        with self.assertRaises(Cancelled):
            self.rent()
        self.assert_blocked_without_archiving()

    def test_deadline_during_verification_prevents_archiving(self):
        now = [0.0]

        def verify(*_):
            now[0] = 601
            return "shutdown"

        self.api.instance_status.side_effect = verify
        with patch("autodl_quick.core.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(QuickError, "等待时限"):
                self.rent()
        self.assert_blocked_without_archiving()

    def test_a_record_changed_during_verification_is_not_archived(self):
        def verify(*_):
            self.journal.write(status="submitting", operation="changed-operation")
            return "shutdown"

        self.api.instance_status.side_effect = verify
        with self.assertRaisesRegex(QuickError, "记录已变化"):
            self.rent()
        self.assertEqual(self.journal.read()["operation"], "changed-operation")
        self.assertEqual(list(self.directory.glob("rental-*.json")), [])
        self.api.create.assert_not_called()

    def test_archive_failure_keeps_the_original_and_prevents_creation(self):
        with patch("autodl_quick.core.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(QuickError, "归档失败"):
                self.rent()
        self.assert_blocked_without_archiving()

    def test_no_prior_record_does_not_add_a_status_request(self):
        self.journal.path.unlink()
        self.rent()
        self.api.instance_status.assert_not_called()
        self.api.create.assert_called_once()

    def test_explicit_stock_rejection_does_not_need_old_instance_verification(self):
        self.journal.write(status="rejected_no_stock", machine_id="taken-host")
        self.rent()
        self.api.instance_status.assert_not_called()
        self.api.create.assert_called_once()

    def test_read_only_wait_neither_checks_nor_archives_a_successful_record(self):
        self.engine.auto_wait(Settings(expand_disk_gb=100), self.cancel)
        self.assertEqual(self.journal.path.read_bytes(), self.original)
        self.assertEqual(list(self.directory.glob("rental-*.json")), [])
        self.api.instance_status.assert_not_called()
        self.api.create.assert_not_called()


class InstanceStatusTests(unittest.TestCase):
    def setUp(self):
        self.api = AutoDL("offline-test-token", read_only=True)
        self.cancel = threading.Event()

    def test_read_only_lookup_matches_the_entire_uuid_and_audits_only_the_path(self):
        self.api.opener.open = MagicMock(return_value=response({"code": "Success", "data": {
            "list": [{"uuid": "old-instance-other", "status": "running"},
                     {"uuid": "old-instance", "status": "shutdown", "root_password": "never-store"}],
            "max_page": 5,
        }}))
        self.assertEqual(self.api.instance_status("old-instance", self.cancel), "shutdown")
        self.api.opener.open.assert_called_once()
        self.assertEqual(self.api.audit, [{"method": "POST", "path": "/api/v1/instance"}])
        request = self.api.opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"page_index": 1, "page_size": 100})
        self.assertNotIn("offline-test-token", json.dumps(self.api.audit))
        self.assertNotIn("never-store", json.dumps(self.api.audit))

    def test_instance_on_later_page_is_found_without_fetching_remaining_pages(self):
        self.api.request = MagicMock(side_effect=[
            {"list": [{"uuid": "another", "status": "shutdown"}], "max_page": 3},
            {"list": [{"uuid": "old-instance", "status": "running"}], "max_page": 3},
        ])
        self.assertEqual(self.api.instance_status("old-instance", self.cancel), "running")
        self.assertEqual(self.api.request.call_count, 2)
        self.assertEqual(self.api.request.call_args.args[2]["page_index"], 2)

    def test_another_shut_down_instance_does_not_count_as_the_requested_instance(self):
        self.api.request = MagicMock(return_value={"list": [{"uuid": "old-instance-other", "status": "shutdown"}], "result_total": 1})
        self.assertIsNone(self.api.instance_status("old-instance", self.cancel))
        self.api.request.assert_called_once()

    def test_empty_list_is_missing_rather_than_shutdown(self):
        self.api.request = MagicMock(return_value={"list": []})
        self.assertIsNone(self.api.instance_status("old-instance", self.cancel))

    def test_duplicate_uuid_with_conflicting_states_is_rejected(self):
        self.api.request = MagicMock(return_value={"list": [
            {"uuid": "old-instance", "status": "shutdown"}, {"uuid": "old-instance", "status": "running"},
        ]})
        with self.assertRaisesRegex(QuickError, "重复 ID"):
            self.api.instance_status("old-instance", self.cancel)

    def test_malformed_or_absent_status_is_rejected(self):
        for status in (None, "", True, 0, [], "shutdown\n", "unexpected state"):
            with self.subTest(status=status):
                self.api.request = MagicMock(return_value={"list": [{"uuid": "old-instance", "status": status}]})
                with self.assertRaises(QuickError):
                    self.api.instance_status("old-instance", self.cancel)

    def test_invalid_list_envelope_is_rejected(self):
        for data in (None, [], {}, {"list": None}, {"list": {}}):
            with self.subTest(data=data):
                self.api.request = MagicMock(return_value=data)
                with self.assertRaises(QuickError):
                    self.api.instance_status("old-instance", self.cancel)

    def test_repeated_page_stops_without_scanning_one_hundred_identical_pages(self):
        self.api.request = MagicMock(return_value={"list": [{"uuid": "another", "status": "shutdown"}], "max_page": 50})
        with self.assertRaisesRegex(QuickError, "分页未前进"):
            self.api.instance_status("old-instance", self.cancel)
        self.assertEqual(self.api.request.call_count, 2)

    def test_unknown_pagination_follows_a_full_page_and_stops_at_a_short_page(self):
        self.api.request = MagicMock(side_effect=[
            {"list": [{"uuid": f"other-{index}", "status": "shutdown"} for index in range(100)]},
            {"list": [{"uuid": "last-other", "status": "shutdown"}]},
        ])
        self.assertIsNone(self.api.instance_status("old-instance", self.cancel))
        self.assertEqual(self.api.request.call_count, 2)

    def test_invalid_id_is_rejected_before_network(self):
        self.api.request = MagicMock()
        for instance_id in ("", None, [], "old/instance", "old instance"):
            with self.subTest(instance_id=instance_id), self.assertRaises(QuickError):
                self.api.instance_status(instance_id, self.cancel)
        self.api.request.assert_not_called()

    def test_cancellation_between_pages_does_not_send_a_second_request(self):
        def first_page(*_):
            self.cancel.set()
            return {"list": [{"uuid": "another", "status": "shutdown"}], "max_page": 2}

        self.api.request = MagicMock(side_effect=first_page)
        with self.assertRaises(Cancelled):
            self.api.instance_status("old-instance", self.cancel)
        self.api.request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
