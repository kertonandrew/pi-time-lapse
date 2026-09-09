"""Trusted SSH bootstrap and detached application deployment worker."""

import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import traceback


JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
SNAPSHOT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\Z")
CONFIG = Path("/etc/pi-timelapse.json")
BACKUPS = Path("/var/backups/pi-timelapse")
TOOLS = {
    "deploy/bundle.py",
    "deploy/install.py",
    "deploy/remote.py",
    "timelapse/config.py",
}
UNITS = (
    "ssh.service",
    "pi-hardware.service",
    "pijuice.service",
    "pi-timelapse-capture.timer",
    "pi-timelapse-transfer.timer",
    "pi-home-assistant-telemetry.timer",
    "pi-home-assistant-controls.timer",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".deployment-")
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, sort_keys=True, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def secure_directory(path):
    path = Path(path)
    for candidate in reversed((path, *path.parents)):
        if not candidate.exists():
            candidate.mkdir(mode=0o700)
        details = candidate.lstat()
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != 0
            or details.st_mode & 0o022
        ):
            raise ValueError(
                "Remote deployment directory has unsafe ownership or permissions"
            )
    os.chmod(path, 0o700)


def regular_bytes(path, limit=65536):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("Expected a regular deployment file")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Deployment file exceeds its size limit")
    return data


def power_status(database=None, proc=Path("/proc"), utc=None):
    try:
        if database is None:
            database = "/var/lib/pi-hardware/metrics.sqlite3"
            environment = Path("/etc/default/pi-hardware")
            if environment.exists():
                for line in regular_bytes(environment).decode().splitlines():
                    if line.strip().startswith("PI_HARDWARE_DATABASE="):
                        tokens = shlex.split(line.strip(), comments=True)
                        if len(tokens) != 1 or not tokens[0].startswith(
                            "PI_HARDWARE_DATABASE=/"
                        ):
                            raise ValueError("Invalid hardware database configuration")
                        database = tokens[0].split("=", 1)[1]
        uri = Path(database).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT timestamp_utc,boot_id,uptime_seconds,power_input_status,battery_charge_percent,battery_temperature_c FROM samples ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("No power samples")
        stamp = datetime.fromisoformat(row["timestamp_utc"])
        if stamp.tzinfo is None:
            raise ValueError("Power timestamp lacks timezone")
        uptime = float((proc / "uptime").read_text().split()[0])
        age = ((utc or datetime.now(timezone.utc)) - stamp).total_seconds()
        monotonic_age = uptime - row["uptime_seconds"]
        fresh = (
            math.isfinite(age)
            and math.isfinite(monotonic_age)
            and 0 <= age <= 420
            and 0 <= monotonic_age <= 420
            and row["boot_id"]
            == (proc / "sys/kernel/random/boot_id").read_text().strip()
        )
        return {
            "available": True,
            "fresh": fresh,
            "age_seconds": round(age, 1),
            "input_present": row["power_input_status"] == "PRESENT",
            "battery_charge_percent": row["battery_charge_percent"],
            "battery_temperature_c": row["battery_temperature_c"],
        }
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {"available": False, "fresh": False, "input_present": False}


def gates(request):
    power = power_status()
    paths = (Path(request["staging_dir"]), Path("/usr/local/lib"), BACKUPS)
    free = []
    for destination in paths:
        while not destination.exists():
            destination = destination.parent
        free.append(shutil.disk_usage(destination).free)
    minimum = request["min_free_mb"] * 1024 * 1024
    return {
        "stable_power_confirmed": request.get("stable_power") is True,
        "fresh_external_power": power["fresh"] and power["input_present"],
        "sufficient_disk": all(value >= minimum for value in free),
        "minimum_free_mb": min(free) // (1024 * 1024),
        "power": power,
    }


def require_gates(request):
    checks = gates(request)
    if not all(
        checks[key]
        for key in ("stable_power_confirmed", "fresh_external_power", "sufficient_disk")
    ):
        raise ValueError(
            "Deployment requires confirmed stable power, fresh input telemetry and sufficient disk space"
        )
    return checks


