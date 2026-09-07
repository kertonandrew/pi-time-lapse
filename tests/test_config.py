import json
import tempfile
import unittest
from pathlib import Path

from timelapse.config import load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"

    def load(self, overrides):
        self.path.write_text(json.dumps(overrides))
        return load_config(self.path)

    def test_defaults_leave_automatic_solar_transfer_unconfigured(self):
        power = load_config(None)["power"]
        self.assertFalse(power["battery_profile_verified"])
        self.assertFalse(power["allow_load_probe"])
        for name in ("minimum_input_w", "stop_input_w", "maximum_battery_discharge_w"):
            self.assertIsNone(power[name])

    def test_configured_thresholds_accept_zero_discharge_and_stop_limit(self):
        result = self.load(
            {
                "power": {
                    "minimum_input_w": 3,
                    "stop_input_w": 0,
                    "maximum_battery_discharge_w": 0,
                }
            }
        )
        self.assertEqual(result["power"]["stop_input_w"], 0)

    def test_numeric_power_fields_reject_bool_string_and_nonfinite(self):
        fields = (
            "minimum_input_w",
            "stop_input_w",
            "maximum_battery_discharge_w",
            "probe_minimum_input_w",
            "start_charge_percent",
            "stop_charge_percent",
            "start_peak_fraction",
            "continue_peak_fraction",
        )
        for field in fields:
            for value in (True, "1", float("nan"), float("inf"), -1):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    self.load({"power": {field: value}})

    def test_threshold_hysteresis_ranges(self):
        cases = (
            {"minimum_input_w": 0},
            {"probe_minimum_input_w": 0},
            {"stop_input_w": 1},
            {"minimum_input_w": 1, "stop_input_w": 2},
            {"minimum_input_w": 1, "probe_minimum_input_w": 2},
            {"start_charge_percent": 101},
            {"stop_charge_percent": 90, "start_charge_percent": 80},
            {"start_peak_fraction": 1.1},
            {"continue_peak_fraction": 0},
            {"continue_peak_fraction": 0.9, "start_peak_fraction": 0.8},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.load({"power": overrides})

    def test_timing_and_count_fields_reject_invalid_types(self):
        fields = (
            "max_age_seconds",
            "sustained_samples",
            "maximum_observation_gap_seconds",
            "probe_cooldown_seconds",
        )
        for field in fields:
            for value in (True, 0, -1, 1.5, "3", None):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    self.load({"power": {field: value}})

    def test_capture_reserve_rejects_nonfinite_bool_and_out_of_range(self):
        for value in (float("nan"), float("inf"), True, "20", -1, 101, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load({"minimum_capture_charge_percent": value})

    def test_paths_are_absolute_strings(self):
        cases = (
            {"spool": "relative"},
            {"hardware_database": None},
            {"power": {"sensor_path": "relative"}},
            {"power": {"sensor_path": "/bad\x00path"}},
            {"camera": {"camera_command": "rpicam-still"}},
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(value)

    def test_camera_options_fail_before_capture(self):
        cases = (
            {"rotation": 90},
            {"rotation": False},
            {"rotation": "180"},
            {"width": True},
            {"height": 0},
            {"settle_ms": 0},
            {"timeout_seconds": float("inf")},
            {"timeout_seconds": 3601},
            {"timeout_seconds": 1, "settle_ms": 1000},
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load({"camera": value})

    def test_unknown_keys_and_nonobjects_are_rejected(self):
        for value in ([], {"unknown": 1}, {"power": []}, {"camera": {"unknown": 1}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(value)


if __name__ == "__main__":
    unittest.main()
