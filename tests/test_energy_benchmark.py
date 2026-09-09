import copy
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ops import energy_benchmark as bench


def sample(second, power=1):
    return {
        "monotonic_seconds": second,
        "read_duration_seconds": 0.02,
        "io_voltage_mv": 5000,
        "io_current_ma": power * 200,
        "io_power_w": power,
        "errors": [],
    }


def guard(second=0):
    return {
        "monotonic_seconds": second,
        "read_duration_seconds": 0.02,
        "status": {
            "powerInput": "PRESENT",
            "powerInput5vIo": "NOT_PRESENT",
            "battery": "NORMAL",
        },
        "charging": {"charging_enabled": False},
        "cpu_temperature_c": 40,
        "errors": [],
    }


def device():
    return SimpleNamespace(
        status=SimpleNamespace(
            GetIoVoltage=Mock(return_value={"error": "NO_ERROR", "data": 5000}),
            GetIoCurrent=Mock(return_value={"error": "NO_ERROR", "data": 200}),
            GetStatus=Mock(
                return_value={"error": "NO_ERROR", "data": guard()["status"]}
            ),
        ),
        config=SimpleNamespace(
            GetChargingConfig=Mock(
                return_value={"error": "NO_ERROR", "data": {"charging_enabled": False}}
            ),
        ),
    )


def complete_trial():
    return {
        "status": "complete",
        "returncode": 0,
        "baseline_start": 0.5,
        "command_start": 2.5,
        "command_end": 5.5,
        "post_end": 7.5,
        "samples": [sample(index, 1 + 0.2 * index) for index in range(9)],
        "guards": [guard(index) for index in range(9)],
    }


class IntegrationTests(unittest.TestCase):
    def test_constant_power_with_exact_clipped_boundaries(self):
        rows = [sample(index, 2) for index in range(6)]
        self.assertAlmostEqual(bench.integrate(rows, 0.25, 4.75, 1.5), 9)

    def test_ramp_integral_interpolates_both_boundaries(self):
        rows = [sample(index, 0.5 + index * 0.5) for index in range(6)]
        start, end = 0.2, 4.7
        expected = 0.5 * (end - start) + 0.25 * (end * end - start * start)
        self.assertAlmostEqual(bench.integrate(rows, start, end, 1.5), expected)

    def test_gaps_missing_coverage_and_invalid_timestamps_are_rejected(self):
        rows = [sample(index) for index in range(6)]
        for broken in (rows[1:], rows[:-1], rows[:2] + rows[3:], rows[:2] + rows[1:]):
            with self.subTest(broken=broken):
                with self.assertRaises(bench.BenchmarkRejected):
                    bench.integrate(broken, 0.5, 4.5, 1.5)

    def test_any_outlier_rejects_energy_instead_of_filtering_it(self):
        rows = [sample(index) for index in range(6)]
        rows[0] = sample(0, 8)
        with self.assertRaisesRegex(bench.BenchmarkRejected, "plausibility"):
            bench.integrate(rows, 2, 4, 1.5)
        self.assertEqual(bench.integrate(rows, 2, 4, 1.5, max_current_a=2), 2)

    def test_invalid_read_errors_power_and_read_duration_reject_energy(self):
        for change in (
            {"errors": ["I2C failed"]},
            {"io_current_ma": None},
            {"io_current_ma": -1},
            {"io_current_ma": float("nan")},
            {"io_current_ma": True},
            {"io_voltage_mv": 4700},
            {"io_power_w": 100},
            {"read_duration_seconds": 2},
        ):
            rows = [sample(index) for index in range(6)]
            rows[2].update(change)
            with self.subTest(change=change):
                with self.assertRaises(bench.BenchmarkRejected):
                    bench.integrate(rows, 0.5, 4.5, 1.5)

    def test_summary_reports_baseline_total_and_incremental_estimate(self):
        result = bench.summarize(complete_trial(), 1.5)
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["baseline_mean_w"], 1.3)
        self.assertAlmostEqual(result["post_idle_mean_w"], 2.3)
        self.assertAlmostEqual(result["command_total_j"], 5.4)
        self.assertAlmostEqual(result["command_incremental_estimate_j"], 1.5)
        self.assertAlmostEqual(result["command_total_wh"], 5.4 / 3600)
        self.assertAlmostEqual(result["command_plus_post_total_j"], 10)
        self.assertAlmostEqual(
            result["command_plus_recovery_incremental_estimate_j"], 3.5
        )
        self.assertEqual(result["recovery_duration_seconds"], 2)
        self.assertEqual(result["sample_coverage"]["max_gap_seconds"], 1)

    def test_recovery_estimate_includes_decaying_post_command_power(self):
        trial = complete_trial()
        trial.update(baseline_start=0, command_start=2, command_end=4, post_end=8)
        trial["samples"] = [
            sample(index, power)
            for index, power in enumerate((1, 1, 1, 3, 3, 2, 1, 1, 1))
        ]
        result = bench.summarize(trial, 1.5)
        self.assertTrue(result["valid"])
        self.assertEqual(result["baseline_mean_w"], 1)
        self.assertEqual(result["command_total_j"], 5)
        self.assertEqual(result["command_incremental_estimate_j"], 3)
        self.assertEqual(result["command_plus_post_total_j"], 11)
        self.assertEqual(result["command_plus_recovery_incremental_estimate_j"], 5)
        self.assertEqual(result["recovery_duration_seconds"], 4)

    def test_negative_incremental_estimates_are_preserved(self):
        trial = complete_trial()
        trial["samples"] = [sample(index, 3 - 0.2 * index) for index in range(9)]
        result = bench.summarize(trial, 1.5)
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["command_incremental_estimate_j"], -1.5)

    def test_uncovered_invalid_or_gapped_guards_invalidate_whole_trial(self):
        cases = []
        for guards in (
            [guard(index) for index in range(1, 9)],
            [guard(index) for index in range(8)],
            [guard(0), guard(4), guard(8)],
        ):
            trial = complete_trial()
            trial["guards"] = guards
            cases.append(trial)
        trial = complete_trial()
        trial["guards"][4]["status"]["powerInput"] = "WEAK"
        cases.append(trial)
        trial = complete_trial()
        trial["returncode"] = 7
        cases.append(trial)
        for trial in cases:
            with self.subTest(trial=trial):
                result = bench.summarize(trial, 1.5)
                self.assertFalse(result["valid"])
                self.assertNotIn("command_total_j", result)
                self.assertNotIn("command_incremental_estimate_j", result)


