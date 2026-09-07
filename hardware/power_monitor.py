#!/usr/bin/python3

import argparse
import csv
import json
import math
import os
import signal
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from datetime import timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

if __package__:
    from . import pijuice_clock
else:
    import pijuice_clock  # type: ignore[no-redef]


METRICS = {
    "battery_charge_percent": "GetChargeLevel",
    "battery_voltage_mv": "GetBatteryVoltage",
    "battery_current_estimate_ma": "GetBatteryCurrent",
    "battery_temperature_c": "GetBatteryTemperature",
    "io_voltage_mv": "GetIoVoltage",
    "io_current_ma": "GetIoCurrent",
}
FIELDS = {
    "session_id": "TEXT NOT NULL",
    "sample_index": "INTEGER NOT NULL",
    "timestamp_utc": "TEXT NOT NULL",
    "uptime_seconds": "REAL",
    "boot_id": "TEXT",
    "time_source": "TEXT NOT NULL",
    "sample_gap_seconds": "REAL",
    "dropped_samples": "INTEGER NOT NULL",
    "battery_present": "INTEGER",
    "battery_status": "TEXT",
    "power_input_status": "TEXT",
    "power_5v_io_status": "TEXT",
    **{field: "REAL" for field in METRICS},
    "battery_power_estimate_w": "REAL",
    "io_power_w": "REAL",
    "cpu_temperature_c": "REAL",
    "memory_available_bytes": "INTEGER",
    "load_1m": "REAL",
    "raw_pijuice_json": "TEXT NOT NULL",
    "errors_json": "TEXT NOT NULL",
}
DEFAULT_DATABASE = "/var/lib/pi-hardware/metrics.sqlite3"
MAX_BUFFER_ROWS = 720


def compact_json(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)


def error_text(error):
    return f"{type(error).__name__}: {error}"[:200]


def read_value(name, reader, errors):
    try:
        return reader()
    except Exception as error:
        errors[name] = error_text(error)
        return None


def read_pijuice(status, method, raw, errors, numeric=False):
    try:
        result = getattr(status, method)()
        compact_json(result)
        raw[method] = result
        if not isinstance(result, dict) or result.get("error") != "NO_ERROR":
            errors[method] = (
                str(result.get("error", "MISSING_STATUS"))[:200]
                if isinstance(result, dict)
                else "INVALID_RESPONSE"
            )
            return None
        value = result.get("data")
        if numeric:
            valid = type(value) in (int, float) and math.isfinite(value)
        else:
            valid = isinstance(value, dict)
        if not valid:
            errors[method] = "INVALID_DATA"
            return None
        return value
    except Exception as error:
        errors[method] = error_text(error)
        raw[method] = {"error": errors[method]}
        return None


