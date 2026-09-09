import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hardware import charge_completion as completion


CONFIG = {"stop_at_percent": 99, "max_sample_age_seconds": 15}


def ok(data, **extra):
    return {"error": "NO_ERROR", "data": data, **extra}


class FakeHat:
    def __init__(self, percent=99, enabled=True, board_faults=0):
        self.enabled = enabled
        self.persistent = True
        self.status = SimpleNamespace(
            GetChargeLevel=Mock(return_value=ok(percent)),
            GetStatus=Mock(return_value=ok({"battery": "NORMAL"})),
            GetFaultStatus=Mock(return_value=ok({})),
        )
        self.interface = SimpleNamespace(ReadData=Mock(return_value=ok([board_faults])))
        self.config = SimpleNamespace(
            GetChargingConfig=Mock(side_effect=self.read_charging),
            SetChargingConfig=Mock(side_effect=self.set_charging),
        )

    def read_charging(self):
        return ok({"charging_enabled": self.enabled}, non_volatile=self.persistent)

    def set_charging(self, enabled, non_volatile=False):
        self.enabled, self.persistent = enabled, non_volatile
        return {"error": "NO_ERROR"}


def observation(percent=99, board_faults=0):
    return completion.observe(
        FakeHat(percent, board_faults=board_faults),
        clock=Mock(side_effect=[100, 100.1]),
    )


class CompletionTests(unittest.TestCase):
    def test_exact_threshold_preserves_raw_percentage(self):
        for percent, charged in ((98.9, False), (99, True), (99.6, True), (100, True)):
            with self.subTest(percent=percent):
                result = completion.evaluate(CONFIG, observation(percent), 100.2)
                self.assertIs(result["charged_at_user_threshold"], charged)
                self.assertIs(result["stop_requested"], charged)
                self.assertEqual(result["reported_charge_percent"], percent)

    def test_invalid_and_stale_soc_cannot_count_as_charged(self):
        for percent in (None, True, "99", -1, 101, float("nan")):
            with self.subTest(percent=percent):
                result = completion.evaluate(CONFIG, observation(percent), 100.2)
                self.assertFalse(result["charged_at_user_threshold"])
                self.assertTrue(result["stop_requested"])
        for now in (99, 116, float("nan")):
            result = completion.evaluate(CONFIG, observation(), now)
            self.assertFalse(result["charged_at_user_threshold"])
            self.assertTrue(result["stop_requested"])

    def test_battery_absence_and_api_failure_are_not_full(self):
        for change in ("absent", "api_error"):
            row = observation()
            if change == "absent":
                row["status"]["battery"] = "NOT_PRESENT"
            else:
                row["errors"]["GetChargeLevel"] = "COMMUNICATION_ERROR"
            result = completion.evaluate(CONFIG, row, 100.2)
            self.assertFalse(result["charged_at_user_threshold"])
            self.assertTrue(result["stop_requested"])

    def test_sensor_faults_stop_independently_of_completion(self):
        for percent, charged in ((98.9, False), (99.6, True)):
            result = completion.evaluate(
                CONFIG, observation(percent, board_faults=0x30), 100.2
            )
            self.assertIs(result["charged_at_user_threshold"], charged)
            self.assertTrue(result["stop_requested"])
            self.assertIn("board_fault_present", result["safety_reasons"])
            self.assertEqual(len(result["sensor_confidence_warnings"]), 2)

    def test_temperature_suspension_is_independent_of_soc(self):
        for percent in (50, 99):
            row = observation(percent)
            row["faults"]["charging_temperature_fault"] = "SUSPEND"
            result = completion.evaluate(CONFIG, row, 100.2)
            self.assertEqual(result["charged_at_user_threshold"], percent >= 99)
            self.assertIn("charging_temperature_fault", result["safety_reasons"])
            self.assertTrue(result["stop_requested"])

    def test_threshold_is_required_and_configurable(self):
        result = completion.evaluate({"stop_at_percent": 95}, observation(96), 100.2)
        self.assertTrue(result["charged_at_user_threshold"])
        for config in (
            {},
            {"stop_at_percent": True},
            {"stop_at_percent": 101},
            {"stop_at_percent": 99, "max_sample_age_seconds": -1},
        ):
            with self.assertRaises(ValueError):
                completion.validate_config(config)


