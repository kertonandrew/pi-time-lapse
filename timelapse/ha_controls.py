"""Validate and durably store the six remotely configurable camera settings."""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat

from .spool import atomic_json, durable_directory, fsync_directory


RESOLUTIONS = ("configured", "1920x1080", "2304x1296")
INTEGER_LIMITS = {
    "interval_seconds": (60, 86400),
    "jpeg_quality": (10, 100),
    "settle_ms": (100, 10000),
}
FIELDS = (
    "capture_enabled",
    "interval_seconds",
    "resolution",
    "jpeg_quality",
    "rotation",
    "settle_ms",
)
MAX_STATE_BYTES = 4096
MAX_SCALAR_BYTES = 64
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def validate_settings(values, complete=False):
    """Return only allowlisted typed scalars; complete requires all six fields."""
    if not isinstance(values, dict) or set(values) - set(FIELDS):
        raise ValueError("Camera controls contain an unsupported field")
    if complete and set(values) != set(FIELDS):
        raise ValueError("Camera controls require all six settings")
    for field, value in values.items():
        if field == "capture_enabled":
            valid = type(value) is bool
        elif field == "resolution":
            valid = isinstance(value, str) and value in RESOLUTIONS
        elif field == "rotation":
            valid = type(value) is int and value in (0, 180)
        else:
            low, high = INTEGER_LIMITS[field]
            valid = type(value) is int and low <= value <= high
        if not valid:
            raise ValueError(f"Invalid camera control: {field}")
    return dict(values)


def decode_desired(field, payload):
    """Decode one exact MQTT scalar without accepting JSON objects or coercion."""
    if field not in FIELDS or not isinstance(payload, bytes):
        raise ValueError("Invalid camera control message")
    if not 1 <= len(payload) <= MAX_SCALAR_BYTES:
        raise ValueError("Camera control payload exceeds its scalar limit")
    try:
        value = payload.decode("ascii")
    except UnicodeError:
        raise ValueError("Camera control payload must be ASCII") from None
    if field == "capture_enabled":
        if value not in ("true", "false"):
            raise ValueError("Capture enable payload must be true or false")
        decoded = value == "true"
    elif field == "resolution":
        decoded = value
    else:
        if not re.fullmatch(r"0|[1-9][0-9]{0,4}", value):
            raise ValueError("Camera number payload must be a canonical integer")
        decoded = int(value)
    return validate_settings({field: decoded})[field]


def encode_reported(settings):
    """Encode validated state into the scalars consumed by native MQTT entities."""
    settings = validate_settings(settings, complete=True)
    return {
        field: (str(value).lower() if type(value) is bool else str(value)).encode(
            "ascii"
        )
        for field, value in settings.items()
    }


def defaults_from_config(config):
    """Return the local camera and schedule defaults exposed as remote settings."""
    camera = config["camera"]
    schedule = config["schedule"]
    return validate_settings(
        {
            "capture_enabled": schedule["enabled"],
            "interval_seconds": schedule["interval_seconds"],
            "resolution": "configured",
            "jpeg_quality": camera["quality"],
            "rotation": camera["rotation"],
            "settle_ms": camera["settle_ms"],
        },
        complete=True,
    )


def _identity(config, device_id=None):
    local = config["remote_controls"]
    expected = local.get("device_id")
    if (
        local.get("enabled") is not True
        or not isinstance(expected, str)
        or not IDENTIFIER.fullmatch(expected)
        or (device_id is not None and device_id != expected)
    ):
        raise ValueError(
            "Remote camera controls are disabled or device identity differs"
        )
    path = Path(local["state_path"])
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise ValueError("Camera control state path must be absolute without traversal")
    for parent in path.parents:
        try:
            details = parent.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid not in (0, os.geteuid())
            or (details.st_mode & 0o022 and not details.st_mode & stat.S_ISVTX)
        ):
            raise ValueError(
                "Camera control state ancestry must contain only trusted directories"
            )
    return path, expected


def _directory(path):
    details = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_mode & 0o022
        or details.st_uid not in (0, os.geteuid())
    ):
        raise ValueError("Camera control state directory must be owned and private")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Camera control state contains duplicate fields")
        result[key] = value
    return result


def _read(path, device_id):
    _directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        details = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_mode & 0o022
            or details.st_uid not in (0, os.geteuid())
            or details.st_size > MAX_STATE_BYTES
        ):
            raise ValueError(
                "Camera control state must be an owned bounded regular file"
            )
        payload = source.read(MAX_STATE_BYTES + 1)
        after = os.fstat(source.fileno())
        if (details.st_size, details.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Camera control state changed during reading")
    if len(payload) > MAX_STATE_BYTES:
        raise ValueError("Camera control state exceeds its byte limit")
    state = json.loads(payload, object_pairs_hook=_unique_object)
    if (
        not isinstance(state, dict)
        or set(state) != {"protocol", "device_id", "settings"}
        or type(state["protocol"]) is not int
        or state["protocol"] != 1
        or state["device_id"] != device_id
    ):
        raise ValueError("Camera control state identity or format differs")
    return validate_settings(state["settings"], complete=True)


def _compatible(config, settings):
    if settings["settle_ms"] >= config["camera"]["timeout_seconds"] * 1000:
        raise ValueError("Camera settling time must remain below the local timeout")
    return settings


def effective_settings(config):
    """Read the validated local overlay only when locally enabled for this device."""
    settings = defaults_from_config(config)
    if config["remote_controls"]["enabled"] is not True:
        return settings
    path, device_id = _identity(config)
    try:
        settings = _read(path, device_id)
    except FileNotFoundError:
        pass
    return _compatible(config, settings)


@contextlib.contextmanager
def _locked(path, name=".settings.lock"):
    durable_directory(path.parent)
    _directory(path.parent)
    descriptor = os.open(
        path.parent / name,
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_mode & 0o022
            or details.st_uid not in (0, os.geteuid())
        ):
            raise ValueError("Camera control lock must be a private regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def session_lock(config, device_id):
    """Reject overlapping control polls without writing outside the state directory."""
    path, _expected = _identity(config, device_id)
    with _locked(path, ".poll.lock"):
        yield


def apply_desired(config, device_id, updates):
    """Durably validate and read back settings; unchanged retained replay writes nothing."""
    updates = validate_settings(updates)
    path, expected = _identity(config, device_id)
    with _locked(path):
        try:
            current = _read(path, expected)
        except FileNotFoundError:
            current = None
        settings = defaults_from_config(config) if current is None else current
        settings = _compatible(config, validate_settings({**settings, **updates}, True))
        if settings != current:
            atomic_json(
                path, {"protocol": 1, "device_id": expected, "settings": settings}
            )
        else:
            fsync_directory(path.parent)
        persisted = _read(path, expected)
        if persisted != settings:
            raise ValueError("Camera control state changed before durable readback")
        return persisted
