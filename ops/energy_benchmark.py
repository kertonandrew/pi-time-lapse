"""Measure foreground commands on the USB-powered HAT-to-Pi rail with charging off.

Direct mode supervises the command and descendants in its process group.
New sessions and daemonizing workloads are unsupported in direct mode and
require an external cgroup supervisor.
"""

import argparse
import json
import math
import os
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


MAX_SAMPLES = 50000
PROCESS_POLL_SECONDS = 0.02
GUARD_INTERVAL_SECONDS = 1.0
MAX_GUARD_GAP_SECONDS = 2.5
LIMITATIONS = [
    "Direct mode supports foreground commands and supervises only the command's process group.",
    "New sessions and daemonizing workloads are unsupported in direct mode and require an external cgroup supervisor.",
    "PiJuice HAT-to-Pi rail readings are provisional, uncalibrated measurements.",
    "This excludes controller input energy, conversion losses and battery energy.",
    "Sampling, CPU-temperature reads and process supervision add overhead.",
    "Sub-sample transients can be missed; observed process exit includes polling delay.",
    "Incremental energy is an estimate using the preceding idle baseline.",
    "Sensor readings average an unknown sampling cadence; the command-plus-recovery estimate is preferable to cutting energy at process exit.",
    "The current ceiling is a configurable plausibility heuristic, not calibration.",
    "Child stdout and stderr are discarded; command success means exit status zero.",
]


class BenchmarkRejected(RuntimeError):
    pass


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def api_read(owner, method, raw, errors):
    try:
        reply = getattr(owner, method)()
        try:
            json.dumps(reply, allow_nan=False)
            raw[method] = reply
        except (TypeError, ValueError):
            raw[method] = {"unserializable_reply": repr(reply)}
            raise ValueError("API reply is not finite JSON")
        if not isinstance(reply, dict) or reply.get("error") != "NO_ERROR":
            raise ValueError("API did not report NO_ERROR")
        return reply.get("data")
    except Exception as error:
        errors.append(f"{method}: {type(error).__name__}: {error}")
        return None


def read_power(device, clock=time.monotonic):
    started = clock()
    raw, errors = {}, []
    voltage = api_read(device.status, "GetIoVoltage", raw, errors)
    current = api_read(device.status, "GetIoCurrent", raw, errors)
    finished = clock()
    return {
        "monotonic_seconds": (started + finished) / 2,
        "read_started_seconds": started,
        "read_finished_seconds": finished,
        "read_duration_seconds": finished - started,
        "io_voltage_mv": voltage,
        "io_current_ma": current,
        "io_power_w": voltage * current / 1000000
        if finite(voltage) and finite(current)
        else None,
        "raw": raw,
        "errors": errors,
    }


def read_guard(device, cpu_temperature_path, clock=time.monotonic):
    started = clock()
    raw, errors = {}, []
    status = api_read(device.status, "GetStatus", raw, errors)
    charging = api_read(device.config, "GetChargingConfig", raw, errors)
    temperature = None
    try:
        raw["cpu_temperature"] = Path(cpu_temperature_path).read_text().strip()
        temperature = float(raw["cpu_temperature"]) / 1000
    except Exception as error:
        errors.append(f"CPU temperature: {type(error).__name__}: {error}")
    finished = clock()
    return {
        "monotonic_seconds": (started + finished) / 2,
        "read_started_seconds": started,
        "read_finished_seconds": finished,
        "read_duration_seconds": finished - started,
        "status": status,
        "charging": charging,
        "cpu_temperature_c": temperature if finite(temperature) else None,
        "raw": raw,
        "errors": errors,
    }


def validate_power(row, max_current_a=1.0):
    if not isinstance(row.get("errors"), list) or row["errors"]:
        raise BenchmarkRejected("Rail readings contain API errors")
    voltage, current = row.get("io_voltage_mv"), row.get("io_current_ma")
    if not finite(voltage) or not 4800 <= voltage <= 5500:
        raise BenchmarkRejected("Pi rail voltage is outside 4.80–5.50 V")
    if not finite(current) or not 0 <= current <= max_current_a * 1000:
        raise BenchmarkRejected(
            "Pi rail current fails the configured plausibility range"
        )
    power = row.get("io_power_w")
    if not finite(power) or not math.isclose(power, voltage * current / 1000000):
        raise BenchmarkRejected("Pi rail power is missing or inconsistent")


