import copy
import json
import math
import os
import re
import stat
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
        "width": 0,
        "height": 0,
        "rotation": 0,
        "quality": 90,
    },
    "schedule": {"enabled": False, "interval_seconds": 300},
    "remote_controls": {
        "enabled": False,
        "device_id": None,
        "state_path": "/var/lib/pi-timelapse-controls/settings.json",
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
    if path is None:
        return validate_config({})
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("Configuration must be a regular file")
        payload = source.read(65537)
    if len(payload) > 65536:
        raise ValueError("Configuration exceeds 64 KiB")
    return validate_config(json.loads(payload))


def validate_config(overrides):
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
    for key in ("timeout_seconds", "quality"):
        positive_integer(camera[key], f"camera.{key}")
    for key in ("width", "height"):
        positive_integer(camera[key], f"camera.{key}", minimum=0)
        if camera[key] > 16384:
            raise ValueError("Camera dimensions must not exceed 16384 pixels")
    if bool(camera["width"]) != bool(camera["height"]):
        raise ValueError("Set both dimensions or use zero for native resolution")
    if not 1 <= camera["quality"] <= 100:
        raise ValueError("Camera JPEG quality must be in 1..100")
    positive_integer(camera["settle_ms"], "camera.settle_ms")
    if camera["timeout_seconds"] > 3600:
        raise ValueError("Camera timeout must not exceed 3600 seconds")
    if camera["settle_ms"] >= camera["timeout_seconds"] * 1000:
        raise ValueError(
            "Camera settling time must be shorter than its process timeout"
        )
    if type(camera["rotation"]) is not int or camera["rotation"] not in (0, 180):
        raise ValueError("Camera rotation must be 0 or 180")
    schedule = result["schedule"]
    if type(schedule["enabled"]) is not bool:
        raise ValueError("schedule.enabled must be a boolean")
    positive_integer(schedule["interval_seconds"], "schedule.interval_seconds", 60)
    if schedule["interval_seconds"] > 86400:
        raise ValueError("Schedule interval must not exceed 86400 seconds")
    remote = result["remote_controls"]
    if type(remote["enabled"]) is not bool:
        raise ValueError("remote_controls.enabled must be a boolean")
    absolute_path(remote["state_path"], "remote_controls.state_path")
    if ".." in Path(remote["state_path"]).parts:
        raise ValueError("Remote settings path must not contain traversal")
    if remote["device_id"] is not None and (
        not isinstance(remote["device_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", remote["device_id"])
    ):
        raise ValueError("Remote device ID must be a stable safe identifier")
    if remote["enabled"] and not remote["device_id"]:
        raise ValueError("Remote controls require a device ID")
    if remote["enabled"]:
        from .ha_controls import defaults_from_config

        defaults_from_config(result)
    return result


def effective_config(config):
    """Snapshot allowlisted remote settings without changing local safety policy."""
    if not config["remote_controls"]["enabled"]:
        return copy.deepcopy(config)
    from .ha_controls import effective_settings

    settings = effective_settings(config)
    result = copy.deepcopy(config)
    result["schedule"].update(
        enabled=settings["capture_enabled"],
        interval_seconds=settings["interval_seconds"],
    )
    result["camera"].update(
        quality=settings["jpeg_quality"],
        rotation=settings["rotation"],
        settle_ms=settings["settle_ms"],
    )
    if settings["resolution"] != "configured":
        width, height = settings["resolution"].split("x")
        result["camera"].update(width=int(width), height=int(height))
    return validate_config(result)
