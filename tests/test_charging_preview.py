import io
import json
import unittest

from ops.preview_charging import MAX_JSON_BYTES, read_json, replay


class ChargingPreviewTests(unittest.TestCase):
    def test_non_finite_non_object_and_oversized_json_are_rejected(self):
        for value in (b'{"value":NaN}', b"[]", b" " * (MAX_JSON_BYTES + 1)):
            with self.subTest(value=value[:30]):
                with self.assertRaises(ValueError):
                    read_json(value)

    def test_empty_observations_do_not_report_success(self):
        with self.assertRaisesRegex(ValueError, "No observations"):
            replay({}, io.BytesIO(b"\n"))

    def test_oversized_blank_line_is_rejected_before_skipping(self):
        with self.assertRaisesRegex(ValueError, "exceeds 64 KiB"):
            replay({}, io.BytesIO(b" " * (MAX_JSON_BYTES + 1)))

    def test_unqualified_profile_cannot_recommend_charging(self):
        config = {
            "profile_id": "unqualified-example",
            "candidate": {
                "charge_current_ma": 625,
                "regulation_voltage_mv": 4200,
                "termination_current_ma": 100,
                "temp_cold_c": 0,
                "temp_cool_c": 10,
                "temp_warm_c": 40,
                "temp_hot_c": 45,
            },
            "verified_battery": {"identity_confirmed": False},
        }
        sample = {
            "boot_id": "test-boot",
            "uptime_seconds": 100,
            "errors": {},
            "battery_present": True,
            "battery_status": "NORMAL",
            "power_input_status": "PRESENT",
            "power_5v_io_status": "NOT_PRESENT",
            "battery_voltage_mv": 3900,
            "battery_temperature_c": 22,
            "cell_temperature_c": 22,
            "cell_temperature_verified": True,
            "io_voltage_mv": 5000,
            "io_current_ma": 150,
            "activity": "idle",
            "charging_enabled": False,
        }
        observations = []
        for second in range(100, 241, 10):
            observations.append(json.dumps(dict(sample, uptime_seconds=second)))
        result = replay(config, io.BytesIO("\n".join(observations).encode()))
        self.assertFalse(result["hardware_writes"])
        self.assertTrue(result["qualification_errors"])
        self.assertNotIn("charge", result["decision_counts"])
        self.assertEqual(result["samples"], 15)


if __name__ == "__main__":
    unittest.main()
