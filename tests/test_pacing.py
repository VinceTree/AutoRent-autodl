"""Offline race and timing regressions. No paid or real network requests."""

from decimal import Decimal
from email.utils import formatdate
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from autodl_quick.core import (APIError, AutoDL, Cancelled, CreateUncertain, Engine,
                              QuickError, ReadUnavailable, RentalJournal, Settings)
from autodl_quick.pacing import PollCadence
from test_autodl_quick import host, response
from test_finder import api_fixture


class VirtualClock:
    def __init__(self):
        self.now = 0.0


class VirtualWait(threading.Event):
    def __init__(self, clock, *, stop_after=None):
        super().__init__()
        self.clock = clock
        self.waits = []
        self.stop_after = stop_after

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.clock.now += timeout
        if self.stop_after is not None and len(self.waits) >= self.stop_after:
            self.set()
        return self.is_set()


class CadenceTests(unittest.TestCase):
    def setUp(self):
        jitter = patch("autodl_quick.pacing.random.uniform", return_value=1)
        jitter.start()
        self.addCleanup(jitter.stop)

    def test_query_duration_counts_toward_interval_without_catchup(self):
        cadence = PollCadence(1, 0)
        self.assertAlmostEqual(cadence.delay(0, 0.3), 0.7)
        self.assertEqual(cadence.delay(1, 4), 0.1)

    def test_idle_slows_down_and_new_candidates_restore_fast_polling(self):
        cadence = PollCadence(1, 0)
        self.assertEqual(cadence.delay(21, 21), 3)
        cadence.activity(22)
        self.assertEqual(cadence.delay(22, 22), 1)
        self.assertEqual(cadence.delay(42, 42), 3)

    def test_failures_back_off_and_a_success_resets_backoff(self):
        cadence = PollCadence(1, 0)
        self.assertEqual([cadence.delay(0, 0, failed=True) for _ in range(7)],
                         [2, 4, 8, 16, 32, 60, 60])
        self.assertEqual(cadence.delay(0, 0), 1)
        self.assertEqual(cadence.delay(0, 0, failed=True), 2)

    def test_server_cooldown_is_never_shortened(self):
        cadence = PollCadence(1, 0)
        self.assertEqual(cadence.delay(0, 0, failed=True, retry_after=180), 180)

    def test_long_user_interval_is_respected_including_on_error(self):
        cadence = PollCadence(120, 0)
        self.assertEqual(cadence.delay(21, 21), 120)
        self.assertEqual(cadence.delay(21, 21, failed=True), 120)

    def test_positive_jitter_does_not_make_probes_faster(self):
        with patch("autodl_quick.pacing.random.uniform", return_value=1.1):
            self.assertAlmostEqual(PollCadence(1, 0).delay(0, 0.2), 0.9)


