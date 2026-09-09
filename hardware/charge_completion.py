"""Apply a configured software charge-completion threshold without enabling charging."""

import argparse
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


LIMITATIONS = [
    "The reported percentage is a firmware estimate and is preserved unchanged.",
    "Reaching the user threshold does not establish battery capacity or sensor health.",
    "This one-shot check never enables charging or changes the battery profile.",
    "A software threshold cannot enforce a charge cutoff while the Pi is off or after an OS or I2C failure.",
]


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("Charge completion configuration must be an object")
    threshold = config.get("stop_at_percent")
    age = config.get("max_sample_age_seconds", 15)
    if not finite(threshold) or not 0 < threshold <= 100:
        raise ValueError("stop_at_percent must be greater than zero and at most 100")
    if not finite(age) or not 0 < age <= 60:
        raise ValueError(
            "max_sample_age_seconds must be greater than zero and at most 60"
        )
    return {"stop_at_percent": threshold, "max_sample_age_seconds": age}


def read_api(name, reader, raw, errors):
    try:
        reply = reader()
        try:
            json.dumps(reply, allow_nan=False)
            raw[name] = reply
        except (ValueError, TypeError):
            raw[name] = {"unserializable_reply": repr(reply)}
            raise ValueError("API reply is not finite JSON")
        if not isinstance(reply, dict) or reply.get("error") != "NO_ERROR":
            raise ValueError("API did not report NO_ERROR")
        if "data" not in reply:
            raise ValueError("API data is missing")
        return reply["data"]
    except Exception as error:
        errors[name] = f"{type(error).__name__}: {error}"
        return None


def observe(hat, clock=time.monotonic):
    started = clock()
    raw, errors = {}, {}
    percent = read_api("GetChargeLevel", hat.status.GetChargeLevel, raw, errors)
    status = read_api("GetStatus", hat.status.GetStatus, raw, errors)
    charging = read_api("GetChargingConfig", hat.config.GetChargingConfig, raw, errors)
    faults = read_api("GetFaultStatus", hat.status.GetFaultStatus, raw, errors)
    board = read_api(
        "BoardFaultStatus_0xFA", lambda: hat.interface.ReadData(0xFA, 1), raw, errors
    )
    for method, data in (
        ("GetStatus", status),
        ("GetChargingConfig", charging),
        ("GetFaultStatus", faults),
    ):
        if not isinstance(data, dict):
            errors.setdefault(method, "Invalid API data")
    if (
        isinstance(charging, dict)
        and type(charging.get("charging_enabled")) is not bool
    ):
        errors.setdefault("GetChargingConfig", "Invalid charging-enabled flag")
    board_faults = None
    if (
        isinstance(board, list)
        and len(board) == 1
        and type(board[0]) is int
        and 0 <= board[0] <= 255
    ):
        board_faults = board[0]
    else:
        errors.setdefault("BoardFaultStatus_0xFA", "Invalid board fault byte")
    finished = clock()
    return {
        "observed_monotonic": started,
        "read_finished_monotonic": finished,
        "read_duration_seconds": finished - started,
        "reported_charge_percent": percent,
        "status": status,
        "charging": charging,
        "faults": faults,
        "board_faults": board_faults,
        "raw": raw,
        "errors": errors,
    }


