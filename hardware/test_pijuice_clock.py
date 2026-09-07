import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from hardware import pijuice_clock


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, 14, 30, 15, tzinfo=timezone.utc)
        self.clock = Mock(
            spec=[
                "GetTime",
                "SetTime",
                "SetAlarm",
                "SetWakeupEnabled",
                "ClearAlarmFlag",
            ]
        )
        self.clock.SetTime.return_value = {"error": "NO_ERROR"}
        self.pijuice = SimpleNamespace(rtcAlarm=self.clock)
        self.bind = Mock()

    def reading(self, value):
        return {
            "error": "NO_ERROR",
            "data": {
                field: getattr(value, field)
                for field in ("year", "month", "day", "hour", "minute", "second")
            },
        }

    def run_clock(self, synchronized=True, now=None):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            pijuice_clock.synchronize_clock(
                self.pijuice,
                synchronized,
                now=lambda: now or self.now,
                bind=self.bind,
            )
        return output.getvalue()

    def test_unsynchronized_clock_is_never_written(self):
        self.clock.GetTime.return_value = self.reading(self.now.replace(year=2000))
        with self.assertRaisesRegex(pijuice_clock.ClockError, "waiting for network"):
            self.run_clock(synchronized=False)
        self.bind.assert_not_called()
        self.clock.GetTime.return_value = self.reading(self.now - timedelta(days=2))
        self.assertEqual(self.run_clock(synchronized=False), "")
        self.bind.assert_called_once_with()
        self.clock.SetTime.assert_not_called()

    def test_reset_and_skew_repairs_use_utc_without_touching_alarms(self):
        for old_time in (self.now.replace(year=2000), self.now - timedelta(seconds=3)):
            with self.subTest(old_time=old_time):
                self.clock.reset_mock()
                self.clock.GetTime.side_effect = [
                    self.reading(old_time),
                    self.reading(self.now),
                ]
                local_time = self.now.astimezone(timezone(timedelta(hours=10)))
                output = self.run_clock(now=local_time)
                self.assertEqual(
                    self.clock.method_calls,
                    [
                        call.GetTime(),
                        call.SetTime(
                            {
                                "year": 2026,
                                "month": 9,
                                "day": 7,
                                "weekday": 2,
                                "hour": 14,
                                "minute": 30,
                                "second": 15,
                            }
                        ),
                        call.GetTime(),
                    ],
                )
                self.assertIn("2026-09-07T14:30:15+00:00", output)

    def test_sunday_uses_pijuice_weekday_one(self):
        sunday = self.now - timedelta(days=1)
        self.clock.GetTime.side_effect = [
            self.reading(sunday.replace(year=2000)),
            self.reading(sunday),
        ]
        self.run_clock(now=sunday)
        self.assertEqual(self.clock.SetTime.call_args.args[0]["weekday"], 1)

    def test_healthy_clock_is_a_quiet_noop(self):
        self.clock.GetTime.return_value = self.reading(self.now - timedelta(seconds=2))
        self.assertEqual(self.run_clock(), "")
        self.clock.SetTime.assert_not_called()
        self.bind.assert_called_once_with()

    def test_failures_never_reach_driver_binding(self):
        reset = self.reading(self.now.replace(year=2000))
        cases = (
            (
                [{"error": "COMMUNICATION_ERROR"}],
                "NO_ERROR",
                self.now,
                "Read RTC failed",
            ),
            ([reset], "COMMUNICATION_ERROR", self.now, "Set RTC failed"),
            ([reset, reset], "NO_ERROR", self.now, "readback"),
            ([reset], "NO_ERROR", self.now.replace(year=2000), "system date"),
        )
        for readings, write_error, system_time, message in cases:
            with self.subTest(message=message):
                self.clock.reset_mock()
                self.clock.GetTime.side_effect = readings
                self.clock.SetTime.return_value = {"error": write_error}
                with self.assertRaisesRegex(pijuice_clock.ClockError, message):
                    self.run_clock(now=system_time)
                self.bind.assert_not_called()


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.device = root / "devices" / "1-0068"
        self.driver = root / "drivers" / "rtc-ds1307"

    def test_only_declared_ds1307_device_can_be_bound(self):
        with self.assertRaisesRegex(pijuice_clock.ClockError, "missing"):
            pijuice_clock.bind_rtc(self.device, self.driver)
        self.assertFalse(self.device.exists())
        self.device.mkdir(parents=True)
        (self.device / "name").write_text("ds1339")
        with self.assertRaisesRegex(pijuice_clock.ClockError, "configured as ds1307"):
            pijuice_clock.bind_rtc(self.device, self.driver)
        self.assertFalse(self.driver.exists())
        (self.device / "name").write_text("ds1307")
        self.driver.mkdir(parents=True)
        with self.assertRaisesRegex(pijuice_clock.ClockError, "did not complete"):
            pijuice_clock.bind_rtc(self.device, self.driver)
        self.assertEqual((self.driver / "bind").read_text(), "1-0068\n")

    def test_module_loading_binds_existing_device_and_next_call_is_quiet(self):
        self.device.mkdir(parents=True)
        (self.device / "name").write_text("ds1307")

        def load_module(*args, **kwargs):
            self.driver.mkdir(parents=True)
            (self.device / "driver").symlink_to(self.driver)

        with patch.object(
            pijuice_clock.subprocess, "run", side_effect=load_module
        ) as run:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                pijuice_clock.bind_rtc(self.device, self.driver)
            self.assertIn("Bound PiJuice RTC", output.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                pijuice_clock.bind_rtc(self.device, self.driver)
            self.assertEqual(output.getvalue(), "")
        run.assert_called_once_with(
            ["/sbin/modprobe", "rtc_ds1307"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )


if __name__ == "__main__":
    unittest.main()