class GuardTests(unittest.TestCase):
    def test_usb_only_and_disabled_charging_are_required(self):
        for group, key, value in (
            ("status", "powerInput", "WEAK"),
            ("status", "powerInput", "BAD"),
            ("status", "powerInput", "NOT_PRESENT"),
            ("status", "powerInput5vIo", "PRESENT"),
            ("status", "powerInput5vIo", None),
            ("status", "battery", "CHARGING_FROM_IN"),
            ("charging", "charging_enabled", True),
            ("charging", "charging_enabled", 0),
            ("charging", "charging_enabled", None),
        ):
            row = guard()
            row[group][key] = value
            with self.subTest(group=group, key=key, value=value):
                with self.assertRaises(bench.BenchmarkRejected):
                    bench.validate_guard(row)

    def test_cpu_limit_rejects_hot_or_unknown_readings(self):
        bench.validate_guard(dict(guard(), cpu_temperature_c=70))
        for temperature in (70.01, None, float("nan"), "40", True):
            with self.subTest(temperature=temperature):
                with self.assertRaises(bench.BenchmarkRejected):
                    bench.validate_guard(dict(guard(), cpu_temperature_c=temperature))

    def test_absent_battery_is_supported_without_battery_measurements(self):
        row = guard()
        row["status"]["battery"] = "NOT_PRESENT"
        bench.validate_guard(row)


class ReadingTests(unittest.TestCase):
    def test_power_timestamp_is_centered_and_raw_values_are_retained(self):
        hardware = device()
        row = bench.read_power(hardware, clock=Mock(side_effect=[1, 1.04]))
        self.assertAlmostEqual(row["monotonic_seconds"], 1.02)
        self.assertAlmostEqual(row["read_duration_seconds"], 0.04)
        self.assertEqual(row["io_power_w"], 1)
        self.assertEqual(set(row["raw"]), {"GetIoVoltage", "GetIoCurrent"})
        self.assertEqual(row["errors"], [])

    def test_api_failure_remains_raw_and_is_not_zero_energy(self):
        hardware = device()
        hardware.status.GetIoCurrent.return_value = {
            "error": "COMMUNICATION_ERROR",
            "data": 777,
        }
        row = bench.read_power(hardware)
        self.assertIsNone(row["io_power_w"])
        self.assertEqual(row["raw"]["GetIoCurrent"]["data"], 777)
        self.assertIn("GetIoCurrent", row["errors"][0])
        with self.assertRaises(bench.BenchmarkRejected):
            bench.validate_power(row)

    def test_nonfinite_api_replies_remain_persistable(self):
        hardware = device()
        hardware.status.GetIoCurrent.return_value["data"] = float("nan")
        row = bench.read_power(hardware)
        json.dumps(row, allow_nan=False)
        self.assertIn("unserializable_reply", row["raw"]["GetIoCurrent"])
        self.assertTrue(row["errors"])

    def test_guard_reads_cpu_and_only_source_and_charging_apis(self):
        hardware = device()
        with patch.object(Path, "read_text", return_value="41500\n"):
            row = bench.read_guard(hardware, Path("/unused"))
        bench.validate_guard(row)
        self.assertEqual(row["cpu_temperature_c"], 41.5)
        hardware.status.GetIoVoltage.assert_not_called()
        hardware.status.GetIoCurrent.assert_not_called()
        self.assertEqual(
            set(row["raw"]), {"GetStatus", "GetChargingConfig", "cpu_temperature"}
        )


