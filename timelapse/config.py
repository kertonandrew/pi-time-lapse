import copy
import json
import math
from pathlib import Path


DEFAULTS = {
    "spool": "/var/lib/pi-timelapse",
    "max_spool_bytes": 2147483648,
    "min_free_bytes": 536870912,
    "hardware_database": "/var/lib/pi-hardware/metrics.sqlite3",
    "minimum_capture_charge_percent": 20,
    "camera": {
        "camera_command": "/usr/bin/rpicam-still",
        "timeout_seconds": 45,
        "settle_ms": 1000,
        "width": 4608,
        "height": 2592,
        "rotation": 180,
    },
    "power": {
        "sensor_path": "/run/pi-power/latest.json",
        "max_age_seconds": 90,
        "battery_profile_verified": False,
        "minimum_input_w": None,
        "stop_input_w": None,
        "maximum_battery_discharge_w": None,
        "start_charge_percent": 85,
        "stop_charge_percent": 75,
        "start_peak_fraction": 0.8,
        "continue_peak_fraction": 0.65,
        "sustained_samples": 3,
        "maximum_observation_gap_seconds": 900,
        "allow_load_probe": False,
        "probe_minimum_input_w": None,
        "probe_cooldown_seconds": 1800,
    },
    "transfer": {
        "max_bytes": 33554432,
        "max_seconds": 120,
        "probe_max_bytes": 2097152,
        "probe_max_seconds": 15,
        "retry_cooldown_seconds": 1800,
    },
    "server": None,
}


def merge(base, overrides):
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if key not in base:
            raise ValueError(f"Unknown configuration key: {key}")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"Configuration {key} must be an object")
            result[key] = merge(base[key], value)
        else:
            result[key] = value
    return result


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def positive_integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")


def absolute_path(value, name):
    if not isinstance(value, str) or "\x00" in value or not Path(value).is_absolute():
        raise ValueError(f"{name} must be an absolute path")


def load_config(path):
    overrides = json.loads(Path(path).read_text()) if path is not None else {}
    if not isinstance(overrides, dict):
        raise ValueError("Configuration must be an object")
    result = merge(DEFAULTS, overrides)
    for key in ("spool", "hardware_database"):
        absolute_path(result[key], key)
    for key in ("max_spool_bytes", "min_free_bytes"):
        positive_integer(result[key], key)
    power = result["power"]
    absolute_path(power["sensor_path"], "power.sensor_path")
    for key in (
        "stop_charge_percent",
        "start_charge_percent",
        "continue_peak_fraction",
        "start_peak_fraction",
    ):
        if not numeric(power[key]):
            raise ValueError(f"power.{key} must be finite and numeric")
    if not 0 <= power["stop_charge_percent"] <= power["start_charge_percent"] <= 100:
        raise ValueError("Invalid battery charge hysteresis")
    if not 0 < power["continue_peak_fraction"] <= power["start_peak_fraction"] <= 1:
        raise ValueError("Invalid observed peak hysteresis")
    for key in (
        "max_age_seconds",
        "sustained_samples",
        "maximum_observation_gap_seconds",
        "probe_cooldown_seconds",
    ):
        positive_integer(power[key], f"power.{key}")
    for key in ("battery_profile_verified", "allow_load_probe"):
        if type(power[key]) is not bool:
            raise ValueError(f"power.{key} must be a boolean")
    for key in (
        "minimum_input_w",
        "stop_input_w",
        "maximum_battery_discharge_w",
        "probe_minimum_input_w",
    ):
        value = power[key]
        if value is not None and (
            not numeric(value)
            or value < 0
            or (key in ("minimum_input_w", "probe_minimum_input_w") and value == 0)
        ):
            raise ValueError(f"power.{key} must be a valid finite threshold or null")
    if power["stop_input_w"] is not None and (
        power["minimum_input_w"] is None
        or power["stop_input_w"] > power["minimum_input_w"]
    ):
        raise ValueError(
            "Stop input threshold must not exceed the configured start input threshold"
        )
    if (
        power["probe_minimum_input_w"] is not None
        and power["minimum_input_w"] is not None
        and power["probe_minimum_input_w"] > power["minimum_input_w"]
    ):
        raise ValueError(
            "Probe input threshold must not exceed the start input threshold"
        )
    for key, value in result["transfer"].items():
        positive_integer(value, f"transfer.{key}")
    if result["server"] is not None and not isinstance(result["server"], dict):
        raise ValueError("server must be an object or null")
    if (
        not numeric(result["minimum_capture_charge_percent"])
        or not 0 <= result["minimum_capture_charge_percent"] <= 100
    ):
        raise ValueError("Invalid capture reserve")
    camera = result["camera"]
    absolute_path(camera["camera_command"], "camera.camera_command")
    for key in ("timeout_seconds", "width", "height"):
        positive_integer(camera[key], f"camera.{key}")
    positive_integer(camera["settle_ms"], "camera.settle_ms")
    if camera["timeout_seconds"] > 3600:
        raise ValueError("Camera timeout must not exceed 3600 seconds")
    if camera["settle_ms"] >= camera["timeout_seconds"] * 1000:
        raise ValueError(
            "Camera settling time must be shorter than its process timeout"
        )
    if type(camera["rotation"]) is not int or camera["rotation"] not in (0, 180):
        raise ValueError("Camera rotation must be 0 or 180")
    return result
