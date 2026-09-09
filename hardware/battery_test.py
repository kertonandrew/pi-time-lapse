"""Bounded discharge test; never enables charging or changes a battery profile."""

import argparse
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


METHODS = (
    "GetStatus",
    "GetBatteryVoltage",
    "GetBatteryTemperature",
    "GetBatteryCurrent",
    "GetIoVoltage",
    "GetIoCurrent",
)


class TestStopped(RuntimeError):
    """The next workload is not eligible to run."""


def value(response):
    if not isinstance(response, dict) or response.get("error") != "NO_ERROR":
        raise TestStopped("sensor_error")
    return response["data"]


def validate_sample(raw, charging, cpu_temperature):
    values = {method: value(raw[method]) for method in METHODS}
    if value(charging).get("charging_enabled") is not False:
        raise TestStopped("charging_not_disabled")
    status = values["GetStatus"]
    if status.get("battery") != "NORMAL":
        raise TestStopped("battery_not_normal")
    for method in METHODS[1:]:
        reading = values[method]
        if type(reading) not in (int, float) or not math.isfinite(reading):
            raise TestStopped("invalid_reading")
    if not 3800 <= values["GetBatteryVoltage"] <= 4200:
        raise TestStopped("battery_voltage_limit")
    if not 10 <= values["GetBatteryTemperature"] <= 35:
        raise TestStopped("reported_temperature_limit")
    if not 4800 <= values["GetIoVoltage"] <= 5250:
        raise TestStopped("pi_rail_voltage_limit")
    if not math.isfinite(cpu_temperature) or not 0 <= cpu_temperature < 70:
        raise TestStopped("cpu_temperature_limit")
    if status.get("powerInput5vIo") != "NOT_PRESENT":
        raise TestStopped("gpio_external_input")
    source = status.get("powerInput")
    if source not in ("PRESENT", "NOT_PRESENT"):
        raise TestStopped("ambiguous_usb_input")
    return source == "NOT_PRESENT"


def rail_quality(raw):
    flags = []
    for method, low, high, flag in (
        ("GetIoCurrent", 0, 1000, "pi_rail_current_out_of_expected_range"),
        ("GetIoVoltage", 4800, 5250, "pi_rail_voltage_out_of_expected_range"),
    ):
        response = raw[method]
        reading = response.get("data") if isinstance(response, dict) else None
        if (
            not isinstance(response, dict)
            or response.get("error") != "NO_ERROR"
            or type(reading) not in (int, float)
            or not math.isfinite(reading)
            or not low <= reading <= high
        ):
            flags.append(flag)
    return flags


def phase(elapsed):
    if elapsed < 120:
        return "battery_idle"
    if elapsed < 150:
        return "battery_cpu"
    if elapsed < 300:
        return "battery_recovery"
    return "complete"


def charge_level_percent(response):
    data = value(response)
    if (
        not isinstance(data, list)
        or len(data) != 2
        or any(type(byte) is not int or not 0 <= byte <= 255 for byte in data)
    ):
        raise TestStopped("invalid_charge_level")
    reading = (data[0] + (data[1] << 8)) / 10
    if not 0 <= reading <= 100:
        raise TestStopped("invalid_charge_level")
    return reading


def set_countdown(hat, seconds):
    response = hat.power.SetPowerOff(seconds)
    if response.get("error") != "NO_ERROR":
        raise TestStopped("power_cut_write_failed")
    data = value(hat.power.GetPowerOff())
    if not isinstance(data, list) or len(data) != 1:
        raise TestStopped("power_cut_readback_invalid")
    if type(data[0]) is not int:
        raise TestStopped("power_cut_readback_invalid")
    if seconds == 255:
        valid = data[0] == 255
    else:
        valid = seconds - 5 <= data[0] <= seconds
    if not valid:
        raise TestStopped("power_cut_readback_mismatch")


