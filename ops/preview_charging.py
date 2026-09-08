"""Replay observations through the charging advisor without device access."""

import argparse
import json
from collections import Counter
from pathlib import Path

from hardware.charging_policy import ChargingPolicy


MAX_JSON_BYTES = 65536
MAX_SAMPLES = 100000


def reject_constant(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def read_json(payload):
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("JSON input exceeds 64 KiB")
    value = json.loads(payload, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def replay(config, source):
    policy = ChargingPolicy(config)
    counts, reasons = Counter(), Counter()
    samples, transitions = 0, []
    previous, last = None, None
    while True:
        payload = source.readline(MAX_JSON_BYTES + 1)
        if not payload:
            break
        if len(payload) > MAX_JSON_BYTES:
            raise ValueError("JSON input exceeds 64 KiB")
        if not payload.strip():
            continue
        if samples >= MAX_SAMPLES:
            raise ValueError("Replay exceeds 100000 samples")
        sample = read_json(payload)
        last = policy.evaluate(
            sample,
            now_uptime=sample.get("uptime_seconds"),
            boot_id=sample.get("boot_id"),
        )
        samples += 1
        decision = last["decision"]
        counts[decision] += 1
        detail = last.get("reasons", [last.get("reason", "")])
        if isinstance(detail, str):
            detail = [detail]
        for reason in detail:
            reasons[str(reason)] += 1
        signature = (decision, tuple(detail))
        if signature != previous:
            transitions.append(
                {
                    "sample": samples,
                    "timestamp_utc": sample.get("timestamp_utc"),
                    "uptime_seconds": sample.get("uptime_seconds"),
                    "decision": decision,
                    "reasons": detail,
                }
            )
            previous = signature
    if not samples:
        raise ValueError("No observations supplied")
    return {
        "mode": "offline_replay",
        "hardware_writes": False,
        "profile_id": config.get("profile_id"),
        "qualification_errors": policy.qualification_errors,
        "samples": samples,
        "decision_counts": dict(counts),
        "reason_counts": dict(reasons),
        "transitions": transitions,
        "last_decision": last,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--observations", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        with args.profile.open("rb") as source:
            config = read_json(source.read(MAX_JSON_BYTES + 1))
        with args.observations.open("rb") as source:
            result = replay(config, source)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"Charging preview failed: {error}\n")
    print(json.dumps(result, indent=2, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
