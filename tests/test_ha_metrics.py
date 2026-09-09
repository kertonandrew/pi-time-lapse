import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hardware.power_monitor import FIELDS
from timelapse.ha_metrics import metrics_from_row, read_metrics


NOW = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
JPEG = b"\xff\xd8photograph\xff\xd9"


def sample(**overrides):
    row = {
        "session_id": "session-1",
        "sample_index": 1,
        "timestamp_utc": (NOW - timedelta(seconds=60)).isoformat(),
        "uptime_seconds": 940.0,
        "boot_id": "boot-1",
        "time_source": "NTP",
        "sample_gap_seconds": 60.0,
        "dropped_samples": 0,
        "battery_present": 1,
        "battery_status": "NORMAL",
        "power_input_status": "PRESENT",
        "power_5v_io_status": "NOT_PRESENT",
        "battery_charge_percent": 82.0,
        "battery_voltage_mv": 3938.0,
        "battery_current_estimate_ma": -50.0,
        "battery_temperature_c": 21.0,
        "io_voltage_mv": 5000.0,
        "io_current_ma": 140.0,
        "battery_power_estimate_w": -0.1969,
        "io_power_w": 0.7,
        "cpu_temperature_c": 42.1,
        "memory_available_bytes": 268435456,
        "load_1m": 0.17,
        "raw_pijuice_json": "{}",
        "errors_json": "{}",
    }
    row.update(overrides)
    return row


def convert(row, **overrides):
    options = {"now": NOW, "current_boot_id": "boot-1", "current_uptime": 1000.0}
    options.update(overrides)
    return metrics_from_row(row, **options)