def validate_guard(row):
    if not isinstance(row.get("errors"), list) or row["errors"]:
        raise BenchmarkRejected("Source or temperature guard contains read errors")
    status, charging = row.get("status"), row.get("charging")
    if not isinstance(status, dict) or status.get("powerInput") != "PRESENT":
        raise BenchmarkRejected("PiJuice HAT USB input is not PRESENT")
    if status.get("powerInput5vIo") != "NOT_PRESENT":
        raise BenchmarkRejected("An external Pi/GPIO supply is present or unknown")
    if not isinstance(charging, dict) or charging.get("charging_enabled") is not False:
        raise BenchmarkRejected("Charging must remain explicitly disabled")
    if status.get("battery") not in ("NORMAL", "NOT_PRESENT"):
        raise BenchmarkRejected("Battery status reports charging or is unknown")
    temperature = row.get("cpu_temperature_c")
    if not finite(temperature) or not -40 <= temperature <= 70:
        raise BenchmarkRejected("CPU temperature is unavailable or outside -40–70 C")


def validate_timing(rows, max_gap, validator):
    if not rows:
        raise BenchmarkRejected("No measurement rows are available")
    previous = None
    for row in rows:
        validator(row)
        timestamp, duration = (
            row.get("monotonic_seconds"),
            row.get("read_duration_seconds"),
        )
        if (
            not finite(timestamp)
            or not finite(duration)
            or not 0 <= duration <= max_gap
        ):
            raise BenchmarkRejected(
                "Measurement timing is invalid or its read was too slow"
            )
        if previous is not None and not 0 < timestamp - previous <= max_gap:
            raise BenchmarkRejected(
                "Measurement coverage has a gap or non-increasing time"
            )
        previous = timestamp


def integrate(samples, start, end, max_gap_seconds, max_current_a=1.0):
    """Return joules using linear interpolation at both exact interval boundaries."""
    if not finite(start) or not finite(end) or start >= end:
        raise BenchmarkRejected("Energy interval is invalid")
    validate_timing(
        samples, max_gap_seconds, lambda row: validate_power(row, max_current_a)
    )
    if (
        len(samples) < 2
        or samples[0]["monotonic_seconds"] > start
        or samples[-1]["monotonic_seconds"] < end
    ):
        raise BenchmarkRejected("Samples do not bracket the complete energy interval")
    energy = 0.0
    for left, right in zip(samples, samples[1:]):
        a, b = left["monotonic_seconds"], right["monotonic_seconds"]
        low, high = max(start, a), min(end, b)
        if high <= low:
            continue
        p, q = left["io_power_w"], right["io_power_w"]
        low_power = p + (q - p) * (low - a) / (b - a)
        high_power = p + (q - p) * (high - a) / (b - a)
        energy += (low_power + high_power) * (high - low) / 2
    return energy


def coverage(rows):
    timestamps = [row.get("monotonic_seconds") for row in rows]
    gaps = [
        b - a for a, b in zip(timestamps, timestamps[1:]) if finite(a) and finite(b)
    ]
    durations = [
        row["read_duration_seconds"]
        for row in rows
        if finite(row.get("read_duration_seconds"))
    ]
    return {
        "count": len(rows),
        "first_monotonic_seconds": timestamps[0] if timestamps else None,
        "last_monotonic_seconds": timestamps[-1] if timestamps else None,
        "max_gap_seconds": max(gaps, default=None),
        "max_read_duration_seconds": max(durations, default=None),
    }


