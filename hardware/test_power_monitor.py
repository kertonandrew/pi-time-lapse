import contextlib
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hardware import power_monitor as monitor


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "metrics.sqlite3"
        self.now = datetime(2026, 9, 7, 13, 14, 15, tzinfo=timezone.utc)

    def row(self, uptime=100, timestamp=None):
        row = dict.fromkeys(monitor.FIELDS)
        row.update(
            timestamp_utc=(timestamp or self.now).isoformat(),
            uptime_seconds=uptime,
            boot_id="boot-a",
            time_source="RTC",
            battery_status="NORMAL",
            power_input_status="PRESENT",
            power_5v_io_status="NOT_PRESENT",
            raw_pijuice_json='{"GetFaultStatus":{"data":{},"error":"NO_ERROR"}}',
            errors_json="{}",
        )
        return row

    def committed(self):
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM samples ORDER BY id")
            ]


class SamplingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.proc = self.root / "proc"
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-a\n")
        (self.proc / "uptime").write_text("123.45 67.89\n")
        (self.proc / "meminfo").write_text(
            "MemTotal: 437248 kB\nMemAvailable: 321000 kB\n"
        )
        (self.proc / "loadavg").write_text("0.01 0.02 0.03 1/50 100\n")
        self.thermal = self.root / "temp"
        self.thermal.write_text("42000\n")
        self.device = SimpleNamespace(status=Mock(), rtcAlarm=Mock())
        self.device.status.GetStatus.return_value = {
            "error": "NO_ERROR",
            "data": {
                "battery": "NORMAL",
                "powerInput": "PRESENT",
                "powerInput5vIo": "NOT_PRESENT",
            },
        }
        self.device.status.GetFaultStatus.return_value = {
            "error": "NO_ERROR",
            "data": {},
        }
        values = [75, 4000, 100, 30, 5000, -160]
        for method, value in zip(monitor.METRICS.values(), values):
            getattr(self.device.status, method).return_value = {
                "error": "NO_ERROR",
                "data": value,
            }
        self.device.rtcAlarm.GetTime.return_value = {
            "error": "NO_ERROR",
            "data": {
                field: getattr(self.now, field)
                for field in ("year", "month", "day", "hour", "minute", "second")
            },
        }
        self.marker = self.root / "synchronized"
        patcher = patch.object(monitor.pijuice_clock, "NTP_SYNCHRONIZED", self.marker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sample(self):
        return monitor.collect_sample(
            self.device, self.proc, self.thermal, now=lambda: self.now
        )

    def test_records_signed_estimates_and_system_metrics(self):
        row = self.sample()
        self.assertEqual(row["battery_power_estimate_w"], 0.4)
        self.assertEqual(row["io_power_w"], -0.8)
        self.assertEqual(row["memory_available_bytes"], 321000 * 1024)
        self.assertEqual(row["cpu_temperature_c"], 42)
        self.assertEqual(row["uptime_seconds"], 123.45)
        self.assertEqual(row["time_source"], "RTC")
        self.assertEqual(json.loads(row["errors_json"]), {})

    def test_absent_battery_is_null_but_raw_response_is_preserved(self):
        self.device.status.GetStatus.return_value["data"]["battery"] = "NOT_PRESENT"
        row = self.sample()
        for field in monitor.METRICS:
            if field.startswith("battery_"):
                self.assertIsNone(row[field])
        self.assertFalse(row["battery_present"])
        self.assertIsNone(row["battery_power_estimate_w"])
        self.assertEqual(
            json.loads(row["raw_pijuice_json"])["GetBatteryVoltage"]["data"], 4000
        )
        self.assertEqual(row["io_power_w"], -0.8)

    def test_failed_read_has_no_zero_or_stale_value(self):
        self.assertEqual(self.sample()["io_current_ma"], -160)
        self.device.status.GetIoCurrent.return_value = {"error": "COMMUNICATION_ERROR"}
        row = self.sample()
        self.assertIsNone(row["io_current_ma"])
        self.assertIsNone(row["io_power_w"])
        self.assertEqual(
            json.loads(row["errors_json"])["GetIoCurrent"], "COMMUNICATION_ERROR"
        )
        self.assertEqual(row["battery_voltage_mv"], 4000)

    def test_exceptions_and_invalid_numeric_data_do_not_abort_other_reads(self):
        self.device.status.GetBatteryVoltage.side_effect = OSError("no response")
        self.device.status.GetIoCurrent.return_value = {
            "error": "NO_ERROR",
            "data": True,
        }
        row = self.sample()
        self.assertEqual(row["battery_charge_percent"], 75)
        self.assertIsNone(row["battery_power_estimate_w"])
        self.assertIsNone(row["io_current_ma"])
        self.assertEqual(
            set(json.loads(row["errors_json"])), {"GetBatteryVoltage", "GetIoCurrent"}
        )

    def test_clock_quality_distinguishes_untrusted_time(self):
        self.device.rtcAlarm.GetTime.return_value["data"]["year"] = 2000
        self.assertEqual(self.sample()["time_source"], "UNSYNC")
        self.marker.touch()
        self.assertEqual(self.sample()["time_source"], "NTP")
        self.now = self.now.replace(year=2000)
        self.assertEqual(self.sample()["time_source"], "UNSYNC")


class StorageTests(Fixture):
    def test_retention_uses_id_despite_backward_wall_clock(self):
        buffer = monitor.SampleBuffer(self.database, retention=2)
        for offset in range(4):
            buffer.append(
                self.row(100 + offset * 60, self.now - timedelta(days=offset))
            )
            buffer.flush()
        rows = self.committed()
        self.assertEqual([row["sample_index"] for row in rows], [2, 3])
        self.assertEqual(rows[-1]["sample_gap_seconds"], 60)
        self.assertLess(rows[-1]["timestamp_utc"], rows[0]["timestamp_utc"])
        self.assertFalse(any("energy" in field or "wh" in field for field in rows[-1]))

    def test_failed_batch_rolls_back_and_retries_without_duplicates(self):
        buffer = monitor.SampleBuffer(self.database)
        buffer.append(self.row())
        buffer.flush()
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "CREATE TRIGGER fail_insert BEFORE INSERT ON samples WHEN NEW.sample_index=2 BEGIN SELECT RAISE(ABORT,'simulated disk failure'); END"
            )
        buffer.append(self.row(160))
        buffer.append(self.row(220))
        with self.assertRaises(sqlite3.IntegrityError):
            buffer.flush()
        self.assertEqual(len(buffer.rows), 2)
        self.assertEqual(len(self.committed()), 1)
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TRIGGER fail_insert")
        buffer.flush()
        self.assertEqual(len(buffer.rows), 0)
        self.assertEqual([row["sample_index"] for row in self.committed()], [0, 1, 2])

    def test_queue_overflow_is_bounded_and_loss_is_explicit(self):
        buffer = monitor.SampleBuffer(self.database, capacity=2)
        for offset in range(5):
            buffer.append(self.row(100 + offset * 60))
        self.assertEqual(len(buffer.rows), 2)
        buffer.flush()
        rows = self.committed()
        self.assertEqual(rows[0]["dropped_samples"], 3)
        self.assertEqual(rows[1]["dropped_samples"], 0)
        self.assertEqual([row["sample_index"] for row in rows], [3, 4])

    def test_new_boot_and_new_process_do_not_imply_continuous_sampling(self):
        buffer = monitor.SampleBuffer(self.database)
        buffer.append(self.row(100))
        next_boot = self.row(10)
        next_boot["boot_id"] = "boot-b"
        buffer.append(next_boot)
        buffer.flush()
        fresh = monitor.SampleBuffer(self.database)
        fresh.append(self.row(500))
        fresh.flush()
        rows = self.committed()
        self.assertTrue(all(row["sample_gap_seconds"] is None for row in rows))
        self.assertNotEqual(rows[1]["session_id"], rows[2]["session_id"])

    def test_export_snapshot_excludes_concurrent_commit(self):
        buffer = monitor.SampleBuffer(self.database)
        buffer.append(self.row())
        buffer.flush()

        class Output(io.StringIO):
            def write(inner, value):
                if not inner.tell():
                    buffer.append(self.row(160))
                    buffer.flush()
                return super().write(value)

        output = Output()
        monitor.export_csv(self.database, output)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(output.getvalue())))), 1)
        self.assertEqual(len(self.committed()), 2)


