import copy
import fcntl
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ops import capture_power_trial as trial
from timelapse.config import DEFAULTS


NOW = datetime(2026, 9, 8, 6, tzinfo=timezone.utc)


def sample(second=0, power=1):
    return {
        "monotonic_seconds": second,
        "read_duration_seconds": 0.1,
        "status": {
            "powerInput": "PRESENT",
            "powerInput5vIo": "NOT_PRESENT",
            "battery": "NORMAL",
        },
        "charging": {"charging_enabled": False},
        "battery_voltage_mv": 4000,
        "io_voltage_mv": 5000,
        "io_power_w": power,
        "source_and_battery_valid": True,
        "errors": [],
    }


def device():
    status = SimpleNamespace()
    config = SimpleNamespace()
    for target, method, data in (
        (status, "GetStatus", sample()["status"]),
        (config, "GetChargingConfig", sample()["charging"]),
        (status, "GetBatteryVoltage", 4000),
        (status, "GetBatteryCurrent", 50),
        (status, "GetIoVoltage", 5000),
        (status, "GetIoCurrent", 200),
    ):
        setattr(target, method, Mock(return_value={"error": "NO_ERROR", "data": data}))
    return SimpleNamespace(status=status, config=config)


class GuardTests(unittest.TestCase):
    def test_external_power_and_disabled_charging_admit_unverified_profile_trial(self):
        trial.guard(sample())
        absent = sample()
        absent["status"]["battery"] = "NOT_PRESENT"
        absent["battery_voltage_mv"] = None
        trial.guard(absent)

    def test_unsafe_or_unknown_states_are_rejected(self):
        cases = [
            ("status", "powerInput", "BAD"),
            ("status", "powerInput", "NOT_PRESENT"),
            ("status", "powerInput", None),
            ("status", "powerInput5vIo", "PRESENT"),
            ("status", "powerInput5vIo", None),
            ("status", "battery", "CHARGING_FROM_IN"),
            ("status", "battery", None),
            ("charging", "charging_enabled", True),
            ("charging", "charging_enabled", 0),
            ("charging", "charging_enabled", None),
        ]
        for group, key, value in cases:
            row = sample()
            row[group][key] = value
            with self.subTest(group=group, key=key, value=value):
                with self.assertRaises(trial.TrialRejected):
                    trial.guard(row)
        for voltage in (3799, 4251, None, float("nan"), "4000"):
            with self.subTest(voltage=voltage):
                with self.assertRaises(trial.TrialRejected):
                    trial.guard(dict(sample(), battery_voltage_mv=voltage))
        for voltage in (4799, 5251, None, float("nan"), "5000"):
            with self.subTest(io_voltage=voltage):
                with self.assertRaises(trial.TrialRejected):
                    trial.guard(dict(sample(), io_voltage_mv=voltage))
        with self.assertRaises(trial.TrialRejected):
            trial.guard(dict(sample(), errors=["I2C failed"]))
        with self.assertRaises(trial.TrialRejected):
            trial.guard(dict(sample(), errors=None))

    def test_live_sample_preserves_raw_readings_and_signed_battery_estimate(self):
        hardware = device()
        hardware.status.GetBatteryCurrent.return_value["data"] = -10
        row = trial.read_sample(
            hardware, clock=Mock(side_effect=[1, 1.2]), now=lambda: NOW
        )
        self.assertEqual(row["io_power_w"], 1)
        self.assertEqual(row["battery_current_estimate_ma"], -10)
        self.assertAlmostEqual(row["monotonic_seconds"], 1.1)
        self.assertTrue(row["source_and_battery_valid"])
        self.assertIn("GetStatus", row["raw"])

    def test_invalid_api_data_invalidates_sample_without_fabricating_zero_power(self):
        hardware = device()
        hardware.status.GetIoCurrent.return_value = {"error": "COMMUNICATION_ERROR"}
        row = trial.read_sample(hardware)
        self.assertIsNone(row["io_power_w"])
        self.assertFalse(row["source_and_battery_valid"])
        self.assertIn("GetIoCurrent", row["errors"][0])


