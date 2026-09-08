"""Configuration for the opt-in Home Assistant MQTT publishers."""

import copy
import json
import re
from pathlib import Path


DEFAULTS = {
    "device_id": None,
    "device_name": "Solar timelapse",
    "topic_prefix": "pi_timelapse",
    "discovery_prefix": "homeassistant",
    "discovery_refresh_seconds": 300,
    "state_directory": "/var/lib/pi-home-assistant",
    "mqtt": {
        "host": None,
        "port": 8883,
        "tls": True,
        "username": None,
        "password_file": None,
        "ca_file": None,
        "cert_file": None,
        "key_file": None,
        "timeout_seconds": 20,
    },
    "telemetry": {
        "database": "/var/lib/pi-hardware/metrics.sqlite3",
        "spool": "/var/lib/pi-timelapse",
        "max_age_seconds": 420,
        "expire_after_seconds": 900,
        "minimum_battery_voltage_mv": 3700,
    },
    "photos": {
        "archive_root": "/srv/pi-timelapse",
        "max_bytes": 8388608,
        "republish_seconds": 3600,
    },
    "controls": {
        "timelapse_config": "/etc/pi-timelapse.json",
        "poll_seconds": 3,
    },
}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
PREFIX = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")


def _merge(base, changes):
    result = copy.deepcopy(base)
    if not isinstance(changes, dict):
        raise ValueError("Configuration must be an object")
    for key, value in changes.items():
        if key not in base:
            raise ValueError(f"Unknown Home Assistant configuration key: {key}")
        result[key] = _merge(base[key], value) if isinstance(base[key], dict) else value
    return result


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")


def _path(value, name):
    if (
        not isinstance(value, str)
        or "\x00" in value
        or not Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise ValueError(f"{name} must be an absolute path without traversal")


def validate_config(changes):
    config = _merge(DEFAULTS, changes)
    if not isinstance(config["device_id"], str) or not IDENTIFIER.fullmatch(
        config["device_id"]
    ):
        raise ValueError(
            "Set a stable unique device_id using letters, numbers, underscore or hyphen"
        )
    if (
        not isinstance(config["device_name"], str)
        or not 1 <= len(config["device_name"]) <= 100
    ):
        raise ValueError("device_name must contain 1..100 characters")
    for key in ("topic_prefix", "discovery_prefix"):
        if (
            not isinstance(config[key], str)
            or len(config[key]) > 128
            or not PREFIX.fullmatch(config[key])
        ):
            raise ValueError(f"Invalid {key}")
    _path(config["state_directory"], "state_directory")
    _integer(
        config["discovery_refresh_seconds"], "discovery_refresh_seconds", 300, 86400
    )
    mqtt = config["mqtt"]
    host = mqtt["host"]
    if host is not None and (
        not isinstance(host, str)
        or not 1 <= len(host) <= 253
        or any(c.isspace() or ord(c) < 32 for c in host)
        or "/" in host
    ):
        raise ValueError("mqtt.host must be a hostname or IP address")
    _integer(mqtt["port"], "mqtt.port", 1, 65535)
    _integer(mqtt["timeout_seconds"], "mqtt.timeout_seconds", 1, 60)
    if type(mqtt["tls"]) is not bool:
        raise ValueError("mqtt.tls must be a boolean")
    if mqtt["username"] is not None and (
        not isinstance(mqtt["username"], str)
        or not 1 <= len(mqtt["username"]) <= 256
        or "\x00" in mqtt["username"]
    ):
        raise ValueError("Invalid MQTT username")
    for key in ("password_file", "ca_file", "cert_file", "key_file"):
        if mqtt[key] is not None:
            _path(mqtt[key], f"mqtt.{key}")
    if bool(mqtt["cert_file"]) != bool(mqtt["key_file"]):
        raise ValueError("Set both MQTT client certificate and key")
    if not mqtt["tls"] and any(
        mqtt[key] for key in ("ca_file", "cert_file", "key_file")
    ):
        raise ValueError("TLS files cannot be used with TLS disabled")
    if mqtt["password_file"] and not mqtt["username"]:
        raise ValueError("MQTT password requires a username")
    telemetry = config["telemetry"]
    for key in ("database", "spool"):
        _path(telemetry[key], f"telemetry.{key}")
    _integer(telemetry["max_age_seconds"], "telemetry.max_age_seconds", 1, 900)
    _integer(
        telemetry["expire_after_seconds"], "telemetry.expire_after_seconds", 60, 3600
    )
    _integer(
        telemetry["minimum_battery_voltage_mv"],
        "telemetry.minimum_battery_voltage_mv",
        3500,
        4200,
    )
    photos = config["photos"]
    _path(photos["archive_root"], "photos.archive_root")
    _integer(photos["max_bytes"], "photos.max_bytes", 4, 33554432)
    _integer(photos["republish_seconds"], "photos.republish_seconds", 300, 86400)
    controls = config["controls"]
    _path(controls["timelapse_config"], "controls.timelapse_config")
    _integer(controls["poll_seconds"], "controls.poll_seconds", 1, 10)
    if controls["poll_seconds"] >= mqtt["timeout_seconds"]:
        raise ValueError("Control polling window must be shorter than MQTT timeout")
    return config


def load_config(path):
    with Path(path).open("rb") as source:
        payload = source.read(65537)
    if len(payload) > 65536:
        raise ValueError("Home Assistant configuration exceeds 64 KiB")
    return validate_config(json.loads(payload))
