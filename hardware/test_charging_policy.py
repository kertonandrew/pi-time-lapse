import copy
import unittest
from unittest.mock import patch

from hardware.charging_policy import ChargingPolicy


def qualified_config():
    return {
        "candidate": {
            "charge_current_ma": 550,
            "regulation_voltage_mv": 4100,
            "termination_current_ma": 50,
            "temp_cold_c": 10,
            "temp_cool_c": 15,
            "temp_warm_c": 30,
            "temp_hot_c": 35,
        },
        "verified_battery": {
            "identity_confirmed": True,
            "source": "synthetic test specification, not a real battery",
            "max_charge_current_ma": 605,
            "max_charge_voltage_mv": 4141,
            "min_charge_temperature_c": 10,
            "max_charge_temperature_c": 35,
            "ntc_r25_ohm": 10000,
            "ntc_beta_k": 3450,
            "temperature_calibration_verified": True,
            "termination_current_compatibility_verified": True,
        },
    }


def observation(uptime=100, **changes):
    sample = {
        "boot_id": "boot-a",
        "uptime_seconds": uptime,
        "errors": {},
        "battery_present": True,
        "battery_status": "NORMAL",
        "power_input_status": "PRESENT",
        "power_5v_io_status": "NOT_PRESENT",
        "battery_voltage_mv": 3900,
        "battery_temperature_c": 25,
        "cell_temperature_c": 25,
        "cell_temperature_verified": True,
        "io_voltage_mv": 5000,
        "io_current_ma": 200,
        "activity": "idle",
        "charging_enabled": False,
    }
    sample.update(changes)
    return sample


