import json
import math
import os
import shutil
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

from .spool import FILENAME_PATTERN, HASH_PATTERN, MAX_IMAGE_BYTES, MAX_RECORD_BYTES


MAX_SPOOL_ENTRIES = 10000
MAX_ERROR_BYTES = 65536
NUMERIC_FIELDS = {
    "battery_percent_estimate": ("battery_charge_percent", 1, 0, 100, "GetChargeLevel"),
    "battery_voltage": ("battery_voltage_mv", 1000, 0, 10000, "GetBatteryVoltage"),
    "battery_current_estimate": (
        "battery_current_estimate_ma",
        1000,
        -10000,
        10000,
        "GetBatteryCurrent",
    ),
    "reported_battery_temperature": (
        "battery_temperature_c",
        1,
        -50,
        150,
        "GetBatteryTemperature",
    ),
    "io_voltage": ("io_voltage_mv", 1000, 0, 10000, "GetIoVoltage"),
    "io_current": ("io_current_ma", 1000, 0, 10000, "GetIoCurrent"),
    "cpu_temperature": ("cpu_temperature_c", 1, -50, 150, "cpu_temperature_c"),
    "memory_available": (
        "memory_available_bytes",
        1048576,
        0,
        2**50,
        "memory_available_bytes",
    ),
    "load_1m": ("load_1m", 1, 0, 10000, "load_1m"),
}
BATTERY_STATUSES = {"NORMAL", "CHARGING_FROM_IN", "CHARGING_FROM_5V_IO", "NOT_PRESENT"}
INPUT_STATUSES = {"NOT_PRESENT", "BAD", "WEAK", "PRESENT"}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def utc_timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Telemetry timestamp is missing or invalid")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Telemetry timestamp is invalid") from error
    if result.utcoffset() is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("Telemetry timestamp must use timezone-aware UTC")
    return result


