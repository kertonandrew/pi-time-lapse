import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hardware import install


class ConfigurationTests(unittest.TestCase):
    def test_preserves_unrelated_options_and_is_idempotent(self):
        original = """# Existing camera settings
gpu_mem=64
camera_auto_detect=1
dtparam=i2c_arm=on
dtparam=audio=on
dtoverlay=vc4-kms-v3d,cma-256,audio
dtoverlay=i2c-rtc,ds1339,wakeup-source
[pi4]
dtoverlay=vc4-kms-v3d,cma-512
[all]
display_auto_detect=1
"""
        updated = install.firmware_config(original)
        self.assertIn("gpu_mem=64\ncamera_auto_detect=1\ndtparam=i2c_arm=on", updated)
        self.assertIn("[pi4]\ndtoverlay=vc4-kms-v3d,cma-512", updated)
        self.assertIn("dtoverlay=vc4-kms-v3d,cma-256,noaudio,nohdmi", updated)
        self.assertIn("dtoverlay=i2c-rtc,ds1307,wakeup-source", updated)
        self.assertNotIn("ds1339", updated)
        self.assertEqual(updated.count("dtoverlay=i2c-rtc"), 1)
        self.assertEqual(install.firmware_config(updated), updated)

    def test_consolidates_matching_overlays_but_rejects_ambiguous_configuration(self):
        updated = install.firmware_config(
            "dtoverlay=i2c-rtc,ds1307\ndtoverlay=i2c-rtc,ds1339\n"
        )
        self.assertEqual(updated.count("dtoverlay=i2c-rtc"), 1)
        for original in (
            "[gpio4=1]\ndtoverlay=i2c-rtc,ds1339\n",
            "dtoverlay=i2c-rtc,ds3231\n",
            "dtoverlay=i2c-rtc,ds1339,i2c0\n",
            "dtoverlay=i2c-rtc,ds1307,addr=0x69\n",
            "dtoverlay=i2c-rtc,ds1307,ds3231\n",
            "dtoverlay=vc4-kms-v3d,cma-256\ndtoverlay=vc4-kms-v3d,cma-512\n",
            install.END + "\n" + install.BEGIN,
        ):
            with (
                self.subTest(original=original),
                self.assertRaises(install.InstallError),
            ):
                install.firmware_config(original)

    def test_removes_only_splash_and_serial_consoles(self):
        original = "console=serial0,115200 console=tty1 root=PARTUUID=abc rootfstype=ext4 fsck.repair=yes rootwait splash quiet cma=256M\n"
        self.assertEqual(
            install.kernel_command_line(original),
            "console=tty1 root=PARTUUID=abc rootfstype=ext4 fsck.repair=yes rootwait quiet cma=256M\n",
        )
        with self.assertRaises(install.InstallError):
            install.kernel_command_line("root=one\nroot=two\n")

    def test_console_uses_installed_vendor_command_without_autologin(self):
        self.assertEqual(
            install.console_override(
                "[Service]\nExecStart=-/sbin/agetty -o '-p -- \\u' --noclear - $TERM\n"
            ),
            "[Service]\nExecStart=\nExecStart=-/sbin/agetty -o '-p -- \\u' --noclear - $TERM\n",
        )
        with self.assertRaises(install.InstallError):
            install.console_override(
                "ExecStart=-/sbin/agetty --autologin andrew tty1\n"
            )


