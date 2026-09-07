import argparse
import ast
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import stat
import struct
import subprocess
import sys
import tempfile
import uuid


PACKAGE_FILES = (
    "__init__.py",
    "__main__.py",
    "capture.py",
    "cli.py",
    "config.py",
    "power.py",
    "receiver.py",
    "spool.py",
    "transfer.py",
)
SERVICES = ("pi-timelapse-capture.service", "pi-timelapse-transfer.service")
TIMERS = ("pi-timelapse-capture.timer", "pi-timelapse-transfer.timer")
UNITS = (*SERVICES, *TIMERS)
CONFIG = "/etc/pi-timelapse.json"
PACKAGE_ROOT = "/usr/local/lib/pi-timelapse/timelapse"
BACKUP_ROOT = "/var/backups/pi-timelapse"
ALLOWED_PATHS = {
    CONFIG,
    *(f"{PACKAGE_ROOT}/{name}" for name in PACKAGE_FILES),
    *(f"/etc/systemd/system/{name}" for name in UNITS),
}
ACTIVE_STATES = {"active", "activating", "reloading", "deactivating"}
ENABLED_STATES = {
    "enabled",
    "enabled-runtime",
    "disabled",
    "masked",
    "masked-runtime",
    "not-found",
}


class InstallError(RuntimeError):
    pass


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=75)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def file_state(path):
    try:
        details = path.lstat()
    except FileNotFoundError:
        return {"type": "absent"}
    if not stat.S_ISREG(details.st_mode):
        raise InstallError(f"Managed destination must be a regular file: {path}")
    return {
        "type": "file",
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        "mode": stat.S_IMODE(details.st_mode),
        "uid": details.st_uid,
        "gid": details.st_gid,
    }


def regular_file(data, mode=0o644, owner=(0, 0)):
    return {
        "type": "file",
        "data": base64.b64encode(data).decode("ascii"),
        "mode": mode,
        "uid": owner[0],
        "gid": owner[1],
    }


def make_directory(path, mode=0o755):
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=mode)
        sync_directory(directory.parent)


