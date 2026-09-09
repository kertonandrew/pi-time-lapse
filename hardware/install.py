#!/usr/bin/python3

import argparse
import base64
import json
import os
import platform
import re
import stat
import struct
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


SYSTEM_MASKS = (
    "lightdm.service",
    "cups.service",
    "cups.socket",
    "cups.path",
    "cups-browsed.service",
    "bluetooth.service",
    "hciuart.service",
    "ModemManager.service",
    "triggerhappy.service",
    "triggerhappy.socket",
    "accounts-daemon.service",
    "colord.service",
    "udisks2.service",
    "glamor-test.service",
    "rp1-test.service",
    "NetworkManager-wait-online.service",
)
USER_MASKS = (
    "pipewire.service",
    "pipewire.socket",
    "pipewire-pulse.service",
    "pipewire-pulse.socket",
    "wireplumber.service",
    "filter-chain.service",
    "pulseaudio.service",
    "pulseaudio.socket",
)
MONITOR = "pi-hardware.service"
TIMER = "pijuice-clock.timer"
GETTY = "getty@tty1.service"
CONFIG = "/boot/firmware/config.txt"
CMDLINE = "/boot/firmware/cmdline.txt"
WATCHDOG_CONFIG = "/etc/systemd/system.conf.d/pi-hardware-watchdog.conf"
WIFI_CONFIG = "/etc/modules-load.d/pi-hardware-wifi.conf"
BEGIN = "# pi-hardware:begin"
END = "# pi-hardware:end"
ACTIVE = {"active", "activating", "reloading"}


class InstallError(RuntimeError):
    pass


def command(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=45)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def file_state(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"type": "absent"}
    result = {
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }
    if path.is_symlink():
        return {**result, "type": "symlink", "target": os.readlink(path)}
    if not stat.S_ISREG(metadata.st_mode):
        raise InstallError(f"Expected regular file or symlink: {path}")
    return {
        **result,
        "type": "file",
        "data": base64.b64encode(path.read_bytes()).decode(),
    }


def regular_file(data, previous=None, mode=0o644):
    metadata = (
        {key: previous[key] for key in ("mode", "uid", "gid")}
        if previous and previous["type"] == "file"
        else {"mode": mode, "uid": 0, "gid": 0}
    )
    return {**metadata, "type": "file", "data": base64.b64encode(data).decode()}


