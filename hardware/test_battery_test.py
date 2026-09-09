import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hardware.battery_test import (
    TestStopped,
    charge_level_percent,
    cleanup_test,
    finish_power,
    phase,
    run_test,
    set_countdown,
    validate_sample,
)


def ok(data):
    return {"data": data, "error": "NO_ERROR"}


def sample(source="NOT_PRESENT"):
    return {
        "GetStatus": ok(
            {"battery": "NORMAL", "powerInput": source, "powerInput5vIo": "NOT_PRESENT"}
        ),
        "GetBatteryVoltage": ok(4000),
        "GetBatteryTemperature": ok(20),
        "GetBatteryCurrent": ok(200),
        "GetIoVoltage": ok(5000),
        "GetIoCurrent": ok(180),
    }


class DischargeGuards(unittest.TestCase):
    def test_charge_level_tenths_and_invalid_data(self):
        self.assertEqual(charge_level_percent(ok([52, 3])), 82)
        self.assertEqual(charge_level_percent(ok([53, 3])), 82.1)
        for response in (ok([255, 255]), ok([]), ok([True, 3]), {"error": "FAIL"}):
            with self.assertRaises(TestStopped):
                charge_level_percent(response)

    def test_source_and_temperature_voltage_limits(self):
        self.assertTrue(validate_sample(sample(), ok({"charging_enabled": False}), 40))
        self.assertFalse(
            validate_sample(sample("PRESENT"), ok({"charging_enabled": False}), 40)
        )
        for method, bad in [
            ("GetBatteryVoltage", 3799),
            ("GetBatteryVoltage", 4201),
            ("GetBatteryTemperature", 36),
            ("GetBatteryTemperature", 9),
            ("GetIoCurrent", float("nan")),
            ("GetIoCurrent", float("inf")),
            ("GetIoVoltage", 4799),
        ]:
            with self.subTest(method=method, bad=bad):
                raw = sample()
                raw[method] = ok(bad)
                with self.assertRaises(TestStopped):
                    validate_sample(raw, ok({"charging_enabled": False}), 40)

    def test_finite_current_outliers_are_not_battery_limit_failures(self):
        for current in (-526, 1139):
            with self.subTest(current=current):
                raw = sample()
                raw["GetIoCurrent"] = ok(current)
                self.assertTrue(
                    validate_sample(raw, ok({"charging_enabled": False}), 40)
                )

    def test_no_workload_with_charging_or_ambiguous_power(self):
        with self.assertRaises(TestStopped):
            validate_sample(sample(), ok({"charging_enabled": True}), 40)
        for source in ["BAD", "WEAK", "unknown"]:
            with self.assertRaises(TestStopped):
                validate_sample(sample(source), ok({"charging_enabled": False}), 40)
        raw = sample()
        raw["GetStatus"]["data"]["powerInput5vIo"] = "PRESENT"
        with self.assertRaises(TestStopped):
            validate_sample(raw, ok({"charging_enabled": False}), 40)

    def test_phases_have_bounded_cpu_duration(self):
        self.assertEqual(phase(119.9), "battery_idle")
        self.assertEqual(phase(120), "battery_cpu")
        self.assertEqual(phase(150), "battery_recovery")
        self.assertEqual(phase(300), "complete")