def status(request):
    states = {}
    for unit in UNITS:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                unit,
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "UnitFileState",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        states[unit] = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
    output = {
        "utc": now(),
        "services": states,
        "power": power_status(),
        "camera_config_present": CONFIG.is_file(),
        "home_assistant_config_present": Path(
            "/etc/pi-home-assistant/config.json"
        ).is_file(),
    }
    snapshots = []
    if BACKUPS.exists():
        for snapshot in sorted(BACKUPS.iterdir(), reverse=True):
            if not SNAPSHOT_ID.fullmatch(snapshot.name) or snapshot.is_symlink():
                continue
            try:
                manifest = json.loads(
                    regular_bytes(snapshot / "manifest.json", 8 * 1024 * 1024)
                )
                if (
                    not isinstance(manifest, dict)
                    or not isinstance(manifest.get("status"), str)
                    or manifest["status"]
                    not in {
                        "prepared",
                        "recovering",
                        "applied",
                        "rolled-back",
                        "recovered",
                    }
                ):
                    raise ValueError("Unrecognized deployment snapshot")
                snapshots.append(
                    {"snapshot": snapshot.name, "status": manifest["status"]}
                )
            except (OSError, ValueError):
                snapshots.append({"snapshot": snapshot.name, "status": "unreadable"})
    output["incomplete_snapshots"] = [
        item
        for item in snapshots
        if item["status"] in {"prepared", "recovering", "unreadable"}
    ]
    output["recent_snapshots"] = snapshots[:10]
    root = Path(request["staging_dir"])
    marker = root / "last-deployment.json"
    if marker.exists():
        output["last_deployment"] = json.loads(regular_bytes(marker))
    if request.get("job"):
        job = root / request["job"]
        state_path = job / "status.json"
        if not state_path.exists():
            raise ValueError("Deployment job does not exist")
        output["job"] = json.loads(regular_bytes(state_path))
        result = subprocess.run(
            [
                "systemctl",
                "show",
                "pi-timelapse-deploy-" + request["job"] + ".service",
                "-p",
                "ActiveState",
                "-p",
                "Result",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output["job"]["systemd"] = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if output["job"]["state"] in {"queued", "running"} and output["job"][
            "systemd"
        ].get("ActiveState") in {"inactive", "failed"}:
            output["job"]["state"] = "interrupted"
    return output


def install_tools(directory, payload):
    if not isinstance(payload, dict) or set(payload) != TOOLS:
        raise ValueError("Unexpected deployment tool set")
    runtime = directory / "runtime"
    (runtime / "deploy").mkdir(parents=True, mode=0o700)
    for name, encoded in payload.items():
        content = base64.b64decode(encoded, validate=True)
        if len(content) > 262144:
            raise ValueError("Deployment tool exceeds size limit")
        compile(content, name, "exec")
        destination = runtime / name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.write_bytes(content)
        destination.chmod(0o600)
    return runtime


def extract_application(directory, request):
    from deploy.bundle import extract_bundle

    content = base64.b64decode(request["artifact"], validate=True)
    if (
        len(content) > 8 * 1024 * 1024
        or hashlib.sha256(content).hexdigest() != request["sha256"]
    ):
        raise ValueError("Deployment artifact checksum or size is invalid")
    archive = directory / "application.tar.gz"
    archive.write_bytes(content)
    archive.chmod(0o600)
    manifest = extract_bundle(
        archive, directory / "source", expected_sha256=request["sha256"]
    )
    if manifest["release_id"] != request["release_id"]:
        raise ValueError("Deployment release identity does not match")
    return manifest


def load_installer(source):
    spec = importlib.util.spec_from_file_location(
        "application_installer", source / "deploy/install.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def changes_for(installer, module, request, runtime):
    changes = installer.plan()
    encoded = request.get("camera_config")
    if encoded is not None:
        content = base64.b64decode(encoded, validate=True)
        if len(content) > 65536:
            raise ValueError("Camera configuration exceeds size limit")
        spec = importlib.util.spec_from_file_location(
            "deployment_camera_config", runtime / "timelapse/config.py"
        )
        config_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config_module)
        config_module.validate_config(json.loads(content))
        before = module.file_state(installer.path(module.CONFIG))
        after = module.regular_file(content, mode=0o600, owner=installer.owner)
        if before != after:
            changes[module.CONFIG] = {"before": before, "after": after}
    return changes


def preview(directory, request):
    extract_application(directory, request)
    module = load_installer(directory / "runtime")
    installer = module.Installer(source_root=directory / "source")
    installer.preflight()
    if request["operation"] in {"rollback", "recover"}:
        backup = BACKUPS / request["snapshot"]
        manifest = getattr(installer, request["operation"])(backup)
        files = list(manifest["files"])
        states = manifest["units"]
    else:
        files = list(changes_for(installer, module, request, directory / "runtime"))
        states = installer.states()
    return {
        "operation": request["operation"],
        "release_id": request["release_id"],
        "files": files,
        "camera_config_supplied": request.get("camera_config") is not None,
        "units": states,
        "gates": gates(request),
        "applied": False,
    }


def worker(directory):
    directory = Path(directory)
    request = json.loads(regular_bytes(directory / "request.json", 16 * 1024 * 1024))
    sys.path.insert(0, str(directory / "runtime"))
    state = {
        "job_id": directory.name,
        "operation": request["operation"],
        "release_id": request["release_id"]
        if request["operation"] == "deploy"
        else None,
        "state": "running",
        "started_utc": now(),
    }
    atomic_json(directory / "status.json", state)
    try:
        require_gates(request)
        extract_application(directory, request)
        module = load_installer(directory / "runtime")
        installer = module.Installer(source_root=directory / "source")
        installer.preflight(apply=True)
        if request["operation"] in {"rollback", "recover"}:
            backup = BACKUPS / request["snapshot"]
            getattr(installer, request["operation"])(backup, apply=True)
        else:
            changes = changes_for(installer, module, request, directory / "runtime")
            require_gates(request)
            backup = installer.apply(changes, installer.states())
        state.update(
            {
                "state": "succeeded",
                "finished_utc": now(),
                "snapshot": backup.name if backup else None,
                "units": installer.states(),
                "power": power_status(),
            }
        )
        atomic_json(directory / "status.json", state)
        atomic_json(directory.parent / "last-deployment.json", state)
    except Exception:
        (directory / "error.log").write_text(traceback.format_exc())
        (directory / "error.log").chmod(0o600)
        state.update(
            {
                "state": "failed",
                "finished_utc": now(),
                "message": "Deployment failed; inspect the private job error.log and installer snapshots before retrying",
            }
        )
        atomic_json(directory / "status.json", state)
        return 1
    return 0


def handle(request):
    if os.geteuid() != 0:
        raise ValueError("Remote deployment requires noninteractive sudo")
    os.umask(0o077)
    operation = request.get("operation")
    if operation not in {"status", "plan", "deploy", "rollback", "recover"}:
        raise ValueError("Unknown deployment operation")
    if not re.fullmatch(
        r"/var/lib/[A-Za-z0-9][A-Za-z0-9_-]{0,63}", request.get("staging_dir", "")
    ):
        raise ValueError("Invalid staging directory")
    if request.get("job") is not None and not JOB_ID.fullmatch(request["job"]):
        raise ValueError("Invalid job ID")
    if operation == "status":
        return status(request)
    if (
        type(request.get("min_free_mb")) is not int
        or not 32 <= request["min_free_mb"] <= 65536
    ):
        raise ValueError("Invalid minimum disk space")
    if operation in {"rollback", "recover"} and not SNAPSHOT_ID.fullmatch(
        request.get("snapshot", "")
    ):
        raise ValueError("Invalid snapshot ID")
    if request.get("apply") is not True:
        with tempfile.TemporaryDirectory(prefix="pi-timelapse-plan-") as temporary:
            directory = Path(temporary)
            runtime = install_tools(directory, request["tools"])
            sys.path.insert(0, str(runtime))
            return preview(directory, request)
    require_gates(request)
    if operation == "plan" or not JOB_ID.fullmatch(request.get("job", "")):
        raise ValueError("Apply requires a deployment job")
    root = Path(request["staging_dir"])
    secure_directory(root)
    directory = root / request["job"]
    directory.mkdir(mode=0o700)
    runtime = install_tools(directory, request["tools"])
    atomic_json(directory / "request.json", request)
    queued = {
        "job_id": directory.name,
        "operation": operation,
        "state": "queued",
        "release_id": request["release_id"],
        "queued_utc": now(),
    }
    atomic_json(directory / "status.json", queued)
    arguments = [
        "systemd-run",
        "--no-block",
        "--unit=pi-timelapse-deploy-" + directory.name,
        "--property=Type=oneshot",
        "--property=TimeoutStartSec=600",
        "--property=UMask=0077",
        "--property=Nice=10",
        sys.executable,
        "-I",
        "-B",
        str(runtime / "deploy/remote.py"),
        "--worker",
        str(directory),
    ]
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    if result.returncode:
        queued.update(
            {
                "state": "failed",
                "message": "Could not start detached deployment service",
            }
        )
        atomic_json(directory / "status.json", queued)
        raise ValueError(queued["message"])
    return queued


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--worker":
    raise SystemExit(worker(sys.argv[2]))
