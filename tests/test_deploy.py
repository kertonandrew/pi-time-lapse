import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "timelapse_installer", Path(__file__).resolve().parents[1] / "deploy/install.py"
)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class FakeSystemd:
    def __init__(self, root):
        self.root = root
        self.commands = []
        self.enabled = {}
        self.active = {}
        self.reload_failure = False
        self.real_health_check = False
        self.health_failure = False

    def unit_exists(self, name):
        return (self.root / "etc/systemd/system" / name).is_file()

    def run(self, arguments):
        self.commands.append(arguments)
        if arguments[0] == installer.sys.executable:
            if self.health_failure:
                self.health_failure = False
                return 1, "", "Simulated unhealthy installation"
            if self.real_health_check:
                return installer.command(arguments)
            return 0, "", ""
        action = arguments[1]
        names = [name for name in arguments[2:] if not name.startswith("--")]
        if action == "is-active":
            state = self.active.get(
                names[0], "inactive" if self.unit_exists(names[0]) else "unknown"
            )
            return (0 if state == "active" else 3, state, "")
        if action == "is-enabled":
            state = self.enabled.get(
                names[0], "disabled" if self.unit_exists(names[0]) else "not-found"
            )
            return (0 if state == "enabled" else 1, state, "")
        if action == "stop":
            for name in names:
                self.active[name] = "inactive"
        elif action == "start":
            for name in names:
                self.active[name] = "active"
        elif action == "disable":
            for name in names:
                self.enabled.pop(name, None)
        elif action in {"enable", "mask"}:
            for name in names:
                self.enabled[name] = ("enabled" if action == "enable" else "masked") + (
                    "-runtime" if "--runtime" in arguments else ""
                )
        elif action == "daemon-reload" and self.reload_failure:
            self.reload_failure = False
            return 1, "", "Simulated reload failure"
        return 0, "", ""


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "root"
        self.root.mkdir()
        self.source = self.base / "source"
        (self.source / "timelapse").mkdir(parents=True)
        (self.source / "deploy").mkdir()
        for name in installer.PACKAGE_FILES:
            (self.source / "timelapse" / name).write_text("value = 1\n")
        (self.source / "timelapse/config.py").write_text(
            "DEFAULTS = {'server': None, 'spool': '/var/lib/pi-timelapse'}\n"
        )
        for name in installer.UNITS:
            (self.source / "deploy" / name).write_text(
                "[Unit]\nDescription=Timelapse test\n"
            )
        self.systemd = FakeSystemd(self.root)
        self.deployer = installer.Installer(
            self.source,
            root=self.root,
            run=self.systemd.run,
            owner=(os.getuid(), os.getgid()),
        )

    def install(self):
        return self.deployer.apply(self.deployer.plan(), self.deployer.states())

    def use_real_package(self):
        package = Path(__file__).resolve().parents[1] / "timelapse"
        for name in installer.PACKAGE_FILES:
            shutil.copy2(package / name, self.source / "timelapse" / name)
        self.systemd.real_health_check = True

    def test_preflight_accepts_supported_os_and_arm_pi_combinations(self):
        self.write_target("/proc/device-tree/model", b"Raspberry Pi Zero W Rev 1.1\0")
        for architecture in ("armv6l", "armv7l", "aarch64"):
            for release in ("bookworm", "trixie"):
                with self.subTest(architecture=architecture, release=release):
                    self.write_target(
                        "/etc/os-release",
                        f'ID=debian\nVERSION_CODENAME="{release}"\n'.encode(),
                    )
                    with patch.object(
                        installer.platform, "machine", return_value=architecture
                    ):
                        self.deployer.preflight()

    def test_preflight_rejects_other_hardware_and_unsupported_os(self):
        self.write_target("/proc/device-tree/model", b"Other ARM computer\0")
        self.write_target("/etc/os-release", b"ID=debian\nVERSION_CODENAME=bookworm\n")
        with patch.object(installer.platform, "machine", return_value="aarch64"):
            with self.assertRaises(installer.InstallError):
                self.deployer.preflight()
            self.write_target(
                "/proc/device-tree/model", b"Raspberry Pi 5 Model B Rev 1.0\0"
            )
            self.write_target(
                "/etc/os-release", b"ID=debian\nVERSION_CODENAME=bullseye\n"
            )
            with self.assertRaises(installer.InstallError):
                self.deployer.preflight()

    def write_target(self, name, data, mode=0o644):
        path = self.deployer.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def test_dry_run_has_no_destination_or_systemd_mutations(self):
        changes = self.deployer.plan()
        self.deployer.states()
        self.assertEqual(set(changes), installer.ALLOWED_PATHS)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(
            all(
                command[1] in {"is-active", "is-enabled"}
                for command in self.systemd.commands
            )
        )

    def test_initial_install_keeps_timers_disabled_and_writes_private_snapshot(self):
        backup = self.install()
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((backup / "manifest.json").stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.deployer.path(installer.CONFIG).stat().st_mode), 0o600
        )
        self.assertEqual(
            json.loads(self.deployer.path(installer.CONFIG).read_text())["server"], None
        )
        self.assertFalse(
            any(command[1] in {"enable", "start"} for command in self.systemd.commands)
        )
        self.assertEqual(self.deployer.plan(), {})
        self.assertIsNone(self.install())

    def test_existing_configuration_bytes_and_permissions_are_preserved(self):
        configuration = self.write_target(
            installer.CONFIG,
            b'{"server": {"host": "configured.example"}}\n',
            mode=0o640,
        )
        before = installer.file_state(configuration)
        backup = self.install()
        self.assertEqual(installer.file_state(configuration), before)
        self.assertNotIn(
            installer.CONFIG,
            json.loads((backup / "manifest.json").read_text())["files"],
        )
        configuration.write_text('{"server": null, "later": "user edit"}\n')
        self.deployer.rollback(backup, apply=True)
        self.assertIn("user edit", configuration.read_text())

    def test_rollback_restores_old_bytes_mode_owner_and_original_absence(self):
        existing = self.write_target(
            installer.PACKAGE_ROOT + "/capture.py", b"old_capture = True\n", mode=0o640
        )
        original = installer.file_state(existing)
        photo = self.write_target(
            "/var/lib/pi-timelapse/images/keep.jpg", b"keep this photograph"
        )
        state = self.write_target(
            "/var/lib/pi-timelapse/transfer-state.json", b"keep this state"
        )
        backup = self.install()
        self.assertNotEqual(installer.file_state(existing), original)
        self.deployer.rollback(backup, apply=True)
        self.assertEqual(installer.file_state(existing), original)
        self.assertFalse(self.deployer.path(installer.CONFIG).exists())
        self.assertFalse(
            self.deployer.path(installer.PACKAGE_ROOT + "/receiver.py").exists()
        )
        self.assertTrue(photo.exists())
        self.assertTrue(state.exists())
        self.assertEqual(
            json.loads((backup / "manifest.json").read_text())["status"], "rolled-back"
        )

    def test_manual_rollback_dry_run_leaves_everything_installed(self):
        backup = self.install()
        before_commands = len(self.systemd.commands)
        self.deployer.rollback(backup)
        self.assertTrue(self.deployer.path(installer.CONFIG).exists())
        self.assertEqual(len(self.systemd.commands), before_commands)

    def test_rollback_refuses_later_edits_before_touching_other_files(self):
        backup = self.install()
        changed = self.deployer.path(installer.PACKAGE_ROOT + "/capture.py")
        changed.write_text("user_edit = True\n")
        before_commands = len(self.systemd.commands)
        with self.assertRaisesRegex(installer.InstallError, "later change"):
            self.deployer.rollback(backup, apply=True)
        self.assertEqual(changed.read_text(), "user_edit = True\n")
        self.assertTrue(self.deployer.path(installer.CONFIG).exists())
        self.assertEqual(len(self.systemd.commands), before_commands)

    def test_updates_stop_own_units_and_restore_prior_timer_state(self):
        self.install()
        capture_timer = installer.TIMERS[0]
        transfer_timer = installer.TIMERS[1]
        self.systemd.enabled[capture_timer] = "enabled"
        self.systemd.active[capture_timer] = "active"
        self.systemd.enabled[transfer_timer] = "disabled"
        self.systemd.active[transfer_timer] = "inactive"
        self.systemd.active[installer.SERVICES[0]] = "activating"
        (self.source / "timelapse/capture.py").write_text("value = 2\n")
        self.systemd.commands.clear()
        backup = self.install()
        actions = [command[1] for command in self.systemd.commands]
        self.assertLess(actions.index("stop"), actions.index("daemon-reload"))
        self.assertEqual(self.systemd.active[capture_timer], "active")
        self.assertEqual(self.systemd.enabled[capture_timer], "enabled")
        self.assertEqual(self.systemd.active[transfer_timer], "inactive")
        self.assertFalse(
            any(
                command[1] == "start" and installer.SERVICES[0] in command
                for command in self.systemd.commands
            )
        )
        self.deployer.rollback(backup, apply=True)
        self.assertEqual(
            self.deployer.path(installer.PACKAGE_ROOT + "/capture.py").read_text(),
            "value = 1\n",
        )
        self.assertEqual(self.systemd.active[capture_timer], "active")

    def test_initial_rollback_disables_later_enabled_timer_before_removing_unit(self):
        backup = self.install()
        unit = installer.TIMERS[0]
        self.systemd.enabled[unit] = "enabled"
        self.systemd.active[unit] = "active"
        self.systemd.commands.clear()
        self.deployer.rollback(backup, apply=True)
        actions = [command[1] for command in self.systemd.commands]
        self.assertLess(actions.index("disable"), actions.index("daemon-reload"))
        self.assertNotIn(unit, self.systemd.enabled)
        self.assertFalse(self.deployer.path(f"/etc/systemd/system/{unit}").exists())

    def test_update_rollback_restores_runtime_only_enablement_after_later_persistent_enable(
        self,
    ):
        self.install()
        timer = installer.TIMERS[0]
        self.systemd.enabled[timer] = "enabled-runtime"
        self.systemd.active[timer] = "active"
        (self.source / "timelapse/capture.py").write_text("value = 2\n")
        backup = self.install()
        self.systemd.enabled[timer] = "enabled"
        self.deployer.rollback(backup, apply=True)
        self.assertEqual(self.systemd.enabled[timer], "enabled-runtime")

    def test_failed_apply_automatically_restores_original_files(self):
        previous = self.write_target(
            installer.PACKAGE_ROOT + "/capture.py", b"previous = True\n", 0o640
        )
        initial = installer.file_state(previous)
        self.systemd.reload_failure = True
        with self.assertRaisesRegex(installer.InstallError, "snapshot was restored"):
            self.install()
        self.assertEqual(installer.file_state(previous), initial)
        self.assertFalse(self.deployer.path(installer.CONFIG).exists())
        manifests = list(
            self.deployer.path(installer.BACKUP_ROOT).glob("*/manifest.json")
        )
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())["status"], "rolled-back")

    def test_health_check_uses_installed_package_without_creating_state_or_bytecode(
        self,
    ):
        self.use_real_package()
        self.install()
        (self.source / "timelapse/capture.py").write_text(
            "raise RuntimeError('source')"
        )
        self.deployer.health_check()
        self.assertFalse(self.deployer.path("/var/lib/pi-timelapse").exists())
        self.assertEqual(list(self.root.rglob("__pycache__")), [])

    def test_installed_import_failure_rolls_back_before_restarting_timers(self):
        self.use_real_package()
        self.install()
        original = self.deployer.path(
            installer.PACKAGE_ROOT + "/capture.py"
        ).read_bytes()
        timer = installer.TIMERS[0]
        self.systemd.enabled[timer] = "enabled"
        self.systemd.active[timer] = "active"
        (self.source / "timelapse/capture.py").write_text(
            "raise RuntimeError('candidate import failed')\n"
        )
        self.systemd.commands.clear()
        with self.assertRaisesRegex(installer.InstallError, "snapshot was restored"):
            self.install()
        self.assertEqual(
            self.deployer.path(installer.PACKAGE_ROOT + "/capture.py").read_bytes(),
            original,
        )
        checks = [
            index
            for index, arguments in enumerate(self.systemd.commands)
            if arguments[0] == installer.sys.executable
        ]
        starts = [
            index
            for index, arguments in enumerate(self.systemd.commands)
            if arguments[:2] == ["systemctl", "start"]
        ]
        self.assertEqual(len(checks), 2)
        self.assertTrue(starts)
        self.assertLess(max(checks), min(starts))
        self.assertEqual(self.systemd.active[timer], "active")

    def test_health_check_rejects_invalid_preserved_configuration(self):
        self.use_real_package()
        self.install()
        configuration = self.deployer.path(installer.CONFIG)
        configuration.write_text('{"schedule": {"interval_seconds": 1}}\n')
        before = configuration.read_bytes()
        with self.assertRaisesRegex(installer.InstallError, "health check failed"):
            self.deployer.health_check()
        self.assertEqual(configuration.read_bytes(), before)

    def test_unchanged_install_still_checks_installed_health(self):
        self.install()
        self.systemd.health_failure = True
        with self.assertRaisesRegex(installer.InstallError, "health check failed"):
            self.install()

    def test_failed_configuration_update_restores_previous_private_configuration(self):
        self.use_real_package()
        self.install()
        configuration = self.deployer.path(installer.CONFIG)
        original = installer.file_state(configuration)
        changes = {
            installer.CONFIG: {
                "before": original,
                "after": installer.regular_file(
                    b'{"schedule": {"interval_seconds": 1}}\n',
                    mode=0o600,
                    owner=(os.getuid(), os.getgid()),
                ),
            }
        }
        with self.assertRaisesRegex(installer.InstallError, "snapshot was restored"):
            self.deployer.apply(changes, self.deployer.states())
        self.assertEqual(installer.file_state(configuration), original)

    def test_failed_rollback_health_keeps_timers_stopped_for_recovery(self):
        self.use_real_package()
        self.install()
        timer = installer.TIMERS[0]
        self.systemd.enabled[timer] = "enabled"
        self.systemd.active[timer] = "active"
        original = self.deployer.path(installer.CONFIG).read_bytes()
        self.deployer.path(installer.CONFIG).write_text(
            '{"schedule": {"interval_seconds": 1}}\n'
        )
        source = self.source / "timelapse/capture.py"
        source.write_bytes(source.read_bytes() + b"\n")
        with self.assertRaisesRegex(installer.InstallError, "rollback failed"):
            self.install()
        self.assertEqual(self.systemd.active[timer], "inactive")
        snapshots = self.deployer.incomplete_snapshots()
        self.assertEqual(len(snapshots), 1)
        self.deployer.path(installer.CONFIG).write_bytes(original)
        self.deployer.recover(snapshots[0], apply=True)
        self.assertEqual(self.systemd.active[timer], "active")

    def test_timer_state_change_after_planning_is_rejected_without_installation(self):
        self.install()
        (self.source / "timelapse/capture.py").write_text("value = 2\n")
        changes, states = self.deployer.plan(), self.deployer.states()
        timer = installer.TIMERS[0]
        self.systemd.enabled[timer] = "enabled"
        self.systemd.active[timer] = "active"
        self.systemd.commands.clear()
        with self.assertRaisesRegex(installer.InstallError, "Unit state changed"):
            self.deployer.apply(changes, states)
        self.assertEqual(
            self.deployer.path(installer.PACKAGE_ROOT + "/capture.py").read_text(),
            "value = 1\n",
        )
        self.assertTrue(
            all(
                arguments[1] in {"is-active", "is-enabled"}
                for arguments in self.systemd.commands
            )
        )
        self.assertEqual(self.systemd.active[timer], "active")

    def test_deployment_lock_excludes_other_install_and_rollback(self):
        backup = self.install()
        changes, states = self.deployer.plan(), self.deployer.states()
        with self.deployer.deployment_lock():
            for operation in (
                lambda: self.deployer.apply(changes, states),
                lambda: self.deployer.rollback(backup, apply=True),
            ):
                with self.assertRaisesRegex(
                    installer.InstallError, "Another deployment"
                ):
                    operation()
        self.assertIsNone(self.deployer.apply(changes, states))
        self.assertEqual(
            stat.S_IMODE(self.deployer.path(installer.LOCK_PATH).stat().st_mode), 0o600
        )

    def test_symlinked_deployment_lock_is_rejected_without_modifications(self):
        sentinel = self.base / "sentinel"
        sentinel.write_text("keep")
        lock = self.deployer.path(installer.LOCK_PATH)
        lock.parent.mkdir(parents=True)
        lock.symlink_to(sentinel)
        with self.assertRaises(OSError):
            self.install()
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertFalse(self.deployer.path(installer.CONFIG).exists())

    def interrupted_update(self):
        self.install()
        timer = installer.TIMERS[0]
        self.systemd.enabled[timer] = "enabled"
        self.systemd.active[timer] = "active"
        for name in ("capture.py", "transfer.py"):
            (self.source / "timelapse" / name).write_text("value = 2\n")
        write_state = installer.write_state

        def interrupted_write(path, state):
            write_state(path, state)
            if path == self.deployer.path(installer.PACKAGE_ROOT + "/capture.py"):
                raise KeyboardInterrupt("Interrupted deployment")

        with patch.object(installer, "write_state", side_effect=interrupted_write):
            with self.assertRaises(KeyboardInterrupt):
                self.install()
        snapshots = self.deployer.incomplete_snapshots()
        self.assertEqual(len(snapshots), 1)
        return snapshots[0]

    def test_interrupted_install_requires_explicit_recovery_before_more_updates(self):
        backup = self.interrupted_update()
        installed = self.deployer.path(installer.PACKAGE_ROOT + "/capture.py")
        self.assertEqual(installed.read_text(), "value = 2\n")
        commands = len(self.systemd.commands)
        self.deployer.recover(backup)
        self.assertEqual(len(self.systemd.commands), commands)
        self.assertEqual(installed.read_text(), "value = 2\n")
        with self.assertRaisesRegex(installer.InstallError, "requires --recover"):
            self.install()
        self.deployer.recover(backup, apply=True)
        self.assertEqual(installed.read_text(), "value = 1\n")
        self.assertEqual(self.systemd.active[installer.TIMERS[0]], "active")
        self.assertEqual(self.deployer.incomplete_snapshots(), [])
        self.assertEqual(
            json.loads((backup / "manifest.json").read_text())["status"], "rolled-back"
        )

    def test_interrupted_recovery_can_be_retried(self):
        backup = self.interrupted_update()
        write_state = installer.write_state

        def failed_write(path, state):
            if path == self.deployer.path(installer.PACKAGE_ROOT + "/capture.py"):
                raise OSError("Storage temporarily unavailable")
            write_state(path, state)

        with patch.object(installer, "write_state", side_effect=failed_write):
            with self.assertRaisesRegex(OSError, "Storage temporarily unavailable"):
                self.deployer.recover(backup, apply=True)
        self.assertEqual(
            json.loads((backup / "manifest.json").read_text())["status"], "recovering"
        )
        self.deployer.recover(backup, apply=True)
        self.assertEqual(self.deployer.incomplete_snapshots(), [])

    def test_recovery_refuses_completed_snapshot_and_later_user_edits(self):
        backup = self.install()
        with self.assertRaisesRegex(installer.InstallError, "interrupted deployment"):
            self.deployer.recover(backup, apply=True)
        backup = self.interrupted_update()
        installed = self.deployer.path(installer.PACKAGE_ROOT + "/capture.py")
        installed.write_text("user_change = True\n")
        with self.assertRaisesRegex(installer.InstallError, "later change"):
            self.deployer.recover(backup, apply=True)
        self.assertEqual(installed.read_text(), "user_change = True\n")

    def test_snapshot_rejects_unmanaged_paths_and_units_without_modifications(self):
        backup = self.install()
        manifest_path = backup / "manifest.json"
        original = json.loads(manifest_path.read_text())
        for variant in ("path", "unit"):
            with self.subTest(variant=variant):
                invalid = copy.deepcopy(original)
                if variant == "path":
                    invalid["files"]["/etc/shadow"] = {
                        "before": {"type": "absent"},
                        "after": {"type": "absent"},
                    }
                else:
                    invalid["units"]["timers"]["unrelated.timer"] = {
                        "enabled": "enabled",
                        "active": "active",
                    }
                manifest_path.write_text(json.dumps(invalid))
                before_commands = len(self.systemd.commands)
                with self.assertRaises(installer.InstallError):
                    self.deployer.rollback(backup, apply=True)
                self.assertEqual(len(self.systemd.commands), before_commands)
        self.assertTrue(self.deployer.path(installer.CONFIG).exists())

    def test_symlinked_destination_and_parent_are_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        target = outside / "config.json"
        target.write_text("{}")
        self.deployer.path("/etc").mkdir()
        self.deployer.path(installer.CONFIG).symlink_to(target)
        with self.assertRaises(installer.InstallError):
            self.deployer.plan()
        self.deployer.path(installer.CONFIG).unlink()
        self.deployer.path("/usr").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(installer.InstallError):
            self.deployer.plan()
        self.assertEqual(target.read_text(), "{}")

    def test_configuration_defaults_are_parsed_without_executing_module_code(self):
        marker = self.base / "must-not-exist"
        (self.source / "timelapse/config.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\nDEFAULTS = {{'server': None}}\n"
        )
        self.deployer.plan()
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
