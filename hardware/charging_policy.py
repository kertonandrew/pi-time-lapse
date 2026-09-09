"""Read-only charging advice; this module cannot configure or enable a charger."""

import copy
import math
import time
from collections import deque


DATA_LIMITS = [
    "Advice cannot enable or disable charging and is not a hardware interlock.",
    "HAT temperature must agree with a verified independent cell probe.",
    "Pi output power does not measure USB or panel input power.",
    "Battery current estimates and state of charge cannot establish charge limits.",
    "Firmware effective limits are predictions, not charger register readback.",
]


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _supported(value, minimum, maximum, step):
    return (
        _number(value) and minimum <= value <= maximum and (value - minimum) % step == 0
    )


class ChargingPolicy:
    """Evaluate validated observations without performing hardware operations."""

    def __init__(self, config: dict):
        self.config = copy.deepcopy(config) if isinstance(config, dict) else {}
        self.candidate = self.config.get("candidate", {})
        self.battery = self.config.get("verified_battery", {})
        self._qualification_errors = self._qualify()
        self._previous: tuple[str, float, float] | None = None
        self._healthy_since: float | None = None
        self._advising_charge = False
        self._temperatures: deque[tuple[float, float]] = deque()
        self._started_at: float | None = None
        self._elapsed_uptime = 0.0
        self._expired = False
        self._latched_fault: str | None = None
        self._last_monotonic: float | None = None

    @property
    def qualification_errors(self):
        return list(self._qualification_errors)

    def _qualify(self):
        errors = []
        if not isinstance(self.candidate, dict) or not isinstance(self.battery, dict):
            self.candidate = {}
            self.battery = {}
            return ["invalid_configuration"]
        if self.battery.get("identity_confirmed") is not True:
            errors.append("battery_identity_unverified")
        source = self.battery.get("source")
        if not isinstance(source, str) or not source.strip():
            errors.append("battery_specification_source_missing")
        if self.battery.get("temperature_calibration_verified") is not True:
            errors.append("temperature_calibration_unverified")
        if self.battery.get("termination_current_compatibility_verified") is not True:
            errors.append("battery_termination_compatibility_unverified")
        required = (
            "max_charge_current_ma",
            "max_charge_voltage_mv",
            "min_charge_temperature_c",
            "max_charge_temperature_c",
            "ntc_r25_ohm",
            "ntc_beta_k",
        )
        for name in required:
            value = self.battery.get(name)
            if not _number(value) or (name not in required[2:4] and value <= 0):
                errors.append(f"invalid_battery_{name}")
        for name, minimum, maximum, step in (
            ("ntc_beta_k", 1, 65534, 1),
            ("ntc_r25_ohm", 10, 655350, 10),
        ):
            value = self.battery.get(name)
            if not isinstance(value, int) or not _supported(
                value, minimum, maximum, step
            ):
                errors.append(f"unsupported_{name}")
        settings = (
            ("charge_current_ma", 550, 2500, 75),
            ("regulation_voltage_mv", 3500, 4440, 20),
            ("termination_current_ma", 50, 400, 50),
        )
        for name, minimum, maximum, step in settings:
            if not _supported(self.candidate.get(name), minimum, maximum, step):
                errors.append(f"unsupported_{name}")
        for name, maximum, tolerance in (
            ("charge_current_ma", "max_charge_current_ma", 1.10),
            ("regulation_voltage_mv", "max_charge_voltage_mv", 1.01),
        ):
            value, limit = self.candidate.get(name), self.battery.get(maximum)
            if _number(value) and _number(limit) and value * tolerance > limit + 1e-9:
                errors.append(f"{name}_exceeds_documented_limit_with_tolerance")
        temperatures = [
            self.candidate.get(f"temp_{name}_c")
            for name in ("cold", "cool", "warm", "hot")
        ]
        if not all(
            isinstance(value, int) and _supported(value, -128, 127, 1)
            for value in temperatures
        ):
            errors.append("unencodable_temperature_threshold")
        if not all(_number(value) for value in temperatures) or not all(
            first < second for first, second in zip(temperatures, temperatures[1:])
        ):
            errors.append("invalid_temperature_threshold_order")
        else:
            low, high = self.battery.get(required[2]), self.battery.get(required[3])
            if (
                _number(low)
                and _number(high)
                and not low <= temperatures[0] < temperatures[-1] <= high
            ):
                errors.append("temperature_thresholds_exceed_documented_range")
            if temperatures[-1] - temperatures[0] <= 4:
                errors.append("temperature_restart_window_empty")
        return errors

    def _reset_dwell(self):
        self._healthy_since = None
        self._advising_charge = False

    def _result(self, decision, reasons, sample, now_uptime, preserve_dwell=False):
        if decision != "charge" and not preserve_dwell:
            self._reset_dwell()
        voltage = self.candidate.get("regulation_voltage_mv")
        result = {
            "mode": "advisory_only",
            "hardware_writes": False,
            "decision": decision,
            "reasons": reasons,
            "suggested_charge_current_ma": self.candidate.get("charge_current_ma")
            if decision == "charge"
            else 0,
            "suggested_regulation_voltage_mv": voltage if _number(voltage) else None,
            "qualification_errors": self.qualification_errors,
            "data_limits": list(DATA_LIMITS),
            "firmware_effective_limits": None,
        }
        temperature = sample.get("battery_temperature_c")
        if not self._qualification_errors and _number(temperature):
            stopped = (
                temperature <= self.candidate["temp_cold_c"]
                or temperature >= self.candidate["temp_hot_c"]
            )
            cool = temperature < self.candidate["temp_cool_c"]
            warm = temperature > self.candidate["temp_warm_c"]
            result["firmware_effective_limits"] = {
                "charge_current_ma": 0
                if stopped
                else self.candidate["charge_current_ma"] * (0.5 if cool else 1),
                "regulation_voltage_mv": max(3500, voltage - (140 if warm else 0)),
                "temperature_region": "stopped"
                if stopped
                else "cool"
                if cool
                else "warm"
                if warm
                else "normal",
                "applies_only_if_charging_enabled": True,
                "predicted_not_readback": True,
            }
        result["healthy_observation_seconds"] = (
            max(0, now_uptime - self._healthy_since)
            if self._healthy_since is not None and _number(now_uptime)
            else 0
        )
        return result

    def _timing_errors(self, sample, now_uptime, boot_id):
        uptime = sample.get("uptime_seconds")
        if (
            not _number(now_uptime)
            or now_uptime < 0
            or not _number(uptime)
            or uptime < 0
        ):
            return ["invalid_uptime"]
        if (
            not isinstance(boot_id, str)
            or not boot_id
            or sample.get("boot_id") != boot_id
        ):
            return ["missing_or_mismatched_boot_id"]
        if not 0 <= now_uptime - uptime <= 15:
            return ["stale_or_future_sample"]
        errors = []
        if self._previous is not None:
            old_boot, old_now, old_uptime = self._previous
            if old_boot != boot_id:
                errors.append("boot_changed")
            else:
                self._elapsed_uptime += max(0, now_uptime - old_now)
                if now_uptime <= old_now or uptime <= old_uptime:
                    errors.append("uptime_rollback_or_repeated_sample")
                elif max(now_uptime - old_now, uptime - old_uptime) > 15:
                    errors.append("observation_gap")
        self._previous = (boot_id, now_uptime, uptime)
        return errors

    def evaluate(self, sample: dict, now_uptime: float, boot_id: str) -> dict:
        """Return bounded advice from a complete, fresh observation of this boot."""
        local_now = time.monotonic()
        local_rollback = (
            self._last_monotonic is not None and local_now < self._last_monotonic
        )
        self._last_monotonic = local_now
        if not isinstance(sample, dict):
            return self._result("fault", ["invalid_sample"], {}, now_uptime)
        timing_errors = self._timing_errors(sample, now_uptime, boot_id)
        if local_rollback:
            timing_errors.append("local_monotonic_clock_rollback")
        if self._started_at is not None and (
            local_now - self._started_at >= 1800 or self._elapsed_uptime >= 1800
        ):
            self._expired = True
        if self._expired:
            return self._result(
                "hold", ["advisory_session_expired"], sample, now_uptime
            )
        if self._latched_fault:
            return self._result("fault", [self._latched_fault], sample, now_uptime)
        if timing_errors:
            self._temperatures.clear()
            return self._result("hold", timing_errors, sample, now_uptime)
        numeric_fields = (
            "battery_voltage_mv",
            "battery_temperature_c",
            "io_voltage_mv",
            "io_current_ma",
        )
        invalid = [
            f"invalid_{name}"
            for name in numeric_fields
            if not _number(sample.get(name))
        ]
        if (
            sample.get("io_current_ma", -1) is not None
            and _number(sample.get("io_current_ma"))
            and sample["io_current_ma"] < 0
        ):
            invalid.append("negative_output_current")
        for name in ("battery_present", "charging_enabled"):
            if not isinstance(sample.get(name), bool):
                invalid.append(f"invalid_{name}")
        if sample.get("cell_temperature_verified") is not None and not isinstance(
            sample["cell_temperature_verified"], bool
        ):
            invalid.append("invalid_cell_temperature_verified")
        if not isinstance(sample.get("errors"), dict) or sample["errors"]:
            invalid.append("telemetry_read_errors")
        states = {
            "battery_status": {"NORMAL", "CHARGING_FROM_IN"},
            "power_input_status": {"PRESENT", "BAD", "WEAK", "NOT_PRESENT"},
            "power_5v_io_status": {"PRESENT", "BAD", "WEAK", "NOT_PRESENT"},
            "activity": {"idle", "capture", "upload"},
        }
        for name, values in states.items():
            if not isinstance(sample.get(name), str) or sample[name] not in values:
                invalid.append(f"invalid_{name}")
        if invalid:
            self._temperatures.clear()
            return self._result("fault", invalid, sample, now_uptime)
        if sample["battery_voltage_mv"] >= 4150:
            self._latched_fault = "battery_voltage_at_or_above_4150mv"
            return self._result("fault", [self._latched_fault], sample, now_uptime)
        if (
            sample.get("cell_temperature_verified") is not True
            or sample.get("cell_temperature_c") is None
        ):
            self._temperatures.clear()
            return self._result(
                "blocked",
                ["verified_independent_cell_temperature_required"],
                sample,
                now_uptime,
            )
        if not _number(sample["cell_temperature_c"]):
            self._temperatures.clear()
            return self._result(
                "fault", ["invalid_cell_temperature_c"], sample, now_uptime
            )
        if self._started_at is None:
            self._started_at = local_now
            self._elapsed_uptime = 0.0
        temperature = sample["cell_temperature_c"]
        self._temperatures.append((now_uptime, temperature))
        while self._temperatures and now_uptime - self._temperatures[0][0] > 120:
            self._temperatures.popleft()
        reasons = []
        if abs(temperature - sample["battery_temperature_c"]) > 3:
            reasons.append("cell_and_hat_temperature_disagreement")
        if not 4800 <= sample["io_voltage_mv"] <= 5250:
            reasons.append("output_rail_out_of_range")
        if reasons:
            return self._result("fault", reasons, sample, now_uptime)
        if self._qualification_errors:
            return self._result(
                "blocked", self.qualification_errors, sample, now_uptime
            )
        if not sample["battery_present"]:
            reasons.append("battery_absent")
        if sample["power_input_status"] != "PRESENT":
            reasons.append("usb_input_not_present")
        if sample["power_5v_io_status"] != "NOT_PRESENT":
            reasons.append("gpio_input_not_supported")
        if sample["battery_voltage_mv"] < 3500:
            reasons.append("deep_discharge_recovery_not_supported")
        if sample["battery_voltage_mv"] >= 4100:
            reasons.append("battery_voltage_at_or_above_4100mv")
        low, high = self.candidate["temp_cold_c"], self.candidate["temp_hot_c"]
        if not low < temperature < high:
            reasons.append("cell_temperature_out_of_range")
        elif not self._advising_charge and not low + 2 <= temperature <= high - 2:
            reasons.append("cell_temperature_outside_restart_window")
        if not low < sample["battery_temperature_c"] < high:
            reasons.append("hat_temperature_at_firmware_stop_threshold")
        if any(
            now_uptime - observed >= 60
            and (temperature - old_temperature) * 60 / (now_uptime - observed) >= 1
            for observed, old_temperature in self._temperatures
        ):
            reasons.append("cell_temperature_rising_too_fast")
        power = sample["io_voltage_mv"] * sample["io_current_ma"] / 1_000_000
        if power >= 2 or (not self._advising_charge and power > 1.5):
            reasons.append("pi_load_too_high")
        if sample["activity"] != "idle":
            reasons.append("capture_or_upload_active")
        if reasons:
            return self._result("hold", reasons, sample, now_uptime)
        if self._healthy_since is None:
            self._healthy_since = now_uptime
        if now_uptime - self._healthy_since < 120:
            return self._result(
                "hold",
                ["qualifying_healthy_observations"],
                sample,
                now_uptime,
                preserve_dwell=True,
            )
        self._advising_charge = True
        return self._result(
            "charge", ["qualified_stable_conditions"], sample, now_uptime
        )