def collect_sample(
    pijuice,
    proc=Path("/proc"),
    thermal=Path("/sys/class/thermal/thermal_zone0/temp"),
    now=pijuice_clock.utc_now,
):
    errors = {}
    raw = {}
    status = read_pijuice(pijuice.status, "GetStatus", raw, errors) or {}
    read_pijuice(pijuice.status, "GetFaultStatus", raw, errors)
    row = {
        field: read_pijuice(pijuice.status, method, raw, errors, numeric=True)
        for field, method in METRICS.items()
    }
    timestamp = now().astimezone(timezone.utc)
    rtc = read_value("rtc_time", lambda: pijuice_clock.read_rtc(pijuice), errors)
    synchronized = pijuice_clock.NTP_SYNCHRONIZED.exists()
    valid_system_time = pijuice_clock.MINIMUM_YEAR <= timestamp.year <= 2099
    source = (
        "NTP"
        if synchronized and valid_system_time
        else "RTC"
        if rtc is not None and abs((rtc - timestamp).total_seconds()) <= 2
        else "UNSYNC"
    )
    present = None
    if status.get("battery") in (
        "NORMAL",
        "CHARGING_FROM_IN",
        "CHARGING_FROM_5V_IO",
        "NOT_PRESENT",
    ):
        present = status["battery"] != "NOT_PRESENT"
    if present is False:
        for field in METRICS:
            if field.startswith("battery_"):
                row[field] = None
    row.update(
        {
            "timestamp_utc": timestamp.isoformat(timespec="milliseconds"),
            "uptime_seconds": read_value(
                "uptime_seconds",
                lambda: float((proc / "uptime").read_text().split()[0]),
                errors,
            ),
            "boot_id": read_value(
                "boot_id",
                lambda: (proc / "sys/kernel/random/boot_id").read_text().strip(),
                errors,
            ),
            "time_source": source,
            "battery_present": present,
            "battery_status": status.get("battery"),
            "power_input_status": status.get("powerInput"),
            "power_5v_io_status": status.get("powerInput5vIo"),
            "cpu_temperature_c": read_value(
                "cpu_temperature_c", lambda: float(thermal.read_text()) / 1000, errors
            ),
            "memory_available_bytes": read_value(
                "memory_available_bytes",
                lambda: next(
                    int(line.split()[1]) * 1024
                    for line in (proc / "meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:")
                ),
                errors,
            ),
            "load_1m": read_value(
                "load_1m",
                lambda: float((proc / "loadavg").read_text().split()[0]),
                errors,
            ),
            "raw_pijuice_json": compact_json(raw),
            "errors_json": compact_json(errors),
        }
    )
    battery_voltage, battery_current = (
        row["battery_voltage_mv"],
        row["battery_current_estimate_ma"],
    )
    io_voltage, io_current = row["io_voltage_mv"], row["io_current_ma"]
    row["battery_power_estimate_w"] = (
        battery_voltage * battery_current / 1_000_000
        if present is True
        and battery_voltage is not None
        and battery_current is not None
        else None
    )
    row["io_power_w"] = (
        io_voltage * io_current / 1_000_000
        if io_voltage is not None and io_current is not None
        else None
    )
    return row


class ErrorReporter:
    def __init__(self, interval=300, clock=time.monotonic, output=None):
        self.interval = interval
        self.clock = clock
        self.output = output if output is not None else sys.stderr
        self.next_report = {}

    def report(self, message, category="sampling"):
        current = self.clock()
        if current >= self.next_report.get(category, 0):
            print(message, file=self.output, flush=True)
            self.next_report[category] = current + self.interval


class SampleBuffer:
    def __init__(self, database, retention=43200, capacity=MAX_BUFFER_ROWS):
        self.database = Path(database)
        self.retention = retention
        self.capacity = capacity
        self.rows = deque()
        self.session_id = str(uuid.uuid4())
        self.sample_index = 0
        self.previous_uptime = None
        self.previous_boot = None

    def append(self, sample):
        row = dict(sample)
        row.update(
            session_id=self.session_id,
            sample_index=self.sample_index,
            dropped_samples=0,
        )
        self.sample_index += 1
        uptime = row["uptime_seconds"]
        same_boot = row["boot_id"] is not None and row["boot_id"] == self.previous_boot
        row["sample_gap_seconds"] = (
            uptime - self.previous_uptime
            if same_boot
            and uptime is not None
            and self.previous_uptime is not None
            and uptime >= self.previous_uptime
            else None
        )
        self.previous_uptime, self.previous_boot = uptime, row["boot_id"]
        self.rows.append(row)
        if len(self.rows) > self.capacity:
            discarded = self.rows.popleft()
            self.rows[0]["dropped_samples"] += discarded["dropped_samples"] + 1

    def flush(self):
        if not self.rows:
            return
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=2)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            columns = ",".join(f"{field} {kind}" for field, kind in FIELDS.items())
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY AUTOINCREMENT,{columns},UNIQUE(session_id,sample_index))"
            )
            with connection:
                names = ",".join(FIELDS)
                placeholders = ",".join("?" for _ in FIELDS)
                connection.executemany(
                    f"INSERT OR IGNORE INTO samples ({names}) VALUES ({placeholders})",
                    [tuple(row[field] for field in FIELDS) for row in self.rows],
                )
                connection.execute(
                    "DELETE FROM samples WHERE id <= (SELECT id FROM samples ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (self.retention,),
                )
            self.rows.clear()
        finally:
            connection.close()


def export_csv(database, output):
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        connection.execute("BEGIN")
        cursor = connection.execute("SELECT * FROM samples ORDER BY id")
        writer = csv.writer(output)
        writer.writerow(column[0] for column in cursor.description)
        writer.writerows(cursor)
    finally:
        connection.close()


