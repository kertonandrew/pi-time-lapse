import argparse
import contextlib
import errno
import importlib.machinery
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / "find-timelapse.command"
LOADER = importlib.machinery.SourceFileLoader(
    "discovery_helper_under_test", str(SOURCE)
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
helper = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(helper)


class DiscoveryArgumentsTests(unittest.TestCase):
    def test_defaults_do_not_assume_a_hostname_or_interface(self):
        args = helper.parse_arguments([])
        self.assertIsNone(args.hostname)
        self.assertIsNone(args.interface)
        self.assertIsNone(args.subnet)
        self.assertIsNone(args.report)

    def test_explicit_arguments_accept_an_ipv4_range_and_optional_hostname(self):
        args = helper.parse_arguments(
            [
                "--subnet",
                "192.0.2.9/24",
                "--hostname",
                "camera.example.invalid",
                "--report",
                "/tmp/new-report.txt",
            ]
        )
        self.assertEqual(args.subnet, ipaddress.IPv4Network("192.0.2.0/24"))
        self.assertEqual(args.hostname, "camera.example.invalid")
        self.assertEqual(args.report, Path("/tmp/new-report.txt"))
        self.assertEqual(
            helper.parse_arguments(["--interface", "wlan0"]).interface, "wlan0"
        )

    def test_broad_ipv6_or_malformed_ranges_and_ambiguous_sources_are_rejected(self):
        cases = [
            ["--subnet", "192.0.2.0/21"],
            ["--subnet", "0.0.0.0/0"],
            ["--subnet", "::1/128"],
            ["--subnet", "invalid"],
            ["--subnet", "192.0.2.0/24", "--interface", "wlan0"],
            ["--interface=-a"],
            ["--interface", "wlan0;id"],
            ["--hostname", "camera\n.example.invalid"],
            ["--hostname=-x"],
        ]
        for arguments in cases:
            with (
                self.subTest(arguments=arguments),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                helper.parse_arguments(arguments)
        self.assertEqual(helper.subnet_argument("192.0.0.0/22").num_addresses, 1024)

    def test_mac_default_route_selects_interface_without_en0_assumption(self):
        outputs = [
            "route to: default\n interface: en7\n",
            "inet 192.0.2.5 netmask 0xffffff00 broadcast 192.0.2.255",
        ]
        with (
            patch.object(
                helper.shutil, "which", side_effect=lambda name: "/tools/" + name
            ),
            patch.object(helper, "command_output", side_effect=outputs) as command,
        ):
            network, address, interface = helper.discover_subnet(system="Darwin")
        self.assertEqual(
            (str(network), address, interface), ("192.0.2.0/24", "192.0.2.5", "en7")
        )
        self.assertEqual(command.call_args_list[1].args[0], ["/tools/ifconfig", "en7"])

    def test_explicit_mac_interface_accepts_dotted_netmask_and_rejects_broad_network(
        self,
    ):
        with (
            patch.object(helper.shutil, "which", return_value="/tools/ifconfig"),
            patch.object(
                helper,
                "command_output",
                return_value="inet 192.0.2.5 netmask 255.255.255.0",
            ),
        ):
            self.assertEqual(
                str(helper.discover_subnet("en9", "Darwin")[0]), "192.0.2.0/24"
            )
        with (
            patch.object(helper.shutil, "which", return_value="/tools/ifconfig"),
            patch.object(
                helper,
                "command_output",
                return_value="inet 192.0.2.5 netmask 0xff000000",
            ),
            self.assertRaises(argparse.ArgumentTypeError),
        ):
            helper.discover_subnet("en9", "Darwin")

    def test_linux_uses_json_ip_output_and_selected_interface(self):
        outputs = [
            json.dumps([{"dev": "wlan4"}]),
            json.dumps(
                [
                    {
                        "addr_info": [
                            {"family": "inet", "local": "192.0.2.9", "prefixlen": 24}
                        ]
                    }
                ]
            ),
        ]
        with (
            patch.object(helper.shutil, "which", return_value="/tools/ip"),
            patch.object(helper, "command_output", side_effect=outputs) as command,
        ):
            network, address, interface = helper.discover_subnet(system="Linux")
        self.assertEqual(
            (str(network), address, interface), ("192.0.2.0/24", "192.0.2.9", "wlan4")
        )
        self.assertEqual(command.call_args_list[1].args[0][-2:], ["dev", "wlan4"])


class DiscoveryReportTests(unittest.TestCase):
    def test_default_reports_are_unique_and_private(self):
        with tempfile.TemporaryDirectory() as name:
            real_mkstemp = tempfile.mkstemp
            paths = []
            with patch.object(
                helper.tempfile,
                "mkstemp",
                side_effect=lambda **kwargs: real_mkstemp(dir=name, **kwargs),
            ):
                for _ in range(2):
                    path, report = helper.create_report()
                    with report:
                        report.write("synthetic discovery report\n")
                    paths.append(path)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotEqual(paths[0], paths[1])

    def test_explicit_report_is_exclusive_private_and_never_follows_a_symlink(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / "existing"
            target.write_text("preserve fixture")
            link = root / "link"
            link.symlink_to(target)
            dangling = root / "dangling"
            dangling.symlink_to(root / "absent")
            for path in (target, link, dangling):
                with self.subTest(path=path.name), self.assertRaises(OSError):
                    helper.create_report(path)
            self.assertEqual(target.read_text(), "preserve fixture")
            self.assertFalse((root / "absent").exists())
            path, report = helper.create_report(root / "new")
            report.close()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_explicit_report_open_has_required_exclusion_and_no_follow_flags(self):
        real_open = os.open
        with (
            tempfile.TemporaryDirectory() as name,
            patch.object(helper.os, "open", side_effect=real_open) as opened,
        ):
            path, report = helper.create_report(Path(name) / "new")
            report.close()
            flags = opened.call_args.args[1]
            self.assertTrue(flags & os.O_EXCL)
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertEqual(opened.call_args.args[2], 0o600)

    def test_report_failure_prevents_all_network_work(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / "existing"
            target.write_text("preserve fixture")
            with (
                patch.object(helper, "inspect_host") as inspect,
                patch.object(helper, "discover_subnet") as discover,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    helper.main(["--subnet", "192.0.2.1/32", "--report", str(target)]),
                    1,
                )
            inspect.assert_not_called()
            discover.assert_not_called()
            self.assertEqual(target.read_text(), "preserve fixture")


class DiscoveryNetworkingTests(unittest.TestCase):
    def test_ping_executable_and_timeout_flags_match_each_platform(self):
        with patch.object(helper.shutil, "which", return_value="/discovered/ping"):
            self.assertEqual(
                helper.ping_command("Darwin"),
                ["/discovered/ping", "-n", "-c", "1", "-W", "300"],
            )
            self.assertEqual(
                helper.ping_command("Linux"),
                ["/discovered/ping", "-n", "-c", "1", "-W", "1"],
            )
            self.assertEqual(
                helper.ping_command("Windows"),
                ["/discovered/ping", "-n", "1", "-w", "300"],
            )
            self.assertIsNone(helper.ping_command("Other"))
        with patch.object(helper.shutil, "which", return_value=None):
            self.assertIsNone(helper.ping_command("Linux"))

    def test_inspection_has_bounded_ping_and_socket_and_sanitizes_remote_banner(self):
        connection = Mock()
        connection.connect_ex.return_value = 0
        connection.recv.return_value = b"SSH-2.0-fixture\x1b[2J\nforged-report\x00"
        with (
            patch.object(
                helper.subprocess, "run", return_value=Mock(returncode=0)
            ) as run,
            patch.object(helper.socket, "socket") as sock,
        ):
            sock.return_value.__enter__.return_value = connection
            address, replied, result, banner = helper.inspect_host(
                ipaddress.IPv4Address("192.0.2.5"), ["/discovered/ping", "-c", "1"]
            )
        self.assertEqual((address, replied, result), ("192.0.2.5", True, 0))
        self.assertNotIn("\x1b", banner)
        self.assertNotIn("\n", banner)
        self.assertNotIn("\x00", banner)
        self.assertEqual(run.call_args.kwargs["timeout"], 1.5)
        self.assertEqual(run.call_args.args[0][-1], "192.0.2.5")
        connection.settimeout.assert_called_once_with(0.6)
        connection.recv.assert_called_once_with(256)

    def test_missing_ping_still_checks_ssh_and_does_not_execute_subprocess(self):
        connection = Mock()
        connection.connect_ex.return_value = errno.ECONNREFUSED
        with (
            patch.object(helper.subprocess, "run") as run,
            patch.object(helper.socket, "socket") as sock,
        ):
            sock.return_value.__enter__.return_value = connection
            result = helper.inspect_host("192.0.2.6")
        self.assertEqual(result, ("192.0.2.6", None, errno.ECONNREFUSED, ""))
        run.assert_not_called()

    def test_ping_timeout_and_network_permission_failure_are_returned(self):
        with (
            patch.object(
                helper.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("fixture-ping", 1.5),
            ),
            patch.object(
                helper.socket,
                "socket",
                side_effect=PermissionError(errno.EACCES, "fixture denied"),
            ),
        ):
            result = helper.inspect_host("192.0.2.7", ["/fixture/ping"])
        self.assertEqual(result, ("192.0.2.7", False, errno.EACCES, ""))

    def test_explicit_subnet_runs_without_platform_interface_tools_or_hostname_lookup(
        self,
    ):
        with tempfile.TemporaryDirectory() as name:
            report = Path(name) / "report"
            with (
                patch.object(helper.platform, "system", return_value="Other"),
                patch.object(helper, "discover_subnet") as discover,
                patch.object(helper, "command_output") as command,
                patch.object(helper, "ping_command", return_value=None),
                patch.object(
                    helper, "neighbor_cache", return_value="synthetic neighbors"
                ),
                patch.object(
                    helper,
                    "inspect_host",
                    side_effect=lambda address, ping: (
                        str(address),
                        None,
                        errno.ECONNREFUSED,
                        "",
                    ),
                ) as inspect,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    helper.main(["--subnet", "192.0.2.0/30", "--report", str(report)]),
                    0,
                )
            discover.assert_not_called()
            command.assert_not_called()
            self.assertEqual(inspect.call_count, 2)
            self.assertIn("Explicit subnet: 192.0.2.0/30", report.read_text())
            self.assertNotIn("remembered hostname", report.read_text())

    def test_optional_hostname_resolution_is_isolated_bounded_and_uses_an_argument(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as name,
            patch.object(
                helper, "command_output", return_value='["192.0.2.1"]'
            ) as command,
            patch.object(helper, "ping_command", return_value=None),
            patch.object(helper, "neighbor_cache", return_value=""),
            patch.object(
                helper,
                "inspect_host",
                return_value=("192.0.2.1", None, 0, "SSH-fixture"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                helper.main(
                    [
                        "--subnet",
                        "192.0.2.1/32",
                        "--hostname",
                        "camera.example.invalid",
                        "--report",
                        str(Path(name) / "report"),
                    ]
                ),
                0,
            )
            self.assertEqual(command.call_args.kwargs["timeout"], 6)
            self.assertEqual(command.call_args.args[0][1:3], ["-I", "-c"])
            self.assertEqual(command.call_args.args[0][-1], "camera.example.invalid")

    def test_discovered_own_address_is_excluded(self):
        with (
            tempfile.TemporaryDirectory() as name,
            patch.object(
                helper,
                "discover_subnet",
                return_value=(
                    ipaddress.IPv4Network("192.0.2.0/30"),
                    "192.0.2.1",
                    "fixture0",
                ),
            ),
            patch.object(helper, "ping_command", return_value=None),
            patch.object(helper, "neighbor_cache", return_value=""),
            patch.object(
                helper,
                "inspect_host",
                side_effect=lambda address, ping: (str(address), None, 0, ""),
            ) as inspect,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                helper.main(
                    ["--interface", "fixture0", "--report", str(Path(name) / "report")]
                ),
                0,
            )
            self.assertEqual(
                [str(call.args[0]) for call in inspect.call_args_list], ["192.0.2.2"]
            )


if __name__ == "__main__":
    unittest.main()
