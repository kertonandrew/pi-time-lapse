import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from timelapse.config import DEFAULTS
from timelapse.power import PowerError, PowerHistory, decide, read_sensor


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def sample(index=0, **overrides):
    result = {
        "sample_id": f"sample-{index}",
        "sequence": index,
        "timestamp_utc": (NOW + timedelta(seconds=index * 60)).isoformat(),
        "observed_uptime_seconds": 1000 + index * 60,
        "pi_boot_id": "boot-1",
        "sensor_session_id": "sensor-1",
        "calibration_id": "calibration-1",
        "quality": "calibrated",
        "source": "solar",
        "time_source": "RTC",
        "input_healthy": True,
        "battery_healthy": True,
        "errors": [],
        "solar_input_w": 4.0,
        "battery_power_w": 0.0,
        "battery_charge_percent": 95,
    }
    result.update(overrides)
    return result


def settings(**overrides):
    result = copy.deepcopy(DEFAULTS["power"])
    result.update(
        battery_profile_verified=True,
        minimum_input_w=3.0,
        stop_input_w=2.0,
        maximum_battery_discharge_w=0.05,
    )
    result.update(overrides)
    return result


class SensorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "latest.json"

    def read(self, value, **overrides):
        self.path.write_text(json.dumps(value))
        arguments = {"now": NOW, "now_uptime": 1000, "boot_id": "boot-1"}
        arguments.update(overrides)
        return read_sensor(self.path, **arguments)

    def test_fresh_calibrated_current_boot_sample(self):
        self.assertEqual(self.read(sample())["sample_id"], "sample-0")

    def test_clock_rollback_cannot_rejuvenate_old_sample(self):
        with self.assertRaisesRegex(PowerError, "monotonic age"):
            self.read(sample(), now_uptime=1200)

    def test_stale_and_future_utc_or_monotonic_times(self):
        cases = (
            {"now": NOW + timedelta(seconds=91)},
            {"now": NOW - timedelta(seconds=1)},
            {"now_uptime": 1091},
            {"now_uptime": 999},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(PowerError):
                self.read(sample(), **arguments)

    def test_previous_boot_is_rejected(self):
        with self.assertRaisesRegex(PowerError, "another Pi boot"):
            self.read(sample(pi_boot_id="old-boot"))

    def test_required_health_identity_and_measurements(self):
        for field in sample():
            value = sample()
            del value[field]
            with self.subTest(field=field), self.assertRaises(PowerError):
                self.read(value)

    def test_invalid_values_fail_closed(self):
        cases = (
            ("solar_input_w", float("nan")),
            ("battery_power_w", float("inf")),
            ("battery_charge_percent", True),
            ("observed_uptime_seconds", "1000"),
            ("observed_uptime_seconds", float("nan")),
            ("solar_input_w", -1),
            ("battery_charge_percent", 101),
            ("sequence", True),
            ("sequence", -1),
            ("sequence", 2**63),
            ("input_healthy", 1),
            ("battery_healthy", False),
            ("errors", {}),
            ("errors", ["temperature"]),
            ("sensor_session_id", " "),
            ("quality", "estimated"),
            ("source", "usb"),
            ("time_source", "UNSYNC"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(PowerError):
                self.read(sample(**{field: value}))

    def test_sensor_object_and_size_are_bounded(self):
        for value in ([], "x" * 17000):
            with self.subTest(value_type=type(value)), self.assertRaises(PowerError):
                self.read(value)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "history.sqlite3"
        self.history = PowerHistory(self.path)

    def record(self, value, **kwargs):
        return self.history.record(value, now=NOW + timedelta(hours=1), **kwargs)

    def recent(self):
        return self.history.recent(now=NOW + timedelta(hours=1))

    def test_duplicate_does_not_write_or_extend_streak(self):
        self.assertTrue(self.record(sample()))
        statements = []
        original = self.history.connect

        def traced():
            connection = original()
            connection.set_trace_callback(statements.append)
            return connection

        self.history.connect = traced
        before = self.path.stat().st_mtime_ns
        self.assertFalse(self.record(sample(), activity="upload"))
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.assertFalse(
            any(
                statement.split()[0]
                in {"BEGIN", "COMMIT", "UPDATE", "INSERT", "DELETE"}
                for statement in statements
            )
        )
        self.assertEqual(len(self.recent()), 1)
        self.assertEqual(self.history.profile()["activity_counts"], {"idle": 1})

    def test_replay_or_changed_sample_invalidates_history(self):
        self.record(sample())
        self.record(sample(1))
        for value in (sample(), sample(1, solar_input_w=99)):
            with self.subTest(value=value), self.assertRaises(PowerError):
                self.record(value)
            self.assertEqual(self.recent(), [])

    def test_regressed_sequence_and_uptime_fail(self):
        self.record(sample(1))
        for value in (sample(2, sequence=0), sample(2, observed_uptime_seconds=1059)):
            with self.subTest(value=value), self.assertRaises(PowerError):
                self.record(value)

    def test_invalidation_survives_reopening_and_needs_new_samples(self):
        for index in range(3):
            self.record(sample(index))
        self.history.invalidate()
        self.history = PowerHistory(self.path)
        self.assertEqual(self.recent(), [])
        self.assertFalse(self.record(sample(2)))
        self.assertEqual(self.recent(), [])
        self.record(sample(3))
        self.assertEqual(len(self.recent()), 1)
        self.assertEqual(self.history.profile()["observations"], 4)

    def test_context_changes_start_a_new_admission_history(self):
        for index in range(3):
            self.record(sample(index))
        for offset, field in enumerate(
            ("sensor_session_id", "calibration_id", "pi_boot_id"), 3
        ):
            value = sample(offset, **{field: f"new-{field}"})
            self.record(value)
            self.assertEqual(len(self.recent()), 1)
            self.assertEqual(decide(value, settings(), self.recent())["action"], "wait")
        self.assertEqual(self.history.profile()["observations"], 6)

    def test_invalid_sensor_health_breaks_existing_streak(self):
        self.record(sample())
        with self.assertRaises(PowerError):
            self.record(sample(1, battery_healthy=False))
        self.assertEqual(self.recent(), [])

    def test_profile_keeps_activity_and_requires_multiple_days(self):
        for index, activity in enumerate(("idle", "upload", "probe")):
            self.record(sample(index), activity=activity)
        report = self.history.profile()
        self.assertEqual(
            report["activity_counts"], {"idle": 1, "upload": 1, "probe": 1}
        )
        self.assertIsNone(report["strongest_observed_utc_half_hour"])

    def test_old_schema_remains_profile_only(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE observations (sample_id TEXT PRIMARY KEY, timestamp_utc TEXT NOT NULL, solar_input_w REAL NOT NULL, battery_power_w REAL NOT NULL, battery_charge_percent REAL NOT NULL)"
            )
            connection.execute(
                "INSERT INTO observations VALUES (?, ?, 4, 0, 95)",
                ("legacy", NOW.isoformat()),
            )
        self.assertEqual(self.recent(), [])
        self.assertEqual(self.history.profile()["observations"], 1)


class DecisionTests(unittest.TestCase):
    def test_full_battery_with_zero_charge_current_can_upload(self):
        observations = [
            sample(index, battery_charge_percent=100, battery_power_w=0)
            for index in range(3)
        ]
        self.assertEqual(
            decide(observations[-1], settings(), observations)["action"], "upload"
        )

    def test_start_and_stop_input_thresholds_are_distinct(self):
        value = sample(solar_input_w=2.5)
        self.assertEqual(decide(value, settings(), [value])["action"], "wait")
        self.assertEqual(
            decide(value, settings(), [value], active=True)["action"], "upload"
        )
        self.assertEqual(
            decide(sample(solar_input_w=1.9), settings(), [], active=True)["action"],
            "wait",
        )

    def test_relative_peak_and_reserve_hysteresis(self):
        value = sample(2, solar_input_w=7, battery_charge_percent=80)
        observations = [sample(solar_input_w=10), sample(1, solar_input_w=10), value]
        policy = settings()
        self.assertEqual(decide(value, policy, observations)["action"], "wait")
        self.assertEqual(
            decide(value, policy, observations, active=True)["action"], "upload"
        )

    def test_missing_threshold_or_unverified_profile_denies_upload(self):
        for overrides in (
            {"stop_input_w": None},
            {"minimum_input_w": None},
            {"maximum_battery_discharge_w": None},
            {"battery_profile_verified": False},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(
                    decide(sample(), settings(**overrides), [], active=True)["action"],
                    "wait",
                )

    def test_duplicate_sequence_context_or_gap_does_not_sustain(self):
        rows = [sample(index) for index in range(3)]
        cases = (
            [rows[0], rows[0], rows[2]],
            [rows[0], dict(rows[1], sequence=0), rows[2]],
            [dict(rows[0], sensor_session_id="old"), rows[1], rows[2]],
            [rows[0], rows[1], dict(rows[2], observed_uptime_seconds=5000)],
        )
        for observations in cases:
            with self.subTest(observations=observations):
                self.assertEqual(
                    decide(observations[-1], settings(), observations)["action"], "wait"
                )

    def test_monotonic_streak_survives_wall_clock_correction(self):
        observations = [
            sample(index, timestamp_utc=(NOW - timedelta(seconds=index)).isoformat())
            for index in range(3)
        ]
        self.assertEqual(
            decide(observations[-1], settings(), observations)["action"], "upload"
        )

    def test_load_probe_requires_opt_in_floor_and_reserve(self):
        value = sample(solar_input_w=1.5)
        policy = settings(allow_load_probe=True, probe_minimum_input_w=1)
        self.assertEqual(decide(value, policy, [value])["action"], "probe")
        for overrides in (
            {"allow_load_probe": False},
            {"probe_minimum_input_w": None},
            {"start_charge_percent": 96},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(
                    decide(value, dict(policy, **overrides), [value])["action"], "wait"
                )

    def test_battery_discharge_stops_even_strong_input(self):
        self.assertEqual(
            decide(sample(battery_power_w=0.1), settings(), [], active=True)["action"],
            "wait",
        )


if __name__ == "__main__":
    unittest.main()