class ProcessTests(unittest.TestCase):
    def test_success_is_measured_from_before_process_start(self):
        trial = {}
        bench.execute_command([sys.executable, "-c", "pass"], 3, Mock(), trial)
        self.assertEqual(trial["returncode"], 0)
        self.assertLess(trial["command_start"], trial["command_end"])

    def test_nonzero_exit_rejects_command(self):
        trial = {}
        with self.assertRaisesRegex(bench.BenchmarkRejected, "status 7"):
            bench.execute_command(
                [sys.executable, "-c", "raise SystemExit(7)"], 3, Mock(), trial
            )
        self.assertEqual(trial["returncode"], 7)

    def test_timeout_terminates_and_reaps_process(self):
        trial = {}
        with self.assertRaisesRegex(bench.BenchmarkRejected, "timeout"):
            bench.execute_command(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                0.1,
                Mock(),
                trial,
            )
        self.assertLess(trial["returncode"], 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(trial["pid"], 0)

    def test_guard_abort_terminates_running_process(self):
        trial = {}
        sampler = Mock()
        sampler.check.side_effect = [None, bench.BenchmarkRejected("USB lost")]
        with self.assertRaisesRegex(bench.BenchmarkRejected, "USB lost"):
            bench.execute_command(
                [sys.executable, "-c", "import time; time.sleep(60)"], 3, sampler, trial
            )
        with self.assertRaises(ProcessLookupError):
            os.kill(trial["pid"], 0)

    def test_spawn_failure_remains_a_failure(self):
        trial = {}
        with self.assertRaises(FileNotFoundError):
            bench.execute_command(
                ["/missing/energy-benchmark-command"], 1, Mock(), trial
            )
        self.assertIn("command_start", trial)
        self.assertNotIn("returncode", trial)


class ReceiptTests(unittest.TestCase):
    def test_replacement_syncs_directory_after_new_record_is_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            path.write_text('{"previous": true}')
            calls = []
            real_fsync = os.fsync

            def sync(descriptor):
                is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
                calls.append((is_directory, json.loads(path.read_text())))
                real_fsync(descriptor)

            with patch.object(bench.os, "fsync", side_effect=sync):
                bench.write_record(path, {"new": True})
            self.assertEqual(
                calls, [(False, {"previous": True}), (True, {"new": True})]
            )
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_directory_sync_failure_is_reported_and_descriptor_is_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            directories = []
            real_fsync = os.fsync

            def sync(descriptor):
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    directories.append(descriptor)
                    raise OSError("directory persistence failed")
                real_fsync(descriptor)

            with patch.object(bench.os, "fsync", side_effect=sync):
                with self.assertRaisesRegex(OSError, "directory persistence"):
                    bench.write_record(path, {"new": True})
            self.assertEqual(json.loads(path.read_text()), {"new": True})
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertEqual(len(directories), 1)
            with self.assertRaises(OSError):
                os.fstat(directories[0])


class RunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cpu = self.root / "cpu-temp"
        self.cpu.write_text("40000\n")
        self.options = bench.parse_args(
            [
                "--output",
                str(self.root / "local" / "result.json"),
                "--trials",
                "1",
                "--sample-hz",
                "10",
                "--pre-idle-seconds",
                "0.1",
                "--post-idle-seconds",
                "0.1",
                "--timeout-seconds",
                "1",
                "--cpu-temperature-path",
                str(self.cpu),
                "--command",
                sys.executable,
                "-c",
                "pass",
            ]
        )

    def test_complete_fake_hardware_run_persists_full_coverage(self):
        result = bench.run(self.options, device_factory=device)
        self.assertEqual(result["status"], "complete")
        trial = result["trials"][0]
        self.assertTrue(trial["energy"]["valid"])
        self.assertGreaterEqual(len(trial["samples"]), 3)
        self.assertGreaterEqual(len(trial["guards"]), 2)
        self.assertIn("source_guard_index", trial["samples"][0])
        self.assertAlmostEqual(trial["energy"]["baseline_mean_w"], 1)
        self.assertAlmostEqual(trial["energy"]["command_incremental_estimate_j"], 0)
        saved = json.loads(self.options.output.read_text())
        self.assertEqual(saved, result)

    def test_initial_source_failure_prevents_process_and_persists_guard(self):
        hardware = device()
        hardware.status.GetStatus.return_value["data"]["powerInput"] = "WEAK"
        with patch.object(bench.subprocess, "Popen") as popen:
            result = bench.run(self.options, device_factory=lambda: hardware)
        popen.assert_not_called()
        self.assertEqual(result["status"], "rejected")
        trial = result["trials"][0]
        self.assertFalse(trial["energy"]["valid"])
        self.assertFalse(trial["guards"][0]["valid"])
        self.assertEqual(
            trial["guards"][0]["raw"]["GetStatus"]["data"]["powerInput"], "WEAK"
        )
        self.assertTrue(self.options.output.exists())

    def test_device_initialization_failure_is_persisted(self):
        result = bench.run(
            self.options, device_factory=Mock(side_effect=OSError("No I2C"))
        )
        self.assertEqual(result["status"], "rejected")
        self.assertIn("No I2C", result["reason"])
        self.assertEqual(json.loads(self.options.output.read_text()), result)

    def test_first_rejection_stops_all_later_trials(self):
        self.options.trials = 5
        failed = {"status": "rejected", "reason": "USB lost"}
        with patch.object(bench, "run_trial", return_value=failed) as run_trial:
            result = bench.run(self.options, device_factory=device)
        self.assertEqual(len(result["trials"]), 1)
        run_trial.assert_called_once()

    def test_sigterm_during_command_persists_record_and_reaps_child(self):
        argv = [
            "--output",
            str(self.options.output),
            "--trials",
            "1",
            "--sample-hz",
            "10",
            "--pre-idle-seconds",
            "0.1",
            "--post-idle-seconds",
            "0.1",
            "--timeout-seconds",
            "3",
            "--cpu-temperature-path",
            str(self.cpu),
            "--command",
            sys.executable,
            "-c",
            "import os, signal, time; os.kill(os.getppid(), signal.SIGTERM); time.sleep(60)",
        ]
        script = f"""
from types import SimpleNamespace
from ops import energy_benchmark as bench

hardware = SimpleNamespace(
    status=SimpleNamespace(
        GetIoVoltage=lambda: {{"error": "NO_ERROR", "data": 5000}},
        GetIoCurrent=lambda: {{"error": "NO_ERROR", "data": 200}},
        GetStatus=lambda: {{"error": "NO_ERROR", "data": {{
            "powerInput": "PRESENT", "powerInput5vIo": "NOT_PRESENT", "battery": "NORMAL"
        }}}},
    ),
    config=SimpleNamespace(
        GetChargingConfig=lambda: {{"error": "NO_ERROR", "data": {{"charging_enabled": False}}}}
    ),
)
original_run = bench.run
bench.run = lambda options: original_run(options, device_factory=lambda: hardware)
raise SystemExit(bench.main({argv!r}))
"""
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        saved = json.loads(self.options.output.read_text())
        self.assertEqual(saved["status"], "rejected")
        self.assertIn("interrupted by signal", saved["reason"])
        self.assertFalse(saved["trials"][0]["energy"]["valid"])
        with self.assertRaises(ProcessLookupError):
            os.kill(saved["trials"][0]["pid"], 0)

    def test_public_output_and_overwrite_are_refused_before_hardware_access(self):
        factory = Mock()
        self.options.output = self.root / "public.json"
        with self.assertRaisesRegex(ValueError, "private"):
            bench.run(self.options, device_factory=factory)
        self.options.output = self.root / "local" / "existing.json"
        self.options.output.parent.mkdir()
        self.options.output.write_text("existing")
        with self.assertRaisesRegex(ValueError, "already exists"):
            bench.run(self.options, device_factory=factory)
        factory.assert_not_called()
        self.assertEqual(self.options.output.read_text(), "existing")

    def test_json_cases_use_argv_and_require_unique_names(self):
        path = self.root / "cases.json"
        cases = [{"name": "one", "command": ["printf", "%s", "a;b"]}]
        path.write_text(json.dumps({"cases": cases}))
        self.options.command = None
        self.options.cases = path
        self.assertEqual(bench.load_cases(self.options), cases)
        path.write_text(json.dumps(cases + copy.deepcopy(cases)))
        with self.assertRaisesRegex(ValueError, "unique"):
            bench.load_cases(self.options)
        path.write_text(json.dumps([{"name": "one", "command": "echo unparsed"}]))
        with self.assertRaisesRegex(ValueError, "argv"):
            bench.load_cases(self.options)


if __name__ == "__main__":
    unittest.main()