def symlink(target):
    return {"type": "symlink", "target": target, "mode": 0o777, "uid": 0, "gid": 0}


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_state(path, state):
    if state["type"] == "absent":
        path.unlink(missing_ok=True)
        if path.parent.exists():
            sync_directory(path.parent)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pi-hardware-", dir=path.parent)
    temporary = Path(temporary)
    try:
        if state["type"] == "symlink":
            os.close(descriptor)
            temporary.unlink()
            temporary.symlink_to(state["target"])
            os.chown(temporary, state["uid"], state["gid"], follow_symlinks=False)
        else:
            with os.fdopen(descriptor, "wb") as output:
                output.write(base64.b64decode(state["data"]))
                output.flush()
                os.fchmod(output.fileno(), state["mode"])
                os.fchown(output.fileno(), state["uid"], state["gid"])
                os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def firmware_config(text):
    if (
        text.count(BEGIN) != text.count(END)
        or text.count(BEGIN) > 1
        or (BEGIN in text and text.index(END) < text.index(BEGIN))
    ):
        raise InstallError("Malformed pi-hardware configuration markers")
    section = "all"
    output = []
    kms = None
    rtc = None
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if line in (BEGIN, END):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        match = re.match(
            r"dtoverlay\s*=\s*(vc4-kms-v3d|disable-bt|i2c-rtc)(?:,|$)", stripped
        )
        if match:
            if section not in ("all", "pi0", "pi0w"):
                if not re.fullmatch(r"(?:pi[1-5]|cm[1-5]|none)", section):
                    raise InstallError(
                        f"Target overlay has an unsupported condition: [{section}]"
                    )
                output.append(line)
                continue
            name, *parameters = stripped.split("=", 1)[1].split(",")
            name = name.strip()
            parameters = [value.strip() for value in parameters]
            if name == "vc4-kms-v3d":
                parameters = [
                    value
                    for value in parameters
                    if value.split("=", 1)[0] not in ("audio", "noaudio", "nohdmi")
                ]
                if kms is not None and kms != parameters:
                    raise InstallError(
                        "Conflicting KMS overlays require manual consolidation"
                    )
                kms = parameters
            if name == "i2c-rtc":
                chips = [value for value in parameters if value in ("ds1307", "ds1339")]
                if len(chips) != 1:
                    raise InstallError(
                        "An existing RTC overlay does not identify the PiJuice DS1307/DS1339"
                    )
                parameters = [
                    value for value in parameters if value not in ("ds1307", "ds1339")
                ]
                for parameter in parameters:
                    key, _, value = parameter.partition("=")
                    if key == "addr":
                        try:
                            valid = int(value, 0) == 0x68
                        except ValueError:
                            valid = False
                    else:
                        valid = key in ("i2c1", "wakeup-source") and value in (
                            "",
                            "on",
                            "true",
                            "1",
                        )
                    if not valid:
                        raise InstallError(
                            f"RTC option is incompatible with PiJuice on bus 1 address 0x68: {parameter}"
                        )
                if rtc is not None and rtc != parameters:
                    raise InstallError(
                        "Conflicting RTC overlays require manual consolidation"
                    )
                rtc = parameters
            continue
        if (
            stripped in ("dtparam=audio=off", "display_auto_detect=0")
            and section == "all"
        ):
            continue
        output.append(line)
    while output and not output[-1].strip():
        output.pop()
    if output and output[-1].strip() == "[all]":
        output.pop()
    block = [
        BEGIN,
        "[all]",
        "dtparam=audio=off",
        "display_auto_detect=0",
        "dtoverlay=" + ",".join(["vc4-kms-v3d", *(kms or []), "noaudio", "nohdmi"]),
        "dtoverlay=disable-bt",
        "dtoverlay=" + ",".join(["i2c-rtc", "ds1307", *(rtc or [])]),
        END,
    ]
    return "\n".join(output).rstrip() + "\n\n" + "\n".join(block) + "\n"


def kernel_command_line(text):
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise InstallError("cmdline.txt must contain one nonempty line")
    tokens = lines[0].split()
    tokens = [
        token
        for token in tokens
        if token != "splash"
        and not re.fullmatch(r"console=(?:serial\d+|ttyAMA\d+|ttyS\d+)(?:,.*)?", token)
    ]
    return " ".join(tokens) + "\n"


def console_override(vendor):
    starts = [line for line in vendor.splitlines() if line.startswith("ExecStart=")]
    if (
        len(starts) != 1
        or "autologin" in starts[0]
        or re.search(r"\s-a\s", starts[0])
        or starts[0].endswith("\\")
    ):
        raise InstallError(
            "Cannot derive a non-autologin console from the vendor getty unit"
        )
    return "[Service]\nExecStart=\n" + starts[0] + "\n"


