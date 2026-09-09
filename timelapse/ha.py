"""Publish bounded telemetry or archived photographs through native MQTT discovery."""

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

from .ha_config import load_config
from .config import load_config as load_timelapse_config
from .ha_controls import (
    FIELDS,
    apply_desired,
    decode_desired,
    effective_settings,
    encode_reported,
    session_lock,
)
from .ha_discovery import build_discovery, topics
from .ha_metrics import read_metrics
from .ha_mqtt import publish_messages, receive_messages
from .ha_photos import latest_photo
from .spool import atomic_json, read_json


VERSION = "0.1.0"


def json_bytes(value):
    return json.dumps(
        value, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode()


def due(previous, interval, now):
    return type(previous) not in (int, float) or not 0 <= now - previous < interval


@contextlib.contextmanager
def state_lock(directory, role):
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Publisher state directory must be a real directory")
    descriptor = os.open(
        directory / f"{role}.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Publisher lock must be a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / f"{role}.json"
        try:
            state = read_json(path)
        except FileNotFoundError:
            state = {}
        yield path, state
    finally:
        os.close(descriptor)


def discovery(config):
    return build_discovery(
        config["device_id"],
        config["device_name"],
        config["topic_prefix"],
        config["discovery_prefix"],
        config["telemetry"]["expire_after_seconds"],
    )


def telemetry_sample(config):
    settings = config["telemetry"]
    return read_metrics(
        Path(settings["database"]),
        Path(settings["spool"]),
        max_age_seconds=settings["max_age_seconds"],
        current_boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        current_uptime=float(Path("/proc/uptime").read_text().split()[0]),
    )


def telemetry_allowed(state, minimum_mv):
    if (
        state.get("usb_input_status") == "PRESENT"
        or state.get("gpio_input_status") == "PRESENT"
    ):
        return True
    voltage = state.get("battery_voltage")
    return type(voltage) in (int, float) and voltage * 1000 >= minimum_mv


def prepare(config, role):
    device_topics = topics(
        config["device_id"], config["topic_prefix"], config["discovery_prefix"]
    )
    if role == "telemetry":
        value = telemetry_sample(config)
        if not telemetry_allowed(
            value, config["telemetry"]["minimum_battery_voltage_mv"]
        ):
            return None, {
                "action": "wait",
                "reason": "Battery reserve or external input is insufficient",
                "hardware_writes": False,
            }
        content_key = hashlib.sha256(json_bytes(value)).hexdigest()
        messages = [(device_topics["state"], json_bytes(value), False)]
        preview = value
    elif role == "photos":
        value = latest_photo(
            Path(config["photos"]["archive_root"]),
            config["device_id"],
            config["photos"]["max_bytes"],
        )
        if value is None:
            return None, {
                "action": "wait",
                "reason": "No committed archive photograph",
                "hardware_writes": False,
            }
        content_key = hashlib.sha256(json_bytes(value["metadata"])).hexdigest()
        messages = [
            (device_topics["image"], value["payload"], True),
            (device_topics["image_metadata"], json_bytes(value["metadata"]), True),
        ]
        preview = {"metadata": value["metadata"], "jpeg_bytes": len(value["payload"])}
    else:
        raise ValueError("Unknown publisher role")
    return (content_key, messages), preview


def run(config, role, dry_run=False, force=False, now=None):
    now = time.time() if now is None else now
    device_topics = topics(
        config["device_id"], config["topic_prefix"], config["discovery_prefix"]
    )
    discovery_topic, document = discovery(config)
    discovery_payload = json_bytes(document)
    discovery_hash = hashlib.sha256(discovery_payload).hexdigest()
    destination = {
        key: config["mqtt"][key] for key in ("host", "port", "tls", "username")
    }
    destination.update(
        discovery_hash=discovery_hash, state_topic=device_topics["state"]
    )
    destination_key = hashlib.sha256(json_bytes(destination)).hexdigest()
    if dry_run:
        prepared, preview = prepare(config, role)
        if prepared is None:
            return preview
        return {
            "action": "preview",
            "role": role,
            "mqtt_writes": False,
            "data": preview,
            "discovery_topic": discovery_topic,
        }
    if not config["mqtt"]["host"]:
        raise ValueError("Set mqtt.host before publishing")
    with state_lock(config["state_directory"], role) as (path, state):
        prepared, preview = prepare(config, role)
        if prepared is None:
            return preview
        content_key, messages = prepared
        force = force or state.get("destination_key") != destination_key
        send_discovery = (
            force
            or state.get("discovery_hash") != discovery_hash
            or due(state.get("discovery_at"), config["discovery_refresh_seconds"], now)
        )
        send_content = force or state.get("content_key") != content_key
        if role == "photos" and due(
            state.get("content_at"), config["photos"]["republish_seconds"], now
        ):
            send_content = True
        outgoing = (
            [(discovery_topic, discovery_payload, True)] if send_discovery else []
        ) + (messages if send_content else [])
        if not outgoing:
            return {"action": "wait", "reason": "Already published", "role": role}
        publish_messages(
            config["mqtt"],
            outgoing,
            client_id=f"ptl-{config['device_id']}-{role}",
            connection_topic=device_topics["connection"] + "/" + role,
        )
        if send_discovery:
            state.update(discovery_hash=discovery_hash, discovery_at=now)
        if send_content:
            state.update(content_key=content_key, content_at=now)
        state["destination_key"] = destination_key
        state["last_success_utc"] = datetime.fromtimestamp(
            now, timezone.utc
        ).isoformat()
        atomic_json(path, state)
    return {
        "action": "published",
        "role": role,
        "messages": len(outgoing),
        "payload_bytes": sum(len(message[1]) for message in outgoing),
    }


def run_controls(config, dry_run=False):
    """Poll desired configuration and report only validated durable local readback."""
    deadline = time.monotonic() + config["mqtt"]["timeout_seconds"]
    local = load_timelapse_config(config["controls"]["timelapse_config"])
    if local["remote_controls"]["enabled"] is not True:
        return {
            "action": "wait",
            "reason": "Remote camera controls are locally disabled",
        }
    if local["remote_controls"]["device_id"] != config["device_id"]:
        raise ValueError("Camera control device identities differ")
    current = effective_settings(local)
    destinations = topics(
        config["device_id"], config["topic_prefix"], config["discovery_prefix"]
    )
    desired_topics = {f"{destinations['desired']}/{field}": field for field in FIELDS}
    if dry_run:
        return {
            "action": "preview",
            "role": "controls",
            "mqtt_writes": False,
            "configuration_writes": False,
            "settings": current,
            "desired_topics": list(desired_topics),
        }
    if not config["mqtt"]["host"]:
        raise ValueError("Set mqtt.host before polling camera controls")
    observed = telemetry_sample(config)
    if not telemetry_allowed(
        observed, config["telemetry"]["minimum_battery_voltage_mv"]
    ):
        return {
            "action": "wait",
            "reason": "Battery reserve or external input is insufficient",
        }

    def mqtt_budget():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Camera controls exceeded the operation deadline")
        return dict(config["mqtt"], timeout_seconds=remaining)

    with session_lock(local, config["device_id"]):
        messages = receive_messages(
            mqtt_budget(),
            list(desired_topics),
            client_id=f"ptl-{config['device_id']}-controls-read",
            connection_topic=f"{destinations['connection']}/controls",
            poll_seconds=config["controls"]["poll_seconds"],
        )
        updates = {}
        for topic, payload in messages:
            if topic not in desired_topics:
                raise ValueError(
                    "MQTT control topic is outside this device's allowlist"
                )
            field = desired_topics[topic]
            updates[field] = decode_desired(field, payload)
        mqtt_budget()
        persisted = apply_desired(local, config["device_id"], updates)
        reported = encode_reported(persisted)
        discovery_topic, document = discovery(config)
        outgoing = [(discovery_topic, json_bytes(document), True)] + [
            (f"{destinations['reported']}/{field}", reported[field], True)
            for field in FIELDS
        ]
        publish_messages(
            mqtt_budget(),
            outgoing,
            client_id=f"ptl-{config['device_id']}-controls-report",
            connection_topic=f"{destinations['connection']}/controls",
        )
    return {
        "action": "reported",
        "role": "controls",
        "settings": persisted,
        "changed_fields": [
            field for field in FIELDS if persisted[field] != current[field]
        ],
    }


def remove(config, role):
    if not config["mqtt"]["host"]:
        raise ValueError("Set mqtt.host before removing discovery")
    device_topics = topics(
        config["device_id"], config["topic_prefix"], config["discovery_prefix"]
    )
    names = (
        ("discovery", "state")
        if role == "telemetry"
        else ("discovery", "image", "image_metadata")
    )
    messages = [(device_topics[name], b"", True) for name in names]
    publish_messages(
        config["mqtt"],
        messages,
        client_id=f"ptl-{config['device_id']}-remove",
        connection_topic=device_topics["connection"] + "/" + role,
    )
    return {"action": "removed", "device_id": config["device_id"]}


def register(config, role="telemetry"):
    if not config["mqtt"]["host"]:
        raise ValueError("Set mqtt.host before registering discovery")
    topic, payload = discovery(config)
    destination = topics(
        config["device_id"], config["topic_prefix"], config["discovery_prefix"]
    )
    publish_messages(
        config["mqtt"],
        [(topic, json_bytes(payload), True)],
        client_id=f"ptl-{config['device_id']}-register",
        connection_topic=destination["connection"] + "/" + role,
    )
    return {"action": "registered", "device_id": config["device_id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discovery")
    registration = commands.add_parser("register")
    registration.add_argument(
        "--role", choices=("telemetry", "photos"), default="telemetry"
    )
    for role in ("telemetry", "photos"):
        subparser = commands.add_parser(role)
        subparser.add_argument("--dry-run", action="store_true")
        subparser.add_argument("--force", action="store_true")
    controls = commands.add_parser("controls")
    controls.add_argument("--dry-run", action="store_true")
    removal = commands.add_parser("remove")
    removal.add_argument("--role", choices=("telemetry", "photos"), required=True)
    removal.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        config = load_config(args.config)
        if args.command == "discovery":
            topic, payload = discovery(config)
            result = {"topic": topic, "payload": payload, "mqtt_writes": False}
        elif args.command == "remove":
            result = remove(config, args.role)
        elif args.command == "register":
            result = register(config, args.role)
        elif args.command == "controls":
            result = run_controls(config, args.dry_run)
        else:
            result = run(config, args.command, args.dry_run, args.force)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        print(
            json.dumps({"error": type(error).__name__, "message": str(error)[:500]}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