class WaitingRaceTests(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock()
        for target, options in (
            ("autodl_quick.core.time.monotonic", {"side_effect": lambda: self.clock.now}),
            ("autodl_quick.pacing.random.uniform", {"return_value": 1}),
        ):
            mocked = patch(target, **options)
            mocked.start()
            self.addCleanup(mocked.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.api = api_fixture()
        self.events = []
        self.journal = RentalJournal(Path(directory.name))
        self.engine = Engine(self.api, lambda *event: self.events.append(event), self.journal)
        self.cancel = VirtualWait(self.clock)

    def run_rent(self, chosen=None):
        return self.engine.auto_wait(chosen or Settings(), self.cancel, execute=True)

    def test_stale_zero_summary_cannot_hide_a_newly_available_host(self):
        self.api.region_stock.return_value = {"RTX 5090": {"idle": 0, "total": 100}}
        self.assertEqual(self.run_rent()["instance_uuid"], "test-instance")
        self.api.region_stock.assert_not_called()
        self.api.machine_batches.assert_called_once()
        self.api.quote.assert_called_once_with("test-host")

    def test_final_quote_is_after_online_check_and_immediately_before_create(self):
        steps = []
        self.api.online.side_effect = lambda _: steps.append("online") or True
        self.api.quote.side_effect = lambda _: steps.append("quote") or Decimal("2.88")
        self.api.create.side_effect = lambda _: steps.append("create") or "test-instance"
        self.run_rent()
        self.assertEqual(steps, ["online", "quote", "create"])

    def test_offline_first_host_does_not_starve_second_host(self):
        self.api.machine_batches.return_value = [[host(machine_id="a"), host(machine_id="b")]]
        self.api.online.side_effect = [False, True]
        self.assertEqual(self.run_rent()["machine_id"], "b")
        self.api.quote.assert_called_once_with("b")
        self.assertEqual(self.cancel.waits, [])

    def test_expensive_first_host_does_not_starve_second_host(self):
        self.api.machine_batches.return_value = [[host(machine_id="a"), host(machine_id="b")]]
        self.api.quote.side_effect = [Decimal("5"), Decimal("2")]
        self.assertEqual(self.run_rent(Settings(max_price="3"))["machine_id"], "b")
        self.api.machine_batches.assert_called_once()
        self.api.create.assert_called_once()
        self.assertEqual(self.cancel.waits, [])

    def test_incompatible_first_host_does_not_starve_second_host(self):
        self.api.machine_batches.return_value = [[host(machine_id="a", highest_cuda_version="12.6"),
                                                  host(machine_id="b")]]
        self.assertEqual(self.run_rent()["machine_id"], "b")
        self.api.quote.assert_called_once_with("b")
        self.assertEqual(self.cancel.waits, [])

    def test_explicit_stock_rejection_tries_same_page_without_requery_or_sleep(self):
        for code in ("NoStock", "GPUNotEnough", "GpuNotEnough"):
            with self.subTest(code=code):
                self.api.reset_mock()
                self.journal.archive()
                self.api.machine_batches.return_value = [[host(machine_id="a"), host(machine_id="b")]]
                self.api.create.side_effect = [APIError(code, "taken"), "test-instance"]
                self.assertEqual(self.run_rent()["machine_id"], "b")
                self.api.machine_batches.assert_called_once()
                self.assertEqual([call.args[0]["instance_info"]["machine_id"]
                                  for call in self.api.create.call_args_list], ["a", "b"])
                self.assertEqual(self.cancel.waits, [])

    def test_rejected_host_can_be_retried_only_after_its_cooldown(self):
        submitted_at = []

        def create(_):
            submitted_at.append(self.clock.now)
            if len(submitted_at) == 1:
                raise APIError("NoStock", "taken")
            return "test-instance"

        self.api.create.side_effect = create
        self.run_rent()
        self.assertEqual(submitted_at, [0, 10])
        self.assertEqual(self.api.quote.call_count, 2)
        self.api.images.assert_called_once()

    def test_next_page_fallback_does_not_retry_a_duplicate_host(self):
        self.api.machine_batches.return_value = [[host(machine_id="a")],
                                                 [host(machine_id="a"), host(machine_id="b")]]
        self.api.create.side_effect = [APIError("NoStock", "taken"), "test-instance"]
        self.assertEqual(self.run_rent()["machine_id"], "b")
        self.assertEqual(self.api.create.call_count, 2)
        self.assertEqual(self.cancel.waits, [])

    def test_empty_market_stops_at_deadline_and_reports_actual_waits(self):
        self.api.machine_batches.return_value = [[]]
        with self.assertRaisesRegex(QuickError, "等待时限"):
            self.run_rent(Settings(wait_minutes=1))
        self.assertEqual(self.clock.now, 60)
        self.api.create.assert_not_called()
        polls = [value for kind, value in self.events if kind == "poll"]
        self.assertEqual([poll["next_delay_seconds"] for poll in polls[:2]], [1, 1])
        self.assertIn(3, [poll["next_delay_seconds"] for poll in polls])
        self.assertLessEqual(polls[-1]["next_delay_seconds"], 3)

    def test_read_failures_back_off_then_resume_without_creating_twice(self):
        self.api.machine_batches.side_effect = [ReadUnavailable("offline"), ReadUnavailable("offline"), [[host()]]]
        self.run_rent()
        self.assertEqual(self.cancel.waits, [2, 4])
        self.api.create.assert_called_once()

    def test_server_cooldown_longer_than_budget_never_sends_another_probe(self):
        self.api.machine_batches.side_effect = ReadUnavailable("limited", retry_after=90)
        with self.assertRaisesRegex(QuickError, "等待时限"):
            self.run_rent(Settings(wait_minutes=1))
        self.assertEqual(self.cancel.waits, [60])
        self.api.machine_batches.assert_called_once()
        self.api.create.assert_not_called()

    def test_stop_during_server_cooldown_prevents_another_request(self):
        self.cancel = VirtualWait(self.clock, stop_after=1)
        self.api.machine_batches.side_effect = ReadUnavailable("limited", retry_after=30)
        with self.assertRaises(Cancelled):
            self.run_rent()
        self.assertEqual(self.cancel.waits, [30])
        self.api.machine_batches.assert_called_once()

    def test_query_past_deadline_cannot_start_preparation_or_creation(self):
        def slow_query(*_):
            self.clock.now = 61
            return [[host()]]

        self.api.machine_batches.side_effect = slow_query
        with self.assertRaisesRegex(QuickError, "等待时限"):
            self.run_rent(Settings(wait_minutes=1))
        self.api.quote.assert_not_called()
        self.api.images.assert_not_called()
        self.api.create.assert_not_called()

    def test_final_quote_past_deadline_cannot_leave_a_submission_marker(self):
        def slow_quote(_):
            self.clock.now = 61
            return Decimal("2.88")

        self.api.quote.side_effect = slow_quote
        with self.assertRaisesRegex(QuickError, "等待时限"):
            self.run_rent(Settings(wait_minutes=1))
        self.assertFalse(self.journal.path.exists())
        self.api.create.assert_not_called()

    def test_successful_auto_wait_is_read_only_and_keeps_result_fields(self):
        result = self.engine.auto_wait(Settings(), self.cancel)
        self.assertEqual(result["status"], "rehearsed")
        self.assertEqual(result["current_price"], "2.88")
        self.assertEqual(result["image_uuid"], "auto-image")
        self.api.create.assert_not_called()
        self.assertFalse(self.journal.path.exists())

    def test_read_only_wait_keeps_a_manually_configured_image(self):
        chosen = Settings(auto_image=False, image_uuid="configured-image", image_cuda="12.8")
        result = self.engine.auto_wait(chosen, self.cancel)
        self.assertEqual(result["image_uuid"], "configured-image")
        self.api.images.assert_not_called()
        self.api.create.assert_not_called()

    def test_first_create_needs_only_four_reads_with_an_uncached_auto_image(self):
        api = AutoDL("offline-test-token")
        requested = []
        submitted_at = []
        replies = {
            "/api/v1/user/machine/list": {"list": [host()], "max_page": 1},
            "/api/v1/image/all": [{"label_name": "PyTorch", "children": [
                {"label_name": {"uuid": "auto-image", "v": "12.8", "chip_corp": "nvidia", "cpu_arch": "x86"}}
            ]}],
            "/api/v1/machine/check_machine_online": 2,
            "/api/v1/order/price/preview": {"origin_price": 3000},
            "/api/v1/order/instance/create/payg": "test-instance",
            "/api/v1/instance/snapshot": {},
        }

        def reply(request, **_):
            path = request.full_url.split("www.autodl.com", 1)[1].split("?", 1)[0]
            requested.append(path)
            if path.endswith("/create/payg"):
                submitted_at.append(self.clock.now)
            self.clock.now += 0.25  # Simulated per-request latency; no real I/O.
            return response({"code": "Success", "data": replies[path]})

        api.opener.open = MagicMock(side_effect=reply)
        engine = Engine(api, journal=self.journal)
        self.assertEqual(engine.auto_wait(Settings(), self.cancel, execute=True)["instance_uuid"], "test-instance")
        self.assertEqual(requested[:5], [
            "/api/v1/user/machine/list", "/api/v1/image/all",
            "/api/v1/machine/check_machine_online", "/api/v1/order/price/preview",
            "/api/v1/order/instance/create/payg",
        ])
        self.assertEqual(submitted_at, [1.0])


class PollingRequestTests(unittest.TestCase):
    def setUp(self):
        self.api = AutoDL("offline-test-token")
        self.clock = VirtualClock()
        clock_patch = patch("autodl_quick.core.time.monotonic", side_effect=lambda: self.clock.now)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.cancel = threading.Event()

    def test_polling_read_uses_one_attempt_and_shorter_timeout(self):
        self.api.opener.open = MagicMock(side_effect=TimeoutError())
        with self.api.polling(self.cancel, 100), patch("autodl_quick.core.time.sleep") as sleep:
            with self.assertRaises(ReadUnavailable):
                self.api.quote("test-host")
        self.api.opener.open.assert_called_once()
        self.assertEqual(self.api.opener.open.call_args.kwargs["timeout"], 5)
        sleep.assert_not_called()
        self.assertEqual(self.api.read_timeout(), 25)

    def test_read_timeout_is_limited_by_remaining_budget(self):
        self.api.opener.open = MagicMock(return_value=response({"code": "Success", "data": {"origin_price": 3000}}))
        self.clock.now = 9
        with self.api.polling(self.cancel, 10):
            self.assertEqual(self.api.quote("test-host"), Decimal("3"))
        self.assertEqual(self.api.opener.open.call_args.kwargs["timeout"], 1)

    def test_cancelled_or_expired_reads_do_not_open_a_connection(self):
        self.api.opener.open = MagicMock()
        with self.api.polling(self.cancel, 0), self.assertRaisesRegex(QuickError, "等待时限"):
            self.api.quote("test-host")
        self.cancel.set()
        with self.api.polling(self.cancel, 100), self.assertRaises(Cancelled):
            self.api.quote("test-host")
        self.api.opener.open.assert_not_called()

    def test_rate_limit_retry_after_seconds_is_preserved(self):
        error = urllib.error.HTTPError("https://www.autodl.com", 429, "limited", {"Retry-After": "37"}, None)
        self.addCleanup(error.close)
        self.api.opener.open = MagicMock(side_effect=error)
        with self.api.polling(self.cancel, 100), self.assertRaises(ReadUnavailable) as caught:
            self.api.quote("test-host")
        self.assertEqual(caught.exception.retry_after, 37)
        self.api.opener.open.assert_called_once()

    def test_retry_after_http_date_is_supported(self):
        error = urllib.error.HTTPError("https://www.autodl.com", 503, "busy",
                                       {"Retry-After": formatdate(130, usegmt=True)}, None)
        self.addCleanup(error.close)
        self.api.opener.open = MagicMock(side_effect=error)
        with self.api.polling(self.cancel, 100), patch("autodl_quick.core.time.time", return_value=100):
            with self.assertRaises(ReadUnavailable) as caught:
                self.api.quote("test-host")
        self.assertEqual(caught.exception.retry_after, 30)

    def test_bad_retry_after_header_has_a_safe_rate_limit_fallback(self):
        for header in ("", "bad", "NaN", "Infinity", "-3"):
            with self.subTest(header=header):
                error = urllib.error.HTTPError("https://www.autodl.com", 429, "limited", {"Retry-After": header}, None)
                self.addCleanup(error.close)
                self.api.opener.open = MagicMock(side_effect=error)
                with self.api.polling(self.cancel, 100), self.assertRaises(ReadUnavailable) as caught:
                    self.api.quote("test-host")
                self.assertEqual(caught.exception.retry_after, 5)

    def test_create_keeps_its_timeout_and_single_attempt_inside_polling(self):
        self.api.opener.open = MagicMock(side_effect=TimeoutError())
        with self.api.polling(self.cancel, 100), self.assertRaises(CreateUncertain):
            self.api.create({})
        self.api.opener.open.assert_called_once()
        self.assertEqual(self.api.opener.open.call_args.kwargs["timeout"], 25)

    def test_waiting_does_not_page_through_thousands_of_busy_hosts(self):
        self.api.request = MagicMock(return_value={
            "list": [host(gpu_idle_num=0)], "max_page": 50, "result_total": 4500,
        })
        with self.api.polling(self.cancel, 100):
            pages = list(self.api.machine_batches(Settings(), self.cancel))
        self.assertEqual(len(pages), 1)
        self.api.request.assert_called_once()

    def test_unknown_idle_count_does_not_prematurely_hide_later_pages(self):
        self.api.request = MagicMock(side_effect=[
            {"list": [host(gpu_idle_num=None)], "max_page": 2},
            {"list": [host(machine_id="ready")], "max_page": 2},
        ])
        with self.api.polling(self.cancel, 100):
            pages = list(self.api.machine_batches(Settings(), self.cancel))
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[1][0]["machine_id"], "ready")


class PollingSettingsTests(unittest.TestCase):
    def test_fractional_intervals_validate_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            Settings(poll_seconds=0.5).save(path)
            self.assertEqual(Settings.load(path).poll_seconds, 0.5)

    def test_nonfinite_or_invalid_intervals_are_rejected(self):
        for interval in (False, "1", float("nan"), float("inf"), -1, 0.49, 3601):
            with self.subTest(interval=interval), self.assertRaises(QuickError):
                Settings(poll_seconds=interval).validate()


if __name__ == "__main__":
    unittest.main()
