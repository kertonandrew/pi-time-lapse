import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from timelapse.config import load_config, validate_config
from timelapse.ha_config import load_config as load_ha_config
from timelapse.setup import doctor, initialize


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_generated_configuration_is_private_generic_and_disabled(self):
        directory = self.root / "operator"
        initialize(directory, "example-camera", "mqtt.example.com")
        camera = load_config(directory / "camera.json")
        ha = load_ha_config(directory / "home-assistant.json")
        self.assertFalse(camera["schedule"]["enabled"])
        self.assertFalse(camera["remote_controls"]["enabled"])
        self.assertFalse(camera["power"]["battery_profile_verified"])
        self.assertIsNone(camera["server"])
        self.assertEqual(camera["camera"]["width"], 0)
        self.assertEqual(camera["camera"]["rotation"], 0)
        self.assertEqual(ha["device_id"], "example-camera")
        self.assertTrue(ha["mqtt"]["tls"])
        self.assertIsNone(ha["mqtt"]["password_file"])
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_existing_files_are_never_overwritten(self):
        initialize(self.root, "first-camera")
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with self.assertRaisesRegex(ValueError, "already exists"):
            initialize(self.root, "second-camera")
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in self.root.iterdir()}
        )

    def test_invalid_id_and_symlink_fail_before_writes(self):
        with self.assertRaises(ValueError):
            initialize(self.root / "invalid", "../bad")
        self.assertFalse((self.root / "invalid").exists())
        (self.root / "alias").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            initialize(self.root / "alias", "example-camera")
        self.assertFalse((self.root / "camera.json").exists())

    def test_doctor_reports_missing_prerequisites_without_accessing_network(self):
        with (
            patch("timelapse.setup.shutil.which", return_value=None),
            patch("timelapse.setup.os.access", return_value=False),
        ):
            result = doctor()
        self.assertFalse(result["capture_prerequisites_present"])
        self.assertFalse(result["network_operations"])
        self.assertFalse(result["hardware_writes"])

    def test_controls_identity_and_camera_bounds_are_local_validation(self):
        for change in (
            {"remote_controls": {"enabled": True}},
            {"remote_controls": {"device_id": "../bad"}},
            {"schedule": {"interval_seconds": 59}},
            {"schedule": {"enabled": "yes"}},
            {"camera": {"quality": True}},
            {"camera": {"quality": 101}},
            {"camera": {"width": 0, "height": 1080}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_config(change)

    def test_configuration_reads_are_bounded_and_refuse_symlinks(self):
        path = self.root / "camera.json"
        path.write_text(json.dumps({"extra": "x" * 65536}))
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            load_config(path)
        (self.root / "alias.json").symlink_to(path)
        with self.assertRaises(OSError):
            load_config(self.root / "alias.json")

    def test_remote_defaults_are_validated_before_a_configuration_is_accepted(self):
        for camera in ({"quality": 5}, {"settle_ms": 50}, {"settle_ms": 15000}):
            validate_config({"camera": camera})
            with self.subTest(camera=camera), self.assertRaises(ValueError):
                validate_config(
                    {
                        "camera": camera,
                        "remote_controls": {
                            "enabled": True,
                            "device_id": "example-camera",
                        },
                    }
                )


if __name__ == "__main__":
    unittest.main()
