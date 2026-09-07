import csv
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import analyze_metrics as analyzer


class AnalysisTests(unittest.TestCase):
    def row(self, index=0, uptime=100, timestamp=None, **changes):
        row = dict.fromkeys(analyzer.REQUIRED_FIELDS, "")
        row.update(
            session_id="session-a",
            sample_index=index,
            timestamp_utc=(
                timestamp or datetime(2026, 9, 7, tzinfo=timezone.utc)
            ).isoformat(),
            uptime_seconds=uptime,
            boot_id="boot-a",
            time_source="RTC",
            dropped_samples=0,
            battery_present=0,
            errors_json="{}",
            io_current_ma=120,
        )
        row.update(changes)
        return row

    def csv(self, rows, fields=None):
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields or analyzer.REQUIRED_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        output.seek(0)
        return output

    def test_reboots_and_sessions_never_bridge_intervals(self):
        rows = [
            self.row(),
            self.row(1, 160),
            self.row(0, 400, session_id="session-b"),
            self.row(0, 10, session_id="session-c", boot_id="boot-b"),
        ]
        result = analyzer.analyze(self.csv(rows))
        self.assertEqual(
            (result["rows"], result["sessions"], result["boots"]), (4, 3, 2)
        )
        self.assertEqual(
            [
                group["observed_intervals_seconds"]["count"]
                for group in result["boot_sessions"]
            ],
            [1, 0, 0],
        )
        self.assertEqual(
            result["boot_sessions"][0]["observed_intervals_seconds"]["max"], 60
        )

    def test_clock_changes_do_not_become_sample_gaps(self):
        start = datetime(2000, 1, 1, tzinfo=timezone.utc)
        corrected = datetime(2026, 9, 7, tzinfo=timezone.utc)
        rows = [
            self.row(timestamp=start, time_source="UNSYNC"),
            self.row(1, 160, timestamp=corrected, time_source="NTP"),
            self.row(
                4, 400, timestamp=corrected - timedelta(hours=1), time_source="NTP"
            ),
        ]
        result = analyzer.analyze(self.csv(rows))
        group = result["boot_sessions"][0]
        self.assertEqual(group["observed_intervals_seconds"]["min"], 60)
        self.assertEqual(
            group["long_gaps_seconds"],
            {"count": 1, "missing": 0, "min": 240, "max": 240},
        )
        self.assertEqual(group["missing_sample_indexes_between_rows"], 2)
        self.assertEqual(group["clock_jumps"]["count"], 2)
        self.assertEqual(
            (group["clock_jumps"]["forward"], group["clock_jumps"]["backward"]), (1, 1)
        )
        self.assertEqual(result["time_sources"], {"UNSYNC": 1, "NTP": 2})

    def test_missing_uptime_or_boot_prevents_interpolation(self):
        rows = [
            self.row(),
            self.row(1, ""),
            self.row(2, 220),
            self.row(0, 300, session_id="unknown", boot_id=""),
            self.row(1, 360, session_id="unknown", boot_id=""),
        ]
        result = analyzer.analyze(self.csv(rows))
        self.assertEqual(result["rows_without_boot_id"], 2)
        self.assertEqual(result["boots"], 1)
        self.assertEqual(
            [
                group["observed_intervals_seconds"]["count"]
                for group in result["boot_sessions"]
            ],
            [0, 0],
        )

    def test_absent_battery_nulls_errors_drops_and_signed_ranges(self):
        failure = json.dumps({"GetBatteryCurrent": "COMMUNICATION_ERROR"})
        rows = [
            self.row(errors_json=failure, dropped_samples=7),
            self.row(
                1,
                160,
                battery_present=1,
                battery_current_estimate_ma=-80,
                battery_power_estimate_w=-0.32,
                io_current_ma=-5,
                errors_json=failure,
            ),
            self.row(2, 220, battery_present="", io_current_ma=""),
        ]
        result = analyzer.analyze(self.csv(rows))
        self.assertEqual(
            result["reported_battery_presence"],
            {"absent": 1, "present": 1, "unknown": 1},
        )
        self.assertEqual(
            result["numeric_ranges"]["battery_current_estimate_ma"],
            {"count": 1, "missing": 2, "min": -80, "max": -80},
        )
        self.assertEqual(result["numeric_ranges"]["io_current_ma"]["min"], -5)
        self.assertEqual(result["recorded_dropped_samples"], 7)
        self.assertEqual(
            result["reading_errors"]["GetBatteryCurrent"],
            {"count": 2, "messages": {"COMMUNICATION_ERROR": 2}},
        )
        self.assertEqual(result["rows_with_errors"], 2)
        self.assertNotIn("energy_wh", result)
        self.assertIsNone(result["numeric_ranges"]["battery_voltage_mv"]["min"])

    def test_expected_interval_is_explicit_for_fast_bench_sampling(self):
        rows = [self.row(), self.row(1, 110)]
        self.assertEqual(
            analyzer.analyze(self.csv(rows), expected_interval=5)["boot_sessions"][0][
                "long_gaps_seconds"
            ]["count"],
            1,
        )
        self.assertEqual(
            analyzer.analyze(self.csv(rows))["boot_sessions"][0]["long_gaps_seconds"][
                "count"
            ],
            0,
        )

    def test_malformed_headers_and_rows(self):
        malformed = [
            "",
            "session_id\na\n",
            "session_id,session_id\na,a\n",
            self.csv([self.row()]).getvalue().rstrip("\r\n") + ",extra\n",
            self.csv([]).getvalue() + '"unterminated\n',
            self.csv([]).getvalue() + "too,few,columns\n",
        ]
        for text in malformed:
            with (
                self.subTest(text=text[:40]),
                self.assertRaises((ValueError, csv.Error)),
            ):
                analyzer.analyze(io.StringIO(text))

    def test_malformed_values_are_rejected(self):
        invalid = [
            dict(io_current_ma="nan"),
            dict(battery_power_estimate_w="inf"),
            dict(uptime_seconds="1e999"),
            dict(memory_available_bytes="2.5"),
            dict(sample_index=-1),
            dict(dropped_samples=""),
            dict(battery_present=2),
            dict(timestamp_utc="2026-09-07T00:00:00"),
            dict(timestamp_utc="yesterday"),
            dict(errors_json="[]"),
            dict(errors_json='{"reading":NaN}'),
            dict(errors_json="{"),
            dict(session_id=" "),
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                analyzer.analyze(self.csv([self.row(**changes)]))

    def test_reordered_or_duplicate_samples_are_rejected(self):
        for row in (self.row(0, 160), self.row(1, 90)):
            with self.subTest(row=row), self.assertRaises(ValueError):
                analyzer.analyze(self.csv([self.row(), row]))

    def test_header_only_export_has_no_fabricated_ranges(self):
        result = analyzer.analyze(self.csv([]))
        self.assertEqual(result["rows"], 0)
        self.assertEqual(result["boot_sessions"], [])
        self.assertEqual(
            result["numeric_ranges"]["io_power_w"],
            {"count": 0, "missing": 0, "min": None, "max": None},
        )

    def test_cli_outputs_json_and_concise_errors(self):
        output = io.StringIO()
        with patch("sys.stdin", self.csv([self.row()])), redirect_stdout(output):
            analyzer.main(["-"])
        self.assertEqual(json.loads(output.getvalue())["rows"], 1)
        errors = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO("bad\n")),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as exit_status,
        ):
            analyzer.main(["-"])
        self.assertEqual(exit_status.exception.code, 2)
        self.assertIn("missing required columns", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
