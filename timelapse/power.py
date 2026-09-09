import json
import math
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path


class PowerError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def parse_time(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or not 2024 <= timestamp.year <= 2099:
        raise ValueError("A valid timezone-aware timestamp is required")
    return timestamp.astimezone(timezone.utc)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


SENSOR_CONTEXT = ("pi_boot_id", "sensor_session_id", "calibration_id")
SENSOR_FIELDS = (
    "sample_id",
    "timestamp_utc",
    "solar_input_w",
    "battery_power_w",
    "battery_charge_percent",
    *SENSOR_CONTEXT,
    "observed_uptime_seconds",
    "sequence",
)


def validate_sample(sample):
    if not isinstance(sample, dict):
        raise ValueError("Sensor record must be an object")
    parse_time(sample["timestamp_utc"])
    if sample.get("quality") != "calibrated" or sample.get("source") != "solar":
        raise ValueError("Calibrated solar measurements are required")
    if sample.get("time_source") not in ("NTP", "RTC"):
        raise ValueError("Sensor clock is not synchronized")
    if (
        sample.get("input_healthy") is not True
        or sample.get("battery_healthy") is not True
    ):
        raise ValueError("Healthy input and battery measurements are required")
    if not isinstance(sample.get("errors"), list) or sample["errors"]:
        raise ValueError("Sensor measurements contain errors or lack health evidence")
    for field in ("sample_id", *SENSOR_CONTEXT):
        if (
            not isinstance(sample.get(field), str)
            or not 1 <= len(sample[field]) <= 128
            or not sample[field].strip()
        ):
            raise ValueError(f"Sensor {field} is required")
    if (
        type(sample.get("sequence")) is not int
        or not 0 <= sample["sequence"] <= 9223372036854775807
    ):
        raise ValueError("Sensor sequence must be a nonnegative 64-bit integer")
    for field in (
        "solar_input_w",
        "battery_power_w",
        "battery_charge_percent",
        "observed_uptime_seconds",
    ):
        if not number(sample.get(field)):
            raise ValueError(f"Missing finite measurement: {field}")
    if (
        sample["solar_input_w"] < 0
        or sample["observed_uptime_seconds"] < 0
        or not 0 <= sample["battery_charge_percent"] <= 100
    ):
        raise ValueError("Sensor measurement is out of range")


def read_sensor(path, max_age_seconds=90, now=None, now_uptime=None, boot_id=None):
    now = now or utc_now()
    try:
        if not number(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("Maximum sensor age must be positive and finite")
        if now_uptime is None:
            now_uptime = float(Path("/proc/uptime").read_text().split()[0])
        if boot_id is None:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        with Path(path).open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("Sensor record exceeds 16 KiB")
        sample = json.loads(raw)
        validate_sample(sample)
        if sample["pi_boot_id"] != boot_id:
            raise ValueError("Sensor record belongs to another Pi boot")
        if (
            not number(now_uptime)
            or not 0
            <= now_uptime - sample["observed_uptime_seconds"]
            <= max_age_seconds
        ):
            raise ValueError("Sensor monotonic age is stale or from the future")
        age = (now - parse_time(sample["timestamp_utc"])).total_seconds()
        if not 0 <= age <= max_age_seconds:
            raise ValueError("Sensor record is stale or from the future")
        return sample
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PowerError(str(error)) from error


class PowerHistory:
    def __init__(self, path, retention_days=30):
        self.path = Path(path)
        self.retention_days = retention_days

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "observations" not in tables:
            connection.execute(
                "CREATE TABLE observations (sample_id TEXT PRIMARY KEY, timestamp_utc TEXT NOT NULL, solar_input_w REAL NOT NULL, battery_power_w REAL NOT NULL, battery_charge_percent REAL NOT NULL)"
            )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(observations)")
        }
        for name, definition in (
            ("pi_boot_id", "TEXT"),
            ("sensor_session_id", "TEXT"),
            ("calibration_id", "TEXT"),
            ("observed_uptime_seconds", "REAL"),
            ("sequence", "INTEGER"),
            ("admission_generation", "INTEGER NOT NULL DEFAULT 0"),
            ("activity", "TEXT NOT NULL DEFAULT 'idle'"),
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE observations ADD COLUMN {name} {definition}"
                )
        if "admission_state" not in tables:
            connection.execute(
                "CREATE TABLE admission_state (id INTEGER PRIMARY KEY CHECK (id=1), generation INTEGER NOT NULL, invalidated INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO admission_state VALUES (1, 0, 0)")
        if connection.in_transaction:
            connection.commit()
        return connection

    def invalidate(self):
        connection = self.connect()
        try:
            if connection.execute(
                "SELECT invalidated FROM admission_state WHERE id=1"
            ).fetchone()[0]:
                return
            with connection:
                connection.execute(
                    "UPDATE admission_state SET generation=generation+1, invalidated=1 WHERE id=1 AND invalidated=0"
                )
        finally:
            connection.close()

    def record(self, sample, now=None, activity="idle"):
        now = now or utc_now()
        try:
            if activity not in ("idle", "upload", "probe"):
                raise ValueError("Unknown observation activity")
            validate_sample(sample)
        except (ValueError, KeyError, TypeError) as error:
            self.invalidate()
            raise PowerError(str(error)) from error
        connection = self.connect()
        try:
            previous = connection.execute(
                "SELECT * FROM observations WHERE sample_id=?", (sample["sample_id"],)
            ).fetchone()
            values = dict(
                sample, timestamp_utc=parse_time(sample["timestamp_utc"]).isoformat()
            )
            if previous is not None:
                latest = connection.execute(
                    "SELECT sample_id FROM observations WHERE pi_boot_id IS NOT NULL ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                if (
                    latest is None
                    or latest["sample_id"] != sample["sample_id"]
                    or any(previous[field] != values[field] for field in SENSOR_FIELDS)
                ):
                    raise PowerError(
                        "Sensor record replays or changes an existing sample"
                    )
                return False
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                state = connection.execute(
                    "SELECT * FROM admission_state WHERE id=1"
                ).fetchone()
                latest = connection.execute(
                    "SELECT * FROM observations WHERE pi_boot_id IS NOT NULL ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                previous = connection.execute(
                    "SELECT * FROM observations WHERE sample_id=?",
                    (sample["sample_id"],),
                ).fetchone()
                if previous is not None:
                    if (
                        latest is None
                        or latest["sample_id"] != sample["sample_id"]
                        or any(
                            previous[field] != values[field] for field in SENSOR_FIELDS
                        )
                    ):
                        raise PowerError(
                            "Sensor record replays or changes an existing sample"
                        )
                    return False
                if latest is not None and latest["pi_boot_id"] == sample["pi_boot_id"]:
                    if (
                        sample["observed_uptime_seconds"]
                        <= latest["observed_uptime_seconds"]
                    ):
                        raise PowerError(
                            "Sensor monotonic observation time did not advance"
                        )
                    if (
                        all(latest[field] == sample[field] for field in SENSOR_CONTEXT)
                        and sample["sequence"] <= latest["sequence"]
                    ):
                        raise PowerError("Sensor sequence did not advance")
                generation = state["generation"]
                if latest is not None and any(
                    latest[field] != sample[field] for field in SENSOR_CONTEXT
                ):
                    generation += 1
                connection.execute(
                    "UPDATE admission_state SET generation=?, invalidated=0 WHERE id=1",
                    (generation,),
                )
                fields = ", ".join((*SENSOR_FIELDS, "admission_generation", "activity"))
                placeholders = ", ".join("?" for _ in range(len(SENSOR_FIELDS) + 2))
                connection.execute(
                    f"INSERT INTO observations ({fields}) VALUES ({placeholders})",
                    (
                        *(values[field] for field in SENSOR_FIELDS),
                        generation,
                        activity,
                    ),
                )
                connection.execute(
                    "DELETE FROM observations WHERE timestamp_utc < ?",
                    ((now - timedelta(days=self.retention_days)).isoformat(),),
                )
                connection.execute(
                    "DELETE FROM observations WHERE rowid NOT IN (SELECT rowid FROM observations ORDER BY rowid DESC LIMIT 10000)"
                )
            return True
        except PowerError:
            connection.close()
            self.invalidate()
            raise
        finally:
            connection.close()

    def recent(self, now=None):
        now = now or utc_now()
        if not self.path.exists():
            return []
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM observations WHERE admission_generation=(SELECT generation FROM admission_state WHERE id=1) AND pi_boot_id IS NOT NULL AND timestamp_utc >= ? AND timestamp_utc <= ? ORDER BY rowid",
                (
                    now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
                    now.isoformat(),
                ),
            )
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def profile(self):
        if not self.path.exists():
            return {
                "observations": 0,
                "days": 0,
                "activity_counts": {},
                "strongest_observed_utc_half_hour": None,
            }
        connection = self.connect()
        try:
            rows = list(
                connection.execute(
                    "SELECT timestamp_utc, solar_input_w, activity FROM observations"
                )
            )
        finally:
            connection.close()
        buckets = {}
        days = set()
        activity_counts = {}
        for row in rows:
            activity_counts[row["activity"]] = (
                activity_counts.get(row["activity"], 0) + 1
            )
            moment = parse_time(row["timestamp_utc"])
            days.add(moment.date())
            bucket = moment.hour * 2 + moment.minute // 30
            buckets.setdefault(bucket, {}).setdefault(moment.date(), []).append(
                row["solar_input_w"]
            )
        medians = {
            bucket: statistics.median(
                statistics.median(values) for values in daily.values()
            )
            for bucket, daily in buckets.items()
            if len(daily) >= 3
        }
        peak = max(medians, key=medians.get) if medians else None
        return {
            "observations": len(rows),
            "days": len(days),
            "activity_counts": activity_counts,
            "strongest_observed_utc_half_hour": None
            if peak is None
            else f"{peak // 2:02d}:{(peak % 2) * 30:02d}",
            "median_delivered_w": None if peak is None else medians[peak],
            "scope": "Sampled delivered input while awake; demand and missing periods bias this profile",
        }