class EnergyTests(unittest.TestCase):
    def rows(self, during=3):
        return [sample(t, during if 4 <= t <= 6 else 1) for t in range(11)]

    def test_time_integral_subtracts_matched_baseline(self):
        result = trial.summarize(self.rows(), 0, 3, 7, 10)
        self.assertTrue(result["valid"])
        self.assertEqual(result["baseline_mean_w"], 1)
        self.assertEqual(result["peak_observed_window_w"], 3)
        self.assertAlmostEqual(result["capture_total_wh"], 10 / 3600)
        self.assertAlmostEqual(result["capture_incremental_wh"], 6 / 3600)
        self.assertAlmostEqual(result["total_window_wh"], 16 / 3600)

    def test_negative_incremental_energy_is_preserved(self):
        result = trial.summarize(self.rows(during=0.5), 0, 3, 7, 10)
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["capture_incremental_wh"], -1.5 / 3600)

    def test_invalid_coverage_gaps_errors_or_source_loss_have_no_numeric_energy(self):
        cases = [self.rows()[1:], self.rows()[:-1], self.rows()[:4] + self.rows()[6:]]
        for change in (
            {"errors": ["I2C failed"]},
            {"source_and_battery_valid": False},
            {"io_power_w": None},
            {"read_duration_seconds": 3},
        ):
            rows = self.rows()
            rows[5].update(change)
            cases.append(rows)
        for rows in cases:
            with self.subTest(rows=rows):
                result = trial.summarize(rows, 0, 3, 7, 10)
                self.assertFalse(result["valid"])
                self.assertNotIn("capture_incremental_wh", result)

    def test_boundary_interpolation_integrates_partial_sample_intervals(self):
        result = trial.summarize(self.rows(), 0.5, 3.5, 6.5, 9.5)
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["capture_total_wh"], 8.5 / 3600)


class TrialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.options = SimpleNamespace(
            config=None,
            output_dir=self.root / "trial",
            deadline=NOW + timedelta(hours=1),
            max_attempts=6,
            baseline_seconds=20,
            interval_seconds=1,
        )

    def test_expired_trial_records_rejection_without_touching_hardware(self):
        self.options.deadline = NOW - timedelta(seconds=1)
        factory = Mock()
        with patch.object(trial, "utc_now", return_value=NOW):
            result = trial.run_trial(self.options, device_factory=factory)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("deadline", result["reason"])
        factory.assert_not_called()
        saved = json.loads(Path(result["record"]).read_text())
        self.assertEqual(saved["attempt"], 1)
        self.assertEqual(saved["status"], "rejected")

    def test_six_rejections_exhaust_trial_and_seventh_writes_nothing(self):
        self.options.deadline = NOW - timedelta(seconds=1)
        with patch.object(trial, "utc_now", return_value=NOW):
            for _ in range(6):
                self.assertEqual(trial.run_trial(self.options)["status"], "rejected")
            final = trial.run_trial(self.options)
        self.assertEqual(final["status"], "refused")
        self.assertEqual(len(list(self.options.output_dir.glob("attempt-*.json"))), 6)

    def test_process_lock_prevents_concurrent_attempt(self):
        self.options.output_dir.mkdir()
        with (self.options.output_dir / ".trial.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = trial.run_trial(self.options)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(list(self.options.output_dir.glob("attempt-*.json")), [])

    def test_failed_capture_keeps_durable_failed_attempt(self):
        with (
            patch.object(trial, "utc_now", return_value=NOW),
            patch.object(trial.Path, "read_text", return_value="test-boot"),
            patch.object(
                trial, "execute_attempt", side_effect=RuntimeError("camera failed")
            ),
        ):
            result = trial.run_trial(self.options, device_factory=Mock())
        self.assertEqual(result["status"], "failed")
        saved = json.loads(Path(result["record"]).read_text())
        self.assertEqual(saved["status"], "failed")
        self.assertFalse(saved["energy"]["valid"])

    def test_near_deadline_is_rejected_before_baseline_or_camera(self):
        self.options.deadline = NOW + timedelta(seconds=60)
        hardware = device()
        with patch.object(trial, "utc_now", return_value=NOW):
            with self.assertRaisesRegex(trial.TrialRejected, "insufficient time"):
                trial.execute_attempt({}, self.options, hardware, DEFAULTS)
        hardware.status.GetStatus.assert_not_called()

    def test_explicit_trial_captures_without_marking_battery_profile_verified(self):
        rows = [sample(t, 3 if 4 <= t <= 6 else 1) for t in range(11)]
        sampler = Mock(samples=rows, failure=None, abort_reason=None)
        sampler.snapshot.side_effect = [rows[0], rows[3], rows[10]]
        config = copy.deepcopy(DEFAULTS)
        config["spool"] = str(self.root / "spool")
        config["min_free_bytes"] = 0
        metadata = {"filename": "photo.jpg", "capture_duration_seconds": 2}
        camera = Mock(return_value=metadata)
        record = {"time_source": "UNSYNC", "status": "started"}
        with (
            patch.object(trial, "read_sample", return_value=sample()),
            patch.object(trial, "Sampler", return_value=sampler),
            patch.object(trial, "utc_now", return_value=NOW),
            patch.object(trial.time, "sleep"),
            patch.object(trial.time, "monotonic", side_effect=[3, 7]),
        ):
            trial.execute_attempt(record, self.options, device(), config, camera)
        self.assertEqual(record["status"], "captured")
        self.assertEqual(record["capture"], metadata)
        self.assertEqual(record["energy"]["capture_interval_seconds"], 4)
        self.assertFalse(config["power"]["battery_profile_verified"])
        self.assertLessEqual(camera.call_args.kwargs["timeout_seconds"], 45)
        self.assertEqual(camera.call_args.kwargs["cancelled"], sampler.cancelled)
        self.assertEqual(camera.call_args.kwargs["lock_timeout_seconds"], 3)
        sampler.stop.assert_called_once()

    def test_source_failure_after_baseline_prevents_camera(self):
        good, bad = sample(0), sample(20)
        bad["status"]["powerInput"] = "BAD"
        bad["source_and_battery_valid"] = False
        sampler = Mock(samples=[good, bad], failure=None, abort_reason=None)
        sampler.snapshot.side_effect = [good, bad]
        camera = Mock()
        record = {"time_source": "UNSYNC"}
        with (
            patch.object(trial, "read_sample", return_value=good),
            patch.object(trial, "Sampler", return_value=sampler),
            patch.object(trial, "utc_now", return_value=NOW),
            patch.object(trial.time, "sleep"),
        ):
            with self.assertRaises(trial.TrialRejected):
                trial.execute_attempt(record, self.options, device(), DEFAULTS, camera)
        camera.assert_not_called()
        self.assertEqual(record["samples"], [good, bad])

    def test_all_background_device_reads_use_one_worker_thread(self):
        threads = []

        def read(_device):
            threads.append(threading.get_ident())
            return sample(len(threads))

        sampler = trial.Sampler(None, 1, reader=read)
        sampler.thread.start()
        try:
            sampler.snapshot()
            sampler.snapshot()
        finally:
            sampler.stop()
        self.assertEqual(len(set(threads)), 1)
        self.assertNotEqual(threads[0], threading.get_ident())

    def test_cancellation_latches_transient_hardware_failure(self):
        sampler = trial.Sampler(None, 1)
        sampler.samples = [sample(1)]
        with patch.object(trial.time, "monotonic", return_value=2):
            self.assertFalse(sampler.cancelled())
            failed = sample(2)
            failed["charging"]["charging_enabled"] = True
            sampler.samples.append(failed)
            self.assertTrue(sampler.cancelled())
            sampler.samples = [sample(2)]
            self.assertTrue(sampler.cancelled())
        self.assertIn("Charging", sampler.abort_reason)

    def test_stale_or_failed_sampler_cancels_camera(self):
        sampler = trial.Sampler(None, 1)
        sampler.samples = [sample(1)]
        with patch.object(trial.time, "monotonic", return_value=3.51):
            self.assertTrue(sampler.cancelled())
        self.assertIn("stale", sampler.abort_reason)
        sampler = trial.Sampler(None, 1)
        sampler.failure = "I2C worker failed"
        self.assertTrue(sampler.cancelled())


if __name__ == "__main__":
    unittest.main()