class DischargeCutoff(unittest.TestCase):
    def hat(self, source="NOT_PRESENT"):
        return SimpleNamespace(
            status=SimpleNamespace(
                GetStatus=Mock(return_value=sample(source)["GetStatus"])
            ),
            power=SimpleNamespace(
                SetPowerOff=Mock(return_value=ok(None)),
                GetPowerOff=Mock(return_value=ok([60])),
            ),
        )

    def test_cutoff_requires_independent_readback(self):
        hat = self.hat()
        for response in [
            ok([255]),
            ok([30]),
            ok([]),
            ok([True]),
            {"error": "COMMUNICATION_ERROR"},
        ]:
            hat.power.GetPowerOff.return_value = response
            with self.assertRaises(TestStopped):
                set_countdown(hat, 120)
        hat.power.GetPowerOff.return_value = ok([119])
        set_countdown(hat, 120)

    @patch("hardware.battery_test.os.sync")
    @patch("hardware.battery_test.subprocess.run")
    def test_battery_completion_keeps_cutoff_and_requests_halt(self, run, sync):
        hat = self.hat()
        with tempfile.TemporaryDirectory() as directory:
            finish_power(hat, Path(directory))
        hat.power.SetPowerOff.assert_called_once_with(60)
        run.assert_called_once_with(
            ["/sbin/shutdown", "-h", "now"], check=True, timeout=10
        )

    @patch("hardware.battery_test.subprocess.run")
    def test_restored_usb_cancels_countdown_without_halt(self, run):
        hat = self.hat("PRESENT")
        hat.power.GetPowerOff.return_value = ok([255])
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "armed"
            marker.write_text("test")
            finish_power(hat, Path(directory))
            self.assertFalse(marker.exists())
        hat.power.SetPowerOff.assert_called_once_with(255)
        run.assert_not_called()

    @patch("hardware.battery_test.os.sync")
    @patch("hardware.battery_test.subprocess.run")
    def test_i2c_failure_still_requests_halt(self, run, sync):
        hat = self.hat()
        hat.status.GetStatus.return_value = {"error": "COMMUNICATION_ERROR"}
        hat.power.SetPowerOff.return_value = {"error": "COMMUNICATION_ERROR"}
        with tempfile.TemporaryDirectory() as directory:
            finish_power(hat, Path(directory))
            self.assertTrue((Path(directory) / "power-cut-error.txt").exists())
        run.assert_called_once()

    def test_charging_enabled_refuses_to_arm(self):
        hat = self.hat()
        hat.config = SimpleNamespace(
            GetChargingConfig=Mock(return_value=ok({"charging_enabled": True}))
        )
        with tempfile.TemporaryDirectory() as directory:
            result = run_test(hat, Path(directory))
            self.assertFalse((Path(directory) / "armed").exists())
        self.assertIn("charging_not_disabled", result)
        hat.power.SetPowerOff.assert_not_called()

    @patch("hardware.battery_test.finish_power")
    def test_existing_countdown_is_not_overwritten(self, finish):
        hat = self.hat()
        hat.config = SimpleNamespace(
            GetChargingConfig=Mock(return_value=ok({"charging_enabled": False}))
        )
        hat.power.GetPowerOff.return_value = ok([45])
        with tempfile.TemporaryDirectory() as directory:
            result = run_test(hat, Path(directory))
            self.assertFalse((Path(directory) / "armed").exists())
        self.assertIn("existing_power_cut_countdown", result)
        hat.power.SetPowerOff.assert_not_called()
        hat.status.GetStatus.assert_not_called()
        finish.assert_not_called()

    @patch("hardware.battery_test.boot_id", return_value="current-boot")
    @patch("hardware.battery_test.finish_power")
    def test_previous_boot_marker_does_not_touch_hardware(self, finish, boot):
        hat = Mock()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "armed").write_text(json.dumps({"boot_id": "previous-boot"}))
            cleanup_test(hat, output)
        finish.assert_not_called()
        self.assertEqual(hat.mock_calls, [])