def decide(sample, settings, observations, active=False):
    def result(action, reason):
        return {
            "action": action,
            "reason": reason,
            "sample_id": sample.get("sample_id"),
        }

    if not settings.get("battery_profile_verified", False):
        return result("wait", "Battery profile has not been verified")
    minimum = settings.get("minimum_input_w")
    stop_minimum = settings.get("stop_input_w")
    discharge_limit = settings.get("maximum_battery_discharge_w")
    if (
        not number(minimum)
        or minimum <= 0
        or not number(stop_minimum)
        or not 0 <= stop_minimum <= minimum
        or not number(discharge_limit)
        or discharge_limit < 0
    ):
        return result(
            "wait",
            "Measured upload input threshold and battery discharge tolerance are not configured",
        )
    reserve = (
        settings["stop_charge_percent"] if active else settings["start_charge_percent"]
    )
    if sample["battery_charge_percent"] < reserve:
        return result("wait", "Battery reserve is below the transfer threshold")
    if sample["battery_power_w"] > discharge_limit:
        return result("wait", "Measured battery discharge exceeds the transfer limit")
    observations = [
        row
        for row in observations
        if all(row.get(field) == sample.get(field) for field in SENSOR_CONTEXT)
    ]
    peak = max(
        [sample["solar_input_w"]] + [row["solar_input_w"] for row in observations]
    )
    relative = (
        settings["continue_peak_fraction"]
        if active
        else settings["start_peak_fraction"]
    )
    threshold = max(stop_minimum if active else minimum, peak * relative)
    if sample["solar_input_w"] >= threshold:
        if active:
            return result("upload", "Fresh input and battery reserve remain sufficient")
        count = settings["sustained_samples"]
        recent = observations[-count:]
        if len(recent) == count and recent[-1]["sample_id"] == sample["sample_id"]:
            stamps = [row["observed_uptime_seconds"] for row in recent]
            gaps_valid = all(
                0 < right - left <= settings["maximum_observation_gap_seconds"]
                for left, right in zip(stamps, stamps[1:])
            )
            identities_valid = len(
                {row["sample_id"] for row in recent}
            ) == count and all(
                left["sequence"] < right["sequence"]
                for left, right in zip(recent, recent[1:])
            )
            readings_valid = all(
                row["solar_input_w"] >= threshold
                and row["battery_charge_percent"] >= reserve
                and row["battery_power_w"] <= discharge_limit
                for row in recent
            )
            if gaps_valid and identities_valid and readings_valid:
                return result(
                    "upload", "Sustained high observed input with battery reserve"
                )
        return result("wait", "Waiting for sustained high input observations")
    probe_floor = settings.get("probe_minimum_input_w")
    if (
        not active
        and settings.get("allow_load_probe", False)
        and number(probe_floor)
        and probe_floor > 0
        and sample["solar_input_w"] >= probe_floor
    ):
        return result(
            "probe",
            "Short real upload can test additional demand under low observed input",
        )
    return result("wait", "Delivered solar input is below the transfer threshold")


