"""Create private operator configuration and check local runtime prerequisites."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys

from .config import load_config, validate_config
from .ha_config import load_config as load_ha_config
from .ha_config import validate_config as validate_ha_config


def initialize(directory, device_id, broker=None):
    """Write complete example configuration with no credentials or enabled timers."""
    directory = Path(directory).absolute()
    camera = validate_config({"remote_controls": {"device_id": device_id}})
    home_assistant = validate_ha_config(
        {"device_id": device_id, "mqtt": {"host": broker}}
    )
    files = {
        directory / "camera.json": camera,
        directory / "home-assistant.json": home_assistant,
    }
    for parent in (directory, *directory.parents):
        if parent.is_symlink():
            raise ValueError("Configuration directory must not use symlinks")
    if directory.exists():
        mode = directory.stat().st_mode
        if not stat.S_ISDIR(mode) or mode & 0o022:
            raise ValueError("Configuration directory must not be group/world writable")
    if any(os.path.lexists(path) for path in files):
        raise ValueError(
            "Configuration already exists; existing files will not be overwritten"
        )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    created = []
    try:
        for path, value in files.items():
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            created.append(path)
            with os.fdopen(descriptor, "w") as output:
                json.dump(value, output, indent=2, sort_keys=True, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return {
        "created": [str(path) for path in files],
        "device_id": device_id,
        "hardware_writes": False,
    }


def doctor(config_path=None, ha_path=None):
    """Check local files and executables without camera, I2C or network operations."""
    config = load_config(config_path)
    checks = {
        "python_3_11_or_newer": sys.version_info >= (3, 11),
        "linux": sys.platform == "linux",
        "camera_executable": shutil.which(config["camera"]["camera_command"])
        is not None,
        "hardware_database_readable": os.access(config["hardware_database"], os.R_OK),
        "boot_identity_readable": os.access("/proc/sys/kernel/random/boot_id", os.R_OK),
    }
    result = {
        "capture_prerequisites_present": all(checks.values()),
        "checks": checks,
        "power_guard_still_required": True,
        "schedule_enabled": config["schedule"]["enabled"],
        "remote_controls_enabled": config["remote_controls"]["enabled"],
        "transfer_destination_configured": bool(config["server"]),
        "ssh_available": shutil.which("ssh") is not None,
        "rsync_available": shutil.which("rsync") is not None,
        "network_operations": False,
        "hardware_writes": False,
    }
    if ha_path is not None:
        ha = load_ha_config(ha_path)
        mqtt = ha["mqtt"]
        try:
            paho = importlib.util.find_spec("paho.mqtt.client") is not None
        except ModuleNotFoundError:
            paho = False
        result["home_assistant"] = {
            "mqtt_dependency_available": paho,
            "broker_configured": mqtt["host"] is not None,
            "tls_enabled": mqtt["tls"],
            "device_identity_matches": config["remote_controls"]["device_id"]
            == ha["device_id"],
            "credentials_configured": mqtt["password_file"] is not None
            or mqtt["cert_file"] is not None,
        }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init")
    create.add_argument("--directory", required=True, type=Path)
    create.add_argument("--device-id", required=True)
    create.add_argument("--broker")
    check = commands.add_parser("doctor")
    check.add_argument("--config", type=Path)
    check.add_argument("--ha-config", type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "init":
            value = initialize(args.directory, args.device_id, args.broker)
        else:
            value = doctor(args.config, args.ha_config)
        print(json.dumps(value, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, OSError, TypeError) as error:
        print(
            json.dumps({"error": type(error).__name__, "message": str(error)}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
