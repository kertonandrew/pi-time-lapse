import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deploy import bundle, remote


class PowerTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "metrics.sqlite3"
        self.proc = self.root / "proc"
        boot = self.proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text("current-boot\n")
        (self.proc / "uptime").write_text("1000.0 500.0\n")
        self.utc = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "CREATE TABLE samples (id INTEGER PRIMARY KEY, timestamp_utc TEXT, "
                "boot_id TEXT, uptime_seconds REAL, power_input_status TEXT, "
                "battery_charge_percent REAL, battery_temperature_c REAL)"
            )

    def sample(self, **overrides):
        row = {
            "timestamp_utc": (self.utc - timedelta(seconds=60)).isoformat(),
            "boot_id": "current-boot",
            "uptime_seconds": 940.0,
            "power_input_status": "PRESENT",
            "battery_charge_percent": 82.0,
            "battery_temperature_c": 21.0,
        }
        row.update(overrides)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT INTO samples (timestamp_utc,boot_id,uptime_seconds,"
                "power_input_status,battery_charge_percent,battery_temperature_c) "
                "VALUES (?,?,?,?,?,?)",
                tuple(row.values()),
            )

    def read(self):
        return remote.power_status(self.database, proc=self.proc, utc=self.utc)

    def test_recent_same_boot_external_power_is_usable(self):
        self.sample()
        status = self.read()
        self.assertTrue(status["available"])
        self.assertTrue(status["fresh"])
        self.assertTrue(status["input_present"])
        self.assertEqual(status["age_seconds"], 60)
        self.assertEqual(status["battery_charge_percent"], 82)
        self.assertEqual(status["battery_temperature_c"], 21)

    def test_clock_changes_old_boot_and_stale_monotonic_samples_fail_closed(self):
        cases = (
            {"timestamp_utc": (self.utc - timedelta(minutes=20)).isoformat()},
            {"timestamp_utc": (self.utc + timedelta(minutes=1)).isoformat()},
            {"timestamp_utc": self.utc.replace(tzinfo=None).isoformat()},
            {"boot_id": "previous-boot"},
            {"uptime_seconds": 1.0},
            {"uptime_seconds": 1100.0},
            {"uptime_seconds": float("inf")},
            {"uptime_seconds": None},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.sample(**changes)
                self.assertFalse(self.read()["fresh"])

    def test_latest_sample_overrides_older_usable_input(self):
        self.sample()
        for input_status in ("NOT_PRESENT", "WEAK", "BAD", "UNKNOWN", None):
            with self.subTest(input_status=input_status):
                self.sample(power_input_status=input_status)
                status = self.read()
                self.assertTrue(status["fresh"])
                self.assertFalse(status["input_present"])

    def test_missing_empty_and_corrupt_databases_are_unavailable_without_creation(self):
        self.assertFalse(self.read()["available"])
        missing = self.root / "missing.sqlite3"
        self.assertFalse(
            remote.power_status(missing, proc=self.proc, utc=self.utc)["available"]
        )
        self.assertFalse(missing.exists())
        self.database.write_bytes(b"not sqlite")
        self.assertFalse(self.read()["available"])

    def test_default_power_source_uses_hardware_environment_despite_broken_camera_config(
        self,
    ):
        self.sample()
        environment = self.root / "pi-hardware.env"
        environment.write_text(f'PI_HARDWARE_DATABASE="{self.database}"\n')
        camera_config = self.root / "camera-config.json"
        original_power_status = remote.power_status

        def fixture_path(value):
            if str(value) == "/etc/default/pi-hardware":
                return environment
            if str(value) == "/usr/local/lib":
                return self.root
            return Path(value)

        def fixture_power():
            return original_power_status(proc=self.proc, utc=self.utc)

        for content in (
            json.dumps({"hardware_database": str(self.root / "wrong.sqlite3")}),
            "invalid camera configuration",
        ):
            with self.subTest(content=content):
                camera_config.write_text(content)
                with (
                    patch.object(remote, "CONFIG", camera_config),
                    patch.object(remote, "Path", side_effect=fixture_path),
                    patch.object(remote, "power_status", side_effect=fixture_power),
                    patch.object(remote, "BACKUPS", self.root / "backups"),
                    patch.object(
                        remote.shutil,
                        "disk_usage",
                        return_value=SimpleNamespace(free=128 * 1024 * 1024),
                    ),
                    patch.object(
                        remote, "regular_bytes", wraps=remote.regular_bytes
                    ) as reads,
                ):
                    checks = remote.require_gates(
                        {
                            "operation": "recover",
                            "staging_dir": str(self.root),
                            "min_free_mb": 64,
                            "stable_power": True,
                        }
                    )
                self.assertTrue(checks["fresh_external_power"])
                self.assertFalse(
                    any(call.args[0] == camera_config for call in reads.call_args_list)
                )

    def test_default_database_remains_usable_when_hardware_environment_is_absent(self):
        self.sample()
        camera_config = self.root / "invalid-camera.json"
        camera_config.write_text("invalid")

        def fixture_path(value):
            if str(value) == "/etc/default/pi-hardware":
                return self.root / "missing.env"
            if str(value) == "/var/lib/pi-hardware/metrics.sqlite3":
                return self.database
            return Path(value)

        with (
            patch.object(remote, "CONFIG", camera_config),
            patch.object(remote, "Path", side_effect=fixture_path),
        ):
            status = remote.power_status(proc=self.proc, utc=self.utc)
        self.assertTrue(status["fresh"])
        self.assertTrue(status["input_present"])


class DeploymentGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        backup_patch = patch.object(remote, "BACKUPS", root / "backups")
        backup_patch.start()
        self.addCleanup(backup_patch.stop)
        self.request = {
            "staging_dir": str(root / "staging"),
            "min_free_mb": 64,
            "stable_power": True,
        }
        self.power = {"available": True, "fresh": True, "input_present": True}
        self.room = SimpleNamespace(free=128 * 1024 * 1024)

    def test_confirmed_fresh_external_input_and_room_on_all_filesystems_pass(self):
        with (
            patch.object(remote, "power_status", return_value=self.power),
            patch.object(remote.shutil, "disk_usage", return_value=self.room),
        ):
            checks = remote.require_gates(self.request)
        self.assertTrue(checks["sufficient_disk"])
        self.assertTrue(checks["fresh_external_power"])

    def test_explicit_stable_power_confirmation_is_required(self):
        for value in (None, False, "yes", 1):
            with (
                self.subTest(value=value),
                patch.object(remote, "power_status", return_value=self.power),
                patch.object(remote.shutil, "disk_usage", return_value=self.room),
            ):
                with self.assertRaises(ValueError):
                    remote.require_gates({**self.request, "stable_power": value})

    def test_stale_or_missing_external_input_blocks_deployment(self):
        for power in (
            {**self.power, "fresh": False},
            {**self.power, "input_present": False},
        ):
            with (
                self.subTest(power=power),
                patch.object(remote, "power_status", return_value=power),
                patch.object(remote.shutil, "disk_usage", return_value=self.room),
            ):
                with self.assertRaises(ValueError):
                    remote.require_gates(self.request)

    def test_low_space_on_any_destination_blocks_deployment(self):
        for index in range(3):
            sizes = [self.room, self.room, self.room]
            sizes[index] = SimpleNamespace(free=32 * 1024 * 1024)
            with (
                self.subTest(destination=index),
                patch.object(remote, "power_status", return_value=self.power),
                patch.object(remote.shutil, "disk_usage", side_effect=sizes),
            ):
                with self.assertRaises(ValueError):
                    remote.require_gates(self.request)


class RemoteOperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.staging = self.root / "jobs"
        self.staging.mkdir(mode=0o700)
        self.request = {
            "operation": "deploy",
            "staging_dir": "/var/lib/pi-deploy-test",
            "job": "a" * 32,
            "snapshot": "20260101T120000Z-" + "b" * 12,
            "min_free_mb": 64,
            "stable_power": True,
            "apply": True,
            "release_id": "c" * 64,
            "tools": {
                name: base64.b64encode(b"pass\n").decode() for name in remote.TOOLS
            },
        }
        self.addCleanup(patch.stopall)
        patch.object(remote.os, "geteuid", return_value=0).start()
        patch.object(remote.os, "umask").start()
        patch.object(remote.sys, "path", list(sys.path)).start()
        patch.object(remote, "BACKUPS", self.root / "backups").start()

    def redirected_path(self, name):
        return self.staging if str(name) == self.request["staging_dir"] else Path(name)

    def test_invalid_staging_job_and_snapshot_are_rejected_before_reading_or_writing(
        self,
    ):
        invalid_requests = [
            {"staging_dir": path}
            for path in (
                "/tmp/deploy",
                "/var/lib/../other",
                "/var/lib/deploy/subdirectory",
                "relative",
                "/var/lib/deploy;command",
            )
        ]
        invalid_requests += [
            {"job": name} for name in ("../outside", "f" * 33, "not-a-job")
        ]
        invalid_requests += [
            {"operation": operation, "snapshot": name}
            for operation in ("rollback", "recover")
            for name in ("../snapshot", "/etc/shadow", "latest")
        ]
        with (
            patch.object(remote, "require_gates") as gates,
            patch.object(remote, "status") as status,
            patch.object(remote, "install_tools") as tools,
            patch.object(remote.subprocess, "run") as run,
        ):
            for change in invalid_requests:
                with self.subTest(change=change), self.assertRaises(ValueError):
                    remote.handle({**self.request, **change})
            gates.assert_not_called()
            status.assert_not_called()
            tools.assert_not_called()
            run.assert_not_called()

    def test_non_root_bootstrap_and_invalid_disk_budget_are_rejected(self):
        with (
            patch.object(remote.os, "geteuid", return_value=1000),
            self.assertRaises(ValueError),
        ):
            remote.handle(self.request)
        for budget in (True, 0, 16, 65537, "64"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                remote.handle({**self.request, "min_free_mb": budget})

    def test_failed_power_preflight_never_creates_job_or_launches_worker(self):
        with (
            patch.object(
                remote, "require_gates", side_effect=ValueError("Unstable input")
            ),
            patch.object(remote, "secure_directory") as directory,
            patch.object(remote.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "Unstable"):
                remote.handle(self.request)
            directory.assert_not_called()
            run.assert_not_called()
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_dry_run_does_not_launch_worker_or_require_stable_power(self):
        expected = {"applied": False}
        with (
            patch.object(remote, "require_gates") as gates,
            patch.object(remote, "preview", return_value=expected),
            patch.object(remote.subprocess, "run") as run,
        ):
            result = remote.handle(
                {
                    **self.request,
                    "operation": "plan",
                    "apply": False,
                    "stable_power": False,
                }
            )
        self.assertEqual(result, expected)
        gates.assert_not_called()
        run.assert_not_called()
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_plan_never_executes_bundled_installer_or_configuration(self):
        repository = Path(__file__).resolve().parents[1]
        source = self.root / "untrusted-source"
        for name in bundle.BUNDLE_PATHS:
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repository / name, destination)
        installer_marker = self.root / "installer-was-executed"
        configuration_marker = self.root / "configuration-was-executed"
        (source / "deploy/install.py").write_text(
            f"from pathlib import Path\nPath({str(installer_marker)!r}).touch()\n"
            "raise RuntimeError('Untrusted installer was executed')\n"
        )
        configuration = source / "timelapse/config.py"
        configuration.write_text(
            configuration.read_text()
            + f"\nPath({str(configuration_marker)!r}).touch()\n"
        )
        artifact = self.root / "untrusted.tar.gz"
        metadata = bundle.build_bundle(source, artifact)
        directory = self.root / "preview"
        directory.mkdir()
        payload = {
            name: base64.b64encode((repository / name).read_bytes()).decode()
            for name in remote.TOOLS
        }
        remote.install_tools(directory, payload)
        request = {
            **self.request,
            **metadata,
            "operation": "plan",
            "apply": False,
            "artifact": base64.b64encode(artifact.read_bytes()).decode(),
            "camera_config": base64.b64encode(b"{}").decode(),
        }
        target = self.root / "target"
        target.mkdir()
        load_installer = remote.load_installer

        def safe_systemd(arguments):
            return 0, "disabled" if arguments[1] == "is-enabled" else "inactive", ""

        def isolated_installer(trusted_source):
            module = load_installer(trusted_source)
            constructor = module.Installer
            constructor.preflight = lambda self, apply=False: None
            module.Installer = lambda source_root: constructor(
                source_root=source_root,
                root=target,
                run=safe_systemd,
                owner=(os.getuid(), os.getgid()),
            )
            return module

        with (
            patch.object(remote, "load_installer", side_effect=isolated_installer),
            patch.object(remote, "gates", return_value={}),
        ):
            result = remote.preview(directory, request)
        self.assertFalse(result["applied"])
        self.assertTrue(result["camera_config_supplied"])
        self.assertIn("/etc/pi-timelapse.json", result["files"])
        self.assertFalse(installer_marker.exists())
        self.assertFalse(configuration_marker.exists())
        self.assertEqual(list(target.iterdir()), [])

    def test_apply_queues_private_job_and_returns_without_waiting_for_installation(
        self,
    ):
        with (
            patch.object(remote, "Path", side_effect=self.redirected_path),
            patch.object(remote, "secure_directory"),
            patch.object(remote, "require_gates", return_value={}),
            patch.object(
                remote.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as launch,
            patch.object(remote, "worker") as worker,
        ):
            result = remote.handle(self.request)
        self.assertEqual(result["state"], "queued")
        job = self.staging / self.request["job"]
        self.assertEqual(
            json.loads((job / "status.json").read_text())["state"], "queued"
        )
        self.assertEqual(json.loads((job / "request.json").read_text()), self.request)
        self.assertEqual(stat.S_IMODE(job.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((job / "request.json").stat().st_mode), 0o600)
        arguments = launch.call_args.args[0]
        self.assertEqual(arguments[0], "systemd-run")
        self.assertIn("--no-block", arguments)
        self.assertIn("--property=Type=oneshot", arguments)
        self.assertIn("--worker", arguments)
        self.assertIn("-I", arguments)
        worker.assert_not_called()

    def test_launch_failure_is_persisted_without_raw_subprocess_output(self):
        private = "sensitive failure details"
        with (
            patch.object(remote, "Path", side_effect=self.redirected_path),
            patch.object(remote, "secure_directory"),
            patch.object(remote, "require_gates", return_value={}),
            patch.object(
                remote.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=1, stdout=private, stderr=private
                ),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Could not start"):
                remote.handle(self.request)
        state = (self.staging / self.request["job"] / "status.json").read_text()
        self.assertEqual(json.loads(state)["state"], "failed")
        self.assertNotIn(private, state)

    def worker_directory(self):
        directory = self.staging / self.request["job"]
        directory.mkdir(mode=0o700)
        (directory / "request.json").write_text(json.dumps(self.request))
        return directory

    def test_worker_rechecks_power_before_touching_application_files(self):
        directory = self.worker_directory()
        with (
            patch.object(remote, "require_gates", side_effect=ValueError("Lost input")),
            patch.object(remote, "extract_application") as extract,
            patch.object(remote, "load_installer") as installer,
        ):
            self.assertEqual(remote.worker(directory), 1)
        extract.assert_not_called()
        installer.assert_not_called()
        state = json.loads((directory / "status.json").read_text())
        self.assertEqual(state["state"], "failed")
        self.assertFalse((self.staging / "last-deployment.json").exists())

    def test_worker_rechecks_power_after_planning_and_before_apply(self):
        directory = self.worker_directory()
        installer = Mock()
        module = SimpleNamespace(Installer=Mock(return_value=installer))
        with (
            patch.object(
                remote,
                "require_gates",
                side_effect=[{}, ValueError("Input disappeared")],
            ),
            patch.object(remote, "extract_application"),
            patch.object(remote, "load_installer", return_value=module),
            patch.object(remote, "changes_for", return_value={}),
        ):
            self.assertEqual(remote.worker(directory), 1)
        installer.apply.assert_not_called()
        self.assertEqual(
            json.loads((directory / "status.json").read_text())["state"], "failed"
        )

    def test_worker_failure_keeps_diagnostics_private_and_preserves_previous_success(
        self,
    ):
        directory = self.worker_directory()
        previous = self.staging / "last-deployment.json"
        previous.write_text('{"state":"succeeded","release_id":"previous"}')
        installer = Mock()
        installer.apply.side_effect = ValueError("private installer failure sentinel")
        module = SimpleNamespace(Installer=Mock(return_value=installer))
        with (
            patch.object(remote, "require_gates", return_value={}),
            patch.object(remote, "extract_application"),
            patch.object(remote, "load_installer", return_value=module),
            patch.object(remote, "changes_for", return_value={}),
        ):
            self.assertEqual(remote.worker(directory), 1)
        state = (directory / "status.json").read_text()
        self.assertEqual(json.loads(state)["state"], "failed")
        self.assertNotIn("private installer failure sentinel", state)
        error = directory / "error.log"
        self.assertIn("private installer failure sentinel", error.read_text())
        self.assertEqual(stat.S_IMODE(error.stat().st_mode), 0o600)
        self.assertEqual(json.loads(previous.read_text())["release_id"], "previous")

    def test_worker_success_persists_snapshot_and_final_service_state(self):
        directory = self.worker_directory()
        snapshot = self.root / self.request["snapshot"]
        installer = Mock()
        installer.apply.return_value = snapshot
        installer.states.return_value = {"timers": {"capture": "disabled"}}
        module = SimpleNamespace(Installer=Mock(return_value=installer))
        with (
            patch.object(remote, "require_gates", return_value={}),
            patch.object(remote, "extract_application"),
            patch.object(remote, "load_installer", return_value=module),
            patch.object(remote, "changes_for", return_value={}),
            patch.object(remote, "power_status", return_value={"fresh": True}),
        ):
            self.assertEqual(remote.worker(directory), 0)
        state = json.loads((directory / "status.json").read_text())
        self.assertEqual(state["state"], "succeeded")
        self.assertEqual(state["snapshot"], snapshot.name)
        self.assertEqual(state["units"], installer.states.return_value)
        self.assertEqual(
            json.loads((self.staging / "last-deployment.json").read_text()), state
        )

    def test_status_reports_interrupted_job_when_detached_service_has_stopped(self):
        directory = self.worker_directory()
        (directory / "status.json").write_text('{"state":"running"}')
        result = SimpleNamespace(
            stdout="ActiveState=inactive\nResult=exit-code\n", returncode=0
        )
        with (
            patch.object(remote, "Path", side_effect=self.redirected_path),
            patch.object(remote.subprocess, "run", return_value=result),
            patch.object(remote, "power_status", return_value={"available": False}),
        ):
            state = remote.handle({**self.request, "operation": "status"})
        self.assertEqual(state["job"]["state"], "interrupted")
        self.assertEqual(
            json.loads((directory / "status.json").read_text())["state"], "running"
        )

    def test_regular_file_reader_rejects_symlinks_and_oversized_files(self):
        source = self.root / "input.json"
        source.write_bytes(b"12345")
        link = self.root / "linked.json"
        link.symlink_to(source)
        with self.assertRaises(OSError):
            remote.regular_bytes(link)
        with self.assertRaisesRegex(ValueError, "size limit"):
            remote.regular_bytes(source, limit=4)
        self.assertEqual(remote.regular_bytes(source, limit=5), b"12345")


class SnapshotDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.staging = self.root / "jobs"
        self.staging.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(remote, "BACKUPS", self.backups).start()
        patch.object(remote, "CONFIG", self.root / "camera.json").start()
        patch.object(remote, "power_status", return_value={"available": False}).start()
        patch.object(
            remote.subprocess,
            "run",
            return_value=SimpleNamespace(stdout="ActiveState=active\n", returncode=0),
        ).start()

    def snapshot(self, index, content):
        directory = self.backups / (f"20260101T{index:06}Z-" + "a" * 12)
        directory.mkdir()
        (directory / "manifest.json").write_text(content)
        return directory

    def status(self):
        return remote.status({"staging_dir": str(self.staging)})

    def test_discovery_reports_incomplete_ids_without_snapshot_contents(self):
        private = "private snapshot content sentinel"
        expected = {}
        for index, status in enumerate(
            ("prepared", "recovering", "applied", "rolled-back")
        ):
            directory = self.snapshot(
                index,
                json.dumps(
                    {
                        "status": status,
                        "files": {
                            "private-configuration-path": {"before": {"data": private}}
                        },
                    }
                ),
            )
            expected[directory.name] = status
        result = self.status()
        self.assertEqual(
            {item["snapshot"]: item["status"] for item in result["recent_snapshots"]},
            expected,
        )
        self.assertEqual(
            {item["snapshot"] for item in result["incomplete_snapshots"]},
            {
                name
                for name, status in expected.items()
                if status in {"prepared", "recovering"}
            },
        )
        encoded = json.dumps(result)
        self.assertNotIn(private, encoded)
        self.assertNotIn("private-configuration-path", encoded)
        self.assertNotIn(str(self.backups), encoded)
        self.assertTrue(
            all(
                set(item) == {"snapshot", "status"}
                for item in result["recent_snapshots"]
            )
        )

    def test_corrupt_nonobject_and_unknown_statuses_fail_closed_without_content_leaks(
        self,
    ):
        private = "private status content sentinel"
        variants = (
            "invalid JSON with " + private,
            "[]",
            "null",
            json.dumps({"status": private}),
            json.dumps({"status": {"private": private}}),
            json.dumps({"status": [private]}),
        )
        identifiers = {
            self.snapshot(index, content).name for index, content in enumerate(variants)
        }
        result = self.status()
        self.assertEqual(
            {item["snapshot"] for item in result["recent_snapshots"]}, identifiers
        )
        self.assertTrue(
            all(
                item["status"] in {"unreadable", "unknown"}
                for item in result["recent_snapshots"]
            )
        )
        self.assertNotIn(private, json.dumps(result))

    def test_unreadable_manifest_is_discoverable_without_exception_text(self):
        directory = self.snapshot(1, '{"status":"prepared"}')
        read = remote.regular_bytes

        def deny_manifest(path, limit=65536):
            if path == directory / "manifest.json":
                raise PermissionError("private filename and contents sentinel")
            return read(path, limit)

        with patch.object(remote, "regular_bytes", side_effect=deny_manifest):
            result = self.status()
        self.assertEqual(
            result["incomplete_snapshots"],
            [{"snapshot": directory.name, "status": "unreadable"}],
        )
        self.assertNotIn("private filename and contents sentinel", json.dumps(result))

    def test_invalid_names_and_symlink_directories_are_not_disclosed_or_followed(self):
        private_name = "private-device-name"
        unrelated = self.backups / private_name
        unrelated.mkdir()
        (unrelated / "manifest.json").write_text('{"status":"prepared"}')
        link = self.backups / ("20260101T000001Z-" + "b" * 12)
        link.symlink_to(unrelated, target_is_directory=True)
        result = self.status()
        self.assertEqual(result["recent_snapshots"], [])
        self.assertEqual(result["incomplete_snapshots"], [])
        self.assertNotIn(private_name, json.dumps(result))

    def test_recent_limit_does_not_hide_older_incomplete_recovery_snapshot(self):
        oldest = self.snapshot(0, '{"status":"prepared"}')
        for index in range(1, 13):
            self.snapshot(index, '{"status":"applied"}')
        result = self.status()
        self.assertEqual(len(result["recent_snapshots"]), 10)
        self.assertNotIn(
            oldest.name, {item["snapshot"] for item in result["recent_snapshots"]}
        )
        self.assertEqual(
            result["incomplete_snapshots"],
            [{"snapshot": oldest.name, "status": "prepared"}],
        )


if __name__ == "__main__":
    unittest.main()
