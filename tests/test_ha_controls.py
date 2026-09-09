import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from timelapse import ha_controls as controls


def camera_config(path):
    return {
        "remote_controls": {
            "enabled": True,
            "device_id": "camera",
            "state_path": str(path),
        },
        "schedule": {"enabled": False, "interval_seconds": 300},
        "camera": {
            "quality": 90,
            "rotation": 0,
            "settle_ms": 1000,
            "timeout_seconds": 45,
        },
    }


class CameraControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "controls/settings.json"
        self.config = camera_config(self.path)

    def test_only_six_typed_scalar_fields_are_accepted(self):
        invalid = (
            {"interval_seconds": True},
            {"interval_seconds": 59},
            {"interval_seconds": 86401},
            {"interval_seconds": 300.0},
            {"jpeg_quality": 9},
            {"jpeg_quality": 101},
            {"settle_ms": 99},
            {"settle_ms": 10001},
            {"rotation": 90},
            {"rotation": False},
            {"resolution": "../../../etc"},
            {"capture_enabled": "true"},
            {"settle_ms": float("nan")},
            {"jpeg_quality": float("inf")},
            {"power": {"battery_profile_verified": True}},
            {"camera_command": "/bin/sh"},
            {"state_path": "/etc/passwd"},
            {"server": "attacker"},
            {"password": "example-only-password"},
        )
        for update in invalid:
            with self.subTest(update=update), self.assertRaises(ValueError):
                controls.apply_desired(self.config, "camera", update)
        self.assertFalse(self.path.parent.exists())

    def test_desired_payloads_have_exact_scalar_encoding_and_finite_bounds(self):
        self.assertTrue(controls.decode_desired("capture_enabled", b"true"))
        self.assertEqual(controls.decode_desired("interval_seconds", b"86400"), 86400)
        self.assertEqual(
            controls.decode_desired("resolution", b"configured"), "configured"
        )
        invalid = [
            b"",
            b" 300",
            b"300\n",
            b"300.0",
            b"3e2",
            b"0300",
            b"+300",
            b"NaN",
            b"null",
            b"true",
            b"[]",
            b"{}",
            b"9" * 65,
            b"\xff",
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                controls.decode_desired("interval_seconds", payload)
        for payload in (b"1", b"True", b"ON", b'"true"'):
            with self.assertRaises(ValueError):
                controls.decode_desired("capture_enabled", payload)

    def test_disabled_controls_do_not_read_or_write_overlay(self):
        self.config["remote_controls"]["enabled"] = False
        with patch.object(
            controls, "_read", side_effect=AssertionError("unexpected read")
        ):
            self.assertFalse(
                controls.effective_settings(self.config)["capture_enabled"]
            )
            with self.assertRaises(ValueError):
                controls.apply_desired(self.config, "camera", {"capture_enabled": True})
        self.assertFalse(self.path.parent.exists())

    def test_durable_partial_update_readback_replay_and_preserved_local_config(self):
        before = copy.deepcopy(self.config)
        changes = {
            "interval_seconds": 600,
            "capture_enabled": True,
            "resolution": "2304x1296",
        }
        result = controls.apply_desired(self.config, "camera", changes)
        self.assertEqual(self.config, before)
        self.assertEqual(controls.effective_settings(self.config), result)
        state = json.loads(self.path.read_text())
        self.assertEqual(set(state), {"protocol", "device_id", "settings"})
        self.assertEqual(state["settings"], result)
        inode = self.path.stat().st_ino
        with patch.object(
            controls, "atomic_json", side_effect=AssertionError("replay wrote state")
        ):
            self.assertEqual(
                controls.apply_desired(self.config, "camera", changes), result
            )
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(controls.encode_reported(result)["capture_enabled"], b"true")
        self.assertEqual(controls.encode_reported(result)["interval_seconds"], b"600")

    def test_identity_mismatch_and_corrupt_records_fail_closed(self):
        with self.assertRaises(ValueError):
            controls.apply_desired(self.config, "other", {"capture_enabled": True})
        controls.apply_desired(self.config, "camera", {})
        state = json.loads(self.path.read_text())
        variants = [
            dict(state, device_id="other"),
            dict(state, protocol=True),
            dict(state, extra="bad"),
            dict(state, settings={"capture_enabled": True}),
        ]
        for bad in variants:
            self.path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                controls.effective_settings(self.config)
            with self.assertRaises(ValueError):
                controls.apply_desired(self.config, "camera", {"jpeg_quality": 80})
        self.path.write_text('{"protocol":1,"protocol":1}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            controls.effective_settings(self.config)

    def test_file_type_size_permissions_and_symlink_ancestry_are_rejected(self):
        controls.apply_desired(self.config, "camera", {})
        original = self.path.read_bytes()
        self.path.write_bytes(b"x" * (controls.MAX_STATE_BYTES + 1))
        with self.assertRaises(ValueError):
            controls.effective_settings(self.config)
        self.path.write_bytes(original)
        self.path.chmod(0o666)
        with self.assertRaises(ValueError):
            controls.effective_settings(self.config)
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(ValueError):
            controls.effective_settings(self.config)
        self.path.unlink()
        external = self.root / "outside.json"
        external.write_bytes(original)
        self.path.symlink_to(external)
        with self.assertRaises(OSError):
            controls.effective_settings(self.config)
        self.path.unlink()
        self.path.write_bytes(original)
        link = self.root / "linked"
        link.symlink_to(self.path.parent, target_is_directory=True)
        self.config["remote_controls"]["state_path"] = str(link / "settings.json")
        with self.assertRaisesRegex(ValueError, "ancestry"):
            controls.effective_settings(self.config)

    def test_settling_time_cannot_exceed_locally_configured_timeout(self):
        self.config["camera"]["timeout_seconds"] = 5
        with self.assertRaisesRegex(ValueError, "timeout"):
            controls.apply_desired(self.config, "camera", {"settle_ms": 5000})
        self.assertFalse(self.path.exists())

    def test_lock_contention_rejects_without_waiting(self):
        with controls.session_lock(self.config, "camera"):
            with self.assertRaises(BlockingIOError):
                with controls.session_lock(self.config, "camera"):
                    self.fail("Concurrent control poll entered")
        with controls._locked(self.path):
            with self.assertRaises(BlockingIOError):
                controls.apply_desired(self.config, "camera", {})

    def test_failed_durability_or_mismatched_readback_never_returns_applied_state(self):
        with patch.object(controls, "atomic_json", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                controls.apply_desired(self.config, "camera", {"capture_enabled": True})
        actual = controls._read

        def mismatch(path, device_id):
            result = actual(path, device_id)
            return dict(result, capture_enabled=False)

        with patch.object(controls, "_read", side_effect=mismatch):
            with self.assertRaisesRegex(ValueError, "readback"):
                controls.apply_desired(self.config, "camera", {"capture_enabled": True})


if __name__ == "__main__":
    unittest.main()