def stop_worker(worker):
    if worker is not None and worker.poll() is None:
        try:
            os.killpg(worker.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            worker.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            worker.wait(timeout=3)


def record(stream, data):
    stream.write(json.dumps(data, allow_nan=False) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def cleanup_test(hat, directory):
    marker = directory / "armed"
    if marker.exists() and json.loads(marker.read_text())["boot_id"] == boot_id():
        finish_power(hat, directory)


def run_test(hat, directory, maximum_wait=900):
    worker = None
    started = None
    armed = False
    terminal = "interrupted"
    began = time.monotonic()
    refreshed = 0.0
    last_phase = None
    consecutive_bad_current = 0
    try:
        if value(hat.config.GetChargingConfig()).get("charging_enabled") is not False:
            raise TestStopped("charging_not_disabled")
        if value(hat.power.GetPowerOff()) != [255]:
            raise TestStopped("existing_power_cut_countdown")
        (directory / "armed").write_text(json.dumps({"boot_id": boot_id()}))
        armed = True
        set_countdown(hat, 120)
        refreshed = time.monotonic()
        with (directory / "samples.jsonl").open("x") as stream:
            while True:
                before = time.monotonic()
                raw = {method: getattr(hat.status, method)() for method in METHODS}
                charge_level = hat.interface.ReadData(0x42, 2)
                charging = hat.config.GetChargingConfig()
                cpu = (
                    float(Path("/sys/class/thermal/thermal_zone0/temp").read_text())
                    / 1000
                )
                now = time.monotonic()
                quality_flags = rail_quality(raw)
                row = {
                    "utc": datetime.now(timezone.utc).isoformat(),
                    "monotonic": now,
                    "read_duration_seconds": now - before,
                    "phase": last_phase or "waiting_for_usb_removal",
                    "cpu_temperature_c": cpu,
                    "raw": raw,
                    "charging": charging,
                    "charge_level_register": charge_level,
                    "quality_flags": quality_flags,
                    "pi_rail_energy_valid": not quality_flags,
                }
                record(stream, row)
                charge_level_percent(charge_level)
                if now - before > 5:
                    raise TestStopped("slow_sensor_read")
                battery_only = validate_sample(raw, charging, cpu)
                current_valid = (
                    "pi_rail_current_out_of_expected_range" not in quality_flags
                )
                consecutive_bad_current = (
                    0 if current_valid else consecutive_bad_current + 1
                )
                if consecutive_bad_current >= 5:
                    raise TestStopped("pi_rail_current_measurement_unreliable")
                if now - refreshed >= 15:
                    set_countdown(hat, 120)
                    refreshed = time.monotonic()
                if started is None:
                    if now - began >= maximum_wait:
                        terminal = "usb_removal_wait_expired"
                        break
                    if not battery_only:
                        time.sleep(2)
                        continue
                    if value(raw["GetBatteryVoltage"]) < 3900:
                        raise TestStopped("insufficient_start_voltage")
                    started = time.monotonic()
                elif not battery_only:
                    terminal = "usb_reconnected"
                    break
                if worker is not None and worker.poll() not in (None, 0):
                    raise TestStopped("cpu_worker_failed")
                current_phase = phase(now - started)
                if current_phase != last_phase:
                    stop_worker(worker)
                    worker = None
                    last_phase = current_phase
                    record(
                        stream,
                        {"event": "phase", "phase": current_phase, "monotonic": now},
                    )
                if current_phase == "battery_cpu" and worker is None and current_valid:
                    worker = subprocess.Popen(
                        [
                            "/usr/bin/timeout",
                            "30",
                            "/usr/bin/nice",
                            "-n",
                            "10",
                            "/usr/bin/python3",
                            str(Path(__file__).resolve()),
                            "cpu",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                if current_phase == "complete":
                    terminal = "complete"
                    break
                time.sleep(2)
    except Exception as error:
        terminal = f"stopped:{type(error).__name__}:{error}"
    finally:
        try:
            stop_worker(worker)
            (directory / "result.json").write_text(
                json.dumps(
                    {
                        "result": terminal,
                        "battery_test_started": started is not None,
                        "charging_enabled_by_test": False,
                        "elapsed_seconds": time.monotonic() - began,
                    },
                    indent=2,
                )
            )
        finally:
            if armed:
                finish_power(hat, directory)
    return terminal


def finish_power(hat, directory):
    try:
        status = value(hat.status.GetStatus())
    except Exception:
        status = {}
    if (
        status.get("powerInput") == "PRESENT"
        or status.get("powerInput5vIo") == "PRESENT"
    ):
        set_countdown(hat, 255)
        (directory / "armed").unlink(missing_ok=True)
        return
    try:
        set_countdown(hat, 60)
    except Exception as error:
        try:
            (directory / "power-cut-error.txt").write_text(str(error))
        except OSError:
            pass
    os.sync()
    subprocess.run(["/sbin/shutdown", "-h", "now"], check=True, timeout=10)


def interrupted(*_):
    raise TestStopped("signal")


def cpu_workload():
    deadline = time.monotonic() + 28
    data = bytes(65536)
    while time.monotonic() < deadline:
        hashlib.sha256(data).digest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "cleanup", "cpu"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "cpu":
        cpu_workload()
        return 0
    if os.geteuid() != 0 or args.output is None or not args.output.is_absolute():
        parser.error("run/cleanup require root and an absolute output directory")
    from pijuice import PiJuice

    hat = PiJuice(1, 0x14)
    os.umask(0o077)
    with Path("/run/pi-battery-test.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "cleanup":
            cleanup_test(hat, args.output)
            return 0
        args.output.mkdir(parents=True, exist_ok=False)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, interrupted)
        result = run_test(hat, args.output)
    return (
        0
        if result in ("complete", "usb_reconnected", "usb_removal_wait_expired")
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
