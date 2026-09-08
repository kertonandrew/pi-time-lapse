import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from timelapse import ssh_gateway, transfer


CONFIG = {
    "root": "/srv/camera-archive",
    "device_id": "camera01",
    "receiver_path": "/usr/local/lib/pi-timelapse-receiver/receiver.py",
    "python_path": "/opt/pi-timelapse-receiver/bin/python3",
    "rsync_path": "/usr/bin/rsync",
}
RSYNC_COMMAND = "rsync --server -cRe.LsfxCIvu --timeout=15 --partial-dir .rsync-partial . /srv/camera-archive/camera01/incoming/"


def receiver_command(action="init", **overrides):
    config = dict(CONFIG, **overrides)
    return shlex.join(
        [
            "python3",
            config["receiver_path"],
            action,
            "--root",
            config["root"],
            "--device",
            config["device_id"],
        ]
    )


class GatewayCommandTests(unittest.TestCase):
    def test_actual_transfer_receiver_commands_map_to_installed_isolated_module(self):
        for action in ("init", "commit", "commit-batch"):
            with (
                self.subTest(action=action),
                patch.object(
                    transfer, "_run_process", return_value=b'{"protocol":1}'
                ) as run,
            ):
                config = dict(
                    CONFIG,
                    remote_root=CONFIG["root"],
                    host="archive.example.invalid",
                    user="ingest",
                    port=22,
                    identity_file="/tmp/synthetic-key",
                    known_hosts_file="/tmp/synthetic-hosts",
                    file_timeout_seconds=5,
                )
                transfer._receiver(
                    config, action, {"protocol": 1}, float("inf"), lambda: True
                )
                kind, arguments = ssh_gateway.command_plan(
                    run.call_args.args[0][-1], CONFIG
                )
                self.assertEqual(kind, "receiver")
                self.assertEqual(
                    arguments,
                    [
                        CONFIG["python_path"],
                        "-I",
                        "-m",
                        "timelapse.receiver",
                        action,
                        "--root",
                        CONFIG["root"],
                        "--device",
                        CONFIG["device_id"],
                    ],
                )
                self.assertNotIn(CONFIG["receiver_path"], arguments)

    def test_rsync_341_real_server_command_has_only_fixed_receiver_options(self):
        kind, arguments = ssh_gateway.command_plan(RSYNC_COMMAND, CONFIG)
        self.assertEqual(kind, "rsync")
        self.assertEqual(arguments[0], "/usr/bin/rsync")
        self.assertIn("--no-links", arguments)
        self.assertIn("--no-devices", arguments)
        self.assertIn("--no-specials", arguments)
        self.assertIn("--max-size=67108864", arguments)
        self.assertNotIn("--sender", arguments)
        self.assertNotIn(CONFIG["root"], arguments)

    def test_known_incremental_compatibility_marker_is_not_a_client_option(self):
        self.assertEqual(
            ssh_gateway.command_plan(
                RSYNC_COMMAND.replace("-cRe.L", "-cRe.iL"), CONFIG
            )[0],
            "rsync",
        )

    def test_other_devices_roots_actions_and_executables_are_rejected(self):
        commands = [
            receiver_command(device_id="other"),
            receiver_command(root="/srv/other"),
            receiver_command(receiver_path="/tmp/evil.py"),
            receiver_command("reindex"),
            receiver_command().replace("python3", "/usr/bin/python3", 1),
            receiver_command() + " --root /tmp",
            receiver_command().replace(
                "--root /srv/camera-archive", "--root /srv/camera-archive/"
            ),
            "python3 -c print(1)",
            "internal-sftp",
            "scp -t /tmp/file",
        ]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ValueError):
                ssh_gateway.command_plan(command, CONFIG)

    def test_rsync_delete_download_protected_args_and_path_options_are_rejected(self):
        commands = [
            RSYNC_COMMAND.replace("--server", "--server --sender"),
            RSYNC_COMMAND.replace("--timeout=15", "--delete --timeout=15"),
            RSYNC_COMMAND.replace("-cRe.LsfxCIvu", "-scRe.LsfxCIvu"),
            "rsync --server -scRe.LsfxCIvu",
            RSYNC_COMMAND.replace("--partial-dir .rsync-partial", "--partial-dir /tmp"),
            RSYNC_COMMAND.replace("incoming/", "images/"),
            RSYNC_COMMAND.replace("camera01/incoming/", "other/incoming/"),
            RSYNC_COMMAND.replace("incoming/", "incoming/../images/"),
            RSYNC_COMMAND + " /tmp/another",
            RSYNC_COMMAND.replace("-cRe.LsfxCIvu", "-lcRe.LsfxCIvu"),
            RSYNC_COMMAND.replace("-cRe.LsfxCIvu", "-cRLe.LsfxCIvu"),
            RSYNC_COMMAND.replace("--server", "--server --daemon"),
            RSYNC_COMMAND.replace(
                "--timeout=15", "--timeout=15 --log-file=/tmp/target"
            ),
            RSYNC_COMMAND.replace(
                "--timeout=15", "--timeout=15 --files-from=/etc/passwd"
            ),
            RSYNC_COMMAND.replace("--timeout=15", "--timeout=15 --inplace"),
            RSYNC_COMMAND.replace("--timeout=15", "--timeout=15 --link-dest=/etc"),
        ]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ValueError):
                ssh_gateway.command_plan(command, CONFIG)

    def test_shell_quoting_environment_control_bytes_and_extra_tokens_are_rejected(
        self,
    ):
        base = receiver_command()
        commands = [
            "",
            " " + base,
            base + " ",
            base.replace(" ", "  ", 1),
            base.replace("python3", "'python3'", 1),
            base.replace("python3", '"python3"', 1),
            "ENV=1 " + base,
            base + "; id",
            base + " && id",
            base + "\n/bin/sh",
            base + " $(id)",
            base + " `id`",
            base + " > /tmp/proof",
            base + "\x00",
            base + "\t",
            "x" * 2049,
            base + "é",
        ]
        for command in commands:
            with self.subTest(length=len(command)), self.assertRaises(ValueError):
                ssh_gateway.command_plan(command, CONFIG)

    def test_local_configuration_rejects_ambiguous_or_unsafe_shapes(self):
        changes = [
            {"root": "/"},
            {"root": "/srv/../tmp"},
            {"root": "/srv//archive"},
            {"root": "/srv/./archive"},
            {"root": "/srv/archive/"},
            {"root": "/srv/a b"},
            {"root": "/srv/archive;id"},
            {"python_path": "python3"},
            {"device_id": "../other"},
            {"timeout_seconds": True},
            {"timeout_seconds": 301},
            {"timeout_seconds": 0},
            {"allow_shell": True},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                ssh_gateway.validate_config(dict(CONFIG, **change))

    def test_duplicate_configuration_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            json.loads(
                '{"root":"/srv/a","root":"/srv/b"}',
                object_pairs_hook=ssh_gateway._unique_object,
            )

    def test_invalid_command_never_starts_a_process(self):
        with (
            patch.object(ssh_gateway.subprocess, "Popen") as popen,
            self.assertRaises(ValueError),
        ):
            ssh_gateway.execute("id", CONFIG)
        popen.assert_not_called()

    def test_no_root_execution_is_permitted(self):
        with (
            patch.object(ssh_gateway.sys, "platform", "linux"),
            patch.object(ssh_gateway.os, "geteuid", return_value=0),
            self.assertRaises(ValueError),
        ):
            ssh_gateway.execute(receiver_command(), CONFIG)


class GatewayProcessTests(unittest.TestCase):
    def test_configuration_reader_checks_file_type_size_permissions_and_duplicates(
        self,
    ):
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name).resolve()
            config = parent / "gateway.json"
            real_fstat = os.fstat

            def root_owned_stat(descriptor):
                values = list(real_fstat(descriptor))
                values[4] = 0
                return os.stat_result(values)

            def parent_fd(path, owner_ids):
                self.assertEqual(owner_ids, {0})
                return os.open(parent, os.O_RDONLY | os.O_DIRECTORY)

            with (
                patch.object(ssh_gateway, "_directory_fd", side_effect=parent_fd),
                patch.object(ssh_gateway.os, "fstat", side_effect=root_owned_stat),
            ):
                config.write_text(json.dumps(CONFIG))
                config.chmod(0o644)
                self.assertEqual(
                    ssh_gateway.load_config(config)["device_id"], "camera01"
                )
                config.chmod(0o666)
                with self.assertRaises(ValueError):
                    ssh_gateway.load_config(config)
                config.chmod(0o644)
                config.write_text(" " * 4097)
                with self.assertRaises(ValueError):
                    ssh_gateway.load_config(config)
                config.write_text('{"root":"/a","root":"/b"}')
                with self.assertRaises(ValueError):
                    ssh_gateway.load_config(config)
                config.unlink()
                config.symlink_to(parent / "outside.json")
                with self.assertRaises(OSError):
                    ssh_gateway.load_config(config)
                config.unlink()
                os.mkfifo(config)
                with self.assertRaises(ValueError):
                    ssh_gateway.load_config(config)

    def test_configuration_owned_by_ingest_is_rejected(self):
        if os.geteuid() == 0:
            self.skipTest("Ownership fixture requires an unprivileged test user")
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name).resolve()
            config = parent / "gateway.json"
            config.write_text(json.dumps(CONFIG))
            with (
                patch.object(
                    ssh_gateway,
                    "_directory_fd",
                    side_effect=lambda path, owner_ids: os.open(
                        parent, os.O_RDONLY | os.O_DIRECTORY
                    ),
                ),
                self.assertRaises(ValueError),
            ):
                ssh_gateway.load_config(config)

    def test_worker_uses_clean_environment_fixed_arguments_and_no_shell(self):
        with (
            patch.dict(
                os.environ,
                {
                    "LD_PRELOAD": "/tmp/evil",
                    "PYTHONPATH": "/tmp/evil",
                    "RSYNC_PROTECT_ARGS": "1",
                },
            ),
            patch.object(ssh_gateway.subprocess, "Popen") as popen,
        ):
            popen.return_value.wait.return_value = 0
            self.assertEqual(
                ssh_gateway._run(["/usr/bin/rsync", "--server"], 10, (10, 11), 120), 0
            )
            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs["env"], ssh_gateway.CLEAN_ENVIRONMENT)
            self.assertFalse(kwargs.get("shell", False))
            self.assertTrue(kwargs["close_fds"])
            self.assertEqual(kwargs["pass_fds"], (10, 11))
            self.assertTrue(kwargs["start_new_session"])
            popen.return_value.wait.assert_called_once_with(timeout=120)

    def test_wall_timeout_kills_the_whole_worker_group(self):
        with (
            patch.object(ssh_gateway.subprocess, "Popen") as popen,
            patch.object(ssh_gateway.os, "killpg") as kill,
        ):
            popen.return_value.pid = 12345
            popen.return_value.poll.return_value = None
            popen.return_value.wait.side_effect = [
                subprocess.TimeoutExpired("synthetic", 5),
                -9,
            ]
            self.assertEqual(ssh_gateway._run(["/usr/bin/rsync"], 10, (10,), 5), 124)
            kill.assert_called_once_with(12345, ssh_gateway.signal.SIGKILL)

    def test_interruption_cleans_up_detached_worker(self):
        with (
            patch.object(ssh_gateway.subprocess, "Popen") as popen,
            patch.object(ssh_gateway.os, "killpg") as kill,
        ):
            popen.return_value.pid = 12345
            popen.return_value.poll.return_value = None
            popen.return_value.wait.side_effect = [SystemExit(143), -9]
            with self.assertRaises(SystemExit):
                ssh_gateway._run(["/usr/bin/rsync"], 10, (10,), 5)
            kill.assert_called_once_with(12345, ssh_gateway.signal.SIGKILL)

    def test_rejections_do_not_echo_remote_command_or_configuration(self):
        with (
            patch.object(ssh_gateway, "load_config", return_value=CONFIG),
            patch.dict(
                os.environ, {"SSH_ORIGINAL_COMMAND": "private-attacker-supplied-value"}
            ),
            patch.object(ssh_gateway.sys, "stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(ssh_gateway.main(["--config", "/etc/camera.json"]), 1)
            self.assertEqual(stderr.getvalue(), "Camera SSH command rejected\n")

    def test_symlinked_or_public_incoming_and_partial_directories_are_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            device = Path(name)
            (device / "outside").mkdir()
            (device / "incoming").symlink_to(device / "outside")

            @contextlib.contextmanager
            def device_lock(config):
                descriptor = os.open(device, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    yield descriptor
                finally:
                    os.close(descriptor)

            with (
                patch.object(ssh_gateway, "_device_lock", device_lock),
                patch.object(ssh_gateway.sys, "platform", "linux"),
                patch.object(ssh_gateway, "_run") as run,
            ):
                if os.geteuid() == 0:
                    self.skipTest(
                        "Private ownership fixture requires an unprivileged test user"
                    )
                with self.assertRaises(OSError):
                    ssh_gateway.execute(RSYNC_COMMAND, CONFIG)
                (device / "incoming").unlink()
                (device / "incoming").mkdir(mode=0o755)
                (device / "incoming").chmod(0o755)
                with self.assertRaises(ValueError):
                    ssh_gateway.execute(RSYNC_COMMAND, CONFIG)
                (device / "incoming").chmod(0o700)
                (device / "incoming/.rsync-partial").symlink_to(device / "outside")
                with self.assertRaises(OSError):
                    ssh_gateway.execute(RSYNC_COMMAND, CONFIG)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