def summarize(trial, max_gap_seconds, max_current_a=1.0):
    samples, guards = trial["samples"], trial["guards"]
    result = {
        "valid": False,
        "sample_coverage": coverage(samples),
        "guard_coverage": coverage(guards),
    }
    try:
        if trial.get("status") != "complete" or trial.get("returncode") != 0:
            raise BenchmarkRejected(
                trial.get("reason") or "Command did not complete successfully"
            )
        start, command_start, command_end, end = (
            trial.get(name)
            for name in ("baseline_start", "command_start", "command_end", "post_end")
        )
        if (
            not all(finite(value) for value in (start, command_start, command_end, end))
            or not start < command_start < command_end < end
        ):
            raise BenchmarkRejected("Measurement phases are incomplete")
        validate_timing(guards, MAX_GUARD_GAP_SECONDS, validate_guard)
        if (
            guards[0]["monotonic_seconds"] > start
            or guards[-1]["monotonic_seconds"] < end
        ):
            raise BenchmarkRejected(
                "Source guards do not bracket all measurement phases"
            )
        idle = integrate(samples, start, command_start, max_gap_seconds, max_current_a)
        command = integrate(
            samples, command_start, command_end, max_gap_seconds, max_current_a
        )
        post = integrate(samples, command_end, end, max_gap_seconds, max_current_a)
        baseline_w = idle / (command_start - start)
        duration = command_end - command_start
        incremental = command - baseline_w * duration
        result.update(
            valid=True,
            baseline_mean_w=baseline_w,
            post_idle_mean_w=post / (end - command_end),
            command_duration_seconds=duration,
            command_total_j=command,
            command_total_wh=command / 3600,
            command_incremental_estimate_j=incremental,
            command_incremental_estimate_wh=incremental / 3600,
            command_plus_post_total_j=command + post,
            command_plus_recovery_incremental_estimate_j=command
            + post
            - baseline_w * (end - command_start),
            recovery_duration_seconds=end - command_end,
            whole_window_j=idle + command + post,
            peak_observed_w=max(row["io_power_w"] for row in samples),
        )
    except BenchmarkRejected as error:
        result["reason"] = str(error)
    return result


class Sampler:
    def __init__(
        self, device, sample_hz, max_current_a, cpu_temperature_path, max_gap_seconds
    ):
        self.device = device
        self.interval = 1 / sample_hz
        self.max_current_a = max_current_a
        self.cpu_temperature_path = cpu_temperature_path
        self.max_gap_seconds = max_gap_seconds
        self.samples, self.guards = [], []
        self.failure = None
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _append(self, row, rows, validator, max_gap):
        rows.append(row)
        row["source_guard_index"] = len(self.guards) - 1
        try:
            validate_timing(rows[-2:], max_gap, validator)
            row["valid"] = True
        except BenchmarkRejected as error:
            row["valid"] = False
            row["invalid_reason"] = str(error)
            raise

    def _run(self):
        next_power = next_guard = time.monotonic()
        try:
            while not self.stopping.is_set():
                now = time.monotonic()
                if now >= next_guard:
                    self._append(
                        read_guard(self.device, self.cpu_temperature_path),
                        self.guards,
                        validate_guard,
                        MAX_GUARD_GAP_SECONDS,
                    )
                    next_guard += GUARD_INTERVAL_SECONDS
                if now >= next_power:
                    if len(self.samples) >= MAX_SAMPLES:
                        raise BenchmarkRejected("RAM sample budget exhausted")
                    self._append(
                        read_power(self.device),
                        self.samples,
                        lambda row: validate_power(row, self.max_current_a),
                        self.max_gap_seconds,
                    )
                    next_power += self.interval
                    self.ready.set()
                self.stopping.wait(
                    max(0, min(next_power, next_guard) - time.monotonic())
                )
        except Exception as error:
            self.failure = f"{type(error).__name__}: {error}"
        finally:
            self.ready.set()

    def start(self):
        self.thread.start()
        if not self.ready.wait(MAX_GUARD_GAP_SECONDS):
            raise BenchmarkRejected("Initial hardware reads timed out")
        self.check()

    def check(self):
        if self.failure:
            raise BenchmarkRejected(self.failure)
        now = time.monotonic()
        for rows, gap in (
            (self.samples, self.max_gap_seconds),
            (self.guards, MAX_GUARD_GAP_SECONDS),
        ):
            if not rows or not 0 <= now - rows[-1]["monotonic_seconds"] <= gap:
                raise BenchmarkRejected("Hardware sample or source guard is stale")

    def wait_until(self, deadline):
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(PROCESS_POLL_SECONDS, remaining))

    def bracket(self, boundary):
        deadline = (
            time.monotonic() + max(self.max_gap_seconds, MAX_GUARD_GAP_SECONDS) + 1
        )
        while (
            self.samples[-1]["monotonic_seconds"] < boundary
            or self.guards[-1]["monotonic_seconds"] < boundary
        ):
            self.check()
            if time.monotonic() >= deadline:
                raise BenchmarkRejected("Measurement boundary was not bracketed")
            time.sleep(PROCESS_POLL_SECONDS)
        self.check()

    def stop(self):
        self.stopping.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=MAX_GUARD_GAP_SECONDS)
            if self.thread.is_alive():
                self.failure = "Hardware sampler did not stop"


