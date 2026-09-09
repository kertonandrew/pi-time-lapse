"""Provisional five-minute CE06795 trial; software stop is not an OS-failure interlock."""

import argparse
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SESSION_SECONDS = 300
INTERVAL_SECONDS = 2
PROFILE_OVERRIDES = {
    "chargeCurrent": 550,
    "regulationVoltage": 4100,
    "terminationCurrent": 50,
    "tempCold": 10,
    "tempCool": 15,
    "tempWarm": 30,
    "tempHot": 35,
}
PROFILE_FIELDS = {
    "capacity",
    "chargeCurrent",
    "terminationCurrent",
    "regulationVoltage",
    "cutoffVoltage",
    "tempCold",
    "tempCool",
    "tempWarm",
    "tempHot",
    "ntcB",
    "ntcResistance",
}
EXTENDED_FIELDS = {"chemistry", "ocv10", "ocv50", "ocv90", "r10", "r50", "r90"}
CONFIG_READS = {
    "firmware": "GetFirmwareVersion",
    "selection": "GetBatteryProfileStatus",
    "profile": "GetBatteryProfile",
    "extended": "GetBatteryExtProfile",
    "temperature_mode": "GetBatteryTempSenseConfig",
    "soc_mode": "GetRsocEstimationConfig",
    "inputs": "GetPowerInputsConfig",
    "charging": "GetChargingConfig",
}
STATUS_READS = (
    "GetStatus",
    "GetFaultStatus",
    "GetBatteryVoltage",
    "GetBatteryTemperature",
    "GetBatteryCurrent",
    "GetChargeLevel",
    "GetIoVoltage",
    "GetIoCurrent",
)
QUIET_UNITS = (
    "pi-timelapse-capture.timer",
    "pi-timelapse-capture.service",
    "pi-timelapse-transfer.timer",
    "pi-timelapse-transfer.service",
)
FAULT_EVENTS = {
    "button_power_off",
    "forced_power_off",
    "forced_sys_power_off",
    "watchdog_reset",
}
LIMITATIONS = [
    "Supplier-supported experimental operating point, not exact-cell qualification.",
    "Reported temperature may fall back to MCU temperature without a source flag.",
    "Battery current and charge percentage are firmware estimates.",
    "Pi-rail current outside 0-500 mA is retained and excluded from rail energy estimates; it is not battery charge current.",
    "Software stop cannot guarantee charge-off after OS, I2C, or HAT failure.",
    "The 4.10 V target does not establish full battery capacity.",
]


class ChargeStopped(RuntimeError):
    """The requested trial cannot continue with the observed state."""


def ok(response):
    if not isinstance(response, dict) or response.get("error") != "NO_ERROR":
        raise ChargeStopped("hat_communication_error")
    return response


def value(response):
    result = ok(response)
    if "data" not in result:
        raise ChargeStopped("missing_hat_data")
    return result["data"]


