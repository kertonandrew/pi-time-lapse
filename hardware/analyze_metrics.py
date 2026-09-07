#!/usr/bin/env python3

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


NUMERIC_FIELDS = (
    "battery_charge_percent",
    "battery_voltage_mv",
    "battery_current_estimate_ma",
    "battery_temperature_c",
    "battery_power_estimate_w",
    "io_voltage_mv",
    "io_current_ma",
    "io_power_w",
    "cpu_temperature_c",
    "memory_available_bytes",
    "load_1m",
    "sample_gap_seconds",
)
REQUIRED_FIELDS = (
    "session_id",
    "sample_index",
    "timestamp_utc",
    "uptime_seconds",
    "boot_id",
    "time_source",
    "dropped_samples",
    "battery_present",
    "errors_json",
    *NUMERIC_FIELDS,
)
LIMITS = [
    "Battery current and power are firmware estimates: positive discharge, negative charge.",
    "GPIO current and power describe the HAT-to-Pi boundary, not total Pi or solar input power.",
    "Missing readings remain missing; reported battery presence does not prove a pack is connected.",
    "Intervals use adjacent recorded uptimes within a known boot and session; gaps are not filled.",
    "Recorded drops and missing sample indexes are separate evidence and must not be added together.",
    "No Wh integration is performed from sparse samples or across unobserved periods.",
]


class Range:
    def __init__(self):
        self.count = 0
        self.missing = 0
        self.minimum = None
        self.maximum = None

    def add(self, value):
        if value is None:
            self.missing += 1
            return
        self.count += 1
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def result(self):
        return {
            "count": self.count,
            "missing": self.missing,
            "min": self.minimum,
            "max": self.maximum,
        }


def number(value, field, line, integer=False, required=False):
    if not value and not required:
        return None
    try:
        result = int(value) if integer else float(value)
        if not math.isfinite(result):
            raise ValueError
    except (ValueError, OverflowError):
        raise ValueError(
            f"line {line}: {field} must be a finite {'integer' if integer else 'number'}"
        ) from None
    return result


def parse_row(row, line):
    if None in row or any(value is None for value in row.values()):
        raise ValueError(f"line {line}: row width does not match the header")
    for field in ("session_id", "time_source"):
        if not row[field].strip():
            raise ValueError(f"line {line}: {field} is empty")
    for field in (
        *NUMERIC_FIELDS,
        "uptime_seconds",
        "sample_index",
        "dropped_samples",
        "battery_present",
    ):
        row[field] = number(
            row[field],
            field,
            line,
            integer=field
            in (
                "memory_available_bytes",
                "sample_index",
                "dropped_samples",
                "battery_present",
            ),
            required=field in ("sample_index", "dropped_samples"),
        )
    for field in (
        "uptime_seconds",
        "sample_gap_seconds",
        "sample_index",
        "dropped_samples",
        "memory_available_bytes",
    ):
        if row[field] is not None and row[field] < 0:
            raise ValueError(f"line {line}: {field} must not be negative")
    if row["battery_present"] not in (None, 0, 1):
        raise ValueError(f"line {line}: battery_present must be empty, 0 or 1")
    try:
        row["timestamp"] = datetime.fromisoformat(row["timestamp_utc"])
        if row["timestamp"].utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ValueError(
            f"line {line}: timestamp_utc must be an ISO timestamp with a timezone"
        ) from None
    try:
        row["errors"] = json.loads(row["errors_json"])
    except json.JSONDecodeError:
        raise ValueError(f"line {line}: errors_json is not valid JSON") from None
    if not isinstance(row["errors"], dict) or any(
        not isinstance(value, str) for value in row["errors"].values()
    ):
        raise ValueError(
            f"line {line}: errors_json must contain an object of error strings"
        )
    row["boot_id"] = row["boot_id"].strip() or None
    return row


