import json
from pathlib import Path
import tempfile
import unittest

from timelapse.ha_config import load_config, validate_config


class HomeAssistantConfigTests(unittest.TestCase):
    def test_secure_defaults_and_stable_identity(self):
        config = validate_config({"device_id": "zero-01"})
        self.assertTrue(config["mqtt"]["tls"])
        self.assertEqual(config["mqtt"]["port"], 8883)
        self.assertIsNone(config["mqtt"]["host"])
        self.assertEqual(config["photos"]["max_bytes"], 8388608)
        with self.assertRaises(ValueError):
            validate_config({})

    def test_wildcards_traversal_and_unknown_fields_rejected(self):
        for overrides in (
            {"device_id": "../../camera"},
            {"topic_prefix": "pi/+"},
            {"discovery_prefix": "homeassistant/#"},
            {"state_directory": "/tmp/../etc"},
            {"photos": {"archive_root": "relative"}},
            {"mqtt": {"password": "example-only-password"}},
            {"mqtt": {"host": "mqtt://host"}},
            {"mqtt": {"host": "host\nline"}},
            {"mqtt": {"tls": "false"}},
            {"mqtt": {"port": True}},
            {"mqtt": {"timeout_seconds": 1000}},
            {"telemetry": {"minimum_battery_voltage_mv": 1000}},
            {"controls": {"timelapse_config": "relative"}},
            {"controls": {"poll_seconds": True}},
            {"controls": {"poll_seconds": 0}},
            {"controls": {"poll_seconds": 11}},
            {"mqtt": {"timeout_seconds": 3}, "controls": {"poll_seconds": 3}},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    validate_config({"device_id": "zero", **overrides})

    def test_mutual_tls_and_credentials_require_valid_pairs(self):
        for settings in (
            {"cert_file": "/etc/cert.pem"},
            {"key_file": "/etc/key.pem"},
            {"password_file": "/etc/password"},
            {"tls": False, "ca_file": "/etc/ca.pem"},
        ):
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    validate_config({"device_id": "zero", "mqtt": settings})
        config = validate_config(
            {"device_id": "zero", "mqtt": {"tls": False, "port": 1883}}
        )
        self.assertFalse(config["mqtt"]["tls"])

    def test_config_read_is_bounded_and_example_validates(self):
        example = (
            Path(__file__).resolve().parents[1]
            / "deploy/home-assistant/config.example.json"
        )
        self.assertEqual(load_config(example)["device_id"], "timelapse-zero-01")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(" " * 65537)
            with self.assertRaisesRegex(ValueError, "64 KiB"):
                load_config(path)
            path.write_text(json.dumps([]))
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