def metrics_from_row(
    row,
    max_age_seconds=420,
    now=None,
    current_boot_id=None,
    current_uptime=None,
):
    row = dict(row)
    if not number(max_age_seconds) or not 0 < max_age_seconds <= 86400:
        raise ValueError("Telemetry maximum age must be between 0 and 86400 seconds")
    if (
        not isinstance(current_boot_id, str)
        or not current_boot_id.strip()
        or not number(current_uptime)
        or current_uptime < 0
    ):
        raise ValueError(
            "Current boot ID and uptime are required for telemetry freshness"
        )
    boot_id = row.get("boot_id")
    uptime = row.get("uptime_seconds")
    if (
        not isinstance(boot_id, str)
        or not boot_id.strip()
        or boot_id != current_boot_id
    ):
        raise ValueError("Latest telemetry is not from the current boot")
    if not number(uptime) or not 0 <= current_uptime - uptime <= max_age_seconds:
        raise ValueError("Latest telemetry is stale or has invalid uptime")
    session_id = row.get("session_id")
    sample_index = row.get("sample_index")
    if (
        not isinstance(session_id, str)
        or not session_id.strip()
        or len(session_id) > 100
        or type(sample_index) is not int
        or sample_index < 0
    ):
        raise ValueError("Latest telemetry has invalid sample identity")
    observed = utc_timestamp(row.get("timestamp_utc"))
    source = row.get("time_source")
    if source not in {"NTP", "RTC", "UNSYNC"}:
        raise ValueError("Latest telemetry has an invalid clock source")
    current_time = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current_time, datetime) or current_time.utcoffset() is None:
        raise ValueError("Current time must be timezone-aware")
    if (
        source != "UNSYNC"
        and not -5 <= (current_time - observed).total_seconds() <= max_age_seconds
    ):
        raise ValueError("Latest telemetry has a stale or future timestamp")
    encoded_errors = row.get("errors_json")
    if not isinstance(encoded_errors, str) or len(encoded_errors) > MAX_ERROR_BYTES:
        raise ValueError("Latest telemetry has invalid error information")
    try:
        errors = json.loads(encoded_errors)
    except ValueError as error:
        raise ValueError("Latest telemetry has malformed error information") from error
    if not isinstance(errors, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in errors.items()
    ):
        raise ValueError("Latest telemetry errors must be a mapping")
    if {"boot_id", "uptime_seconds"}.intersection(errors):
        raise ValueError("Latest telemetry has unverified freshness information")
    error_keys = set(errors)
    result = {
        "observed_at": row["timestamp_utc"] if source != "UNSYNC" else None,
        "sample_timestamp_utc": row["timestamp_utc"],
        "time_source": source,
        "boot_id": boot_id,
        "session_id": session_id,
        "sample_index": sample_index,
        "uptime": uptime,
    }
    for key, (field, divisor, minimum, maximum, error_key) in NUMERIC_FIELDS.items():
        value = row.get(field)
        valid = (
            number(value) and minimum <= value <= maximum and error_key not in errors
        )
        result[key] = value / divisor if valid else None
        absent_battery_metric = (
            row.get("battery_present") == 0
            and row.get("battery_status") == "NOT_PRESENT"
            and "GetStatus" not in errors
            and (key.startswith("battery_") or key == "reported_battery_temperature")
        )
        if not valid and error_key not in errors and not absent_battery_metric:
            error_keys.add(field)
    status_error = "GetStatus" in errors
    for key, field, allowed in (
        ("battery_status", "battery_status", BATTERY_STATUSES),
        ("usb_input_status", "power_input_status", INPUT_STATUSES),
        ("gpio_input_status", "power_5v_io_status", INPUT_STATUSES),
    ):
        value = row.get(field)
        valid = isinstance(value, str) and value in allowed and not status_error
        result[key] = value if valid else None
        if not valid and not status_error:
            error_keys.add(field)
    present = row.get("battery_present")
    battery_status = result["battery_status"]
    present_valid = (
        type(present) is int and present in (0, 1) and battery_status is not None
    )
    if present_valid and bool(present) != (battery_status != "NOT_PRESENT"):
        present_valid = False
    if not present_valid:
        error_keys.add("battery_presence")
        result["battery_status"] = None
    if not present_valid or not present:
        for key in NUMERIC_FIELDS:
            if key.startswith("battery_") or key == "reported_battery_temperature":
                result[key] = None
    result["battery_power_estimate"] = (
        round(result["battery_voltage"] * result["battery_current_estimate"], 6)
        if result["battery_voltage"] is not None
        and result["battery_current_estimate"] is not None
        else None
    )
    result["io_power"] = (
        round(result["io_voltage"] * result["io_current"], 6)
        if result["io_voltage"] is not None and result["io_current"] is not None
        else None
    )
    result["telemetry_errors"] = len(error_keys)
    result["telemetry_error_keys"] = sorted(error_keys)
    return result


def _directory_entries(directory, remaining):
    entries = {}
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if len(entries) >= remaining:
                raise ValueError("Spool exceeds the bounded inspection limit")
            details = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError("Spool contains a non-regular entry")
            entries[entry.name] = details.st_size
    return entries


