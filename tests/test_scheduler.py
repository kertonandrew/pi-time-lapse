from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from timelapse.config import effective_config, validate_config
from timelapse.ha_controls import apply_desired
from timelapse.scheduler import run_scheduled


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = validate_config(
            {"spool": str(self.root / "spool"), "schedule": {"enabled": True}}
        )
        self.now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        self.capture = Mock(return_value={"filename": "synthetic.jpg"})

    def run_at(self, uptime, now=None, boot="test-boot"):
        return run_scheduled(
            self.config, self.capture, self.now if now is None else now, uptime, boot
        )

    def test_disabled_does_not_create_state_or_call_capture(self):
        self.config["schedule"]["enabled"] = False
        self.assertEqual(self.run_at(0)["action"], "wait")
        self.capture.assert_not_called()
        self.assertFalse(Path(self.config["spool"]).exists())

    def test_one_capture_per_interval_without_catchup_burst(self):
        self.assertEqual(self.run_at(10), {"filename": "synthetic.jpg"})
        self.assertEqual(self.run_at(309)["action"], "wait")
        self.run_at(310)
        self.run_at(10000)
        self.assertEqual(self.run_at(10001)["action"], "wait")
        self.assertEqual(self.capture.call_count, 3)

    def test_same_boot_wall_clock_jump_does_not_bypass_interval(self):
        self.run_at(10)
        self.assertEqual(
            self.run_at(11, self.now + timedelta(days=10))["action"], "wait"
        )
        self.assertEqual(
            self.run_at(12, self.now - timedelta(days=10))["action"], "wait"
        )

    def test_reboot_uses_elapsed_time_and_bounds_bad_clock_retry(self):
        self.run_at(100)
        self.assertEqual(
            self.run_at(1, self.now + timedelta(seconds=1), "new-boot")["action"],
            "wait",
        )
        self.assertEqual(
            self.run_at(2, self.now - timedelta(days=1), "new-boot")["action"], "wait"
        )
        self.run_at(300, self.now - timedelta(days=1), "new-boot")
        self.assertEqual(self.capture.call_count, 2)

    def test_failed_capture_does_not_retry_every_scheduler_tick(self):
        self.capture.side_effect = RuntimeError("Synthetic power rejection")
        with self.assertRaises(RuntimeError):
            self.run_at(0)
        self.capture.side_effect = None
        self.assertEqual(self.run_at(60)["action"], "wait")
        self.assertEqual(self.capture.call_count, 1)

    def test_failure_to_persist_attempt_prevents_capture(self):
        with patch(
            "timelapse.scheduler.atomic_json",
            side_effect=OSError("Synthetic disk failure"),
        ):
            with self.assertRaises(OSError):
                self.run_at(0)
        self.capture.assert_not_called()

    def test_corrupt_state_does_not_trigger_fresh_capture(self):
        self.run_at(0)
        (Path(self.config["spool"]) / "schedule-state.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.run_at(500)
        self.assertEqual(self.capture.call_count, 1)

    def test_configuration_is_a_snapshot_and_cannot_override_safety_or_paths(self):
        config = validate_config(
            {
                "remote_controls": {
                    "enabled": True,
                    "device_id": "fixture-camera",
                    "state_path": str(self.root / "controls/settings.json"),
                },
            }
        )
        apply_desired(
            config,
            "fixture-camera",
            {
                "capture_enabled": True,
                "interval_seconds": 600,
                "rotation": 180,
                "resolution": "1920x1080",
                "jpeg_quality": 80,
            },
        )
        snapshot = effective_config(config)
        apply_desired(
            config, "fixture-camera", {"capture_enabled": False, "rotation": 0}
        )
        self.assertTrue(snapshot["schedule"]["enabled"])
        self.assertEqual(snapshot["camera"]["rotation"], 180)
        self.assertEqual(snapshot["camera"]["width"], 1920)
        self.assertEqual(snapshot["camera"]["quality"], 80)
        self.assertEqual(snapshot["schedule"]["interval_seconds"], 600)
        for key in (
            "spool",
            "hardware_database",
            "power",
            "server",
            "minimum_capture_charge_percent",
        ):
            self.assertEqual(snapshot[key], config[key])
        self.assertEqual(
            snapshot["camera"]["camera_command"], config["camera"]["camera_command"]
        )
        self.assertFalse(effective_config(config)["schedule"]["enabled"])
        self.assertFalse(config["schedule"]["enabled"])

    def test_unknown_remote_field_fails_closed_before_capture(self):
        config = validate_config(
            {
                "remote_controls": {
                    "enabled": True,
                    "device_id": "fixture-camera",
                    "state_path": str(self.root / "controls/settings.json"),
                }
            }
        )
        apply_desired(config, "fixture-camera", {})
        path = Path(config["remote_controls"]["state_path"])
        value = json.loads(path.read_text())
        value["settings"]["camera_command"] = "/bin/false"
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            effective_config(config)


if __name__ == "__main__":
    unittest.main()
