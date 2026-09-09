"""Run at most one due capture without replaying missed scheduled intervals."""

from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import time

from .spool import atomic_json, durable_directory


def read_attempt(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("Scheduled capture state must be a regular file")
        payload = source.read(4097)
    if len(payload) > 4096:
        raise ValueError("Scheduled capture state is too large")
    value = json.loads(payload)
    if (
        not isinstance(value, dict)
        or set(value) != {"boot_id", "uptime", "attempted_at_utc"}
        or not isinstance(value["boot_id"], str)
        or not value["boot_id"]
        or type(value["uptime"]) not in (int, float)
        or not math.isfinite(value["uptime"])
        or value["uptime"] < 0
    ):
        raise ValueError("Invalid scheduled capture state")
    timestamp = datetime.fromisoformat(value["attempted_at_utc"])
    if timestamp.tzinfo is None:
        raise ValueError("Scheduled capture state requires a timezone")
    return value


def due(previous, interval, now, uptime, boot_id):
    if previous is None:
        return True
    if previous["boot_id"] == boot_id:
        return uptime - previous["uptime"] >= interval
    elapsed = (
        now - datetime.fromisoformat(previous["attempted_at_utc"])
    ).total_seconds()
    return elapsed >= interval if elapsed >= 0 else uptime >= interval


def run_scheduled(config, capture_action, now=None, uptime=None, boot_id=None):
    """Persist each admitted attempt before capture; failed attempts keep the interval."""
    if not config["schedule"]["enabled"]:
        return {"action": "wait", "reason": "Scheduled capture is disabled"}
    now = datetime.now(timezone.utc) if now is None else now
    uptime = time.monotonic() if uptime is None else uptime
    boot_id = (
        Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if boot_id is None
        else boot_id
    )
    root = Path(config["spool"])
    durable_directory(root)
    descriptor = os.open(
        root / "schedule.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Schedule lock must be a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"action": "wait", "reason": "Another scheduled capture is active"}
        path = root / "schedule-state.json"
        previous = read_attempt(path)
        if not due(
            previous, config["schedule"]["interval_seconds"], now, uptime, boot_id
        ):
            return {"action": "wait", "reason": "Capture interval has not elapsed"}
        atomic_json(
            path,
            {"boot_id": boot_id, "uptime": uptime, "attempted_at_utc": now.isoformat()},
        )
        return capture_action()
    finally:
        os.close(descriptor)