class Schedule:
    def __init__(self):
        self.seconds = 0
        self.stopped = False

    def clock(self):
        return self.seconds

    def wait(self, seconds):
        self.seconds += seconds
        return self.stopped

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True


class DaemonTests(Fixture):
    def test_first_transition_periodic_and_stop_flushes(self):
        schedule = Schedule()
        buffer = monitor.SampleBuffer(self.database)
        seen = []

        def sample(device):
            seen.append(schedule.seconds)
            row = self.row(schedule.seconds)
            if schedule.seconds >= 60:
                row["power_input_status"] = "NOT_PRESENT"
            if schedule.seconds == 420:
                schedule.set()
            return row

        flushed = []
        original_flush = buffer.flush

        def flush():
            flushed.append(schedule.seconds)
            original_flush()

        with patch.object(buffer, "flush", side_effect=flush):
            monitor.run_monitor(
                buffer,
                stop=schedule,
                factory=Mock(),
                clock=schedule.clock,
                sample=sample,
                recover=Mock(),
            )
        self.assertEqual(flushed, [0, 60, 360, 420])
        self.assertEqual(len(self.committed()), 8)
        self.assertEqual(seen, list(range(0, 421, 60)))

    def test_fast_sampling_recovers_clock_only_once_per_minute(self):
        schedule = Schedule()
        buffer = monitor.SampleBuffer(self.database)
        recover = Mock(side_effect=RuntimeError("RTC unavailable"))

        def sample(device):
            if schedule.seconds == 65:
                schedule.set()
            return self.row(schedule.seconds)

        with contextlib.redirect_stderr(io.StringIO()):
            monitor.run_monitor(
                buffer,
                interval=5,
                stop=schedule,
                factory=Mock(),
                clock=schedule.clock,
                sample=sample,
                recover=recover,
            )
        self.assertEqual(recover.call_count, 2)
        self.assertEqual(len(self.committed()), 14)
        self.assertIn("clock_recovery", json.loads(self.committed()[0]["errors_json"]))

    def test_flush_duration_does_not_extend_five_minute_batch(self):
        schedule = Schedule()
        buffer = monitor.SampleBuffer(self.database)
        flushed = []
        original_flush = buffer.flush

        def sample(device):
            if schedule.seconds == 360:
                schedule.set()
            return self.row(schedule.seconds)

        def flush():
            flushed.append((schedule.seconds, buffer.rows[-1]["sample_index"]))
            original_flush()
            schedule.seconds += 2

        with patch.object(buffer, "flush", side_effect=flush):
            monitor.run_monitor(
                buffer,
                stop=schedule,
                factory=Mock(),
                clock=schedule.clock,
                sample=sample,
                recover=Mock(),
            )
        self.assertEqual(flushed, [(0, 0), (300, 5), (360, 6)])
        self.assertEqual(len(self.committed()), 7)

    def test_unexpected_failure_flushes_completed_samples(self):
        schedule = Schedule()
        buffer = monitor.SampleBuffer(self.database)

        def sample(device):
            if schedule.seconds == 120:
                raise RuntimeError("unexpected failure")
            return self.row(schedule.seconds)

        with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
            monitor.run_monitor(
                buffer,
                stop=schedule,
                factory=Mock(),
                clock=schedule.clock,
                sample=sample,
                recover=Mock(),
            )
        self.assertEqual(len(self.committed()), 2)

    def test_clock_recovery_work_does_not_skip_every_other_minute(self):
        schedule = Schedule()
        buffer = monitor.SampleBuffer(self.database)
        recoveries = []

        def recover(device, synchronized):
            recoveries.append(schedule.seconds)
            schedule.seconds += 1

        def sample(device):
            if schedule.seconds >= 121:
                schedule.set()
            return self.row(schedule.seconds)

        monitor.run_monitor(
            buffer,
            stop=schedule,
            factory=Mock(),
            clock=schedule.clock,
            sample=sample,
            recover=recover,
        )
        self.assertEqual(recoveries, [0, 60, 120])

    def test_clock_failure_cannot_poison_sample_controller(self):
        schedule = Schedule()
        buffer = monitor.SampleBuffer(self.database)
        clock_device, sample_device = object(), object()
        factory = Mock(side_effect=[clock_device, sample_device])
        recover = Mock(side_effect=OSError("clock transfer failed"))

        def sample(device):
            self.assertIs(device, sample_device)
            schedule.set()
            return self.row(schedule.seconds)

        with contextlib.redirect_stderr(io.StringIO()):
            monitor.run_monitor(
                buffer,
                stop=schedule,
                factory=factory,
                clock=schedule.clock,
                sample=sample,
                recover=recover,
            )
        self.assertIs(recover.call_args.args[0], clock_device)
        self.assertEqual(len(self.committed()), 1)

    def test_error_logging_is_bounded_per_category(self):
        output = io.StringIO()
        reporter = monitor.ErrorReporter(clock=lambda: 0, output=output)
        for _ in range(20):
            reporter.report("read failure")
            reporter.report("database failure", "storage")
        self.assertEqual(
            output.getvalue().splitlines(), ["read failure", "database failure"]
        )

    def test_environment_defaults_and_minimum_interval(self):
        with patch.dict(
            "os.environ",
            {"PI_HARDWARE_INTERVAL": "5", "PI_HARDWARE_FLUSH_SECONDS": "60"},
        ):
            args = monitor.arguments([])
        self.assertEqual((args.interval, args.flush_seconds), (5, 60))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            monitor.arguments(["--interval", "0"])


if __name__ == "__main__":
    unittest.main()
