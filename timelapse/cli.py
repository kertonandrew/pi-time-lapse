import argparse
import fcntl
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from .config import effective_config, load_config
from .power import (
    PowerError,
    PowerHistory,
    capture_guard,
    decide,
    number,
    parse_time,
    read_sensor,
    utc_now,
)


def emit(value):
    print(json.dumps(value, sort_keys=True, allow_nan=False), flush=True)


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(state, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_state(path):
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("Transfer state is not an object")
        return value
    except FileNotFoundError:
        return {}


def cooling_down(state, key, seconds, now, now_uptime=None, boot_id=None):
    if key not in state:
        return False
    if key + "_boot_id" in state:
        if boot_id is None:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if state[key + "_boot_id"] == boot_id:
            if now_uptime is None:
                now_uptime = time.monotonic()
            previous = state[key + "_uptime"]
            if not number(previous) or previous < 0:
                raise ValueError("Invalid monotonic transfer state")
            return now_uptime - previous < seconds
    elapsed = (now - parse_time(state[key])).total_seconds()
    return elapsed < seconds


def record_attempt(state, key, now):
    state[key] = now.isoformat()
    state[key + "_boot_id"] = (
        Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    )
    state[key + "_uptime"] = time.monotonic()


def power_decision(config, history, active=False):
    settings = config["power"]
    try:
        sample = read_sensor(settings["sensor_path"], settings["max_age_seconds"])
        history.record(sample)
        return decide(sample, settings, history.recent(), active=active)
    except (PowerError, OSError, ValueError):
        history.invalidate()
        raise


def upload(config, spool, history, dry_run=False):
    if not config["server"] or config["server"].get("enabled") is False:
        return {"action": "wait", "reason": "Server destination is not configured"}
    try:
        if not spool.list_pending(timeout_seconds=1):
            return {"action": "wait", "reason": "No pending photographs"}
    except TimeoutError:
        return {"action": "wait", "reason": "Capture queue is busy"}
    state_path = Path(config["spool"]) / "transfer-state.json"
    state = read_state(state_path)
    now = utc_now()
    limits = config["transfer"]
    if cooling_down(state, "last_attempt_utc", limits["retry_cooldown_seconds"], now):
        return {"action": "wait", "reason": "Transfer retry cooldown is active"}
    decision = power_decision(config, history)
    if decision["action"] == "wait" or dry_run:
        return decision
    probe = decision["action"] == "probe"
    if probe and cooling_down(
        state, "last_probe_utc", config["power"]["probe_cooldown_seconds"], now
    ):
        return {"action": "wait", "reason": "Load probe cooldown is active"}
    record_attempt(state, "last_attempt_utc", now)
    if probe:
        record_attempt(state, "last_probe_utc", now)
    save_state(state_path, state)

    last_check = float("-inf")
    last_eligible = False

    def eligible():
        nonlocal last_check, last_eligible
        current_uptime = time.monotonic()
        if current_uptime - last_check < 1:
            return last_eligible
        last_check = current_uptime
        try:
            sample = read_sensor(
                config["power"]["sensor_path"], config["power"]["max_age_seconds"]
            )
            history.record(sample, activity="probe" if probe else "upload")
            current = decide(sample, config["power"], history.recent(), active=True)
            last_eligible = current["action"] == "upload"
            if probe and not last_eligible:
                settings = dict(config["power"])
                settings["minimum_input_w"] = settings["probe_minimum_input_w"]
                settings["stop_input_w"] = settings["probe_minimum_input_w"]
                settings["continue_peak_fraction"] = 0
                last_eligible = (
                    decide(sample, settings, [], active=True)["action"] == "upload"
                )
            return last_eligible
        except (PowerError, OSError, ValueError):
            history.invalidate()
            last_eligible = False
            return False

    from .transfer import transfer

    result = transfer(
        spool,
        config["server"],
        eligible,
        max_bytes=limits["probe_max_bytes"] if probe else limits["max_bytes"],
        max_seconds=limits["probe_max_seconds"] if probe else limits["max_seconds"],
    )
    return {"decision": decision, "transfer": result}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Capture original photographs and transfer during measured power opportunities"
    )
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("capture", "scheduled-capture", "probe", "upload", "status"):
        subparser = commands.add_parser(command)
        if command in ("capture", "scheduled-capture", "upload"):
            subparser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = load_config(arguments.config)
        if arguments.command in ("capture", "scheduled-capture"):
            config = effective_config(config)
        if arguments.command in ("capture", "scheduled-capture") and arguments.dry_run:
            emit(
                {
                    "action": "capture",
                    "camera": config["camera"],
                    "schedule": config["schedule"],
                    "spool": config["spool"],
                    "power_check": True,
                    "shutdown": False,
                }
            )
            return 0
        if (
            arguments.command == "scheduled-capture"
            and not config["schedule"]["enabled"]
        ):
            emit({"action": "wait", "reason": "Scheduled capture is disabled"})
            return 0
        from .spool import Spool

        spool = Spool(
            Path(config["spool"]), config["max_spool_bytes"], config["min_free_bytes"]
        )
        if arguments.command not in ("capture", "scheduled-capture"):
            history = PowerHistory(Path(config["spool"]) / "power-history.sqlite3")
        if arguments.command in ("capture", "scheduled-capture"):
            from .capture import capture

            def guarded_capture():
                source = capture_guard(
                    config["hardware_database"],
                    config["minimum_capture_charge_percent"],
                    config["power"]["battery_profile_verified"],
                )
                return capture(spool, time_source=source, **config["camera"])

            if arguments.command == "scheduled-capture":
                from .scheduler import run_scheduled

                emit(run_scheduled(config, guarded_capture))
            else:
                emit(guarded_capture())
        elif arguments.command == "status":
            pending = spool.list_pending()
            emit(
                {
                    "pending_images": len(pending),
                    "pending_bytes": sum(row["size_bytes"] for row in pending),
                    "server_configured": bool(config["server"]),
                    "power_profile": history.profile(),
                    "shutdown_enabled": False,
                }
            )
        elif arguments.command == "probe":
            decision = power_decision(config, history)
            emit({"decision": decision, "power_profile": history.profile()})
        else:
            lock_path = Path(config["spool"]) / "transfer.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    emit({"action": "wait", "reason": "Another transfer is active"})
                    return 0
                result = upload(config, spool, history, arguments.dry_run)
                emit(result)
                if result.get("transfer", {}).get("status") == "error":
                    return 1
        return 0
    except PowerError as error:
        emit({"action": "wait", "reason": str(error)})
        return 0
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        emit({"error": f"{type(error).__name__}: {error}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