def _read_record(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_RECORD_BYTES:
            raise ValueError("Spool record has an invalid type or size")
        payload = stream.read(MAX_RECORD_BYTES + 1)
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError("Spool record exceeds its size limit")
    try:
        record = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Spool record contains invalid JSON") from error
    if not isinstance(record, dict):
        raise ValueError("Spool record must contain an object")
    return record


def _spool_summary(spool_root):
    root = Path(spool_root)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("Spool must be a real directory")
    directories = [root / name for name in ("images", "metadata", "receipts")]
    present = [directory.exists() for directory in directories]
    for directory in directories:
        if directory.is_symlink():
            raise ValueError("Spool contains a symbolic-link directory")
    if any(present) and not all(present):
        raise ValueError("Spool directory layout is incomplete")
    parent = root
    while not parent.exists():
        parent = parent.parent
    free = shutil.disk_usage(parent).free
    if not any(present):
        return {
            "pending_photos": 0,
            "stored_photos": 0,
            "spool_bytes": 0,
            "free_bytes": free,
        }
    entries = []
    remaining = MAX_SPOOL_ENTRIES
    for directory in directories:
        records = _directory_entries(directory, remaining)
        remaining -= len(records)
        entries.append(records)
    images, metadata_files, receipt_files = entries
    if any(name.startswith(".") for name in metadata_files):
        raise ValueError("Spool publication is incomplete or in progress")
    committed = {}
    for name in metadata_files:
        if not name.endswith(".json"):
            raise ValueError("Spool contains an unexpected metadata entry")
        record = _read_record(root / "metadata" / name)
        filename = record.get("filename")
        size = record.get("size_bytes")
        digest = record.get("sha256")
        if (
            not isinstance(filename, str)
            or len(filename) > 200
            or not FILENAME_PATTERN.fullmatch(filename)
            or f"{Path(filename).stem}.json" != name
            or type(size) is not int
            or not 0 < size <= MAX_IMAGE_BYTES
            or images.get(filename) != size
            or not isinstance(digest, str)
            or not HASH_PATTERN.fullmatch(digest)
        ):
            raise ValueError("Spool image metadata does not match its file")
        utc_timestamp(record.get("captured_at_utc"))
        if record.get("time_source") not in {"NTP", "RTC", "UNSYNC"}:
            raise ValueError("Spool capture clock source is invalid")
        if (
            not isinstance(record.get("boot_id"), str)
            or not record["boot_id"].strip()
            or not number(record.get("capture_duration_seconds"))
            or record["capture_duration_seconds"] < 0
        ):
            raise ValueError("Spool capture identity or duration is invalid")
        committed[filename] = record
    if {name for name in images if not name.startswith(".")} != set(committed):
        raise ValueError("Spool contains an image without committed metadata")
    pending = len(committed)
    for name in receipt_files:
        if name.startswith("."):
            raise ValueError("Spool receipt publication is in progress")
        record = _read_record(root / "receipts" / name)
        filename = record.get("filename")
        if (
            not isinstance(filename, str)
            or filename not in committed
            or name != f"{Path(filename).stem}.json"
        ):
            raise ValueError("Spool receipt does not identify a committed image")
        if any(
            record.get(key) != committed[filename][key]
            for key in ("filename", "size_bytes", "sha256")
        ):
            raise ValueError("Spool receipt does not match its image")
        pending -= 1
    return {
        "pending_photos": pending,
        "stored_photos": len(committed),
        "spool_bytes": sum(sum(group.values()) for group in entries),
        "free_bytes": free,
    }


def read_metrics(
    database: Path,
    spool_root: Path,
    max_age_seconds=420,
    now=None,
    current_boot_id=None,
    current_uptime=None,
):
    columns = {
        "session_id",
        "sample_index",
        "timestamp_utc",
        "uptime_seconds",
        "boot_id",
        "time_source",
        "battery_present",
        "battery_status",
        "power_input_status",
        "power_5v_io_status",
        *(value[0] for value in NUMERIC_FIELDS.values()),
    }
    projection = (
        ",".join(sorted(columns)) + ",substr(errors_json,1,65537) AS errors_json"
    )
    try:
        connection = sqlite3.connect(
            Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=2
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                f"SELECT {projection} FROM samples ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as error:
        raise ValueError(f"Hardware telemetry cannot be read: {error}") from error
    if row is None:
        raise ValueError("Hardware telemetry is empty")
    result = metrics_from_row(
        row, max_age_seconds, now, current_boot_id, current_uptime
    )
    try:
        result.update(_spool_summary(spool_root))
    except (OSError, ValueError) as error:
        result.update(
            {
                key: None
                for key in (
                    "pending_photos",
                    "stored_photos",
                    "spool_bytes",
                    "free_bytes",
                )
            }
        )
        result["telemetry_error_keys"].append("spool")
        result["telemetry_errors"] += 1
        result["spool_error"] = str(error)[:200]
    return result