class QualificationTests(unittest.TestCase):
    def test_only_fully_documented_compatible_profile_qualifies(self):
        self.assertEqual(ChargingPolicy(qualified_config()).qualification_errors, [])
        for field in qualified_config()["verified_battery"]:
            with self.subTest(field=field):
                config = qualified_config()
                config["verified_battery"][field] = None
                policy = ChargingPolicy(config)
                self.assertTrue(policy.qualification_errors)
                result = policy.evaluate(observation(), 100, "boot-a")
                self.assertEqual(result["decision"], "blocked")
                self.assertEqual(result["suggested_charge_current_ma"], 0)

    def test_tolerances_apply_to_programmed_limits(self):
        for field, value in (
            ("max_charge_current_ma", 604.9),
            ("max_charge_voltage_mv", 4140.9),
        ):
            with self.subTest(field=field):
                config = qualified_config()
                config["verified_battery"][field] = value
                self.assertTrue(ChargingPolicy(config).qualification_errors)

    def test_termination_compatibility_cannot_be_assumed(self):
        config = qualified_config()
        config["verified_battery"]["termination_current_compatibility_verified"] = False
        self.assertIn(
            "battery_termination_compatibility_unverified",
            ChargingPolicy(config).qualification_errors,
        )

    def test_thermistor_values_must_be_encodable_and_avoid_sentinel(self):
        for field, invalid_values in (
            ("ntc_beta_k", (0, 3450.5, 65535, True)),
            ("ntc_r25_ohm", (0, 10000.5, 10001, 655360, True)),
        ):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    config = qualified_config()
                    config["verified_battery"][field] = value
                    self.assertIn(
                        f"unsupported_{field}",
                        ChargingPolicy(config).qualification_errors,
                    )

    def test_thermal_thresholds_are_signed_byte_integers(self):
        for field, value in (
            ("temp_cold_c", -129),
            ("temp_hot_c", 128),
            ("temp_cool_c", 15.25),
            ("temp_warm_c", 30.0),
            ("temp_cold_c", True),
        ):
            with self.subTest(field=field, value=value):
                config = qualified_config()
                config["candidate"][field] = value
                self.assertIn(
                    "unencodable_temperature_threshold",
                    ChargingPolicy(config).qualification_errors,
                )

    def test_register_range_and_steps_are_enforced(self):
        invalid_settings = {
            "charge_current_ma": [549, 551, 2525, True, float("nan")],
            "regulation_voltage_mv": [3499, 4101, 4441, False, float("inf")],
            "termination_current_ma": [49, 51, 450, True, float("-inf")],
        }
        for field, values in invalid_settings.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    config = qualified_config()
                    config["candidate"][field] = value
                    self.assertIn(
                        f"unsupported_{field}",
                        ChargingPolicy(config).qualification_errors,
                    )

    def test_temperature_limits_must_be_ordered_and_inside_cell_specification(self):
        for field, value in (
            ("temp_cold_c", 9),
            ("temp_cool_c", 30),
            ("temp_hot_c", 36),
            ("temp_warm_c", True),
        ):
            with self.subTest(field=field):
                config = qualified_config()
                config["candidate"][field] = value
                self.assertTrue(ChargingPolicy(config).qualification_errors)

    def test_configuration_is_copied_and_error_list_is_not_mutable(self):
        config = qualified_config()
        policy = ChargingPolicy(config)
        config["candidate"]["charge_current_ma"] = 2500
        self.assertEqual(policy.candidate["charge_current_ma"], 550)
        blocked_policy = ChargingPolicy({})
        errors = blocked_policy.qualification_errors
        original_errors = copy.copy(errors)
        errors.clear()
        self.assertTrue(original_errors)
        self.assertEqual(blocked_policy.qualification_errors, original_errors)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch(
            "hardware.charging_policy.time.monotonic", return_value=1000
        ).start()
        self.addCleanup(patch.stopall)
        self.policy = ChargingPolicy(qualified_config())

    def evaluate(self, uptime, **changes):
        return self.policy.evaluate(
            observation(uptime, **changes), uptime, changes.get("boot_id", "boot-a")
        )

    def qualify(self, start=100, **changes):
        for uptime in range(start, start + 121, 10):
            result = self.evaluate(uptime, **changes)
        self.assertEqual(result["decision"], "charge", result)
        return result

    def test_stable_dwell_and_read_only_contract(self):
        self.assertEqual(self.evaluate(100)["decision"], "hold")
        for uptime in range(110, 220, 10):
            result = self.evaluate(uptime)
            self.assertEqual(result["decision"], "hold")
            self.assertEqual(result["healthy_observation_seconds"], uptime - 100)
        result = self.evaluate(220)
        self.assertEqual(result["decision"], "charge")
        self.assertEqual(result["suggested_charge_current_ma"], 550)
        self.assertEqual(result["mode"], "advisory_only")
        self.assertFalse(result["hardware_writes"])

    def test_solar_dip_requires_full_new_dwell(self):
        for state in ("BAD", "WEAK", "NOT_PRESENT"):
            with self.subTest(state=state):
                self.policy = ChargingPolicy(qualified_config())
                self.qualify()
                self.assertEqual(
                    self.evaluate(230, power_input_status=state)["decision"], "hold"
                )
                self.assertEqual(self.evaluate(240)["decision"], "hold")
                self.qualify(250)

    def test_load_hysteresis_preserves_running_advice_until_two_watts(self):
        self.assertEqual(self.evaluate(100, io_current_ma=301)["decision"], "hold")
        self.qualify(110, io_current_ma=300)
        self.assertEqual(self.evaluate(240, io_current_ma=399)["decision"], "charge")
        self.assertEqual(self.evaluate(250, io_current_ma=400)["decision"], "hold")
        self.assertEqual(self.evaluate(260, io_current_ma=350)["decision"], "hold")

    def test_capture_upload_and_gpio_input_pause(self):
        for changes in (
            {"activity": "capture"},
            {"activity": "upload"},
            {"power_5v_io_status": "PRESENT"},
        ):
            with self.subTest(changes=changes):
                self.policy = ChargingPolicy(qualified_config())
                self.qualify()
                result = self.evaluate(230, **changes)
                self.assertEqual(result["decision"], "hold")
                self.assertEqual(result["suggested_charge_current_ma"], 0)

    def test_temperature_restart_hysteresis_and_hard_boundaries(self):
        for temperature in (10, 11.9, 33.1, 35):
            with self.subTest(temperature=temperature):
                result = self.evaluate(
                    100,
                    cell_temperature_c=temperature,
                    battery_temperature_c=temperature,
                )
                self.assertEqual(result["decision"], "hold")
                self.policy = ChargingPolicy(qualified_config())
        for temperature in (12, 33):
            self.policy = ChargingPolicy(qualified_config())
            self.qualify(
                cell_temperature_c=temperature, battery_temperature_c=temperature
            )
        self.policy = ChargingPolicy(qualified_config())
        self.qualify(cell_temperature_c=12, battery_temperature_c=12)
        self.assertEqual(
            self.evaluate(230, cell_temperature_c=11, battery_temperature_c=11)[
                "decision"
            ],
            "charge",
        )
        self.assertEqual(
            self.evaluate(240, cell_temperature_c=10, battery_temperature_c=10)[
                "decision"
            ],
            "hold",
        )

    def test_hat_reading_never_substitutes_for_independent_temperature(self):
        for changes in (
            {"cell_temperature_c": None},
            {"cell_temperature_verified": False},
        ):
            with self.subTest(changes=changes):
                self.policy = ChargingPolicy(qualified_config())
                result = self.evaluate(100, **changes)
                self.assertEqual(result["decision"], "blocked")
                self.assertIn(
                    "verified_independent_cell_temperature_required", result["reasons"]
                )
        sample = observation(110)
        sample.pop("cell_temperature_verified")
        self.assertEqual(
            self.policy.evaluate(sample, 110, "boot-a")["decision"], "blocked"
        )

    def test_temperature_disagreement_boundary(self):
        self.assertEqual(
            self.evaluate(100, battery_temperature_c=28)["decision"], "hold"
        )
        result = self.evaluate(110, battery_temperature_c=28.01)
        self.assertEqual(result["decision"], "fault")
        self.assertIn("cell_and_hat_temperature_disagreement", result["reasons"])

    def test_firmware_prediction_uses_hat_temperature_and_does_not_change_profile(self):
        for hat, cell, expected_current, expected_voltage, region in (
            (14, 16, 275, 4100, "cool"),
            (15, 16, 550, 4100, "normal"),
            (30, 29, 550, 4100, "normal"),
            (31, 29, 550, 3960, "warm"),
            (35, 33, 0, 3960, "stopped"),
        ):
            with self.subTest(hat=hat):
                self.policy = ChargingPolicy(qualified_config())
                result = self.evaluate(
                    100, battery_temperature_c=hat, cell_temperature_c=cell
                )
                limits = result["firmware_effective_limits"]
                self.assertEqual(limits["charge_current_ma"], expected_current)
                self.assertEqual(limits["regulation_voltage_mv"], expected_voltage)
                self.assertEqual(limits["temperature_region"], region)
                self.assertTrue(limits["predicted_not_readback"])
                self.assertEqual(self.policy.candidate["charge_current_ma"], 550)

    def test_temperature_rise_stops_after_sixty_seconds(self):
        self.qualify()
        for uptime in range(230, 280, 10):
            temperature = 25 + (uptime - 220) / 60
            self.assertEqual(
                self.evaluate(
                    uptime,
                    cell_temperature_c=temperature,
                    battery_temperature_c=temperature,
                )["decision"],
                "charge",
            )
        result = self.evaluate(280, cell_temperature_c=26, battery_temperature_c=26)
        self.assertEqual(result["decision"], "hold")
        self.assertIn("cell_temperature_rising_too_fast", result["reasons"])

    def test_warm_firmware_prediction_clamps_to_minimum_regulation(self):
        config = qualified_config()
        config["candidate"]["regulation_voltage_mv"] = 3500
        self.policy = ChargingPolicy(config)
        result = self.evaluate(100, cell_temperature_c=31, battery_temperature_c=31)
        self.assertEqual(
            result["firmware_effective_limits"]["regulation_voltage_mv"], 3500
        )

    def test_voltage_pause_recovery_and_latched_upper_fault(self):
        self.assertEqual(
            self.evaluate(100, battery_voltage_mv=3499)["decision"], "hold"
        )
        self.qualify(110, battery_voltage_mv=3500)
        self.assertEqual(
            self.evaluate(240, battery_voltage_mv=4100)["decision"], "hold"
        )
        self.qualify(250)
        self.assertEqual(
            self.evaluate(380, battery_voltage_mv=4150)["decision"], "fault"
        )
        result = self.evaluate(390, battery_voltage_mv=3900)
        self.assertEqual(result["decision"], "fault")
        self.assertIn("battery_voltage_at_or_above_4150mv", result["reasons"])

    def test_output_rail_boundaries_and_missing_battery(self):
        for voltage, expected in (
            (4799, "fault"),
            (4800, "hold"),
            (5250, "hold"),
            (5251, "fault"),
        ):
            self.policy = ChargingPolicy(qualified_config())
            self.assertEqual(
                self.evaluate(100, io_voltage_mv=voltage)["decision"], expected
            )
        self.policy = ChargingPolicy(qualified_config())
        self.assertEqual(self.evaluate(100, battery_present=False)["decision"], "hold")

    def test_numeric_and_state_corruption_never_produces_charge_advice(self):
        for field in (
            "battery_voltage_mv",
            "battery_temperature_c",
            "cell_temperature_c",
            "io_voltage_mv",
            "io_current_ma",
        ):
            for value in (True, float("nan"), float("inf"), "25"):
                with self.subTest(field=field, value=value):
                    self.policy = ChargingPolicy(qualified_config())
                    self.assertEqual(
                        self.evaluate(100, **{field: value})["decision"], "fault"
                    )
        for field in (
            "errors",
            "battery_status",
            "power_input_status",
            "power_5v_io_status",
            "activity",
            "charging_enabled",
            "battery_present",
        ):
            with self.subTest(field=field):
                self.policy = ChargingPolicy(qualified_config())
                sample = observation()
                sample.pop(field)
                self.assertEqual(
                    self.policy.evaluate(sample, 100, "boot-a")["decision"], "fault"
                )

    def test_stale_gap_duplicate_and_boot_change_reset_dwell(self):
        scenarios = (
            (observation(214), 230, "boot-a"),
            (observation(236), 236, "boot-a"),
            (observation(220), 220, "boot-a"),
            (observation(200), 200, "boot-a"),
            (observation(10, boot_id="boot-b"), 10, "boot-b"),
            (observation(230, boot_id=""), 230, ""),
        )
        for sample, now, boot in scenarios:
            with self.subTest(sample=sample, now=now, boot=boot):
                self.policy = ChargingPolicy(qualified_config())
                self.qualify()
                result = self.policy.evaluate(sample, now, boot)
                self.assertEqual(result["decision"], "hold")
                self.assertEqual(result["healthy_observation_seconds"], 0)

    def test_gap_clears_temperature_history(self):
        self.evaluate(100, cell_temperature_c=20, battery_temperature_c=20)
        self.assertEqual(
            self.evaluate(200, cell_temperature_c=25, battery_temperature_c=25)[
                "decision"
            ],
            "hold",
        )
        self.qualify(210)

    def test_thirty_minute_expiry_survives_dips_boots_and_later_recovery(self):
        self.qualify()
        self.assertEqual(
            self.evaluate(230, power_input_status="BAD")["decision"], "hold"
        )
        self.assertEqual(self.evaluate(10, boot_id="boot-b")["decision"], "hold")
        self.clock.return_value = 2800
        result = self.evaluate(20, boot_id="boot-b")
        self.assertEqual(result["reasons"], ["advisory_session_expired"])
        self.clock.return_value = 1000
        self.assertEqual(
            self.evaluate(30, boot_id="boot-b")["reasons"], ["advisory_session_expired"]
        )

    def test_replay_elapsed_time_expires_without_wall_clock_sleep(self):
        self.evaluate(100)
        for uptime in range(110, 1900, 10):
            self.evaluate(uptime)
        result = self.evaluate(1900)
        self.assertEqual(result["decision"], "hold")
        self.assertEqual(result["reasons"], ["advisory_session_expired"])

    def test_local_monotonic_rollback_holds(self):
        self.qualify()
        self.clock.return_value = 999
        result = self.evaluate(230)
        self.assertIn("local_monotonic_clock_rollback", result["reasons"])
        self.assertEqual(result["decision"], "hold")


if __name__ == "__main__":
    unittest.main()