def terminate_group(process):
    for signum, timeout in ((signal.SIGTERM, 1), (signal.SIGKILL, 2)):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        if signum == signal.SIGTERM:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        break
    process.wait(timeout=2)


def execute_command(command, timeout_seconds, sampler, trial):
    process = None
    trial["command_start"] = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            shell=False,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        trial["pid"] = process.pid
        deadline = trial["command_start"] + timeout_seconds
        while True:
            sampler.check()
            returncode = process.poll()
            if returncode is not None:
                trial["command_end"] = time.monotonic()
                trial["returncode"] = returncode
                if returncode:
                    raise BenchmarkRejected(f"Command exited with status {returncode}")
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    return
                raise BenchmarkRejected(
                    "Command left running descendants in its process group"
                )
            if time.monotonic() >= deadline:
                raise BenchmarkRejected("Command exceeded its timeout")
            time.sleep(PROCESS_POLL_SECONDS)
    finally:
        if process is not None:
            terminate_group(process)
            trial.setdefault("command_end", time.monotonic())
            trial["returncode"] = process.returncode


def run_trial(options, case, number, device):
    trial = {
        "case": case["name"],
        "trial": number,
        "command": case["command"],
        "status": "running",
        "started_at_utc": utc_now(),
    }
    sampler = Sampler(
        device,
        options.sample_hz,
        options.max_current_a,
        options.cpu_temperature_path,
        options.max_gap_seconds,
    )
    try:
        sampler.start()
        trial["baseline_start"] = sampler.samples[0]["monotonic_seconds"]
        sampler.wait_until(trial["baseline_start"] + options.pre_idle_seconds)
        execute_command(case["command"], options.timeout_seconds, sampler, trial)
        trial["post_end"] = trial["command_end"] + options.post_idle_seconds
        sampler.wait_until(trial["post_end"])
        sampler.bracket(trial["post_end"])
        trial["status"] = "complete"
    except (Exception, KeyboardInterrupt) as error:
        trial["status"] = "rejected"
        trial["reason"] = f"{type(error).__name__}: {error}"
    finally:
        sampler.stop()
        if sampler.failure:
            trial["status"] = "rejected"
            trial.setdefault("reason", sampler.failure)
        trial["samples"] = list(sampler.samples)
        trial["guards"] = list(sampler.guards)
        trial["finished_at_utc"] = utc_now()
        trial["energy"] = summarize(
            trial, options.max_gap_seconds, options.max_current_a
        )
        if not trial["energy"]["valid"]:
            trial["status"] = "rejected"
            trial.setdefault("reason", trial["energy"]["reason"])
    return trial


def write_record(path, record):
    descriptor, temporary = tempfile.mkstemp(prefix=".energy-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream, allow_nan=False, indent=2, sort_keys=True)
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


def load_cases(options):
    if options.command is not None:
        cases = [{"name": options.name, "command": options.command}]
    else:
        with options.cases.open() as stream:
            cases = json.load(stream)
        if isinstance(cases, dict):
            cases = cases.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 50:
        raise ValueError("Provide between 1 and 50 command cases")
    names = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("name"), str)
            or not case["name"]
            or case["name"] in names
        ):
            raise ValueError("Each case needs a unique nonempty name")
        names.add(case["name"])
        command = case.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not command[0]
            or any(not isinstance(value, str) or "\x00" in value for value in command)
        ):
            raise ValueError("Each command must be a nonempty JSON argv array")
    return cases


