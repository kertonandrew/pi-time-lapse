import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hardware import charge_test


def ok(data=None, **extra):
    return {"error": "NO_ERROR", "data": copy.deepcopy(data), **extra}


class FakeHat:
    def __init__(self):
        self.enabled = False
        self.persistent = True
        self.source = "PRESENT"
        self.temperature = 20
        self.voltage = 3980
        self.faults = {"forced_power_off": True}
        self.mask_enable = False
        self.fail_disable = False
        self.profile = {
            "capacity": 1000,
            "chargeCurrent": 625,
            "terminationCurrent": 50,
            "regulationVoltage": 4180,
            "cutoffVoltage": 3000,
            "tempCold": 0,
            "tempCool": 2,
            "tempWarm": 49,
            "tempHot": 65,
            "ntcB": 3450,
            "ntcResistance": 10000,
        }
        self.extended = {
            "chemistry": "LIPO",
            "ocv10": 3500,
            "ocv50": 3800,
            "ocv90": 4150,
            "r10": 0.15,
            "r50": 0.12,
            "r90": 0.1,
        }
        self.selection = {
            "validity": "VALID",
            "source": "DIP_SWITCH",
            "origin": "PREDEFINED",
            "profile": "PJZERO_1000",
        }
        self.inputs = {
            "precedence": "USB_MICRO",
            "gpio_in_enabled": True,
            "no_battery_turn_on": False,
            "usb_micro_current_limit": "2.5A",
            "usb_micro_dpm": "4.20V",
        }
        self.temperature_mode = "AUTO_DETECT"
        self.writes = []
        self.power = SimpleNamespace(GetPowerOff=Mock(return_value=ok([255])))
        self.config = SimpleNamespace(
            GetFirmwareVersion=Mock(
                return_value=ok({"version": "1.6", "variant": "0"})
            ),
            GetBatteryProfileStatus=Mock(side_effect=lambda: ok(self.selection)),
            GetBatteryProfile=Mock(side_effect=lambda: ok(self.profile)),
            GetBatteryExtProfile=Mock(side_effect=lambda: ok(self.extended)),
            GetBatteryTempSenseConfig=Mock(
                side_effect=lambda: ok(self.temperature_mode)
            ),
            GetRsocEstimationConfig=Mock(return_value=ok("AUTO_DETECT")),
            GetPowerInputsConfig=Mock(
                side_effect=lambda: ok(self.inputs, non_volatile=True)
            ),
            GetChargingConfig=Mock(
                side_effect=lambda: ok(
                    {"charging_enabled": self.enabled}, non_volatile=self.persistent
                )
            ),
            SetChargingConfig=Mock(side_effect=self.set_charging),
            SetCustomBatteryProfile=Mock(side_effect=self.set_profile),
            SetCustomBatteryExtProfile=Mock(
                side_effect=lambda data: self.update("extended", data)
            ),
            SetBatteryTempSenseConfig=Mock(
                side_effect=lambda data: self.update("temperature_mode", data)
            ),
            SetPowerInputsConfig=Mock(
                side_effect=lambda data, non_volatile: self.update("inputs", data)
            ),
        )
        self.status = SimpleNamespace(
            GetStatus=Mock(
                side_effect=lambda: ok(
                    {
                        "battery": "CHARGING_FROM_IN" if self.enabled else "NORMAL",
                        "powerInput": self.source,
                        "powerInput5vIo": "NOT_PRESENT",
                    }
                )
            ),
            GetFaultStatus=Mock(side_effect=lambda: ok(self.faults)),
            GetBatteryVoltage=Mock(side_effect=lambda: ok(self.voltage)),
            GetBatteryTemperature=Mock(side_effect=lambda: ok(self.temperature)),
            GetBatteryCurrent=Mock(return_value=ok(-500)),
            GetChargeLevel=Mock(return_value=ok(80)),
            GetIoVoltage=Mock(return_value=ok(5000)),
            GetIoCurrent=Mock(return_value=ok(180)),
        )

    def set_charging(self, configuration, non_volatile=False):
        enabled = configuration["charging_enabled"]
        self.writes.append(("charging", enabled, non_volatile))
        if not enabled and self.fail_disable:
            return {"error": "COMMUNICATION_ERROR"}
        if enabled and self.mask_enable:
            return ok()
        self.enabled = enabled
        self.persistent = non_volatile
        return ok()

    def update(self, key, data):
        if self.enabled:
            raise AssertionError("profile write while charging")
        self.writes.append((key, copy.deepcopy(data)))
        setattr(self, key, copy.deepcopy(data))
        return ok()

    def set_profile(self, profile):
        self.update("profile", profile)
        self.selection = {
            "validity": "VALID",
            "source": "HOST",
            "origin": "CUSTOM",
            "profile": "UNKNOWN",
        }
        return ok()


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            callback, self.on_sleep = self.on_sleep, None
            callback()


class ChargeTrialTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.directory = Path(
            self.resources.enter_context(tempfile.TemporaryDirectory())
        )
        self.control_lock = self.directory / "control.lock"
        self.hat = FakeHat()
        self.clock = FakeClock()
        self.resources.enter_context(
            patch.object(charge_test.time, "monotonic", self.clock.monotonic)
        )
        self.resources.enter_context(
            patch.object(charge_test.time, "sleep", self.clock.sleep)
        )
        self.quiet = Mock()

    def prepare(self, **kwargs):
        return charge_test.prepare_trial(
            self.hat, self.directory, self.control_lock, self.quiet, **kwargs
        )

    def run_trial(self):
        return charge_test.run_trial(
            self.hat, self.directory, self.control_lock, self.quiet
        )

    def assert_disabled(self):
        self.assertFalse(self.hat.enabled)
        self.assertTrue(self.hat.persistent)
        self.assertTrue((self.directory / "stop-requested.json").exists())

    def test_prepare_snapshots_complete_original_and_never_enables(self):
        original = charge_test.read_configuration(self.hat)
        self.prepare()
        saved = json.loads((self.directory / "original.json").read_text())
        self.assertEqual(saved, original)
        self.assertEqual(
            self.hat.profile,
            dict(original["profile"]["data"], **charge_test.PROFILE_OVERRIDES),
        )
        self.assertEqual(self.hat.extended, original["extended"]["data"])
        self.assertEqual(self.hat.temperature_mode, "NTC")
        self.assertEqual(self.hat.inputs["usb_micro_current_limit"], "1.5A")
        self.assertFalse(
            any(write[:2] == ("charging", True) for write in self.hat.writes)
        )
        self.assertEqual(
            (self.directory / "original.json").stat().st_mode & 0o777, 0o600
        )

    def test_prepare_rejects_unknown_base_without_mutations(self):
        self.hat.profile["capacity"] = 2000
        with self.assertRaisesRegex(
            charge_test.ChargeStopped, "unexpected_base_profile"
        ):
            self.prepare()
        self.assertEqual(self.hat.writes, [])

    def test_prepare_rejects_existing_power_cut_without_cancelling_it(self):
        self.hat.power.GetPowerOff.return_value = ok([119])
        with self.assertRaisesRegex(charge_test.ChargeStopped, "power_cut_armed"):
            self.prepare()
        self.assertEqual(self.hat.writes, [])

    def test_prepare_requires_readback_and_disables_after_failure(self):
        self.hat.config.SetPowerInputsConfig.side_effect = lambda *args, **kwargs: ok()
        with self.assertRaisesRegex(
            charge_test.ChargeStopped, "configuration_changed:inputs"
        ):
            self.prepare()
        self.assertFalse(self.hat.enabled)
        self.assertTrue(self.hat.persistent)
        self.assertFalse((self.directory / "prepared.json").exists())

    def test_prepare_disables_before_any_profile_write(self):
        self.hat.enabled = True
        self.prepare()
        self.assertEqual(self.hat.writes[0], ("charging", False, True))
        self.assertTrue(
            json.loads((self.directory / "original.json").read_text())["charging"][
                "data"
            ]["charging_enabled"]
        )

    def test_five_minute_trial_enables_only_volatile_and_disables_at_deadline(self):
        self.prepare()
        before = self.clock.now
        result = self.run_trial()
        self.assertEqual(result, "completed_time_limit")
        self.assertEqual(self.clock.now - before, 302.25)
        self.assert_disabled()
        self.assertEqual(
            [write for write in self.hat.writes if write[:2] == ("charging", True)],
            [("charging", True, False)],
        )
        saved = json.loads((self.directory / "result.json").read_text())
        self.assertTrue(saved["charge_status_observed"])
        self.assertEqual(saved["elapsed_since_enable_attempt_seconds"], 302.25)

    def test_masked_enable_write_error_is_detected_and_cleaned_up(self):
        self.prepare()
        self.hat.mask_enable = True
        result = self.run_trial()
        self.assertIn("charging_readback_mismatch", result)
        self.assert_disabled()

    def test_enable_communication_exception_still_disables(self):
        self.prepare()
        original = self.hat.config.SetChargingConfig.side_effect

        def uncertain_enable(configuration, non_volatile=False):
            result = original(configuration, non_volatile)
            if configuration["charging_enabled"]:
                raise OSError("uncertain write")
            return result

        self.hat.config.SetChargingConfig.side_effect = uncertain_enable
        self.assertIn("uncertain write", self.run_trial())
        self.assert_disabled()

    def test_changed_profile_stops_and_disables(self):
        self.prepare()
        self.clock.on_sleep = lambda: self.hat.profile.update(chargeCurrent=625)
        self.assertIn("configuration_changed:profile", self.run_trial())
        self.assert_disabled()

    def test_power_cut_armed_before_run_prevents_enable(self):
        self.prepare()
        self.hat.power.GetPowerOff.return_value = ok([119])
        self.assertIn("configuration_changed:power_cut", self.run_trial())
        self.assertFalse(
            any(write[:2] == ("charging", True) for write in self.hat.writes)
        )
        self.assert_disabled()

    def test_power_cut_armed_during_run_stops_charging(self):
        self.prepare()
        self.clock.on_sleep = lambda: setattr(
            self.hat.power.GetPowerOff, "return_value", ok([59])
        )
        self.assertIn("configuration_changed:power_cut", self.run_trial())
        self.assert_disabled()

    def test_usb_loss_stops_and_disables(self):
        self.prepare()
        self.clock.on_sleep = lambda: setattr(self.hat, "source", "NOT_PRESENT")
        self.assertIn("usb_source_changed", self.run_trial())
        self.assert_disabled()

    def test_temperature_boundary_stops_and_disables(self):
        self.prepare()
        self.clock.on_sleep = lambda: setattr(self.hat, "temperature", 35)
        self.assertIn("reported_temperature_limit", self.run_trial())
        self.assert_disabled()

    def test_target_voltage_stops_and_disables(self):
        self.prepare()
        self.clock.on_sleep = lambda: setattr(self.hat, "voltage", 4100)
        self.assertEqual(self.run_trial(), "reached_trial_voltage")
        self.assert_disabled()
        saved = json.loads((self.directory / "result.json").read_text())
        self.assertFalse(saved["observe_regulation"])
        self.assertTrue(saved["regulation_target_observed"])

    def test_regulation_mode_continues_at_target_until_deadline(self):
        self.prepare(observe_regulation=True)
        self.clock.on_sleep = lambda: setattr(self.hat, "voltage", 4101)
        self.assertEqual(self.run_trial(), "completed_time_limit")
        self.assert_disabled()
        saved = json.loads((self.directory / "result.json").read_text())
        self.assertTrue(saved["observe_regulation"])
        self.assertTrue(saved["regulation_target_observed"])
        self.assertEqual(saved["elapsed_since_enable_attempt_seconds"], 302.25)

    def test_regulation_mode_upper_voltage_guard_still_stops(self):
        self.prepare(observe_regulation=True)
        self.clock.on_sleep = lambda: setattr(self.hat, "voltage", 4150)
        self.assertIn("battery_voltage_limit", self.run_trial())
        self.assert_disabled()

    def test_default_mode_upper_voltage_guard_still_stops(self):
        self.prepare()
        self.clock.on_sleep = lambda: setattr(self.hat, "voltage", 4150)
        self.assertIn("battery_voltage_limit", self.run_trial())
        self.assert_disabled()

    def test_regulation_mode_requires_initial_voltage_below_target(self):
        self.prepare(observe_regulation=True)
        self.hat.voltage = 4100
        self.assertIn("already_at_trial_voltage", self.run_trial())
        self.assertFalse(
            any(write[:2] == ("charging", True) for write in self.hat.writes)
        )
        self.assert_disabled()

    def test_invalid_saved_regulation_mode_prevents_enable(self):
        self.prepare(observe_regulation=True)
        path = self.directory / "prepared.json"
        prepared = json.loads(path.read_text())
        prepared["observe_regulation"] = "false"
        path.write_text(json.dumps(prepared))
        self.assertIn("prepared_configuration_invalid", self.run_trial())
        self.assertFalse(
            any(write[:2] == ("charging", True) for write in self.hat.writes)
        )
        self.assert_disabled()

    def test_invalid_requested_regulation_mode_has_no_hardware_writes(self):
        with self.assertRaisesRegex(
            charge_test.ChargeStopped, "invalid_regulation_mode"
        ):
            self.prepare(observe_regulation=1)
        self.assertEqual(self.hat.writes, [])

    def test_regulation_cli_flag_rejected_for_run_and_stop(self):
        for action in ("run", "stop"):
            with self.subTest(action=action):
                with (
                    patch(
                        "sys.argv",
                        [
                            "charge_test.py",
                            action,
                            "--output",
                            str(self.directory),
                            "--observe-regulation",
                        ],
                    ),
                    patch("sys.stderr"),
                    patch.object(charge_test.os, "geteuid") as get_uid,
                    self.assertRaises(SystemExit) as error,
                ):
                    charge_test.main()
                self.assertEqual(error.exception.code, 2)
                get_uid.assert_not_called()

    def test_high_initial_voltage_does_not_enable(self):
        self.prepare()
        self.hat.voltage = 4100
        self.assertIn("already_at_trial_voltage", self.run_trial())
        self.assertFalse(
            any(write[:2] == ("charging", True) for write in self.hat.writes)
        )
        self.assert_disabled()

    def test_independent_stop_prevents_future_enable(self):
        self.prepare()
        charge_test.stop_trial(self.hat, self.directory, self.control_lock)
        self.assertIn("stop_requested", self.run_trial())
        self.assertFalse(
            any(write[:2] == ("charging", True) for write in self.hat.writes)
        )
        self.assert_disabled()

    def test_independent_stop_during_session_cannot_be_reenabled(self):
        self.prepare()
        self.clock.on_sleep = lambda: charge_test.stop_trial(
            self.hat, self.directory, self.control_lock
        )
        self.assertIn("stop_requested", self.run_trial())
        self.assertEqual(
            sum(write[:2] == ("charging", True) for write in self.hat.writes), 1
        )
        self.assert_disabled()

    def test_invalid_sensor_stops_and_disables(self):
        self.prepare()
        self.clock.on_sleep = lambda: setattr(
            self.hat.status.GetIoCurrent, "return_value", ok("invalid")
        )
        self.assertIn("invalid_numeric_reading", self.run_trial())
        self.assert_disabled()

    def test_current_api_error_still_stops_charging(self):
        self.prepare()
        self.clock.on_sleep = lambda: setattr(
            self.hat.status.GetIoCurrent,
            "return_value",
            {"error": "COMMUNICATION_ERROR"},
        )
        self.assertIn("hat_communication_error", self.run_trial())
        self.assert_disabled()

    def test_current_outlier_does_not_stop_and_is_excluded_from_energy(self):
        self.prepare()
        self.clock.on_sleep = lambda: setattr(
            self.hat.status.GetIoCurrent, "return_value", ok(-409)
        )
        self.assertEqual(self.run_trial(), "completed_time_limit")
        self.assert_disabled()
        rows = [
            json.loads(line)
            for line in (self.directory / "samples.jsonl").read_text().splitlines()
        ]
        self.assertTrue(rows[0]["pi_rail_energy_valid"])
        self.assertTrue(
            all(row["raw"]["GetIoCurrent"]["data"] == -409 for row in rows[1:])
        )
        self.assertTrue(all(not row["pi_rail_energy_valid"] for row in rows[1:]))
        self.assertTrue(
            all(
                "pi_rail_current_out_of_expected_range" in row["quality_flags"]
                for row in rows[1:]
            )
        )

    def test_observed_idle_load_above_one_point_five_watts_is_permitted(self):
        self.prepare()
        self.hat.status.GetIoVoltage.return_value = ok(5062)
        self.hat.status.GetIoCurrent.return_value = ok(304)
        self.assertEqual(self.run_trial(), "completed_time_limit")
        self.assert_disabled()

    def test_current_expected_range_marks_energy_quality_without_stop(self):
        expected = self.prepare()
        self.hat.status.GetIoVoltage.return_value = ok(5250)
        self.hat.status.GetIoCurrent.return_value = ok(500)
        row = charge_test.observe(self.hat)
        self.assertFalse(
            charge_test.validate_observation(row, expected, False, self.hat.faults)
        )
        self.assertTrue(row["pi_rail_energy_valid"])
        self.hat.status.GetIoCurrent.return_value = ok(501)
        row = charge_test.observe(self.hat)
        self.assertFalse(
            charge_test.validate_observation(row, expected, False, self.hat.faults)
        )
        self.assertFalse(row["pi_rail_energy_valid"])
        self.assertIn("pi_rail_current_out_of_expected_range", row["quality_flags"])

    def test_rail_voltage_ceiling_remains_enforced_at_allowed_current(self):
        expected = self.prepare()
        self.hat.status.GetIoVoltage.return_value = ok(5251)
        self.hat.status.GetIoCurrent.return_value = ok(500)
        with self.assertRaisesRegex(charge_test.ChargeStopped, "pi_rail_voltage_limit"):
            charge_test.validate_observation(
                charge_test.observe(self.hat), expected, False, self.hat.faults
            )

    def test_observation_gap_stops_and_disables(self):
        self.prepare()
        self.clock.on_sleep = lambda: setattr(self.clock, "now", self.clock.now + 11)
        self.assertIn("observation_gap", self.run_trial())
        self.assert_disabled()

    def test_new_fault_stops_while_historical_flags_are_permitted(self):
        self.prepare()
        self.clock.on_sleep = lambda: self.hat.faults.update(watchdog_reset=True)
        self.assertIn("new_hat_fault", self.run_trial())
        self.assert_disabled()

    def test_workload_becoming_active_stops_and_disables(self):
        self.prepare()
        self.quiet.side_effect = [
            None,
            None,
            charge_test.ChargeStopped("camera_or_transfer_active"),
        ]
        self.assertIn("camera_or_transfer_active", self.run_trial())
        self.assert_disabled()

    def test_logging_failure_still_disables(self):
        self.prepare()
        original = charge_test.os.fsync
        calls = 0

        def disk_full(fd):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("disk full")
            return original(fd)

        with patch.object(charge_test.os, "fsync", side_effect=disk_full):
            self.assertIn("disk full", self.run_trial())
        self.assert_disabled()

    def test_stop_disables_even_when_marker_cannot_be_written(self):
        self.prepare()
        self.hat.enabled = True
        with patch.object(charge_test, "save_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(
                charge_test.ChargeStopped, "stop_marker_unwritten"
            ):
                charge_test.stop_trial(self.hat, self.directory, self.control_lock)
        self.assertFalse(self.hat.enabled)
        self.assertTrue(self.hat.persistent)
        self.assertEqual(self.control_lock.read_text(), "")
        self.assertIn("run_permit_absent", self.run_trial())
        self.assertFalse(
            any(write[:2] == ("charging", True) for write in self.hat.writes)
        )

    def test_reported_charging_after_disable_is_explicit_failure(self):
        self.hat.enabled = True
        self.hat.status.GetStatus.side_effect = lambda: ok(
            {"battery": "CHARGING_FROM_IN"}
        )
        with self.assertRaisesRegex(charge_test.ChargeStopped, "charge_off_unverified"):
            charge_test.disable_charging(self.hat)
        self.assertFalse(self.hat.enabled)
        self.assertEqual(self.hat.config.SetChargingConfig.call_count, 3)

    def test_prepare_does_not_change_profile_until_charge_off_is_observed(self):
        self.hat.status.GetStatus.side_effect = lambda: ok(
            {"battery": "CHARGING_FROM_IN"}
        )
        with self.assertRaisesRegex(charge_test.ChargeStopped, "charge_off_unverified"):
            self.prepare()
        self.hat.config.SetCustomBatteryProfile.assert_not_called()

    def test_unverified_disable_is_an_explicit_failure(self):
        self.hat.enabled = True
        self.hat.fail_disable = True
        with self.assertRaisesRegex(charge_test.ChargeStopped, "charge_off_unverified"):
            charge_test.stop_trial(self.hat, self.directory, self.control_lock)
        self.assertTrue(self.hat.enabled)
        self.assertEqual(self.hat.config.SetChargingConfig.call_count, 3)

    def test_tampered_preparation_cannot_increase_current(self):
        self.prepare()
        prepared = json.loads((self.directory / "prepared.json").read_text())
        prepared["expected"]["profile"]["chargeCurrent"] = 1000
        (self.directory / "prepared.json").write_text(json.dumps(prepared))
        self.assertIn("prepared_configuration_invalid", self.run_trial())
        self.assertFalse(
            any(write[:2] == ("charging", True) for write in self.hat.writes)
        )
        self.assert_disabled()

    def test_session_cannot_be_run_twice(self):
        self.prepare()
        self.hat.voltage = 4100
        self.run_trial()
        self.hat.voltage = 3980
        self.assertIn("FileExistsError", self.run_trial())
        self.assertFalse(
            any(write[:2] == ("charging", True) for write in self.hat.writes)
        )

    @patch.object(charge_test.subprocess, "run")
    def test_quiet_check_requires_all_units_inactive(self, run):
        blocks = [
            f"Id={unit}\nLoadState=loaded\nActiveState=inactive"
            for unit in charge_test.QUIET_UNITS
        ]
        run.return_value = SimpleNamespace(stdout="\n\n".join(blocks))
        charge_test.require_quiet()
        run.return_value.stdout = run.return_value.stdout.replace(
            "ActiveState=inactive", "ActiveState=active", 1
        )
        with self.assertRaisesRegex(
            charge_test.ChargeStopped, "camera_or_transfer_active"
        ):
            charge_test.require_quiet()


if __name__ == "__main__":
    unittest.main()