class Session:
    def __init__(self, boot_id, session_id):
        self.boot_id, self.session_id = boot_id, session_id
        self.rows = 0
        self.uptime = Range()
        self.intervals = Range()
        self.gaps = Range()
        self.missing_indexes = 0
        self.jumps = {"count": 0, "forward": 0, "backward": 0, "largest": None}
        self.previous = None

    def add(self, row, line, gap_threshold, clock_threshold):
        previous = self.previous
        self.rows += 1
        self.uptime.add(row["uptime_seconds"])
        if previous is not None:
            index_delta = row["sample_index"] - previous["sample_index"]
            if index_delta <= 0:
                raise ValueError(
                    f"line {line}: sample_index must increase within a boot/session"
                )
            self.missing_indexes += index_delta - 1
            if (
                self.boot_id
                and row["uptime_seconds"] is not None
                and previous["uptime_seconds"] is not None
            ):
                interval = row["uptime_seconds"] - previous["uptime_seconds"]
                if interval < 0:
                    raise ValueError(
                        f"line {line}: uptime decreased within a boot/session"
                    )
                self.intervals.add(interval)
                if interval > gap_threshold:
                    self.gaps.add(interval)
                offset_change = (
                    row["timestamp"] - previous["timestamp"]
                ).total_seconds() - interval
                if abs(offset_change) > clock_threshold:
                    self.jumps["count"] += 1
                    self.jumps["forward" if offset_change > 0 else "backward"] += 1
                    largest = self.jumps["largest"]
                    if largest is None or abs(offset_change) > abs(
                        largest["offset_change_seconds"]
                    ):
                        self.jumps["largest"] = {
                            "from_sample_index": previous["sample_index"],
                            "to_sample_index": row["sample_index"],
                            "offset_change_seconds": offset_change,
                        }
        self.previous = row

    def result(self):
        return {
            "boot_id": self.boot_id,
            "session_id": self.session_id,
            "rows": self.rows,
            "uptime_seconds": self.uptime.result(),
            "observed_intervals_seconds": self.intervals.result(),
            "long_gaps_seconds": self.gaps.result(),
            "missing_sample_indexes_between_rows": self.missing_indexes,
            "clock_jumps": self.jumps,
        }


def analyze(source, expected_interval=60, clock_jump_threshold=2):
    if not math.isfinite(expected_interval * 1.5) or expected_interval <= 0:
        raise ValueError("expected interval must be a positive finite number")
    if not math.isfinite(clock_jump_threshold) or clock_jump_threshold < 0:
        raise ValueError("clock jump threshold must be a nonnegative finite number")
    reader = csv.DictReader(source, strict=True)
    if reader.fieldnames is None:
        raise ValueError("CSV header is missing")
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError("CSV header has duplicate columns")
    missing = sorted(set(REQUIRED_FIELDS) - set(reader.fieldnames))
    if missing:
        raise ValueError(f"missing required columns: {', '.join(missing)}")
    ranges = {field: Range() for field in NUMERIC_FIELDS}
    sessions, errors = {}, {}
    time_sources, batteries = Counter(), Counter()
    rows = drops = error_rows = unknown_boots = 0
    for raw in reader:
        row = parse_row(raw, reader.line_num)
        rows += 1
        drops += row["dropped_samples"]
        error_rows += bool(row["errors"])
        unknown_boots += row["boot_id"] is None
        time_sources[row["time_source"]] += 1
        batteries[
            {None: "unknown", 0: "absent", 1: "present"}[row["battery_present"]]
        ] += 1
        for field, values in ranges.items():
            values.add(row[field])
        for reading, error in row["errors"].items():
            errors.setdefault(reading, Counter())[error] += 1
        key = row["boot_id"], row["session_id"]
        if key not in sessions:
            sessions[key] = Session(*key)
        sessions[key].add(
            row, reader.line_num, expected_interval * 1.5, clock_jump_threshold
        )
    return {
        "rows": rows,
        "sessions": len({session_id for _, session_id in sessions}),
        "boots": len({boot_id for boot_id, _ in sessions if boot_id}),
        "rows_without_boot_id": unknown_boots,
        "time_sources": dict(time_sources),
        "reported_battery_presence": dict(batteries),
        "rows_with_errors": error_rows,
        "reading_errors": {
            reading: {"count": sum(counts.values()), "messages": dict(counts)}
            for reading, counts in errors.items()
        },
        "recorded_dropped_samples": drops,
        "numeric_ranges": {field: values.result() for field, values in ranges.items()},
        "expected_interval_seconds": expected_interval,
        "long_gap_threshold_seconds": expected_interval * 1.5,
        "clock_jump_threshold_seconds": clock_jump_threshold,
        "boot_sessions": [session.result() for session in sessions.values()],
        "limits": LIMITS,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Summarize Pi hardware CSV exports offline without integrating sparse samples"
    )
    parser.add_argument("csv_path", help="CSV export path, or - for stdin")
    parser.add_argument(
        "--expected-interval",
        type=float,
        default=60,
        help="Configured sampling interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--clock-jump-threshold",
        type=float,
        default=2,
        help="Minimum absolute clock offset change in seconds (default: 2)",
    )
    args = parser.parse_args(argv)
    try:
        if args.csv_path == "-":
            result = analyze(
                sys.stdin, args.expected_interval, args.clock_jump_threshold
            )
        else:
            with Path(args.csv_path).open(encoding="utf-8-sig", newline="") as source:
                result = analyze(
                    source, args.expected_interval, args.clock_jump_threshold
                )
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    except (OSError, UnicodeError, ValueError, csv.Error) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    main()