class MetricsConversionTests(unittest.TestCase):
    def test_valid_units_and_original_observation_identity_are_preserved(self):
        row = sample()
        result = convert(row)
        self.assertEqual(result["observed_at"], row["timestamp_utc"])
        self.assertEqual(result["session_id"], "session-1")
        self.assertEqual(result["sample_index"], 1)
        self.assertEqual(result["battery_voltage"], 3.938)
        self.assertEqual(result["battery_current_estimate"], -0.05)
        self.assertAlmostEqual(result["battery_power_estimate"], -0.1969)
        self.assertEqual(result["io_power"], 0.7)
        self.assertEqual(result["memory_available"], 256)
        self.assertEqual(result["reported_battery_temperature"], 21)
        self.assertEqual(result["telemetry_errors"], 0)
        self.assertNotIn("solar_input_w", result)
        self.assertNotIn("cell_temperature_c", result)
        json.dumps(result, allow_nan=False)

    def test_same_observation_does_not_gain_a_new_timestamp_or_age(self):
        first = convert(sample())
        later = convert(sample(), now=NOW + timedelta(seconds=10), current_uptime=1010)
        self.assertEqual(first, later)

    def test_stale_cross_boot_empty_boot_and_clock_rollback_are_rejected(self):
        for change in (
            {"uptime_seconds": 579},
            {"uptime_seconds": 1001},
            {"uptime_seconds": -1},
            {"uptime_seconds": float("nan")},
            {"boot_id": "boot-old"},
            {"boot_id": ""},
            {"boot_id": None},
            {"timestamp_utc": (NOW - timedelta(seconds=421)).isoformat()},
            {"timestamp_utc": (NOW + timedelta(seconds=6)).isoformat()},
            {"timestamp_utc": "2026-09-08T06:00:00"},
            {"time_source": "guessed"},
            {"session_id": ""},
            {"sample_index": True},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                convert(sample(**change))
        for change in (
            {"current_boot_id": ""},
            {"current_uptime": None},
            {"now": NOW.replace(tzinfo=None)},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                convert(sample(), **change)

    def test_unsynchronized_clock_does_not_create_a_confirmed_timestamp(self):
        row = sample(time_source="UNSYNC", timestamp_utc="1970-01-01T00:01:00+00:00")
        result = convert(row)
        self.assertIsNone(result["observed_at"])
        self.assertEqual(result["sample_timestamp_utc"], row["timestamp_utc"])
        self.assertEqual(result["io_power"], 0.7)
        with self.assertRaises(ValueError):
            convert(sample(time_source="UNSYNC", uptime_seconds=1))

    def test_read_errors_invalidate_affected_value_and_derived_power(self):
        result = convert(
            sample(
                errors_json=json.dumps(
                    {
                        "GetIoCurrent": "READ_FAILED",
                        "GetBatteryTemperature": "INVALID_DATA",
                    }
                )
            )
        )
        self.assertIsNone(result["io_current"])
        self.assertIsNone(result["io_power"])
        self.assertIsNone(result["reported_battery_temperature"])
        self.assertEqual(result["battery_voltage"], 3.938)
        self.assertEqual(result["telemetry_errors"], 2)

    def test_negative_output_current_is_unknown_not_solar_generation(self):
        result = convert(sample(io_current_ma=-405, io_power_w=-2.025))
        self.assertIsNone(result["io_current"])
        self.assertIsNone(result["io_power"])
        self.assertIn("io_current_ma", result["telemetry_error_keys"])

    def test_missing_readings_are_unknown_and_counted_as_errors(self):
        result = convert(sample(io_current_ma=None))
        self.assertIsNone(result["io_current"])
        self.assertIsNone(result["io_power"])
        self.assertIn("io_current_ma", result["telemetry_error_keys"])

    def test_reported_freshness_read_errors_reject_cached_identity(self):
        for key in ("boot_id", "uptime_seconds"):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "unverified freshness"),
            ):
                convert(sample(errors_json=json.dumps({key: "READ_FAILED"})))

    def test_nonnumeric_and_nonfinite_values_never_publish_zero_or_nan(self):
        for value in ("0", True, float("nan"), float("inf"), -1, 101):
            with self.subTest(value=value):
                result = convert(sample(battery_charge_percent=value))
                self.assertIsNone(result["battery_percent_estimate"])
                self.assertIn("battery_charge_percent", result["telemetry_error_keys"])
                json.dumps(result, allow_nan=False)

    def test_absent_battery_masks_plausible_stale_readings(self):
        result = convert(sample(battery_present=0, battery_status="NOT_PRESENT"))
        for key in (
            "battery_percent_estimate",
            "battery_voltage",
            "battery_current_estimate",
            "battery_power_estimate",
            "reported_battery_temperature",
        ):
            self.assertIsNone(result[key])
        self.assertEqual(result["battery_status"], "NOT_PRESENT")
        self.assertEqual(result["telemetry_errors"], 0)

    def test_unknown_or_conflicting_battery_presence_masks_estimates(self):
        for change in (
            {"battery_present": None},
            {"battery_present": 0},
            {"battery_status": "INVALID"},
            {"errors_json": '{"GetStatus":"READ_FAILED"}'},
        ):
            with self.subTest(change=change):
                result = convert(sample(**change))
                self.assertIsNone(result["battery_voltage"])
                self.assertIsNone(result["battery_status"])
                self.assertIn("battery_presence", result["telemetry_error_keys"])

    def test_malformed_error_details_reject_the_latest_row(self):
        for value in (None, "[ ]", "{", '{"foo":NaN}', '{"foo":1}', " " * 65537):
            with self.subTest(value=str(value)[:30]), self.assertRaises(ValueError):
                convert(sample(errors_json=value))


class MetricsDatabaseAndSpoolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "metrics.sqlite3"
        self.spool = self.root / "spool"

    def insert(self, row):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            columns = ",".join(f"{name} {kind}" for name, kind in FIELDS.items())
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY AUTOINCREMENT,{columns})"
            )
            connection.execute(
                f"INSERT INTO samples ({','.join(FIELDS)}) VALUES ({','.join('?' for _ in FIELDS)})",
                tuple(row.get(name) for name in FIELDS),
            )

    def read(self):
        return read_metrics(
            self.database,
            self.spool,
            now=NOW,
            current_boot_id="boot-1",
            current_uptime=1000,
        )

    def make_photo(self, filename="capture.jpg", receipt=False):
        for name in ("images", "metadata", "receipts"):
            (self.spool / name).mkdir(parents=True, exist_ok=True)
        (self.spool / "images" / filename).write_bytes(JPEG)
        record = {
            "filename": filename,
            "size_bytes": len(JPEG),
            "sha256": hashlib.sha256(JPEG).hexdigest(),
            "captured_at_utc": NOW.isoformat(),
            "time_source": "RTC",
            "boot_id": "boot-1",
            "capture_duration_seconds": 1.25,
        }
        (self.spool / "metadata" / f"{Path(filename).stem}.json").write_text(
            json.dumps(record)
        )
        if receipt:
            (self.spool / "receipts" / f"{Path(filename).stem}.json").write_text(
                json.dumps(record)
            )
        return record

    def test_missing_database_is_not_created(self):
        with self.assertRaisesRegex(ValueError, "cannot be read"):
            self.read()
        self.assertFalse(self.database.exists())
        self.assertFalse(self.spool.exists())

    def test_empty_database_has_no_metrics(self):
        self.insert(sample())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM samples")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.read()

    def test_latest_malformed_row_is_not_replaced_by_older_healthy_data(self):
        self.insert(sample())
        self.insert(sample(sample_index=2, errors_json="{"))
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.read()

    def test_absent_spool_is_empty_without_creating_directories(self):
        self.insert(sample())
        before = self.database.read_bytes()
        result = self.read()
        self.assertEqual(result["pending_photos"], 0)
        self.assertEqual(result["stored_photos"], 0)
        self.assertEqual(result["spool_bytes"], 0)
        self.assertGreater(result["free_bytes"], 0)
        self.assertFalse(self.spool.exists())
        self.assertEqual(before, self.database.read_bytes())

    def test_counts_validate_receipts_without_opening_or_hashing_images(self):
        self.insert(sample())
        self.make_photo("pending.jpg")
        self.make_photo("synced.jpg", receipt=True)
        from timelapse.ha_metrics import _read_record as original_read_json

        def record_only(path):
            self.assertNotEqual(path.suffix, ".jpg")
            return original_read_json(path)

        with patch("timelapse.ha_metrics._read_record", side_effect=record_only):
            result = self.read()
        self.assertEqual(result["pending_photos"], 1)
        self.assertEqual(result["stored_photos"], 2)
        self.assertEqual(
            result["spool_bytes"],
            sum(path.stat().st_size for path in self.spool.glob("*/*")),
        )
        self.assertEqual(result["telemetry_errors"], 0)

    def test_corrupt_spool_does_not_hide_hardware_metrics_or_publish_fake_counts(self):
        self.insert(sample())
        self.make_photo(receipt=True)
        path = self.spool / "receipts" / "capture.json"
        receipt = json.loads(path.read_text())
        receipt["sha256"] = "0" * 64
        path.write_text(json.dumps(receipt))
        result = self.read()
        self.assertEqual(result["battery_voltage"], 3.938)
        self.assertIsNone(result["pending_photos"])
        self.assertIsNone(result["stored_photos"])
        self.assertIn("spool", result["telemetry_error_keys"])
        self.assertIn("receipt", result["spool_error"])

    def test_missing_committed_image_is_unknown(self):
        self.insert(sample())
        self.make_photo()
        (self.spool / "images" / "capture.jpg").unlink()
        self.assertIsNone(self.read()["stored_photos"])

    def test_partial_directories_and_pending_publication_are_unknown(self):
        self.insert(sample())
        (self.spool / "images").mkdir(parents=True)
        self.assertIsNone(self.read()["pending_photos"])
        self.make_photo()
        (self.spool / "metadata" / ".capture.pending.json").write_text("{}")
        self.assertIsNone(self.read()["pending_photos"])

    def test_symlinks_and_oversized_metadata_do_not_get_followed(self):
        self.insert(sample())
        self.make_photo()
        metadata = self.spool / "metadata" / "capture.json"
        metadata.write_text(" " * 65537)
        self.assertIsNone(self.read()["stored_photos"])
        metadata.unlink()
        metadata.symlink_to(self.database)
        self.assertIsNone(self.read()["stored_photos"])

    def test_inspection_is_bounded(self):
        self.insert(sample())
        self.make_photo()
        with patch("timelapse.ha_metrics.MAX_SPOOL_ENTRIES", 1):
            result = self.read()
        self.assertIsNone(result["spool_bytes"])
        self.assertIn("bounded", result["spool_error"])


if __name__ == "__main__":
    unittest.main()