def power_signature(row):
    raw = json.loads(row["raw_pijuice_json"])
    return compact_json(
        {
            "battery": row["battery_status"],
            "input": row["power_input_status"],
            "io": row["power_5v_io_status"],
            "fault": raw.get("GetFaultStatus"),
            "status_error": json.loads(row["errors_json"]).get("GetStatus"),
        }
    )


def advance_deadline(deadline, interval, current):
    return deadline + interval * max(1, math.floor((current - deadline) / interval) + 1)


def run_monitor(
    buffer,
    interval=60,
    flush_seconds=300,
    stop=None,
    factory=None,
    clock=time.monotonic,
    sample=collect_sample,
    recover=pijuice_clock.synchronize_clock,
):
    if factory is None:
        from pijuice import PiJuice

        factory = partial(PiJuice, 1, 0x14)
    stop = stop or threading.Event()
    reporter = ErrorReporter(clock=clock)
    next_sample = clock()
    next_recovery = next_sample
    last_flush = next_sample
    previous_signature = None
    first_sample = True
    try:
        while not stop.is_set():
            if stop.wait(max(0, next_sample - clock())):
                break
            extra_errors = {}
            if clock() >= next_recovery:
                try:
                    recover(factory(), pijuice_clock.NTP_SYNCHRONIZED.exists())
                except Exception as error:
                    extra_errors["clock_recovery"] = error_text(error)
                next_recovery = advance_deadline(next_recovery, 60, clock())
            try:
                pijuice = factory()
            except Exception as error:
                extra_errors["controller"] = error_text(error)
                pijuice = SimpleNamespace(status=None, rtcAlarm=None)
            row = sample(pijuice)
            if extra_errors:
                row["errors_json"] = compact_json(
                    {**json.loads(row["errors_json"]), **extra_errors}
                )
            buffer.append(row)
            errors = json.loads(row["errors_json"])
            if errors:
                reporter.report(f"Hardware sample has errors: {compact_json(errors)}")
            signature = power_signature(row)
            if (
                first_sample
                or signature != previous_signature
                or clock() - last_flush >= flush_seconds
            ):
                try:
                    buffer.flush()
                except (sqlite3.Error, OSError) as error:
                    reporter.report(
                        f"Metrics write failed; {len(buffer.rows)} samples buffered: {error_text(error)}",
                        "storage",
                    )
                else:
                    last_flush = next_sample
                    first_sample = False
                    previous_signature = signature
            next_sample = advance_deadline(next_sample, interval, clock())
    finally:
        buffer.flush()


def arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Sample PiJuice and system health, or export committed samples as CSV"
    )
    parser.add_argument(
        "--database", default=os.environ.get("PI_HARDWARE_DATABASE", DEFAULT_DATABASE)
    )
    parser.add_argument(
        "--interval", type=float, default=os.environ.get("PI_HARDWARE_INTERVAL", "60")
    )
    parser.add_argument(
        "--flush-seconds",
        type=float,
        default=os.environ.get("PI_HARDWARE_FLUSH_SECONDS", "300"),
    )
    parser.add_argument(
        "--retention",
        type=int,
        default=os.environ.get("PI_HARDWARE_RETENTION", "43200"),
    )
    parser.add_argument("--export-csv", action="store_true")
    result = parser.parse_args(argv)
    if not math.isfinite(result.interval) or result.interval < 5:
        parser.error("--interval must be at least 5 seconds")
    if not math.isfinite(result.flush_seconds) or result.flush_seconds < 5:
        parser.error("--flush-seconds must be at least 5 seconds")
    if result.retention < 1:
        parser.error("--retention must be positive")
    return result


def main(argv=None):
    args = arguments(argv)
    try:
        if args.export_csv:
            export_csv(args.database, sys.stdout)
        else:
            stop = threading.Event()
            for event in (signal.SIGTERM, signal.SIGINT):
                signal.signal(event, lambda signum, frame: stop.set())
            run_monitor(
                SampleBuffer(args.database, args.retention),
                args.interval,
                args.flush_seconds,
                stop,
            )
    except (OSError, sqlite3.Error, ImportError) as error:
        print(f"Hardware monitor failed: {error_text(error)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