def write_state(path, state):
    if state["type"] == "absent":
        path.unlink(missing_ok=True)
        if path.parent.exists():
            sync_directory(path.parent)
        return
    make_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".pi-timelapse-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(base64.b64decode(state["data"], validate=True))
            stream.flush()
            os.fchmod(stream.fileno(), state["mode"])
            os.fchown(stream.fileno(), state["uid"], state["gid"])
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Installer:
    def __init__(self, source_root=None, root=Path("/"), run=command, owner=(0, 0)):
        self.source = (
            Path(source_root)
            if source_root is not None
            else Path(__file__).resolve().parents[1]
        )
        self.root = Path(root).resolve()
        self.run = run
        self.owner = owner

    def path(self, name):
        return self.root / name.lstrip("/")

    def checked(self, arguments):
        code, output, error = self.run(arguments)
        if code:
            raise InstallError(
                f"Command failed ({code}): {' '.join(arguments)}: {error}"
            )
        return output

    def preflight(self, apply=False):
        if apply and os.geteuid() != 0:
            raise InstallError("--apply requires root")
        if sys.version_info[:2] != (3, 11):
            raise InstallError("Deployment requires Python 3.11")
        model = self.path("/proc/device-tree/model").read_text().rstrip("\x00\n")
        if (
            model != "Raspberry Pi Zero W Rev 1.1"
            or platform.machine() != "armv6l"
            or struct.calcsize("P") != 4
        ):
            raise InstallError(
                "Deployment supports Raspberry Pi Zero W Rev 1.1 with a 32-bit ARMv6 OS"
            )
        release = self.path("/etc/os-release").read_text()
        if not re.search(
            r'^VERSION_CODENAME=["\']?bookworm["\']?$', release, re.M
        ) or not re.search(r'^ID=["\']?(raspbian|debian)["\']?$', release, re.M):
            raise InstallError("Deployment requires Raspberry Pi OS Bookworm")
        self.checked(["systemctl", "show", "--property=Version", "--value"])

    def check_parent_directories(self, name):
        current = self.path(name).parent
        while current != self.root:
            if current.is_symlink():
                raise InstallError(
                    f"Managed parent directory must not be a symlink: {current}"
                )
            current = current.parent

    def states(self):
        result = {"timers": {}, "services": {}}
        for unit in UNITS:
            _, active, _ = self.run(["systemctl", "is-active", unit])
            if active not in {
                "active",
                "inactive",
                "activating",
                "deactivating",
                "reloading",
                "failed",
                "unknown",
            }:
                raise InstallError(
                    f"Cannot establish active state for {unit}: {active}"
                )
            if unit in TIMERS:
                _, enabled, _ = self.run(["systemctl", "is-enabled", unit])
                enabled = enabled or "not-found"
                if enabled not in ENABLED_STATES:
                    raise InstallError(
                        f"Unsupported timer enablement for {unit}: {enabled}"
                    )
                result["timers"][unit] = {"enabled": enabled, "active": active}
            else:
                result["services"][unit] = {"active": active}
        return result

    def plan(self):
        desired = {}
        for name in PACKAGE_FILES:
            source = self.source / "timelapse" / name
            if source.is_symlink() or not source.is_file():
                raise InstallError(f"Missing regular package source: {source}")
            compile(source.read_bytes(), str(source), "exec")
            desired[f"{PACKAGE_ROOT}/{name}"] = regular_file(
                source.read_bytes(), owner=self.owner
            )
        for name in UNITS:
            source = self.source / "deploy" / name
            if source.is_symlink() or not source.is_file():
                raise InstallError(f"Missing regular unit source: {source}")
            desired[f"/etc/systemd/system/{name}"] = regular_file(
                source.read_bytes(), owner=self.owner
            )
        self.check_parent_directories(CONFIG)
        if file_state(self.path(CONFIG))["type"] == "absent":
            tree = ast.parse((self.source / "timelapse/config.py").read_bytes())
            assignments = [
                node
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "DEFAULTS"
                    for target in node.targets
                )
            ]
            if len(assignments) != 1:
                raise InstallError(
                    "Configuration source must declare exactly one DEFAULTS object"
                )
            defaults = ast.literal_eval(assignments[0].value)
            if not isinstance(defaults, dict):
                raise InstallError("Configuration DEFAULTS must be an object")
            data = (
                json.dumps(defaults, indent=2, sort_keys=True, allow_nan=False) + "\n"
            ).encode()
            desired[CONFIG] = regular_file(data, mode=0o600, owner=self.owner)
        changes = {}
        for name, after in desired.items():
            self.check_parent_directories(name)
            before = file_state(self.path(name))
            if before != after:
                changes[name] = {"before": before, "after": after}
        return changes

    def save_manifest(self, backup, manifest):
        payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        write_state(
            backup / "manifest.json",
            regular_file(payload, mode=0o600, owner=self.owner),
        )

    def stop_units(self):
        for units in (TIMERS, SERVICES):
            active = [
                unit
                for unit in units
                if self.run(["systemctl", "is-active", unit])[1] in ACTIVE_STATES
            ]
            if active:
                self.checked(["systemctl", "stop", *active])

    def restore_timers(self, states):
        for unit in TIMERS:
            target = states[unit]
            _, current, _ = self.run(["systemctl", "is-enabled", unit])
            current = current or "not-found"
            enabled = target["enabled"]
            if enabled == "not-found":
                if current not in {"not-found", "disabled"}:
                    self.checked(["systemctl", "disable", unit])
            elif current != enabled:
                if enabled in {"enabled", "enabled-runtime"}:
                    if current in {"enabled", "enabled-runtime"}:
                        self.checked(["systemctl", "disable", unit])
                        self.checked(["systemctl", "disable", "--runtime", unit])
                    self.checked(
                        [
                            "systemctl",
                            "enable",
                            *(["--runtime"] if enabled == "enabled-runtime" else []),
                            unit,
                        ]
                    )
                elif enabled == "disabled":
                    self.checked(["systemctl", "disable", unit])
                    self.checked(["systemctl", "disable", "--runtime", unit])
                else:
                    self.checked(
                        [
                            "systemctl",
                            "mask",
                            *(["--runtime"] if enabled == "masked-runtime" else []),
                            unit,
                        ]
                    )
            _, restored, _ = self.run(["systemctl", "is-enabled", unit])
            restored = restored or "not-found"
            if restored != enabled and not (
                enabled == "not-found"
                and restored == "disabled"
                and self.path(f"/etc/systemd/system/{unit}").is_file()
            ):
                raise InstallError(
                    f"Timer enablement was not restored for {unit}: {restored}"
                )
            if target["active"] in ACTIVE_STATES:
                self.checked(["systemctl", "start", unit])
            elif self.run(["systemctl", "is-active", unit])[1] in ACTIVE_STATES:
                self.checked(["systemctl", "stop", unit])

    def apply(self, changes, states):
        if not changes:
            return None
        self.validate_manifest({"version": 1, "files": changes, "units": states})
        for name, change in changes.items():
            if file_state(self.path(name)) != change["before"]:
                raise InstallError(f"File changed after planning: {name}")
        stamp = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:12]
        )
        backup = self.path(BACKUP_ROOT) / stamp
        self.check_parent_directories(f"{BACKUP_ROOT}/{stamp}/manifest.json")
        make_directory(backup, mode=0o700)
        os.chmod(backup, 0o700)
        manifest = {
            "version": 1,
            "status": "prepared",
            "files": changes,
            "units": states,
        }
        self.save_manifest(backup, manifest)
        try:
            self.stop_units()
            for name, change in changes.items():
                if file_state(self.path(name)) != change["before"]:
                    raise InstallError(f"File changed after planning: {name}")
                write_state(self.path(name), change["after"])
            self.checked(["systemctl", "daemon-reload"])
            self.restore_timers(states["timers"])
            manifest["status"] = "applied"
            self.save_manifest(backup, manifest)
        except Exception as error:
            try:
                self.rollback(backup, apply=True)
            except Exception as recovery_error:
                raise InstallError(
                    f"Installation failed ({error}); rollback failed ({recovery_error}); snapshot: {backup}"
                ) from error
            raise InstallError(
                f"Installation failed and snapshot was restored: {error}; snapshot: {backup}"
            ) from error
        return backup

    def validate_manifest(self, manifest):
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != 1
            or not isinstance(manifest.get("files"), dict)
        ):
            raise InstallError("Invalid deployment snapshot")
        if not set(manifest["files"]).issubset(ALLOWED_PATHS):
            raise InstallError("Snapshot contains an unmanaged destination")
        for name, change in manifest["files"].items():
            self.check_parent_directories(name)
            if not isinstance(change, dict) or set(change) != {"before", "after"}:
                raise InstallError("Invalid snapshot file change")
            for state in change.values():
                if not isinstance(state, dict) or state.get("type") not in {
                    "absent",
                    "file",
                }:
                    raise InstallError("Invalid snapshot file state")
                if state["type"] == "file":
                    if (
                        set(state) != {"type", "data", "mode", "uid", "gid"}
                        or any(
                            type(state[key]) is not int or state[key] < 0
                            for key in ("mode", "uid", "gid")
                        )
                        or state["mode"] > 0o7777
                    ):
                        raise InstallError("Invalid snapshot file attributes")
                    base64.b64decode(state["data"], validate=True)
        units = manifest.get("units")
        if (
            not isinstance(units, dict)
            or set(units) != {"timers", "services"}
            or set(units["timers"]) != set(TIMERS)
            or set(units["services"]) != set(SERVICES)
        ):
            raise InstallError("Snapshot contains invalid unit names")
        for state in units["timers"].values():
            if (
                not isinstance(state, dict)
                or state.get("enabled") not in ENABLED_STATES
                or state.get("active")
                not in {*ACTIVE_STATES, "inactive", "unknown", "failed"}
            ):
                raise InstallError("Snapshot contains invalid timer states")
        return manifest

    def rollback(self, backup, apply=False):
        backup = Path(backup)
        if backup.is_symlink() or (backup / "manifest.json").is_symlink():
            raise InstallError("Snapshot must not be a symlink")
        manifest = self.validate_manifest(
            json.loads((backup / "manifest.json").read_text())
        )
        for name, change in manifest["files"].items():
            if file_state(self.path(name)) not in (change["before"], change["after"]):
                raise InstallError(f"Rollback would overwrite a later change: {name}")
        if not apply:
            return manifest
        self.stop_units()
        for unit, target in manifest["units"]["timers"].items():
            if (
                target["enabled"] == "not-found"
                and self.path(f"/etc/systemd/system/{unit}").is_file()
            ):
                self.checked(["systemctl", "disable", unit])
                self.checked(["systemctl", "disable", "--runtime", unit])
        for name, change in reversed(list(manifest["files"].items())):
            if file_state(self.path(name)) not in (change["before"], change["after"]):
                raise InstallError(f"Rollback would overwrite a later change: {name}")
            write_state(self.path(name), change["before"])
        self.checked(["systemctl", "daemon-reload"])
        self.restore_timers(manifest["units"]["timers"])
        manifest["status"] = "rolled-back"
        self.save_manifest(backup, manifest)
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preview, deploy, or restore the Pi Zero W timelapse application"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the operation; omitted means dry-run",
    )
    parser.add_argument("--rollback", type=Path, metavar="BACKUP_DIRECTORY")
    arguments = parser.parse_args(argv)
    installer = Installer()
    try:
        installer.preflight(apply=arguments.apply)
        if arguments.rollback:
            manifest = installer.rollback(arguments.rollback)
            print(
                json.dumps(
                    {
                        "operation": "rollback",
                        "files": list(manifest["files"]),
                        "timers": manifest["units"]["timers"],
                    },
                    indent=2,
                )
            )
            if arguments.apply:
                installer.rollback(arguments.rollback, apply=True)
        else:
            changes, states = installer.plan(), installer.states()
            print(
                json.dumps(
                    {
                        "operation": "install",
                        "files": list(changes),
                        "preserve_config": CONFIG not in changes,
                        "timers": states["timers"],
                    },
                    indent=2,
                )
            )
            if arguments.apply:
                backup = installer.apply(changes, states)
                print(
                    f"Installed; rollback snapshot: {backup}"
                    if backup
                    else "Already installed; no changes"
                )
        print(
            "Timers retain their prior state; enable initial timers separately."
            if arguments.apply
            else "Dry-run only; no changes made."
        )
    except (
        InstallError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Timelapse deployment failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