class InstallerTests(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0)
        self.addCleanup(os.umask, previous_umask)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calls = []
        self.active = {
            unit: "inactive"
            for unit in (*install.SYSTEM_MASKS, install.MONITOR, install.TIMER)
        }
        self.active.update(
            {
                "lightdm.service": "active",
                "cups.service": "active",
                "hciuart.service": "active",
                install.TIMER: "active",
            }
        )
        self.fail_monitor_start = False
        self.module_resolution = (
            0,
            "insmod /lib/modules/fixture-kernel/kernel/drivers/net/wireless/broadcom/brcm80211/brcmfmac/brcmfmac.ko",
            "",
        )
        self.installer = install.Installer(self.root, run=self.run_command)
        original_regular = install.regular_file
        original_symlink = install.symlink

        def regular(data, previous=None, mode=0o644):
            state = original_regular(data, previous, mode)
            if not previous or previous["type"] != "file":
                state.update(uid=os.getuid(), gid=os.getgid())
            return state

        def symlink(target):
            return {**original_symlink(target), "uid": os.getuid(), "gid": os.getgid()}

        for patcher in (
            patch.object(install, "regular_file", regular),
            patch.object(install, "symlink", symlink),
            patch.object(install.platform, "release", return_value="fixture-kernel"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.put("/proc/device-tree/model", "Raspberry Pi Zero W Rev 1.1\x00")
        self.put("/etc/os-release", "ID=raspbian\nVERSION_CODENAME=bookworm\n")
        self.put("/proc/cmdline", "root=PARTUUID=abc rootwait\n")
        self.put(
            "/boot/config-fixture-kernel",
            "CONFIG_BCM2835_WDT=y\n# CONFIG_WATCHDOG_NOWAYOUT is not set\n# CONFIG_WATCHDOG_SYSFS is not set\n",
        )
        self.link("/dev/i2c-1", "/dev/null")
        self.link("/dev/watchdog0", "/dev/null")
        self.put("/sys/module/bcm2835_wdt/parameters/nowayout", "N\n")
        self.installer.path("/sys/bus/platform/drivers/bcm2835-wdt").mkdir(parents=True)
        self.link(
            "/sys/class/watchdog/watchdog0/device/driver",
            str(self.installer.path("/sys/bus/platform/drivers/bcm2835-wdt")),
        )
        self.put(
            install.CONFIG,
            "gpu_mem=64\ndtoverlay=vc4-kms-v3d,cma-256\ndtoverlay=i2c-rtc,ds1339\n",
        )
        self.put(
            install.CMDLINE,
            "console=serial0,115200 console=tty1 root=PARTUUID=abc rootwait splash\n",
        )
        self.put(
            "/boot/firmware/overlays/README",
            "Name:   vc4-kms-v3d\nParams: noaudio\n        nohdmi\n\nName: disable-bt\n",
        )
        self.put(
            "/lib/systemd/system/getty@.service",
            "[Service]\nExecStart=-/sbin/agetty --noclear - $TERM\n",
        )
        self.put("/lib/systemd/system/multi-user.target", "[Unit]\n")
        for unit in (*install.SYSTEM_MASKS, install.TIMER):
            self.put(
                f"/lib/systemd/system/{unit}",
                "[Unit]\n" if unit == "triggerhappy.service" else "[Unit]\n[Install]\n",
            )
        for unit in install.USER_MASKS:
            self.put(f"/usr/lib/systemd/user/{unit}", "[Unit]\n[Install]\n")
        self.link(
            "/etc/systemd/system/default.target", "/lib/systemd/system/graphical.target"
        )
        self.link(
            "/etc/systemd/system/multi-user.target.wants/lightdm.service",
            "/lib/systemd/system/lightdm.service",
        )
        self.link("/etc/systemd/system/hciuart.service", "/dev/null")
        self.link(
            f"/run/systemd/system/timers.target.wants/{install.TIMER}",
            f"/lib/systemd/system/{install.TIMER}",
        )
        self.link(
            "/etc/systemd/user/sockets.target.wants/pipewire.socket",
            "/usr/lib/systemd/user/pipewire.socket",
        )
        self.link("/etc/systemd/user/wireplumber.service", "/dev/null")
        self.put(
            "/etc/systemd/system/getty@tty1.service.d/autologin.conf",
            "[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin andrew tty1\n",
        )
        self.put("/etc/default/pi-hardware", "PI_HARDWARE_INTERVAL=15\n")
        self.put("/etc/unrelated.conf", "retain this exactly\n")

    def put(self, name, data):
        path = self.installer.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)

    def link(self, name, target):
        path = self.installer.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)

    def enabled(self, unit, scope):
        directories = (
            ("/etc/systemd/user",)
            if scope == "global"
            else ("/etc/systemd/system", "/run/systemd/system")
        )
        for directory in directories:
            path = self.installer.path(f"{directory}/{unit}")
            if path.is_symlink() and os.readlink(path) == "/dev/null":
                return "masked-runtime" if directory.startswith("/run") else "masked"
        vendor = "/usr/lib/systemd/user" if scope == "global" else "/lib/systemd/system"
        definitions = [
            self.installer.path(f"{directory}/{unit}")
            for directory in (*directories, vendor)
        ]
        definition = next((path for path in definitions if path.is_file()), None)
        if definition is None:
            return "not-found"
        for directory in directories:
            if any(
                path.name == unit
                and path.is_symlink()
                and path.parent.name.endswith((".wants", ".requires"))
                for path in self.installer.path(directory).rglob("*")
            ):
                return "enabled-runtime" if directory.startswith("/run") else "enabled"
        return "disabled" if "[Install]" in definition.read_text() else "static"

    def run_command(self, args):
        self.calls.append(args)
        if args[0] == "/usr/bin/python3":
            return 0, "", ""
        if "modprobe" in args:
            return self.module_resolution
        scope = "global" if "--global" in args else "system"
        verb = args[2] if scope == "global" else args[1]
        if verb == "is-enabled":
            state = self.enabled(args[-1], scope)
            return (0 if state.startswith("enabled") else 1), state, ""
        if verb == "is-active":
            state = self.active.get(args[-1], "inactive")
            return (0 if state == "active" else 3), state, ""
        if verb == "stop":
            for unit in args[2:]:
                self.active[unit] = "inactive"
        if verb == "restart":
            unit = args[-1]
            if unit == install.MONITOR and self.fail_monitor_start:
                self.fail_monitor_start = False
                return 1, "", "injected service failure"
            if self.enabled(unit, "system") in (
                "masked",
                "masked-runtime",
                "not-found",
            ):
                return 1, "", "Unit cannot start"
            self.active[unit] = "active"
        return 0, "", ""

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): install.file_state(path)
            for path in self.root.rglob("*")
            if (path.is_file() or path.is_symlink())
            and "/var/backups/" not in str(path)
        }

    def test_dry_run_has_no_writes_or_service_mutations(self):
        before = self.snapshot()
        with (
            patch.object(install, "Installer", return_value=self.installer),
            patch.object(install.platform, "machine", return_value="armv6l"),
            patch.object(install.struct, "calcsize", return_value=4),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(install.main([]), 0)
        self.assertIn("Dry-run only", output.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(
            all(
                call[0] == "/usr/bin/python3" or call[-2] in ("is-active", "is-enabled")
                for call in self.calls
            )
        )

    def test_apply_rerun_and_full_rollback_preserve_original_states(self):
        original_files = self.snapshot()
        original_states = self.installer.states()
        self.assertEqual(
            original_states["system:triggerhappy.service"]["enabled"], "static"
        )
        self.assertEqual(
            original_states[f"system:{install.TIMER}"]["enabled"], "enabled-runtime"
        )
        backup = self.installer.apply(self.installer.plan(), original_states)
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "applied")
        self.assertEqual(
            manifest["files"][f"/etc/systemd/system/{install.MONITOR}"]["before"],
            {"type": "absent"},
        )
        self.assertEqual(self.enabled(install.MONITOR, "system"), "enabled")
        self.assertEqual(self.enabled(install.TIMER, "system"), "disabled")
        self.assertTrue(
            all(
                self.enabled(unit, "system") == "masked"
                for unit in install.SYSTEM_MASKS
            )
        )
        self.assertTrue(
            all(self.enabled(unit, "global") == "masked" for unit in install.USER_MASKS)
        )
        self.assertEqual(
            self.installer.path("/etc/default/pi-hardware").read_text(),
            "PI_HARDWARE_INTERVAL=15\n",
        )
        self.assertEqual(self.installer.plan(), {})
        self.assertIsNone(self.installer.apply({}, self.installer.states()))
        current = self.snapshot()
        self.installer.rollback(backup, apply=False)
        self.assertEqual(self.snapshot(), current)
        self.installer.rollback(backup)
        self.assertEqual(self.snapshot(), original_files)
        self.assertEqual(self.installer.states(), original_states)

    def test_service_failure_automatically_restores_files_and_states(self):
        original_files = self.snapshot()
        original_states = self.installer.states()
        self.fail_monitor_start = True
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(install.InstallError, "injected service failure"),
        ):
            self.installer.apply(self.installer.plan(), original_states)
        self.assertEqual(self.snapshot(), original_files)
        self.assertEqual(self.installer.states(), original_states)
        manifests = list(
            self.installer.path("/var/backups/pi-hardware-installer").glob(
                "*/manifest.json"
            )
        )
        self.assertEqual(json.loads(manifests[0].read_text())["status"], "rolled-back")

    def test_rollback_rejects_later_user_edits_before_mutating_anything(self):
        backup = self.installer.apply(self.installer.plan(), self.installer.states())
        with self.installer.path(install.CONFIG).open("a") as output:
            output.write("gpu_freq=250\n")
        edited = self.snapshot()
        calls = len(self.calls)
        with self.assertRaisesRegex(install.InstallError, "later change"):
            self.installer.rollback(backup)
        self.assertEqual(self.snapshot(), edited)
        self.assertEqual(len(self.calls), calls)

    def test_preflight_rejects_other_models_and_hidden_overlay_edits(self):
        self.put("/proc/device-tree/model", "Raspberry Pi Zero 2 W Rev 1.0\x00")
        with self.assertRaisesRegex(install.InstallError, "Only Raspberry Pi Zero W"):
            self.installer.preflight()
        self.put(install.CONFIG, "include usercfg.txt\n")
        self.put("/boot/firmware/usercfg.txt", "dtoverlay=i2c-rtc,ds1339\n")
        with self.assertRaisesRegex(install.InstallError, "configured in an include"):
            self.installer.check_includes(install.CONFIG, set())

    def test_preflight_requires_i2c_device(self):
        self.installer.path("/dev/i2c-1").unlink()
        with (
            patch.object(install.platform, "machine", return_value="armv6l"),
            patch.object(install.struct, "calcsize", return_value=4),
            self.assertRaisesRegex(install.InstallError, "/dev/i2c-1"),
        ):
            self.installer.preflight()

    def test_watchdog_requires_character_device_and_verified_stoppable_driver(self):
        with (
            patch.object(install.platform, "machine", return_value="armv6l"),
            patch.object(install.struct, "calcsize", return_value=4),
        ):
            for value in ("1", "Y", "unknown", ""):
                with self.subTest(nowayout=value):
                    self.put("/sys/module/bcm2835_wdt/parameters/nowayout", value)
                    with self.assertRaisesRegex(install.InstallError, "nowayout=0"):
                        self.installer.preflight()
            for value in ("0", "N"):
                self.put("/sys/module/bcm2835_wdt/parameters/nowayout", value)
                self.installer.preflight()
            self.installer.path("/dev/watchdog0").unlink()
            with self.assertRaisesRegex(install.InstallError, "/dev/watchdog0"):
                self.installer.preflight()
            self.installer.preflight(rollback=True)

    def test_watchdog_file_install_and_rollback_never_reexec_manager(self):
        for previous in (None, "[Manager]\nRuntimeWatchdogSec=10s\n"):
            with self.subTest(previous=previous):
                if previous is not None:
                    self.put(install.WATCHDOG_CONFIG, previous)
                backup = self.installer.apply(
                    self.installer.plan(), self.installer.states()
                )
                self.assertEqual(
                    self.installer.path(install.WATCHDOG_CONFIG).read_text(),
                    "[Manager]\nWatchdogDevice=/dev/watchdog0\nRuntimeWatchdogSec=15s\nRebootWatchdogSec=0\nKExecWatchdogSec=0\n",
                )
                self.installer.rollback(backup)
                restored = self.installer.path(install.WATCHDOG_CONFIG)
                self.assertEqual(
                    restored.read_text() if restored.exists() else None, previous
                )
        self.assertFalse(
            any("daemon-reexec" in call or "reboot" in call for call in self.calls)
        )

    def test_watchdog_fallback_proves_builtin_default_and_rejects_overrides(self):
        self.installer.path("/sys/module/bcm2835_wdt/parameters/nowayout").unlink()
        self.put(
            "/boot/config-test-kernel",
            "CONFIG_BCM2835_WDT=y\n# CONFIG_WATCHDOG_NOWAYOUT is not set\n# CONFIG_WATCHDOG_SYSFS is not set\n",
        )
        self.put("/proc/cmdline", "root=PARTUUID=abc rootwait\n")
        with patch.object(install.platform, "release", return_value="test-kernel"):
            self.installer.check_watchdog()
            for override in (
                "bcm2835_wdt.nowayout=1",
                "bcm2835-wdt.nowayout=N",
                "bcm2835_wdt.nowayout",
            ):
                with self.subTest(override=override):
                    self.put("/proc/cmdline", "rootwait " + override)
                    with self.assertRaisesRegex(
                        install.InstallError, "command-line override"
                    ):
                        self.installer.check_watchdog()
            self.put("/proc/cmdline", "rootwait\n")
            for configuration in (
                "CONFIG_BCM2835_WDT=m\n# CONFIG_WATCHDOG_NOWAYOUT is not set\n",
                "CONFIG_BCM2835_WDT=y\nCONFIG_WATCHDOG_NOWAYOUT=y\n",
            ):
                with self.subTest(configuration=configuration):
                    self.put(
                        "/boot/config-test-kernel",
                        configuration + "# CONFIG_WATCHDOG_SYSFS is not set\n",
                    )
                    with self.assertRaisesRegex(
                        install.InstallError, "built-in BCM2835 driver"
                    ):
                        self.installer.check_watchdog()

    def test_watchdog_runtime_flag_and_systemd_override_are_not_hidden_by_module_default(
        self,
    ):
        self.put(
            "/boot/config-fixture-kernel",
            "CONFIG_BCM2835_WDT=y\nCONFIG_WATCHDOG_SYSFS=y\n# CONFIG_WATCHDOG_NOWAYOUT is not set\n",
        )
        with self.assertRaisesRegex(install.InstallError, "runtime attribute"):
            self.installer.check_watchdog()
        self.put("/sys/class/watchdog/watchdog0/nowayout", "1\n")
        with self.assertRaisesRegex(install.InstallError, "watchdog0 nowayout=0"):
            self.installer.check_watchdog()
        self.put("/sys/class/watchdog/watchdog0/nowayout", "0\n")
        self.installer.check_watchdog()
        self.put("/proc/cmdline", "rootwait systemd.watchdog_sec=20s\n")
        with self.assertRaisesRegex(install.InstallError, "systemd.watchdog_sec"):
            self.installer.check_watchdog()

    def test_watchdog_rejects_unverified_driver_even_with_stoppable_parameter(self):
        self.installer.path("/sys/class/watchdog/watchdog0/device/driver").unlink()
        with self.assertRaisesRegex(install.InstallError, "bcm2835-wdt driver"):
            self.installer.check_watchdog()

    def test_wifi_candidate_requires_explicit_opt_in(self):
        self.assertNotIn(install.WIFI_CONFIG, self.installer.plan())
        with (
            patch.object(install.platform, "machine", return_value="armv6l"),
            patch.object(install.struct, "calcsize", return_value=4),
        ):
            self.installer.preflight()
            self.assertFalse(any("modprobe" in call for call in self.calls))
            self.installer.wifi_early_load = True
            self.installer.preflight()
        self.assertIn(install.WIFI_CONFIG, self.installer.plan())
        self.assertIn(
            [
                "env",
                "-u",
                "MODPROBE_OPTIONS",
                "modprobe",
                "--show-depends",
                "--use-blacklist",
                "brcmfmac",
            ],
            self.calls,
        )

    def test_wifi_preflight_rejects_failure_blacklist_and_install_hooks(self):
        for result in (
            (1, "", "module not found"),
            (0, "", ""),
            (0, "install /bin/false", ""),
            (0, "insmod /a/module.ko\ninstall /custom/command", ""),
        ):
            with self.subTest(result=result):
                self.module_resolution = result
                with self.assertRaises(install.InstallError):
                    self.installer.check_wifi_module()
        for output in (
            "insmod /a/dependency.ko option=value\ninsmod /b/brcmfmac.ko.xz",
            "builtin brcmfmac",
        ):
            self.module_resolution = (0, output, "")
            self.installer.check_wifi_module()

    def test_wifi_file_install_and_rollback_preserve_network_settings(self):
        self.installer.wifi_early_load = True
        self.put(
            "/etc/NetworkManager/system-connections/field.nmconnection",
            "[connection]\nid=field\n",
        )
        original_connection = self.installer.path(
            "/etc/NetworkManager/system-connections/field.nmconnection"
        ).read_bytes()
        for previous in (None, "existing-module\n"):
            with self.subTest(previous=previous):
                if previous is not None:
                    self.put(install.WIFI_CONFIG, previous)
                backup = self.installer.apply(
                    self.installer.plan(), self.installer.states()
                )
                self.assertEqual(
                    self.installer.path(install.WIFI_CONFIG).read_text(), "brcmfmac\n"
                )
                self.assertEqual(
                    self.installer.path(
                        "/etc/NetworkManager/system-connections/field.nmconnection"
                    ).read_bytes(),
                    original_connection,
                )
                self.assertEqual(self.installer.plan(), {})
                self.installer.rollback(backup)
                restored = self.installer.path(install.WIFI_CONFIG)
                self.assertEqual(
                    restored.read_text() if restored.exists() else None, previous
                )
        self.assertFalse(
            any(
                "modprobe" in call or "systemd-modules-load.service" in call
                for call in self.calls
            )
        )

    def test_stale_plan_fails_before_any_managed_write(self):
        changes = self.installer.plan()
        self.put(install.CMDLINE, "root=PARTUUID=later rootwait\n")
        before = self.snapshot()
        with self.assertRaisesRegex(install.InstallError, "changed after preflight"):
            self.installer.apply(changes, self.installer.states())
        self.assertEqual(self.snapshot(), before)

    def test_mid_apply_conflict_restores_completed_writes_and_preserves_user_edit(self):
        changes = self.installer.plan()
        original = self.installer.path(install.CONFIG).read_text()
        original_write = install.write_state

        def write(path, state):
            original_write(path, state)
            if (
                path == self.installer.path(install.CONFIG)
                and state == changes[install.CONFIG]["after"]
            ):
                self.put(install.CMDLINE, "root=PARTUUID=later rootwait\n")

        with (
            patch.object(install, "write_state", write),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(install.InstallError, "changed after preflight"),
        ):
            self.installer.apply(changes, self.installer.states())
        self.assertEqual(self.installer.path(install.CONFIG).read_text(), original)
        self.assertEqual(
            self.installer.path(install.CMDLINE).read_text(),
            "root=PARTUUID=later rootwait\n",
        )

    def test_rejects_symlinked_install_directory(self):
        self.installer.path("/usr/local/lib").mkdir(parents=True)
        self.link("/usr/local/lib/pi-time-lapse", "/tmp")
        with self.assertRaisesRegex(
            install.InstallError, "directory must not be a symlink"
        ):
            self.installer.plan()

    def test_automatic_rollback_restores_unconflicted_files_after_completed_write_conflict(
        self,
    ):
        changes = self.installer.plan()
        original_cmdline = self.installer.path(install.CMDLINE).read_text()
        original_write = install.write_state

        def write(path, state):
            original_write(path, state)
            if (
                path == self.installer.path(install.CMDLINE)
                and state == changes[install.CMDLINE]["after"]
            ):
                self.put(install.CONFIG, "gpu_mem=128\n")
                raise OSError("injected post-replace error")

        with (
            patch.object(install, "write_state", write),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                install.InstallError,
                "post-replace error.*rollback incomplete.*preserved later edits",
            ),
        ):
            self.installer.apply(changes, self.installer.states())
        self.assertEqual(
            self.installer.path(install.CONFIG).read_text(), "gpu_mem=128\n"
        )
        self.assertEqual(
            self.installer.path(install.CMDLINE).read_text(), original_cmdline
        )
        manifests = list(
            self.installer.path("/var/backups/pi-hardware-installer").glob(
                "*/manifest.json"
            )
        )
        self.assertEqual(
            json.loads(manifests[0].read_text())["status"], "rollback-incomplete"
        )

    def test_absent_default_file_is_installed_and_removed_on_rollback(self):
        self.installer.path("/etc/default/pi-hardware").unlink()
        backup = self.installer.apply(self.installer.plan(), self.installer.states())
        self.assertIn(
            "PI_HARDWARE_INTERVAL",
            self.installer.path("/etc/default/pi-hardware").read_text(),
        )
        self.installer.rollback(backup)
        self.assertFalse(self.installer.path("/etc/default/pi-hardware").exists())


if __name__ == "__main__":
    unittest.main()