class EnforcementTests(unittest.TestCase):
    def test_threshold_disables_once_and_verifies_persistence(self):
        hat = FakeHat()
        result = completion.enforce(hat, CONFIG)
        self.assertEqual(result["action"], "disabled_and_verified")
        self.assertTrue(result["charging_disabled_verified"])
        hat.config.SetChargingConfig.assert_called_once_with(False, non_volatile=True)
        again = completion.enforce(hat, CONFIG)
        self.assertEqual(again["action"], "already_disabled")
        self.assertEqual(hat.config.SetChargingConfig.call_count, 1)

    def test_falling_soc_does_not_restart_charging(self):
        hat = FakeHat()
        completion.enforce(hat, CONFIG)
        hat.status.GetChargeLevel.return_value = ok(98)
        result = completion.enforce(hat, CONFIG)
        self.assertFalse(result["charged_at_user_threshold"])
        self.assertEqual(result["action"], "left_unchanged")
        self.assertFalse(hat.enabled)
        hat.config.SetChargingConfig.assert_called_once_with(False, non_volatile=True)

    def test_already_disabled_never_causes_repeated_nv_writes(self):
        hat = FakeHat(enabled=False, board_faults=0x30)
        hat.persistent = False
        result = completion.enforce(hat, CONFIG)
        self.assertEqual(result["action"], "already_disabled")
        self.assertTrue(result["charged_at_user_threshold"])
        hat.config.SetChargingConfig.assert_not_called()

    def test_sensor_fault_below_threshold_only_disables(self):
        hat = FakeHat(percent=30, board_faults=0x30)
        result = completion.enforce(hat, CONFIG)
        self.assertFalse(result["charged_at_user_threshold"])
        self.assertEqual(result["action"], "disabled_and_verified")
        hat.config.SetChargingConfig.assert_called_once_with(False, non_volatile=True)

    def test_unknown_configuration_attempts_only_disable(self):
        hat = FakeHat(percent=30)
        hat.config.GetChargingConfig.side_effect = [
            {"error": "COMMUNICATION_ERROR"},
            ok({"charging_enabled": False}, non_volatile=True),
        ]
        result = completion.enforce(hat, CONFIG)
        self.assertEqual(result["action"], "disabled_and_verified")
        hat.config.SetChargingConfig.assert_called_once_with(False, non_volatile=True)

    def test_malformed_api_data_cannot_count_as_charged(self):
        hat = FakeHat()
        hat.status.GetFaultStatus.return_value = ok(None)
        result = completion.enforce(hat, CONFIG)
        self.assertFalse(result["charged_at_user_threshold"])
        self.assertEqual(result["action"], "disabled_and_verified")

    def test_stale_off_observation_is_not_claimed_as_verified(self):
        hat = FakeHat(enabled=False)
        result = completion.enforce(
            hat, CONFIG, clock=Mock(side_effect=[100, 120, 120])
        )
        self.assertFalse(result["charged_at_user_threshold"])
        self.assertEqual(result["action"], "disabled_and_verified")
        hat.config.SetChargingConfig.assert_called_once_with(False, non_volatile=True)

    def test_failed_or_unverified_disable_does_not_claim_success(self):
        for readback in (
            ok({"charging_enabled": True}, non_volatile=True),
            ok({"charging_enabled": False}, non_volatile=False),
            {"error": "COMMUNICATION_ERROR"},
        ):
            hat = FakeHat()
            hat.config.GetChargingConfig.side_effect = [hat.read_charging(), readback]
            result = completion.enforce(hat, CONFIG)
            self.assertEqual(result["action"], "stop_failed")
            self.assertFalse(result["charging_disabled_verified"])

    def test_cli_consumes_threshold_file_and_writes_a_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, output = root / "config.json", root / "local" / "receipt.json"
            config.write_text(json.dumps(CONFIG))
            hat = FakeHat(percent=99.6)
            with contextlib.redirect_stdout(io.StringIO()):
                code = completion.main(
                    ["--config", str(config), "--output", str(output)],
                    device_factory=lambda: hat,
                )
            self.assertEqual(code, 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["configuration"]["stop_at_percent"], 99)
            self.assertEqual(result["reported_charge_percent"], 99.6)
            self.assertTrue(result["charged_at_user_threshold"])
            hat.config.SetChargingConfig.assert_called_once_with(
                False, non_volatile=True
            )


class ReceiptTests(unittest.TestCase):
    def test_replacement_syncs_directory_after_new_record_is_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            path.write_text('{"previous": true}')
            calls = []
            real_fsync = os.fsync

            def sync(descriptor):
                is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
                calls.append((is_directory, json.loads(path.read_text())))
                real_fsync(descriptor)

            with patch.object(completion.os, "fsync", side_effect=sync):
                completion.write_record(path, {"new": True})
            self.assertEqual(
                calls, [(False, {"previous": True}), (True, {"new": True})]
            )
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_directory_sync_failure_is_reported_and_descriptor_is_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            directories = []
            real_fsync = os.fsync

            def sync(descriptor):
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    directories.append(descriptor)
                    raise OSError("directory persistence failed")
                real_fsync(descriptor)

            with patch.object(completion.os, "fsync", side_effect=sync):
                with self.assertRaisesRegex(OSError, "directory persistence"):
                    completion.write_record(path, {"new": True})
            self.assertEqual(json.loads(path.read_text()), {"new": True})
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertEqual(len(directories), 1)
            with self.assertRaises(OSError):
                os.fstat(directories[0])


if __name__ == "__main__":
    unittest.main()