def run(options, device_factory=None):
    path = options.output.resolve()
    if "local" not in path.parts:
        raise ValueError("Output must be inside a private local/ directory")
    if path.exists():
        raise ValueError("Output already exists; choose a new record path")
    cases = load_cases(options)
    expected = (
        len(cases)
        * options.trials
        * (
            options.pre_idle_seconds
            + options.timeout_seconds
            + options.post_idle_seconds
            + 5
        )
        * options.sample_hz
    )
    if expected > MAX_SAMPLES:
        raise ValueError("Maximum run duration exceeds the RAM sample budget")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "mode": "usb_only_command_energy_benchmark",
        "hardware_writes": False,
        "started_at_utc": utc_now(),
        "sample_hz": options.sample_hz,
        "max_gap_seconds": options.max_gap_seconds,
        "guard_interval_seconds": GUARD_INTERVAL_SECONDS,
        "max_guard_gap_seconds": MAX_GUARD_GAP_SECONDS,
        "max_current_a": options.max_current_a,
        "process_poll_seconds": PROCESS_POLL_SECONDS,
        "pre_idle_seconds": options.pre_idle_seconds,
        "post_idle_seconds": options.post_idle_seconds,
        "timeout_seconds": options.timeout_seconds,
        "requested_trials_per_case": options.trials,
        "cases": cases,
        "limitations": LIMITATIONS,
        "status": "running",
        "trials": [],
    }
    try:
        if device_factory is None:
            from pijuice import PiJuice

            def device_factory():
                return PiJuice(1, 0x14)

        device = device_factory()
        for number in range(1, options.trials + 1):
            for case in cases:
                trial = run_trial(options, case, number, device)
                record["trials"].append(trial)
                if trial["status"] != "complete":
                    raise BenchmarkRejected(trial["reason"])
        record["status"] = "complete"
    except (Exception, KeyboardInterrupt) as error:
        record["status"] = "rejected"
        record["reason"] = f"{type(error).__name__}: {error}"
    finally:
        record["finished_at_utc"] = utc_now()
        write_record(path, record)
    return record


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New JSON record inside private local/",
    )
    parser.add_argument("--sample-hz", type=float, default=5)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--pre-idle-seconds", type=float, default=20)
    parser.add_argument("--post-idle-seconds", type=float, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--max-gap-seconds", type=float)
    parser.add_argument("--max-current-a", type=float, default=1)
    parser.add_argument(
        "--cpu-temperature-path",
        type=Path,
        default=Path("/sys/class/thermal/thermal_zone0/temp"),
    )
    parser.add_argument("--name", default="command")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--cases",
        type=Path,
        help="JSON list of objects with name and foreground command argv",
    )
    source.add_argument(
        "--command",
        nargs=argparse.REMAINDER,
        help="Foreground argv; descendants must remain in the command's process group",
    )
    options = parser.parse_args(argv)
    for name, low, high in (
        ("sample_hz", 0.2, 10),
        ("trials", 1, 50),
        ("pre_idle_seconds", 0.1, 600),
        ("post_idle_seconds", 0.1, 600),
        ("timeout_seconds", 0.1, 3600),
        ("max_current_a", 0.01, 10),
    ):
        if (
            not finite(getattr(options, name))
            or not low <= getattr(options, name) <= high
        ):
            parser.error(f"{name} must be between {low} and {high}")
    if options.max_gap_seconds is None:
        options.max_gap_seconds = max(0.3, 2 / options.sample_hz)
    if (
        not finite(options.max_gap_seconds)
        or not 1 / options.sample_hz < options.max_gap_seconds <= 15
    ):
        parser.error(
            "max_gap_seconds must exceed one sample interval and be at most 15"
        )
    return options


def main(argv=None):
    options = parse_args(argv)

    def interrupted(signum, frame):
        raise BenchmarkRejected(f"Benchmark interrupted by signal {signum}")

    previous = {
        signum: signal.signal(signum, interrupted)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        record = run(options)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print(
        json.dumps(
            {
                "status": record["status"],
                "output": str(options.output.resolve()),
                "trials": len(record["trials"]),
                "reason": record.get("reason"),
            }
        )
    )
    return 0 if record["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