class Installer:
    def __init__(
        self,
        root=Path("/"),
        source=Path(__file__).parent,
        run=command,
        wifi_early_load=False,
    ):
        self.root = Path(root)
        self.source = Path(source)
        self.run = run
        self.wifi_early_load = wifi_early_load

    def path(self, absolute):
        path = Path(absolute)
        if not path.is_absolute() or ".." in path.parts:
            raise InstallError(f"Invalid managed path: {absolute}")
        return self.root / str(path).lstrip("/")

    def checked(self, args):
        code, output, error = self.run(args)
        if code:
            raise InstallError(f"{' '.join(args)}: {error or output}")
        return output

    def preflight(self, rollback=False):
        model = self.path("/proc/device-tree/model").read_text().rstrip("\x00\n")
        release = self.path("/etc/os-release").read_text()
        if (
            model != "Raspberry Pi Zero W Rev 1.1"
            or platform.machine() != "armv6l"
            or struct.calcsize("P") != 4
        ):
            raise InstallError(
                "Only Raspberry Pi Zero W Rev 1.1 with a 32-bit ARMv6 OS is supported"
            )
        if not re.search(
            r'^VERSION_CODENAME=["\']?bookworm["\']?$', release, re.M
        ) or not re.search(r'^ID=["\']?(raspbian|debian)["\']?$', release, re.M):
            raise InstallError("Only Raspberry Pi OS Bookworm is supported")
        if rollback:
            return
        for name in (
            CONFIG,
            CMDLINE,
            "/lib/systemd/system/getty@.service",
            "/lib/systemd/system/multi-user.target",
        ):
            path = self.path(name)
            if not path.is_file() or (name in (CONFIG, CMDLINE) and path.is_symlink()):
                raise InstallError(f"Missing or unsupported system file: {name}")
        self.checked(["/usr/bin/python3", "-c", "import pijuice, smbus, sqlite3"])
        bus = self.path("/dev/i2c-1")
        if not bus.is_char_device() or not os.access(bus, os.R_OK | os.W_OK):
            raise InstallError(
                "PiJuice requires an accessible /dev/i2c-1 character device"
            )
        self.check_watchdog()
        if self.wifi_early_load:
            self.check_wifi_module()
        readme = self.path("/boot/firmware/overlays/README").read_text()
        match = re.search(r"Name:\s+vc4-kms-v3d\n(.*?)(?=\nName:|\Z)", readme, re.S)
        if (
            not match
            or not all(option in match[1] for option in ("noaudio", "nohdmi"))
            or not re.search(r"^Name:\s+disable-bt$", readme, re.M)
        ):
            raise InstallError(
                "Installed overlays do not document the required display/Bluetooth options"
            )
        self.check_includes(CONFIG, set())

    def check_wifi_module(self):
        dependencies = self.checked(
            [
                "env",
                "-u",
                "MODPROBE_OPTIONS",
                "modprobe",
                "--show-depends",
                "--use-blacklist",
                "brcmfmac",
            ]
        )
        operations = [
            line.split()[0] for line in dependencies.splitlines() if line.strip()
        ]
        if "install" in operations or not any(
            operation in ("insmod", "builtin") for operation in operations
        ):
            raise InstallError(
                "Experimental Wi-Fi early loading requires ordinary brcmfmac resolution without a blacklist or install hook"
            )

    def check_watchdog(self):
        if not self.path("/dev/watchdog0").is_char_device():
            raise InstallError(
                "Runtime watchdog requires a /dev/watchdog0 character device"
            )
        driver = self.path("/sys/class/watchdog/watchdog0/device/driver")
        if (
            not driver.exists()
            or driver.resolve()
            != self.path("/sys/bus/platform/drivers/bcm2835-wdt").resolve()
        ):
            raise InstallError(
                "Runtime watchdog requires watchdog0 to use the bcm2835-wdt driver"
            )
        parameters = {
            token.split("=", 1)[0].replace("-", "_")
            for token in self.path("/proc/cmdline").read_text().split()
        }
        if "systemd.watchdog_sec" in parameters:
            raise InstallError(
                "Runtime watchdog configuration conflicts with systemd.watchdog_sec"
            )
        runtime_nowayout = self.path("/sys/class/watchdog/watchdog0/nowayout")
        if runtime_nowayout.exists():
            if runtime_nowayout.read_text().strip() != "0":
                raise InstallError("Runtime watchdog requires watchdog0 nowayout=0")
            return
        configuration = self.path(f"/boot/config-{platform.release()}").read_text()
        if not re.search(r"^# CONFIG_WATCHDOG_SYSFS is not set$", configuration, re.M):
            raise InstallError(
                "Cannot verify watchdog nowayout without its runtime attribute or CONFIG_WATCHDOG_SYSFS disabled"
            )
        parameter = self.path("/sys/module/bcm2835_wdt/parameters/nowayout")
        if parameter.exists():
            if parameter.read_text().strip() not in ("0", "N"):
                raise InstallError("Runtime watchdog requires bcm2835_wdt nowayout=0")
            return
        if not re.search(
            r"^CONFIG_BCM2835_WDT=y$", configuration, re.M
        ) or not re.search(
            r"^# CONFIG_WATCHDOG_NOWAYOUT is not set$", configuration, re.M
        ):
            raise InstallError(
                "Watchdog fallback requires a built-in BCM2835 driver with CONFIG_WATCHDOG_NOWAYOUT disabled"
            )
        if "bcm2835_wdt.nowayout" in parameters:
            raise InstallError(
                "Cannot verify watchdog nowayout with a kernel command-line override"
            )

    def check_includes(self, name, seen):
        if name in seen:
            raise InstallError("Recursive firmware include")
        seen.add(name)
        for line in self.path(name).read_text().splitlines():
            match = re.match(r"\s*include\s+([^#]+)", line)
            if not match:
                continue
            child = str(Path(name).parent / match[1].strip())
            content = self.path(child).read_text()
            if re.search(
                r"^\s*dtoverlay\s*=\s*(vc4-kms-v3d|disable-bt|i2c-rtc)(?:,|\s|$)",
                content,
                re.M,
            ):
                raise InstallError(
                    f"Target overlay is configured in an include: {child}"
                )
            self.check_includes(child, seen.copy())

    def states(self):
        result = {}
        for scope, units in (
            ("system", (*SYSTEM_MASKS, MONITOR, TIMER, GETTY)),
            ("global", USER_MASKS),
        ):
            for unit in units:
                prefix = (
                    ["systemctl", "--global"] if scope == "global" else ["systemctl"]
                )
                _, enabled, _ = self.run([*prefix, "is-enabled", unit])
                active = (
                    self.run(["systemctl", "is-active", unit])[1]
                    if scope == "system"
                    else None
                )
                result[f"{scope}:{unit}"] = {
                    "enabled": enabled or "not-found",
                    "active": active,
                }
        return result

    def plan(self):
        desired = {}

        def edit(name, data):
            desired[name] = regular_file(data, file_state(self.path(name)))

        edit(CONFIG, firmware_config(self.path(CONFIG).read_text()).encode())
        edit(CMDLINE, kernel_command_line(self.path(CMDLINE).read_text()).encode())
        for filename in ("pijuice_clock.py", "power_monitor.py"):
            desired[f"/usr/local/lib/pi-time-lapse/{filename}"] = regular_file(
                (self.source / filename).read_bytes()
            )
        for filename in ("pi-hardware.service", "pijuice-clock.service"):
            desired[f"/etc/systemd/system/{filename}"] = regular_file(
                (self.source / filename).read_bytes()
            )
        desired[WATCHDOG_CONFIG] = regular_file(
            (self.source / "pi-hardware-watchdog.conf").read_bytes()
        )
        if self.wifi_early_load:
            desired[WIFI_CONFIG] = regular_file(
                (self.source / "pi-hardware-wifi.conf").read_bytes()
            )
        defaults = "/etc/default/pi-hardware"
        if file_state(self.path(defaults))["type"] == "absent":
            desired[defaults] = regular_file(
                (self.source / "pi-hardware.default").read_bytes()
            )
        for unit in SYSTEM_MASKS:
            desired[f"/etc/systemd/system/{unit}"] = symlink("/dev/null")
        for unit in USER_MASKS:
            desired[f"/etc/systemd/user/{unit}"] = symlink("/dev/null")
        desired["/etc/systemd/system/default.target"] = symlink(
            "/lib/systemd/system/multi-user.target"
        )
        getty = "/etc/systemd/system/getty@tty1.service.d/zz-pi-hardware-login.conf"
        for path in self.path("/etc/systemd/system/getty@tty1.service.d").glob(
            "*.conf"
        ):
            if path.name > Path(getty).name and "ExecStart=" in path.read_text():
                raise InstallError(
                    f"Later getty override would defeat console login: {path}"
                )
        edit(
            getty,
            console_override(
                self.path("/lib/systemd/system/getty@.service").read_text()
            ).encode(),
        )
        desired[f"/etc/systemd/system/getty.target.wants/{GETTY}"] = symlink(
            "/lib/systemd/system/getty@.service"
        )
        desired[f"/etc/systemd/system/multi-user.target.wants/{MONITOR}"] = symlink(
            f"/etc/systemd/system/{MONITOR}"
        )
        for directory in ("/etc/systemd/system", "/run/systemd/system"):
            for path in self.path(directory).rglob("*"):
                if (
                    path.is_symlink()
                    and path.parent.name.endswith((".wants", ".requires"))
                    and (path.name == TIMER or Path(os.readlink(path)).name == TIMER)
                ):
                    desired["/" + str(path.relative_to(self.root))] = {"type": "absent"}
        for name in desired:
            parent = self.path(name).parent
            while parent != self.root:
                if parent.is_symlink():
                    raise InstallError(
                        f"Managed directory must not be a symlink: {parent}"
                    )
                parent = parent.parent
        return {
            name: {"before": file_state(self.path(name)), "after": state}
            for name, state in desired.items()
            if file_state(self.path(name)) != state
        }

    def save_manifest(self, backup, manifest):
        write_state(
            backup / "manifest.json",
            regular_file(json.dumps(manifest, indent=2).encode(), mode=0o600),
        )
        sync_directory(backup.parent)
        sync_directory(backup.parent.parent)

    def apply(self, changes, states):
        active_masks = [
            unit
            for unit in (*SYSTEM_MASKS, TIMER)
            if states[f"system:{unit}"]["active"] in ACTIVE
        ]
        if (
            not changes
            and not active_masks
            and states[f"system:{MONITOR}"]["active"] in ACTIVE
        ):
            return None
        stamp = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        backup = self.path("/var/backups/pi-hardware-installer") / stamp
        backup.mkdir(parents=True, mode=0o700)
        created = set()
        for name in changes:
            parent = self.path(name).parent
            while not parent.exists() and parent != self.root:
                created.add("/" + str(parent.relative_to(self.root)))
                parent = parent.parent
        manifest = {
            "version": 1,
            "status": "prepared",
            "files": changes,
            "units": states,
            "created_directories": sorted(created),
        }
        self.save_manifest(backup, manifest)
        for name, change in changes.items():
            if file_state(self.path(name)) != change["before"]:
                raise InstallError(f"File changed after preflight: {name}")
        attempted = []
        try:
            for name, change in changes.items():
                if file_state(self.path(name)) != change["before"]:
                    raise InstallError(f"File changed after preflight: {name}")
                attempted.append(name)
                write_state(self.path(name), change["after"])
            self.checked(["systemctl", "daemon-reload"])
            if active_masks:
                self.checked(["systemctl", "stop", *active_masks])
            self.checked(["systemctl", "restart", MONITOR])
            manifest["status"] = "applied"
            self.save_manifest(backup, manifest)
        except Exception as error:
            print(f"Apply failed; restoring snapshot {backup}", flush=True)
            try:
                self.rollback(backup, partial_files=attempted)
            except Exception as rollback_error:
                raise InstallError(
                    f"Apply failed ({error}); rollback incomplete ({rollback_error}); backup: {backup}"
                ) from error
            raise
        return backup

    def restart_original(self, unit):
        masks = {}
        for directory in ("/etc/systemd/system", "/run/systemd/system"):
            path = self.path(f"{directory}/{unit}")
            state = file_state(path)
            if state["type"] == "symlink" and state["target"] == "/dev/null":
                masks[path] = state
        try:
            for path in masks:
                path.unlink()
            if masks:
                self.checked(["systemctl", "daemon-reload"])
            self.checked(["systemctl", "restart", unit])
        finally:
            for path, state in masks.items():
                write_state(path, state)
            if masks:
                self.checked(["systemctl", "daemon-reload"])

    def rollback(self, backup, apply=True, partial_files=None):
        manifest = json.loads((Path(backup) / "manifest.json").read_text())
        if manifest.get("version") != 1:
            raise InstallError("Unsupported backup manifest")
        changes = (
            manifest["files"]
            if partial_files is None
            else {name: manifest["files"][name] for name in partial_files}
        )
        conflicts = []
        for name, change in changes.items():
            if file_state(self.path(name)) not in (change["before"], change["after"]):
                if partial_files is None:
                    raise InstallError(
                        f"Rollback would overwrite a later change: {name}"
                    )
                conflicts.append(name)
        changes = {
            name: change for name, change in changes.items() if name not in conflicts
        }
        if not apply:
            return manifest
        if self.run(["systemctl", "is-active", MONITOR])[1] in ACTIVE:
            self.checked(["systemctl", "stop", MONITOR])
        for name, change in changes.items():
            write_state(self.path(name), change["before"])
        self.checked(["systemctl", "daemon-reload"])
        for key, state in manifest["units"].items():
            scope, unit = key.split(":", 1)
            if scope == "system":
                current = self.run(["systemctl", "is-active", unit])[1]
                if state["active"] in ACTIVE and (
                    current not in ACTIVE or unit == MONITOR
                ):
                    self.restart_original(unit)
                elif state["active"] not in ACTIVE and current in ACTIVE:
                    self.checked(["systemctl", "stop", unit])
            prefix = ["systemctl", "--global"] if scope == "global" else ["systemctl"]
            _, restored, _ = self.run([*prefix, "is-enabled", unit])
            if (restored or "not-found") != state["enabled"]:
                raise InstallError(f"Unit enablement differs after rollback: {key}")
        for name in sorted(manifest["created_directories"], key=len, reverse=True):
            try:
                self.path(name).rmdir()
            except OSError:
                pass
        manifest["status"] = "rollback-incomplete" if conflicts else "rolled-back"
        self.save_manifest(Path(backup), manifest)
        if conflicts:
            raise InstallError(
                f"Rollback preserved later edits: {', '.join(conflicts)}"
            )
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preview, install, or roll back the Pi Zero W Bookworm hardware baseline"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the displayed operation; omitted means dry-run",
    )
    parser.add_argument("--rollback", type=Path, metavar="BACKUP_DIRECTORY")
    parser.add_argument(
        "--wifi-early-load",
        action="store_true",
        help="Load brcmfmac earlier to reduce Wi-Fi startup delay",
    )
    args = parser.parse_args(argv)
    installer = Installer(wifi_early_load=args.wifi_early_load)
    try:
        if args.apply and os.geteuid() != 0:
            raise InstallError("--apply requires root")
        installer.preflight(rollback=bool(args.rollback))
        if args.rollback:
            manifest = installer.rollback(args.rollback, apply=False)
            print(
                json.dumps(
                    {
                        "operation": "rollback",
                        "files": list(manifest["files"]),
                        "units": manifest["units"],
                    },
                    indent=2,
                )
            )
            if args.apply:
                installer.rollback(args.rollback)
        else:
            changes, states = installer.plan(), installer.states()
            print(
                json.dumps(
                    {
                        "operation": "install",
                        "files": list(changes),
                        "mask_system": SYSTEM_MASKS,
                        "mask_global_user": USER_MASKS,
                        "start": MONITOR,
                        "stop_timer": TIMER,
                    },
                    indent=2,
                )
            )
            if args.apply:
                backup = installer.apply(changes, states)
                print(
                    f"Installed; rollback snapshot: {backup}"
                    if backup
                    else "Already installed; no changes"
                )
        print(
            "Reboot separately to activate or restore firmware, user-session and runtime watchdog settings."
            if args.apply
            else "Dry-run only; no changes made."
        )
    except (InstallError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Hardware installer failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