def capture_guard(
    database,
    minimum_charge_percent=20,
    battery_profile_verified=False,
    now_uptime=None,
    boot_id=None,
):
    if now_uptime is None:
        now_uptime = float(Path("/proc/uptime").read_text().split()[0])
    if boot_id is None:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    try:
        connection = sqlite3.connect(
            Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=2
        )
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM samples ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        if (
            row is None
            or row["boot_id"] != boot_id
            or row["uptime_seconds"] is None
            or not 0 <= now_uptime - row["uptime_seconds"] <= 420
        ):
            raise PowerError("Current-boot hardware telemetry is unavailable or stale")
        if json.loads(row["errors_json"]):
            raise PowerError("Hardware telemetry contains reading errors")
        if row["battery_present"] == 0:
            if (
                row["power_input_status"] != "PRESENT"
                and row["power_5v_io_status"] != "PRESENT"
            ):
                raise PowerError("No battery and no confirmed external power")
        elif row["battery_present"] == 1:
            if not battery_profile_verified:
                raise PowerError("Battery profile has not been verified")
            if (
                not number(row["battery_charge_percent"])
                or row["battery_charge_percent"] < minimum_charge_percent
            ):
                raise PowerError("Battery reserve is below the capture threshold")
        else:
            raise PowerError("Battery presence is unknown")
        return row["time_source"]
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        raise PowerError(str(error)) from error
