"""Run a bounded capture experiment with battery fallback and charging disabled."""

import argparse
import fcntl
import json
import math
import os
import queue
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from timelapse.capture import capture
from timelapse.config import load_config
from timelapse.spool import Spool, fsync_directory


MAX_SAMPLES = 512
MAX_RECORD_BYTES = 1048576
MAX_GAP_SECONDS = 2.5
USB_STATES = ("PRESENT", "WEAK", "BAD", "NOT_PRESENT")
LIMITATIONS = [
    "Supervised battery-fallback trial; the battery profile remains unverified.",
    "HAT-to-Pi rail energy is provisional and excludes PiJuice conversion losses.",
    "Battery current is a firmware estimate, not calibrated battery energy.",
    "Controller USB input watts, solar watts and efficiency are unavailable.",
    "Sampling and software add overhead; brief power peaks can be missed.",
]


class TrialRejected(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def parse_deadline(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("Deadline must include the UTC offset or Z")
    if not 2024 <= parsed.year <= 2099:
        raise ValueError("Deadline year is invalid")
    return parsed


def write_record(path, record):
    payload = json.dumps(record, allow_nan=False, sort_keys=True).encode() + b"\n"
    if len(payload) > MAX_RECORD_BYTES:
        raise RuntimeError("Trial record exceeds one MiB")
    descriptor, name = tempfile.mkstemp(prefix=".trial-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def read_sample(device, clock=time.monotonic, now=utc_now):
    started = clock()
    raw, errors, values = {}, [], {}
    for field, owner, method, numeric in (
        ("status", device.status, "GetStatus", False),
        ("charging", device.config, "GetChargingConfig", False),
        ("battery_voltage_mv", device.status, "GetBatteryVoltage", True),
        ("battery_current_estimate_ma", device.status, "GetBatteryCurrent", True),
        ("io_voltage_mv", device.status, "GetIoVoltage", True),
        ("io_current_ma", device.status, "GetIoCurrent", True),
    ):
        try:
            reply = getattr(owner, method)()
            encoded = json.dumps(reply, allow_nan=False)
            if len(encoded) > 4096:
                raise ValueError("Oversized reply")
            raw[method] = reply
            if not isinstance(reply, dict) or reply.get("error") != "NO_ERROR":
                raise ValueError("Unsuccessful or missing API error status")
            value = reply.get("data")
            if not (finite(value) if numeric else isinstance(value, dict)):
                raise ValueError("Invalid API data")
            values[field] = value
        except Exception as error:
            values[field] = None
            errors.append(f"{method}: {type(error).__name__}: {error}"[:200])
    finished = clock()
    voltage, current = values["io_voltage_mv"], values["io_current_ma"]
    values.update(
        timestamp_utc=now().isoformat(),
        monotonic_seconds=(started + finished) / 2,
        read_duration_seconds=finished - started,
        io_power_w=voltage * current / 1000000
        if voltage is not None and current is not None
        else None,
        errors=errors,
        raw=raw,
    )
    try:
        guard(values)
        values["source_and_battery_valid"] = True
    except TrialRejected as error:
        values["source_and_battery_valid"] = False
        values["invalid_reason"] = str(error)
    return values


def guard(sample):
    if not isinstance(sample.get("errors"), list) or sample["errors"]:
        raise TrialRejected("Live hardware readings contain errors")
    status, charging = sample.get("status"), sample.get("charging")
    if not isinstance(status, dict) or not isinstance(charging, dict):
        raise TrialRejected("Live status or charging configuration is missing")
    if status.get("powerInput") not in USB_STATES:
        raise TrialRejected("PiJuice USB input state is unknown")
    if status.get("powerInput5vIo") != "NOT_PRESENT":
        raise TrialRejected("A direct Pi/GPIO supply is present or unknown")
    if charging.get("charging_enabled") is not False:
        raise TrialRejected("Charging is enabled or its state is unknown")
    if status.get("battery") not in ("NORMAL", "NOT_PRESENT"):
        raise TrialRejected("Battery state is charging, abnormal or unknown")
    if status["battery"] == "NORMAL":
        voltage = sample.get("battery_voltage_mv")
        if not finite(voltage) or not 3800 <= voltage <= 4250:
            raise TrialRejected(
                "Battery voltage is outside the 3.80–4.25 V trial reserve"
            )
    elif status["powerInput"] != "PRESENT":
        raise TrialRejected("Without a battery, PiJuice USB input must be PRESENT")
    if not finite(sample.get("io_power_w")) or sample["io_power_w"] < 0:
        raise TrialRejected("HAT-to-Pi power is missing or reversed")
    voltage = sample.get("io_voltage_mv")
    if not finite(voltage) or not 4800 <= voltage <= 5250:
        raise TrialRejected("HAT-to-Pi voltage is outside the 4.80–5.25 V trial range")


class Sampler:
    def __init__(self, device, interval, reader=read_sample):
        self.device = device
        self.interval = interval
        self.reader = reader
        self.samples = []
        self.requests = queue.Queue()
        self.failure = None
        self.abort_reason = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        next_read = time.monotonic()
        try:
            while True:
                try:
                    request = self.requests.get(
                        timeout=max(0, next_read - time.monotonic())
                    )
                    if request is None:
                        return
                except queue.Empty:
                    request = False
                if len(self.samples) >= MAX_SAMPLES:
                    raise RuntimeError("Trial sample limit reached")
                sample = self.reader(self.device)
                self.samples.append(sample)
                if request is not False:
                    request.put(sample)
                next_read = time.monotonic() + self.interval
        except Exception as error:
            self.failure = f"{type(error).__name__}: {error}"[:200]

    def snapshot(self):
        response = queue.Queue(maxsize=1)
        self.requests.put(response)
        try:
            return response.get(timeout=8)
        except queue.Empty as error:
            raise TrialRejected("Live sampler did not respond") from error

    def stop(self):
        self.requests.put(None)
        self.thread.join(timeout=8)
        if self.thread.is_alive():
            self.failure = "Live sampler did not stop"

    def cancelled(self):
        if self.abort_reason is not None:
            return True
        try:
            if self.failure:
                raise TrialRejected(self.failure)
            rows = list(self.samples)
            if not rows:
                raise TrialRejected("No live samples are available")
            for sample in rows:
                guard(sample)
                if sample.get("source_and_battery_valid") is not True:
                    raise TrialRejected("Source or battery validity was lost")
                duration = sample.get("read_duration_seconds")
                if not finite(duration) or not 0 <= duration <= MAX_GAP_SECONDS:
                    raise TrialRejected(
                        "Hardware read duration is invalid or excessive"
                    )
            if any(
                not 0
                < right["monotonic_seconds"] - left["monotonic_seconds"]
                <= MAX_GAP_SECONDS
                for left, right in zip(rows, rows[1:])
            ):
                raise TrialRejected("Hardware sampling continuity was lost")
            age = time.monotonic() - rows[-1]["monotonic_seconds"]
            if not 0 <= age <= MAX_GAP_SECONDS:
                raise TrialRejected("Most recent hardware sample is stale")
        except (TrialRejected, KeyError, TypeError) as error:
            self.abort_reason = str(error)
            return True
        return False


def integrate(samples, start, end):
    energy = 0.0
    for left, right in zip(samples, samples[1:]):
        a, b = left["monotonic_seconds"], right["monotonic_seconds"]
        low, high = max(start, a), min(end, b)
        if high <= low:
            continue
        p, q = left["io_power_w"], right["io_power_w"]
        low_power = p + (q - p) * (low - a) / (b - a)
        high_power = p + (q - p) * (high - a) / (b - a)
        energy += (low_power + high_power) / 2 * (high - low)
    return energy / 3600


def summarize(samples, baseline_start, capture_start, capture_end, post_end):
    invalid = {"valid": False, "reason": None}
    if not baseline_start < capture_start < capture_end < post_end:
        return dict(invalid, reason="Measurement intervals are incomplete")
    if (
        len(samples) < 4
        or samples[0]["monotonic_seconds"] > baseline_start
        or samples[-1]["monotonic_seconds"] < post_end
    ):
        return dict(invalid, reason="Samples do not cover all measurement intervals")
    for sample in samples:
        try:
            guard(sample)
        except TrialRejected as error:
            return dict(invalid, reason=str(error))
        if (
            sample.get("errors")
            or sample.get("source_and_battery_valid") is not True
            or not finite(sample.get("io_power_w"))
            or not finite(sample.get("read_duration_seconds"))
            or sample["read_duration_seconds"] > MAX_GAP_SECONDS
        ):
            return dict(invalid, reason="Source, battery or sensor validity was lost")
    source_states = sorted({sample["status"]["powerInput"] for sample in samples})
    source = {
        "usb_source_state": source_states[0] if len(source_states) == 1 else "MIXED",
        "usb_source_states": source_states,
    }
    if len(source_states) != 1:
        return dict(
            invalid,
            **source,
            reason="USB source state changed during the measurement window",
        )
    if any(
        not 0
        < right["monotonic_seconds"] - left["monotonic_seconds"]
        <= MAX_GAP_SECONDS
        for left, right in zip(samples, samples[1:])
    ):
        return dict(invalid, reason="Sampling gap exceeds 2.5 seconds or time reversed")
    pre_w = (
        integrate(samples, baseline_start, capture_start)
        * 3600
        / (capture_start - baseline_start)
    )
    post_w = integrate(samples, capture_end, post_end) * 3600 / (post_end - capture_end)
    baseline_w = (pre_w + post_w) / 2
    capture_wh = integrate(samples, capture_start, capture_end)
    return {
        "valid": True,
        "provisional": True,
        **source,
        "pre_idle_mean_w": pre_w,
        "post_idle_mean_w": post_w,
        "baseline_mean_w": baseline_w,
        "peak_observed_window_w": max(sample["io_power_w"] for sample in samples),
        "total_window_wh": integrate(samples, baseline_start, post_end),
        "capture_total_wh": capture_wh,
        "capture_incremental_wh": capture_wh
        - baseline_w * (capture_end - capture_start) / 3600,
        "capture_interval_seconds": capture_end - capture_start,
    }


def execute_attempt(record, options, device, config, capture_fn=capture):
    required_seconds = options.baseline_seconds * 2 + 55
    if utc_now() + timedelta(seconds=required_seconds) >= options.deadline:
        raise TrialRejected("Deadline leaves insufficient time for a bounded trial")
    initial = read_sample(device)
    record["initial_guard"] = initial
    guard(initial)
    sampler = Sampler(device, options.interval_seconds)
    sampler.thread.start()
    try:
        first = sampler.snapshot()
        guard(first)
        baseline_start = first["monotonic_seconds"]
        time.sleep(options.baseline_seconds)
        before_capture = sampler.snapshot()
        guard(before_capture)
        if any(not sample["source_and_battery_valid"] for sample in sampler.samples):
            raise TrialRejected("Hardware validity was lost during the baseline")
        if (
            utc_now() + timedelta(seconds=options.baseline_seconds + 50)
            >= options.deadline
        ):
            raise TrialRejected("Deadline leaves insufficient time for capture")
        camera = dict(config["camera"])
        camera["timeout_seconds"] = min(camera["timeout_seconds"], 45)
        spool = Spool(
            Path(config["spool"]), config["max_spool_bytes"], config["min_free_bytes"]
        )
        capture_start = time.monotonic()
        record["capture_start_monotonic_seconds"] = capture_start
        try:
            record["capture"] = capture_fn(
                spool,
                time_source=record["time_source"],
                cancelled=sampler.cancelled,
                lock_timeout_seconds=3,
                **camera,
            )
            record["status"] = "captured"
        finally:
            capture_end = time.monotonic()
            record["capture_end_monotonic_seconds"] = capture_end
            time.sleep(options.baseline_seconds)
            final = sampler.snapshot()
            record["energy"] = summarize(
                sampler.samples,
                baseline_start,
                capture_start,
                capture_end,
                final["monotonic_seconds"],
            )
    finally:
        sampler.stop()
        record["samples"] = list(sampler.samples)
        if sampler.failure:
            record["sampler_failure"] = sampler.failure
            record["energy"] = {"valid": False, "reason": sampler.failure}
        if sampler.abort_reason:
            record["capture_abort_reason"] = sampler.abort_reason
            record["energy"] = {"valid": False, "reason": sampler.abort_reason}


def run_trial(options, device_factory=None, capture_fn=capture):
    output = options.output_dir
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / ".trial.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "refused", "reason": "Another trial is running"}
        existing = list(output.glob("attempt-*.json"))
        if len(existing) >= options.max_attempts:
            return {"status": "refused", "reason": "Trial attempt limit reached"}
        attempt = len(existing) + 1
        path = output / f"attempt-{attempt:04d}.json"
        if path.exists():
            raise RuntimeError("Trial attempt records are not contiguous")
        record = {
            "mode": "supervised_battery_fallback_capture_trial",
            "policy_revision": 2,
            "status": "started",
            "attempt": attempt,
            "started_at_utc": utc_now().isoformat(),
            "deadline_utc": options.deadline.isoformat(),
            "time_source": "NTP"
            if Path("/run/systemd/timesync/synchronized").exists()
            else "UNSYNC",
            "boot_id": None,
            "samples": [],
            "energy": {"valid": False, "reason": "No completed measurement"},
            "limitations": LIMITATIONS,
        }
        write_record(path, record)
        try:
            if utc_now() >= options.deadline:
                raise TrialRejected("Trial deadline has passed")
            record["boot_id"] = (
                Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            )
            config = load_config(options.config)
            if device_factory is None:
                from pijuice import PiJuice

                device = PiJuice(1, 0x14)
            else:
                device = device_factory()
            execute_attempt(record, options, device, config, capture_fn)
        except TrialRejected as error:
            record.update(status="rejected", reason=str(error))
        except Exception as error:
            record.update(
                status="failed", reason=f"{type(error).__name__}: {error}"[:300]
            )
        finally:
            if record["status"] != "captured":
                record["energy"] = {
                    "valid": False,
                    "reason": "Capture was not completed",
                }
            record["finished_at_utc"] = utc_now().isoformat()
            write_record(path, record)
        return {
            "record": str(path),
            "status": record["status"],
            "energy": record["energy"],
            "reason": record.get("reason"),
            "capture": record.get("capture"),
        }


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/pi-timelapse.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--deadline", type=parse_deadline, required=True)
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument("--baseline-seconds", type=float, default=20)
    parser.add_argument("--interval-seconds", type=float, default=1)
    result = parser.parse_args(argv)
    if not result.output_dir.is_absolute():
        parser.error("--output-dir must be absolute")
    if not 1 <= result.max_attempts <= 6:
        parser.error("--max-attempts must be between one and six")
    if not finite(result.baseline_seconds) or not 5 <= result.baseline_seconds <= 60:
        parser.error("--baseline-seconds must be between five and sixty")
    if not finite(result.interval_seconds) or not 0.5 <= result.interval_seconds <= 2:
        parser.error("--interval-seconds must be between 0.5 and two")
    return result


def main(argv=None):
    os.umask(0o077)
    result = run_trial(arguments(argv))
    print(json.dumps(result, allow_nan=False, sort_keys=True), flush=True)
    return 0 if result["status"] in ("captured", "rejected", "refused") else 1


if __name__ == "__main__":
    raise SystemExit(main())
