import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from timelapse import cli
from timelapse.config import load_config
from timelapse.transfer import USER_PATTERN


class CliTests(unittest.TestCase):
    def test_transfer_accepts_existing_dotted_ssh_username(self):
        self.assertIsNotNone(USER_PATTERN.fullmatch("example.user"))
        self.assertIsNone(USER_PATTERN.fullmatch("-oProxyCommand=bad"))
        self.assertIsNone(USER_PATTERN.fullmatch("name@another-host"))

    def test_dry_capture_does_not_touch_hardware_or_storage(self):
        output = StringIO()
        with (
            patch("timelapse.cli.capture_guard", side_effect=AssertionError),
            patch("timelapse.spool.Spool", side_effect=AssertionError),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.main(["capture", "--dry-run"]), 0)
        self.assertFalse(json.loads(output.getvalue())["shutdown"])

    def test_missing_destination_does_not_touch_network_or_power(self):
        with patch("timelapse.cli.power_decision", side_effect=AssertionError):
            result = cli.upload(load_config(None), Mock(), Mock())
        self.assertEqual(result["action"], "wait")

    def test_disabled_schedule_does_not_touch_hardware_or_storage(self):
        output = StringIO()
        with (
            patch("timelapse.cli.capture_guard", side_effect=AssertionError),
            patch("timelapse.cli.PowerHistory", side_effect=AssertionError),
            patch("timelapse.spool.Spool", side_effect=AssertionError),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.main(["scheduled-capture"]), 0)
        self.assertEqual(json.loads(output.getvalue())["action"], "wait")

    def test_failed_power_guard_never_invokes_camera(self):
        output = StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps({"spool": str(Path(temporary) / "spool")})
            )
            with (
                patch(
                    "timelapse.cli.capture_guard",
                    side_effect=cli.PowerError("no power"),
                ),
                patch("timelapse.capture.capture", side_effect=AssertionError),
                redirect_stdout(output),
            ):
                self.assertEqual(cli.main(["--config", str(config_path), "capture"]), 0)
        self.assertEqual(json.loads(output.getvalue())["reason"], "no power")

    def test_state_survives_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            cli.save_state(path, {"attempt": 1})
            cli.save_state(path, {"attempt": 2})
            self.assertEqual(cli.read_state(path), {"attempt": 2})
            self.assertEqual(list(path.parent.glob(".state-*")), [])

    def test_corrupt_state_is_not_treated_as_new(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text("broken")
            with self.assertRaises(ValueError):
                cli.read_state(path)

    def test_wall_clock_rollback_does_not_erase_cooldown(self):
        now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        state = {"last_attempt_utc": (now + timedelta(hours=1)).isoformat()}
        self.assertTrue(cli.cooling_down(state, "last_attempt_utc", 60, now))

    def test_wall_clock_forward_jump_does_not_erase_same_boot_cooldown(self):
        now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        state = {
            "last_attempt_utc": (now - timedelta(days=1)).isoformat(),
            "last_attempt_utc_boot_id": "boot",
            "last_attempt_utc_uptime": 100,
        }
        self.assertTrue(
            cli.cooling_down(
                state, "last_attempt_utc", 60, now, now_uptime=101, boot_id="boot"
            )
        )
        self.assertFalse(
            cli.cooling_down(
                state, "last_attempt_utc", 60, now, now_uptime=161, boot_id="boot"
            )
        )

    def test_empty_queue_does_not_start_cooldown(self):
        config = load_config(None)
        config["server"] = {"host": "example.invalid"}
        spool = Mock()
        spool.list_pending.return_value = []
        with (
            patch("timelapse.cli.save_state", side_effect=AssertionError),
            patch("timelapse.cli.power_decision", side_effect=AssertionError),
        ):
            self.assertEqual(
                cli.upload(config, spool, Mock())["reason"], "No pending photographs"
            )

    def test_transfer_failure_is_visible_to_systemd(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"spool": str(Path(temporary) / "spool")}))
            with (
                patch(
                    "timelapse.cli.upload",
                    return_value={"transfer": {"status": "error"}},
                ),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(cli.main(["--config", str(path), "upload"]), 1)


if __name__ == "__main__":
    unittest.main()