class DischargeLifecycle(unittest.TestCase):
    def run_sequence(self, samples, sleep_steps, worker=None, fail_result=False):
        clock = SimpleNamespace(now=0.0)
        steps = iter(sleep_steps)
        launch_times = []

        def sleep(_):
            clock.now += next(steps)

        def launch_worker(*args, **kwargs):
            launch_times.append(clock.now)
            return worker

        hat = SimpleNamespace(
            interface=SimpleNamespace(ReadData=Mock(return_value=ok([52, 3]))),
            status=SimpleNamespace(
                **{
                    method: Mock(side_effect=[raw[method] for raw in samples])
                    for method in samples[0]
                }
            ),
            power=SimpleNamespace(
                GetPowerOff=Mock(return_value=ok([255])), SetPowerOff=Mock()
            ),
            config=SimpleNamespace(
                GetChargingConfig=Mock(return_value=ok({"charging_enabled": False}))
            ),
        )
        original_read = Path.read_text
        original_write = Path.write_text

        def read_text(path, *args, **kwargs):
            if str(path) == "/sys/class/thermal/thermal_zone0/temp":
                return "40000"
            return original_read(path, *args, **kwargs)

        def write_text(path, *args, **kwargs):
            if fail_result and path.name == "result.json":
                raise OSError("disk full")
            return original_write(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            output = Path(directory)
            stack.enter_context(
                patch("hardware.battery_test.boot_id", return_value="boot")
            )
            stack.enter_context(
                patch(
                    "hardware.battery_test.time.monotonic",
                    side_effect=lambda: clock.now,
                )
            )
            stack.enter_context(
                patch("hardware.battery_test.time.sleep", side_effect=sleep)
            )
            stack.enter_context(patch.object(Path, "read_text", read_text))
            stack.enter_context(patch.object(Path, "write_text", write_text))
            cutoff = stack.enter_context(patch("hardware.battery_test.set_countdown"))
            stop = stack.enter_context(patch("hardware.battery_test.stop_worker"))
            finish = stack.enter_context(patch("hardware.battery_test.finish_power"))
            launch = stack.enter_context(
                patch(
                    "hardware.battery_test.subprocess.Popen", side_effect=launch_worker
                )
            )
            result = None
            error = None
            try:
                result = run_test(hat, output)
            except OSError as caught:
                error = caught
            rows = [
                json.loads(line)
                for line in (output / "samples.jsonl").read_text().splitlines()
            ]
            saved = (
                json.loads((output / "result.json").read_text())
                if (output / "result.json").exists()
                else None
            )
            finish.assert_called_once_with(hat, output)
        return SimpleNamespace(
            result=result,
            error=error,
            rows=rows,
            saved=saved,
            stop=stop,
            launch=launch,
            launch_times=launch_times,
            cutoff=cutoff,
        )

    def test_usb_wait_then_battery_workload_then_usb_return(self):
        worker = Mock()
        worker.poll.return_value = None
        run = self.run_sequence(
            [sample("PRESENT"), sample(), sample(), sample("PRESENT")],
            [2, 120, 2],
            worker,
        )
        self.assertEqual(run.result, "usb_reconnected")
        self.assertIsNone(run.error)
        self.assertTrue(run.saved["battery_test_started"])
        self.assertFalse(run.saved["charging_enabled_by_test"])
        self.assertEqual(run.saved["elapsed_seconds"], 124)
        self.assertEqual(
            [row["phase"] for row in run.rows if row.get("event") == "phase"],
            ["battery_idle", "battery_cpu"],
        )
        self.assertEqual(run.rows[0]["phase"], "waiting_for_usb_removal")
        run.launch.assert_called_once()
        self.assertEqual(run.launch.call_args.args[0][:2], ["/usr/bin/timeout", "30"])
        run.stop.assert_called_with(worker)

    def test_isolated_usb_current_outlier_is_recorded_without_aborting(self):
        for current in (-526, 1139):
            with self.subTest(current=current):
                bad = sample("PRESENT")
                bad["GetIoCurrent"] = ok(current)
                run = self.run_sequence(
                    [bad, sample("PRESENT"), sample(), sample()], [2, 2, 300]
                )
                self.assertEqual(run.result, "complete")
                self.assertEqual(run.rows[0]["raw"]["GetIoCurrent"], ok(current))
                self.assertEqual(
                    run.rows[0]["quality_flags"],
                    ["pi_rail_current_out_of_expected_range"],
                )
                self.assertFalse(run.rows[0]["pi_rail_energy_valid"])
                self.assertTrue(run.rows[1]["pi_rail_energy_valid"])

    def test_five_consecutive_current_outliers_stop_and_clean_up_workload(self):
        worker = Mock()
        worker.poll.return_value = None
        bad = sample()
        bad["GetIoCurrent"] = ok(1139)
        run = self.run_sequence(
            [sample(), sample(), *[bad] * 5], [120, 2, 2, 2, 2, 2], worker
        )
        self.assertIn("pi_rail_current_measurement_unreliable", run.result)
        run.launch.assert_called_once()
        run.stop.assert_called_with(worker)
        invalid = [row for row in run.rows if row.get("quality_flags")]
        self.assertEqual(len(invalid), 5)
        self.assertTrue(all(row["raw"]["GetIoCurrent"] == ok(1139) for row in invalid))

    def test_cpu_launch_waits_for_valid_current_without_extending_test(self):
        worker = Mock()
        worker.poll.return_value = None
        bad = sample()
        bad["GetIoCurrent"] = ok(-526)
        run = self.run_sequence(
            [sample(), bad, sample(), sample(), sample()],
            [120, 2, 28, 150],
            worker,
        )
        self.assertEqual(run.result, "complete")
        self.assertEqual(run.launch_times, [122])
        self.assertEqual(run.saved["elapsed_seconds"], 300)
        self.assertEqual(run.launch.call_args.args[0][:2], ["/usr/bin/timeout", "30"])
        self.assertIn(unittest.mock.call(worker), run.stop.call_args_list)

    def test_valid_current_resets_consecutive_outlier_count(self):
        bad = sample()
        bad["GetIoCurrent"] = ok(-526)
        run = self.run_sequence(
            [sample(), *[bad] * 4, sample(), *[bad] * 4, sample("PRESENT")],
            [2] * 10,
        )
        self.assertEqual(run.result, "usb_reconnected")
        run.launch.assert_not_called()

    def test_invalid_temperature_during_workload_stops_and_cleans_up(self):
        bad = sample()
        bad["GetBatteryTemperature"] = ok(36)
        worker = Mock()
        worker.poll.return_value = None
        run = self.run_sequence([sample(), sample(), bad], [120, 2], worker)
        self.assertIn("reported_temperature_limit", run.result)
        run.launch.assert_called_once()
        run.stop.assert_called_with(worker)
        self.assertEqual(run.rows[-1]["raw"]["GetBatteryTemperature"], ok(36))

    def test_cpu_worker_failure_is_recorded_and_cleaned_up(self):
        worker = Mock()
        worker.poll.return_value = 1
        run = self.run_sequence([sample(), sample(), sample()], [120, 2], worker)
        self.assertIn("cpu_worker_failed", run.result)
        self.assertEqual(run.saved["result"], run.result)
        run.stop.assert_called_with(worker)

    def test_result_write_failure_still_stops_workload_and_cleans_up(self):
        worker = Mock()
        worker.poll.return_value = None
        run = self.run_sequence(
            [sample(), sample(), sample("PRESENT")],
            [120, 2],
            worker,
            fail_result=True,
        )
        self.assertIsInstance(run.error, OSError)
        self.assertEqual(str(run.error), "disk full")
        self.assertIsNone(run.saved)
        run.stop.assert_called_with(worker)


if __name__ == "__main__":
    unittest.main()