def evaluate(config, sample, now_monotonic):
    config = validate_config(config)
    reasons, warnings = [], []
    percent = sample.get("reported_charge_percent")
    observed = sample.get("observed_monotonic")
    duration = sample.get("read_duration_seconds")
    fresh = (
        finite(observed)
        and finite(now_monotonic)
        and finite(duration)
        and 0 <= now_monotonic - observed <= config["max_sample_age_seconds"]
        and 0 <= duration <= config["max_sample_age_seconds"]
    )
    if not fresh:
        reasons.append("stale_or_invalid_observation_time")
    percent_valid = finite(percent) and 0 <= percent <= 100
    if not percent_valid:
        reasons.append("invalid_charge_percentage")
    errors = sample.get("errors")
    reads_valid = isinstance(errors, dict) and not errors
    if not reads_valid:
        reasons.append("telemetry_read_errors")
    status = sample.get("status")
    battery_present = isinstance(status, dict) and status.get("battery") in (
        "NORMAL",
        "CHARGING_FROM_IN",
        "CHARGING_FROM_5V_IO",
    )
    if not battery_present:
        reasons.append("battery_absent_or_status_unknown")
    charging = sample.get("charging")
    charging_known = (
        isinstance(charging, dict) and type(charging.get("charging_enabled")) is bool
    )
    if not charging_known:
        reasons.append("charging_configuration_unknown")
    board_faults = sample.get("board_faults")
    if type(board_faults) is not int or not 0 <= board_faults <= 255:
        reasons.append("board_fault_status_unknown")
    elif board_faults:
        reasons.append("board_fault_present")
        if board_faults & 0x10:
            warnings.append("fuel_gauge_fault_percentage_is_an_estimate")
        if board_faults & 0x20:
            warnings.append("ntc_fault_battery_temperature_unverified")
    faults = sample.get("faults")
    if not isinstance(faults, dict):
        reasons.append("hat_fault_status_unknown")
    else:
        if faults.get("battery_profile_invalid"):
            reasons.append("battery_profile_invalid")
        temperature_fault = faults.get("charging_temperature_fault", "NORMAL")
        if temperature_fault not in ("NORMAL", "COOL", "WARM"):
            reasons.append("charging_temperature_fault")
    charged = (
        fresh
        and percent_valid
        and reads_valid
        and battery_present
        and percent >= config["stop_at_percent"]
    )
    safety_reasons = list(reasons)
    if charged:
        reasons.append("user_charge_threshold_reached")
    return {
        "stop_at_percent": config["stop_at_percent"],
        "reported_charge_percent": percent,
        "charged_at_user_threshold": charged,
        "stop_requested": bool(reasons),
        "reasons": reasons,
        "safety_reasons": safety_reasons,
        "sensor_confidence_warnings": warnings,
    }


def enforce(hat, config, clock=time.monotonic):
    config = validate_config(config)
    sample = observe(hat, clock=clock)
    result = {
        "mode": "stop_only_software_charge_completion",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "configuration": config,
        "observation": sample,
        "limitations": list(LIMITATIONS),
        "hardware_write_attempted": False,
        **evaluate(config, sample, clock()),
    }
    if not result["stop_requested"]:
        result["action"] = "left_unchanged"
        return result
    charging = sample["charging"]
    if (
        isinstance(charging, dict)
        and charging.get("charging_enabled") is False
        and "stale_or_invalid_observation_time" not in result["safety_reasons"]
    ):
        result["action"] = "already_disabled"
        result["charging_disabled_verified"] = True
        return result
    result["hardware_write_attempted"] = True
    try:
        written = hat.config.SetChargingConfig(False, non_volatile=True)
        result["disable_write_reply"] = written
        readback = hat.config.GetChargingConfig()
        result["disable_readback"] = readback
        if not isinstance(written, dict) or written.get("error") != "NO_ERROR":
            raise RuntimeError("Charging-disable write did not report NO_ERROR")
        if (
            not isinstance(readback, dict)
            or readback.get("error") != "NO_ERROR"
            or not isinstance(readback.get("data"), dict)
            or readback["data"].get("charging_enabled") is not False
            or readback.get("non_volatile") is not True
        ):
            raise RuntimeError("Persistent charging-disable readback was not verified")
        result["action"] = "disabled_and_verified"
        result["charging_disabled_verified"] = True
    except Exception as error:
        result["action"] = "stop_failed"
        result["charging_disabled_verified"] = False
        result["error"] = f"{type(error).__name__}: {error}"
    return result


def write_record(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".charge-completion-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(result, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None, device_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args(argv)
    with options.config.open() as stream:
        config = validate_config(json.load(stream))
    if device_factory is None:
        from pijuice import PiJuice

        def device_factory():
            return PiJuice(1, 0x14)

    result = enforce(device_factory(), config)
    if options.output is not None:
        write_record(options.output, result)
    print(json.dumps(result, allow_nan=False, sort_keys=True))
    return 1 if result["action"] == "stop_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