def number(reading):
    if type(reading) not in (float, int) or not math.isfinite(reading):
        raise ChargeStopped("invalid_numeric_reading")
    return reading


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, data, exclusive=False):
    with path.open("x" if exclusive else "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_configuration(hat):
    configuration = {
        name: getattr(hat.config, method)() for name, method in CONFIG_READS.items()
    }
    configuration["power_cut"] = hat.power.GetPowerOff()
    return configuration


def require_base(configuration):
    data = {name: value(response) for name, response in configuration.items()}
    if data["firmware"] != {"version": "1.6", "variant": "0"}:
        raise ChargeStopped("unexpected_firmware")
    profile = data["profile"]
    extended = data["extended"]
    if not isinstance(profile, dict) or set(profile) != PROFILE_FIELDS:
        raise ChargeStopped("incomplete_base_profile")
    if not isinstance(extended, dict) or set(extended) != EXTENDED_FIELDS:
        raise ChargeStopped("incomplete_extended_profile")
    if any(type(v) is not int for v in profile.values()):
        raise ChargeStopped("invalid_base_profile")
    if (
        profile["capacity"] != 1000
        or profile["ntcResistance"] != 10000
        or profile["ntcB"] != 3450
        or not 3000 <= profile["cutoffVoltage"] <= 3500
        or extended["chemistry"] != "LIPO"
        or data["selection"].get("validity") != "VALID"
    ):
        raise ChargeStopped("unexpected_base_profile")
    if data["temperature_mode"] not in ("NTC", "AUTO_DETECT"):
        raise ChargeStopped("thermistor_mode_required")
    if data["power_cut"] != [255]:
        raise ChargeStopped("power_cut_armed")
    for key in EXTENDED_FIELDS - {"chemistry"}:
        number(extended[key])
    if not 2500 <= extended["ocv10"] < extended["ocv50"] < extended["ocv90"] <= 4300:
        raise ChargeStopped("invalid_extended_profile")
    if any(extended[key] <= 0 for key in ("r10", "r50", "r90")):
        raise ChargeStopped("invalid_extended_profile")
    return data


def set_charging(hat, enabled, persistent=False):
    written = hat.config.SetChargingConfig(
        {"charging_enabled": enabled}, non_volatile=persistent
    )
    actual = hat.config.GetChargingConfig()
    ok(written)
    if value(actual) != {"charging_enabled": enabled}:
        raise ChargeStopped("charging_readback_mismatch")
    if actual.get("non_volatile") is not persistent:
        raise ChargeStopped("charging_persistence_mismatch")
    return actual


def disable_charging(hat):
    last_error = None
    for _ in range(3):
        try:
            set_charging(hat, False, persistent=True)
            began = time.monotonic()
            normal_readings = 0
            while time.monotonic() - began <= 5:
                actual = hat.config.GetChargingConfig()
                if (
                    value(actual) != {"charging_enabled": False}
                    or actual.get("non_volatile") is not True
                ):
                    raise ChargeStopped("charging_readback_mismatch")
                status = value(hat.status.GetStatus())
                if time.monotonic() - began >= 2 and status.get("battery") == "NORMAL":
                    normal_readings += 1
                    if normal_readings >= 2:
                        return dict(actual, observed_battery_status="NORMAL")
                else:
                    normal_readings = 0
                time.sleep(0.25)
            raise ChargeStopped("charger_still_reports_charging")
        except Exception as error:
            last_error = error
    raise ChargeStopped("charge_off_unverified") from last_error


def expected_configuration(original):
    base = require_base(original)
    inputs = dict(base["inputs"], usb_micro_current_limit="1.5A")
    return {
        "firmware": base["firmware"],
        "profile": dict(base["profile"], **PROFILE_OVERRIDES),
        "extended": base["extended"],
        "temperature_mode": "NTC",
        "soc_mode": base["soc_mode"],
        "inputs": inputs,
        "power_cut": [255],
    }


def verify_configuration(configuration, expected, enabled):
    for key, wanted in expected.items():
        if value(configuration[key]) != wanted:
            raise ChargeStopped(f"configuration_changed:{key}")
    selection = value(configuration["selection"])
    if (
        selection.get("validity") != "VALID"
        or selection.get("origin") != "CUSTOM"
        or selection.get("source") != "HOST"
    ):
        raise ChargeStopped("custom_profile_not_selected")
    if value(configuration["charging"]) != {"charging_enabled": enabled}:
        raise ChargeStopped("charging_state_changed")
    if configuration["charging"].get("non_volatile") is not (not enabled):
        raise ChargeStopped("charging_persistence_changed")
    if configuration["inputs"].get("non_volatile") is not True:
        raise ChargeStopped("input_configuration_not_persistent")


def require_quiet():
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "show",
            *QUIET_UNITS,
            "--property=Id,ActiveState,LoadState",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    units = {}
    for block in result.stdout.strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in fields:
            units[fields["Id"]] = fields
    if set(units) != set(QUIET_UNITS):
        raise ChargeStopped("workload_state_unavailable")
    if any(
        state.get("ActiveState") != "inactive" or state.get("LoadState") != "loaded"
        for state in units.values()
    ):
        raise ChargeStopped("camera_or_transfer_active")


def observe(hat):
    began = time.monotonic()
    raw = {method: getattr(hat.status, method)() for method in STATUS_READS}
    configuration = read_configuration(hat)
    now = time.monotonic()
    quality_flags = []
    for method, low, high, flag in (
        ("GetIoCurrent", 0, 500, "pi_rail_current_out_of_expected_range"),
        ("GetIoVoltage", 4800, 5250, "pi_rail_voltage_out_of_expected_range"),
    ):
        response = raw[method]
        reading = response.get("data") if isinstance(response, dict) else None
        if (
            not isinstance(response, dict)
            or response.get("error") != "NO_ERROR"
            or type(reading) not in (float, int)
            or not math.isfinite(reading)
            or not low <= reading <= high
        ):
            quality_flags.append(flag)
    return {
        "utc": timestamp(),
        "monotonic": now,
        "read_duration_seconds": now - began,
        "raw": raw,
        "configuration": configuration,
        "quality_flags": quality_flags,
        "pi_rail_energy_valid": not quality_flags,
    }


def validate_observation(row, expected, enabled, initial_faults):
    if not 0 <= number(row["read_duration_seconds"]) <= 5:
        raise ChargeStopped("slow_sensor_read")
    verify_configuration(row["configuration"], expected, enabled)
    raw = {method: value(response) for method, response in row["raw"].items()}
    status = raw["GetStatus"]
    if (
        status.get("powerInput") != "PRESENT"
        or status.get("powerInput5vIo") != "NOT_PRESENT"
    ):
        raise ChargeStopped("usb_source_changed")
    if status.get("battery") not in ("NORMAL", "CHARGING_FROM_IN"):
        raise ChargeStopped("battery_status_invalid")
    faults = raw["GetFaultStatus"]
    if any(key not in FAULT_EVENTS | {"charging_temperature_fault"} for key in faults):
        raise ChargeStopped("hat_fault")
    if faults.get("charging_temperature_fault", "NORMAL") not in (
        "NORMAL",
        "COOL",
        "WARM",
    ):
        raise ChargeStopped("hat_temperature_fault")
    if any(faults.get(key) and not initial_faults.get(key) for key in FAULT_EVENTS):
        raise ChargeStopped("new_hat_fault")
    for method in STATUS_READS[2:]:
        number(raw[method])
    if not 3800 <= raw["GetBatteryVoltage"] < 4150:
        raise ChargeStopped("battery_voltage_limit")
    if not 10 < raw["GetBatteryTemperature"] < 35:
        raise ChargeStopped("reported_temperature_limit")
    if not 4800 <= raw["GetIoVoltage"] <= 5250:
        raise ChargeStopped("pi_rail_voltage_limit")
    if not 0 <= raw["GetChargeLevel"] <= 100:
        raise ChargeStopped("invalid_charge_percentage")
    return raw["GetBatteryVoltage"] >= 4100


@contextmanager
def locked(path, blocking=True):
    with path.open("a+") as stream:
        os.fchmod(stream.fileno(), 0o600)
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(stream, flags)
        yield stream


def permit_token(directory):
    return hashlib.sha256(str(directory.resolve()).encode()).hexdigest() + "\n"


def clear_permit(stream):
    stream.seek(0)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def grant_permit(stream, directory):
    clear_permit(stream)
    stream.write(permit_token(directory))
    stream.flush()
    os.fsync(stream.fileno())


def consume_permit(stream, directory):
    stream.seek(0)
    if stream.read(100) != permit_token(directory):
        raise ChargeStopped("run_permit_absent")
    clear_permit(stream)


def stop_trial(hat, directory, control_lock):
    with locked(control_lock) as control:
        marker_error = None
        try:
            clear_permit(control)
            save_json(directory / "stop-requested.json", {"utc": timestamp()})
        except OSError as error:
            marker_error = error
        disabled = disable_charging(hat)
        if marker_error is not None:
            raise ChargeStopped("stop_marker_unwritten") from marker_error
        save_json(
            directory / "stop-result.json", {"utc": timestamp(), "charging": disabled}
        )
    return disabled


def prepare_trial(
    hat, directory, control_lock, quiet=require_quiet, observe_regulation=False
):
    if type(observe_regulation) is not bool:
        raise ChargeStopped("invalid_regulation_mode")
    quiet()
    original = read_configuration(hat)
    expected = expected_configuration(original)
    save_json(directory / "original.json", original, exclusive=True)
    try:
        with locked(control_lock) as control:
            if (directory / "stop-requested.json").exists():
                raise ChargeStopped("stop_requested")
            clear_permit(control)
            disable_charging(hat)
            for method, argument in (
                ("SetCustomBatteryProfile", expected["profile"]),
                ("SetCustomBatteryExtProfile", expected["extended"]),
                ("SetBatteryTempSenseConfig", "NTC"),
            ):
                ok(getattr(hat.config, method)(argument))
                if value(hat.config.GetChargingConfig()) != {"charging_enabled": False}:
                    raise ChargeStopped("charging_state_changed_during_prepare")
            ok(hat.config.SetPowerInputsConfig(expected["inputs"], non_volatile=True))
            final = read_configuration(hat)
            verify_configuration(final, expected, False)
            save_json(
                directory / "prepared.json",
                {
                    "utc": timestamp(),
                    "status": "prepared_charging_disabled",
                    "configuration": final,
                    "expected": expected,
                    "duration_seconds": SESSION_SECONDS,
                    "observe_regulation": observe_regulation,
                    "limitations": LIMITATIONS,
                },
                exclusive=True,
            )
            grant_permit(control, directory)
    except BaseException:
        disable_charging(hat)
        raise
    return expected


def load_prepared(directory):
    original = json.loads((directory / "original.json").read_text())
    prepared = json.loads((directory / "prepared.json").read_text())
    expected = expected_configuration(original)
    if (
        prepared.get("expected") != expected
        or prepared.get("duration_seconds") != SESSION_SECONDS
        or prepared.get("status") != "prepared_charging_disabled"
        or type(prepared.get("observe_regulation")) is not bool
    ):
        raise ChargeStopped("prepared_configuration_invalid")
    return expected, prepared["observe_regulation"]


def run_trial(hat, directory, control_lock, quiet=require_quiet):
    started = None
    outcome = "interrupted"
    charge_observed = False
    observe_regulation = None
    regulation_target_observed = False
    try:
        expected, observe_regulation = load_prepared(directory)
        save_json(directory / "run-claimed.json", {"utc": timestamp()}, exclusive=True)
        quiet()
        first = observe(hat)
        initial_faults = value(first["raw"]["GetFaultStatus"])
        if validate_observation(first, expected, False, initial_faults):
            raise ChargeStopped("already_at_trial_voltage")
        save_json(directory / "baseline.json", first, exclusive=True)
        with (directory / "samples.jsonl").open("x") as stream:
            os.fchmod(stream.fileno(), 0o600)
            with locked(control_lock) as control:
                if (directory / "stop-requested.json").exists():
                    raise ChargeStopped("stop_requested")
                consume_permit(control, directory)
                started = time.monotonic()
                set_charging(hat, True, persistent=False)
            previous = started
            while True:
                now = time.monotonic()
                if now - started >= SESSION_SECONDS:
                    outcome = "completed_time_limit"
                    break
                if now < previous or now - previous > 10:
                    raise ChargeStopped("observation_gap")
                if (directory / "stop-requested.json").exists():
                    raise ChargeStopped("stop_requested")
                quiet()
                row = observe(hat)
                stream.write(json.dumps(row, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                reached_target = validate_observation(
                    row, expected, True, initial_faults
                )
                charge_observed |= (
                    value(row["raw"]["GetStatus"]).get("battery") == "CHARGING_FROM_IN"
                )
                previous = row["monotonic"]
                regulation_target_observed |= reached_target
                if reached_target and not observe_regulation:
                    outcome = "reached_trial_voltage"
                    break
                remaining = SESSION_SECONDS - (time.monotonic() - started)
                time.sleep(max(0, min(INTERVAL_SECONDS, remaining)))
    except Exception as error:
        outcome = f"stopped:{type(error).__name__}:{error}"
    finally:
        stopped = stop_trial(hat, directory, control_lock)
        save_json(
            directory / "result.json",
            {
                "utc": timestamp(),
                "outcome": outcome,
                "enable_attempted": started is not None,
                "charge_status_observed": charge_observed,
                "observe_regulation": observe_regulation,
                "regulation_target_observed": regulation_target_observed,
                "elapsed_since_enable_attempt_seconds": None
                if started is None
                else time.monotonic() - started,
                "charging": stopped,
                "limitations": LIMITATIONS,
            },
        )
    return outcome


def interrupted(*_):
    raise ChargeStopped("signal")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "stop"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observe-regulation", action="store_true")
    args = parser.parse_args()
    if args.observe_regulation and args.action != "prepare":
        parser.error("--observe-regulation is available only with prepare")
    if os.geteuid() != 0 or not args.output.is_absolute():
        parser.error("root and an absolute private output directory are required")
    os.umask(0o077)
    if args.output.is_symlink():
        parser.error("output must not be a symlink")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.output, 0o700)
    from pijuice import PiJuice

    hat = PiJuice(1, 0x14)
    control_lock = Path("/run/pi-charge-control.lock")
    if args.action == "stop":
        stop_trial(hat, args.output, control_lock)
        return 0
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    with locked(Path("/run/pi-charge-test.lock"), blocking=False):
        with locked(Path("/run/pi-battery-test.lock"), blocking=False):
            if args.action == "prepare":
                prepare_trial(
                    hat,
                    args.output,
                    control_lock,
                    observe_regulation=args.observe_regulation,
                )
                return 0
            outcome = run_trial(hat, args.output, control_lock)
    return 0 if outcome in ("completed_time_limit", "reached_trial_voltage") else 1


if __name__ == "__main__":
    raise SystemExit(main())
