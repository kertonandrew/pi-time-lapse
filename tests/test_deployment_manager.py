import base64
import copy
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy import bundle, manage, profiles


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class DeploymentManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifacts = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.artifacts.cleanup)
        root = Path(cls.artifacts.name).resolve()
        source = root / "source"
        for name in bundle.BUNDLE_PATHS:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"value = 1\n")
        cls.artifact = root / "camera.tar.gz"
        cls.release = bundle.build_bundle(source, cls.artifact)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / "device"
        profiles.initialize_profile(
            self.directory,
            "camera.example",
            "operator",
            "garden-camera",
            "mqtt.example",
            "trusted-camera",
        )
        self.profile_path = self.directory / "profile.json"
        self.profile = profiles.load_profile(self.profile_path)
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)

    def arguments(self, operation="deploy", *extra):
        return manage.parser().parse_args(
            [
                "--profile",
                str(self.profile_path),
                operation,
                "--bundle",
                str(self.artifact),
                *extra,
            ]
        )

    def run_main(self, arguments):
        output, error = io.StringIO(), io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", error):
            code = manage.main(arguments)
        return code, output.getvalue(), error.getvalue()

    def test_profile_rejects_missing_and_extra_fields_at_every_schema_level(self):
        for section in (None, "ssh", "remote", "files"):
            for change in ("missing", "extra"):
                with self.subTest(section=section, change=change):
                    value = copy.deepcopy(self.profile)
                    target = value if section is None else value[section]
                    if change == "missing":
                        target.pop(next(iter(target)))
                    else:
                        target["unrecognized"] = True
                    with self.assertRaises(ValueError):
                        profiles.validate_profile(value)

    def test_profile_rejects_option_injection_and_unsafe_remote_paths(self):
        variants = (
            ("ssh", "host", "-oProxyCommand=anything"),
            ("ssh", "host", "camera.example; touch /tmp/marker"),
            ("ssh", "host", "camera.example\nother-host"),
            ("ssh", "user", "-root"),
            ("ssh", "user", "operator@other.example"),
            ("ssh", "host_key_alias", "-oStrictHostKeyChecking=no"),
            ("ssh", "port", True),
            ("ssh", "port", 0),
            ("ssh", "port", 65536),
            ("ssh", "identity_file", "key\nfile"),
            ("remote", "staging_dir", "/var/lib"),
            ("remote", "staging_dir", "/var/lib/../etc"),
            ("remote", "staging_dir", "/tmp/device"),
            ("remote", "python", "/usr/bin/python3; reboot"),
            ("remote", "python", "python3"),
            ("remote", "min_free_mb", True),
            ("remote", "min_free_mb", 31),
            ("files", "camera", "../camera.json"),
            ("files", "camera", "/etc/camera.json"),
            ("files", "camera", "folder\\camera.json"),
        )
        for section, key, invalid in variants:
            with self.subTest(section=section, key=key, invalid=invalid):
                value = copy.deepcopy(self.profile)
                value[section][key] = invalid
                with self.assertRaises(ValueError):
                    profiles.validate_profile(value)

    def test_boolean_schema_version_is_not_an_integer_version(self):
        self.profile["schema_version"] = True
        with self.assertRaises(ValueError):
            profiles.validate_profile(self.profile)

    def test_initialize_writes_private_inert_templates_without_credentials(self):
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for path in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        camera = json.loads((self.directory / "camera.json").read_text())
        assistant = json.loads((self.directory / "home-assistant.json").read_text())
        self.assertFalse(camera["schedule"]["enabled"])
        self.assertFalse(camera["remote_controls"]["enabled"])
        self.assertFalse(camera["power"]["battery_profile_verified"])
        self.assertIsNone(camera["server"])
        self.assertTrue(assistant["mqtt"]["tls"])
        self.assertIsNone(assistant["mqtt"]["password_file"])
        self.assertEqual(assistant["device_id"], "garden-camera")
        self.assertTrue((self.directory / "hardware.env").read_bytes())

    def test_initialize_refuses_existing_configuration_without_overwriting(self):
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with self.assertRaises(ValueError):
            profiles.initialize_profile(
                self.directory, "new.example", "newuser", "new-camera"
            )
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in self.directory.iterdir()}
        )

    def test_profile_and_private_files_reject_public_permissions(self):
        self.profile_path.chmod(0o640)
        with self.assertRaises(ValueError):
            profiles.load_profile(self.profile_path)
        self.profile_path.chmod(0o600)
        self.directory.chmod(0o750)
        with self.assertRaises(ValueError):
            profiles.load_profile(self.profile_path)

    def test_private_file_rejects_symlinks_nonregular_files_and_oversize(self):
        link = self.directory / "linked.json"
        link.symlink_to(self.profile_path)
        with self.assertRaises(ValueError):
            profiles.private_file(link)
        fifo = self.directory / "pipe"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(ValueError):
            profiles.private_file(fifo)
        with self.assertRaises(ValueError):
            profiles.private_file(self.profile_path, maximum=8)

    def test_private_file_rejects_hardlinks(self):
        linked = self.directory / "shared-profile.json"
        os.link(self.profile_path, linked)
        with self.assertRaises(ValueError):
            profiles.private_file(linked)

    def test_profile_reference_rejects_symlinks_escaping_the_profile(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "camera.json").write_text("{}")
        (self.directory / "linked").symlink_to(outside, target_is_directory=True)
        self.profile["files"]["camera"] = "linked/camera.json"
        with self.assertRaises(ValueError):
            profiles.profile_file(self.profile_path, self.profile, "camera")

    def test_initialize_rejects_symlink_directory(self):
        link = self.root / "link"
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            profiles.initialize_profile(
                link, "camera.example", "operator", "other-camera"
            )

    def test_ssh_enforces_trusted_noninteractive_connection_without_forwarding(self):
        arguments = manage.ssh_arguments(self.profile, self.profile_path)
        options = [
            arguments[index + 1] for index, arg in enumerate(arguments) if arg == "-o"
        ]
        for value in (
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            "ClearAllForwardings=yes",
            "ForwardAgent=no",
            "ConnectionAttempts=1",
            "HostKeyAlias=trusted-camera",
        ):
            self.assertIn(value, options)
        self.assertEqual(arguments[-2], "camera.example")
        self.assertEqual(
            shlex.split(arguments[-1]),
            ["sudo", "-n", "/usr/bin/python3", "-I", "-B", "-"],
        )
        self.assertNotIn("-A", arguments)

    def test_ssh_uses_selected_identity_and_known_hosts_as_separate_arguments(self):
        identity = self.directory / "device-key"
        known_hosts = self.directory / "host-keys"
        profiles.write_private(identity, b"test key")
        profiles.write_private(known_hosts, b"test host key")
        self.profile["ssh"].update(
            identity_file="device-key", known_hosts_file="host-keys"
        )
        arguments = manage.ssh_arguments(self.profile, self.profile_path)
        self.assertEqual(arguments[arguments.index("-i") + 1], str(identity))
        self.assertIn("IdentitiesOnly=yes", arguments)
        self.assertIn("UserKnownHostsFile=" + str(known_hosts), arguments)

    def test_validate_does_not_contact_the_network(self):
        with patch.object(
            manage.subprocess, "run", side_effect=AssertionError("Network call")
        ):
            result = manage.validate_local(self.profile_path, self.profile)
        self.assertFalse(result["network_contacted"])
        self.assertTrue(result["profile_valid"])
        self.assertTrue(all(item["valid"] for item in result["files"].values()))

    def test_operation_preserves_camera_configuration_unless_explicit(self):
        arguments = self.arguments()
        request = manage.operation_request(self.profile_path, self.profile, arguments)
        self.assertNotIn("camera_config", request)
        self.assertNotIn("home_assistant", request)
        self.assertNotIn("hardware_env", request)
        self.assertFalse(request["apply"])
        self.assertIsNone(request["job"])
        self.assertEqual(request["release_id"], self.release["release_id"])
        arguments.include_camera_config = True
        request = manage.operation_request(self.profile_path, self.profile, arguments)
        self.assertEqual(
            base64.b64decode(request["camera_config"]),
            (self.directory / "camera.json").read_bytes(),
        )

    def test_apply_requires_explicit_stable_power(self):
        for operation, extra in (
            ("deploy", []),
            ("rollback", ["--snapshot", "previous"]),
            ("recover", ["--snapshot", "previous"]),
        ):
            with self.subTest(operation=operation):
                arguments = self.arguments(operation, "--apply", *extra)
                with self.assertRaisesRegex(ValueError, "stable-power"):
                    manage.operation_request(self.profile_path, self.profile, arguments)
                arguments.stable_power = True
                request = manage.operation_request(
                    self.profile_path, self.profile, arguments
                )
                self.assertTrue(request["apply"])
                self.assertRegex(request["job"], r"^[a-f0-9]{32}$")

    def test_plan_cannot_apply_and_recovery_cannot_replace_config(self):
        for arguments in (
            self.arguments("plan", "--apply", "--stable-power"),
            self.arguments(
                "rollback", "--snapshot", "previous", "--include-camera-config"
            ),
            self.arguments(
                "recover", "--snapshot", "previous", "--include-camera-config"
            ),
        ):
            with self.subTest(operation=arguments.command):
                with self.assertRaises(ValueError):
                    manage.operation_request(self.profile_path, self.profile, arguments)

    def test_malformed_archive_is_rejected_before_constructing_ssh_connection(self):
        artifact = self.root / "invalid.tar.gz"
        artifact.write_bytes(b"not an archive")
        with patch.object(manage, "Connection") as connection:
            code, _, error = self.run_main(
                ["--profile", str(self.profile_path), "plan", "--bundle", str(artifact)]
            )
        self.assertEqual(code, 1)
        self.assertIn("archive", error)
        connection.assert_not_called()

    def test_connection_failure_keeps_remote_diagnostics_in_private_logs(self):
        sentinel = "private-mqtt-password-sentinel"
        run = Mock(
            return_value=subprocess.CompletedProcess([], 255, sentinel, sentinel)
        )
        connection = manage.Connection(self.profile_path, self.profile, run=run)
        with self.assertRaises(manage.DeploymentError) as raised:
            connection.request(manage.base_request(self.profile, "status"))
        self.assertNotIn(sentinel, str(raised.exception))
        self.assertEqual(stat.S_IMODE(connection.logs.stat().st_mode), 0o700)
        logs = list(connection.logs.glob("*.json"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(stat.S_IMODE(logs[0].stat().st_mode), 0o600)
        self.assertIn(sentinel, logs[0].read_text())

    def test_connection_rejects_symlinked_diagnostic_directory_before_network(self):
        outside = self.root / "outside-logs"
        outside.mkdir(mode=0o700)
        (self.directory / "runs").symlink_to(outside, target_is_directory=True)
        run = Mock()
        connection = manage.Connection(self.profile_path, self.profile, run=run)
        with self.assertRaises(ValueError):
            connection.request(manage.base_request(self.profile, "status"))
        run.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_connection_timeout_records_private_failure_diagnostics(self):
        run = Mock(side_effect=subprocess.TimeoutExpired(["ssh"], 4))
        connection = manage.Connection(self.profile_path, self.profile, run=run)
        with self.assertRaises(manage.DeploymentError):
            connection.request(manage.base_request(self.profile, "status"), timeout=4)
        logs = list(connection.logs.glob("*.json"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(stat.S_IMODE(logs[0].stat().st_mode), 0o600)
        self.assertEqual(json.loads(logs[0].read_text())["timeout_seconds"], 4)

    def test_invalid_json_ssh_response_is_a_sanitized_connection_error(self):
        sentinel = "invalid private response"
        run = Mock(return_value=subprocess.CompletedProcess([], 0, sentinel, ""))
        connection = manage.Connection(self.profile_path, self.profile, run=run)
        with self.assertRaises(manage.DeploymentError) as raised:
            connection.request(manage.base_request(self.profile, "status"))
        self.assertNotIn(sentinel, str(raised.exception))

    def test_nonobject_ssh_response_is_rejected(self):
        for payload in ("null", "[]", '"invalid"'):
            with self.subTest(payload=payload):
                run = Mock(return_value=subprocess.CompletedProcess([], 0, payload, ""))
                connection = manage.Connection(self.profile_path, self.profile, run=run)
                with self.assertRaises(manage.DeploymentError):
                    connection.request(manage.base_request(self.profile, "status"))

    def test_job_status_requires_expected_identity_and_recognized_state(self):
        responses = (
            {},
            {"job": None},
            {"job": {"job_id": "other-job", "state": "succeeded"}},
            {"job": {"job_id": "expected-job"}},
            {"job": {"job_id": "expected-job", "state": "unrecognized"}},
        )
        for response in responses:
            with self.subTest(response=response):
                run = Mock(
                    return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(response), ""
                    )
                )
                connection = manage.Connection(self.profile_path, self.profile, run=run)
                request = manage.base_request(self.profile, "status")
                request["job"] = "expected-job"
                with self.assertRaises(manage.DeploymentError):
                    connection.request(request)

    def test_apply_acknowledgement_must_match_dispatched_job(self):
        request = manage.operation_request(
            self.profile_path,
            self.profile,
            self.arguments("deploy", "--apply", "--stable-power"),
        )
        for response in (
            {},
            {"job_id": "another-job", "state": "queued"},
            {"job_id": request["job"], "state": "succeeded"},
        ):
            with self.subTest(response=response):
                run = Mock(
                    return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(response), ""
                    )
                )
                connection = manage.Connection(self.profile_path, self.profile, run=run)
                with self.assertRaises(manage.DeploymentError):
                    connection.request(request)

    def test_uncertain_dispatch_records_intent_before_sending_over_ssh(self):
        dispatched = []

        def timeout(arguments, **kwargs):
            intents = list((self.directory / "runs").glob("*-intent.json"))
            self.assertEqual(len(intents), 1)
            intent = json.loads(intents[0].read_text())
            self.assertEqual(stat.S_IMODE(intents[0].stat().st_mode), 0o600)
            self.assertEqual(intent["operation"], "deploy")
            dispatched.append(intent)
            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])

        connection = manage.Connection(self.profile_path, self.profile, run=timeout)
        with patch.object(manage, "Connection", return_value=connection):
            code, output, error = self.run_main(
                [
                    "--profile",
                    str(self.profile_path),
                    "deploy",
                    "--bundle",
                    str(self.artifact),
                    "--apply",
                    "--stable-power",
                ]
            )
        self.assertEqual(code, 1)
        self.assertEqual(len(dispatched), 1)
        self.assertIn(dispatched[0]["job_id"], output)
        self.assertIn("dispatching", output)
        self.assertIn("timed out", error)

    def test_wait_returns_terminal_failure_and_success_without_retrying(self):
        for state in ("succeeded", "failed", "interrupted"):
            with self.subTest(state=state):
                connection = manage.Connection(self.profile_path, self.profile)
                clock = Clock()
                response = {"job": {"job_id": "job", "state": state}}
                with (
                    patch.object(
                        connection, "request", return_value=response
                    ) as request,
                    patch.object(manage.time, "monotonic", clock.monotonic),
                    patch.object(manage.time, "sleep", clock.sleep),
                ):
                    self.assertEqual(connection.wait("job", 10), response)
                request.assert_called_once()

    def test_wait_zero_returns_unknown_without_contacting_ssh(self):
        connection = manage.Connection(self.profile_path, self.profile)
        with patch.object(connection, "request") as request:
            result = connection.wait("job", 0)
        request.assert_not_called()
        self.assertEqual(result["job"]["state"], "unknown")

    def test_wait_budget_also_bounds_ssh_requests(self):
        clock = Clock()
        timeouts = []

        def delayed_ssh(arguments, **kwargs):
            timeouts.append(kwargs["timeout"])
            clock.now += kwargs["timeout"]
            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])

        connection = manage.Connection(self.profile_path, self.profile, run=delayed_ssh)
        with (
            patch.object(manage.time, "monotonic", clock.monotonic),
            patch.object(manage.time, "sleep", clock.sleep),
        ):
            result = connection.wait("job", 7)
        self.assertLessEqual(clock.now, 7)
        self.assertTrue(all(timeout <= 7 for timeout in timeouts))
        self.assertEqual(result["job"]["state"], "unknown")

    def test_wait_expiry_does_not_misreport_connection_errors_as_success(self):
        clock = Clock()
        connection = manage.Connection(self.profile_path, self.profile)
        with (
            patch.object(
                connection, "request", side_effect=manage.DeploymentError("offline")
            ),
            patch.object(manage.time, "monotonic", clock.monotonic),
            patch.object(manage.time, "sleep", clock.sleep),
        ):
            result = connection.wait("job", 12)
        self.assertEqual(result["job"]["state"], "unknown")
        self.assertEqual(clock.now, 12)

    def test_cli_wait_limits_reject_invalid_budgets_before_network(self):
        for seconds in (-1, 601):
            with (
                self.subTest(seconds=seconds),
                patch.object(manage, "Connection") as connection,
            ):
                code, _, error = self.run_main(
                    [
                        "--profile",
                        str(self.profile_path),
                        "deploy",
                        "--bundle",
                        str(self.artifact),
                        "--wait",
                        str(seconds),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("between 0 and 600", error)
            connection.assert_not_called()

    def test_main_returns_distinct_exit_codes_for_unknown_and_failed_jobs(self):
        for state, expected in (
            ("succeeded", 0),
            ("failed", 1),
            ("interrupted", 1),
            ("unknown", 2),
        ):
            with self.subTest(state=state):
                connection = Mock()
                connection.request.return_value = {
                    "job": {"job_id": "job", "state": state}
                }
                with patch.object(manage, "Connection", return_value=connection):
                    code, output, error = self.run_main(
                        ["--profile", str(self.profile_path), "status", "--job", "job"]
                    )
                self.assertEqual(code, expected)
                self.assertIn(state, output)
                self.assertEqual(error, "")


if __name__ == "__main__":
    unittest.main()
