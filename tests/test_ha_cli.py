import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from timelapse import ha, ha_controls
from timelapse.ha_config import validate_config
from tests.test_ha_controls import camera_config


class HomeAssistantPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = validate_config(
            {
                "device_id": "test-camera",
                "discovery_refresh_seconds": 3600,
                "state_directory": str(self.root / "state"),
                "mqtt": {"host": "broker.test"},
            }
        )
        self.metrics = {
            "usb_input_status": "PRESENT",
            "gpio_input_status": "NOT_PRESENT",
            "battery_voltage": 3.9,
            "observed_at": "2026-09-08T06:00:00+00:00",
            "session_id": "s1",
            "sample_index": 1,
        }
        self.metric_reader = patch(
            "timelapse.ha.telemetry_sample", return_value=self.metrics
        ).start()
        self.publisher = patch("timelapse.ha.publish_messages").start()
        self.addCleanup(patch.stopall)

    def controls_fixture(self):
        local = camera_config(self.root / "controls/settings.json")
        local["remote_controls"]["device_id"] = self.config["device_id"]
        patch("timelapse.ha.load_timelapse_config", return_value=local).start()
        subscriber = patch("timelapse.ha.receive_messages", return_value=[]).start()
        return local, subscriber

    def test_controls_preview_has_no_network_or_configuration_writes(self):
        local, subscriber = self.controls_fixture()
        result = ha.run_controls(self.config, dry_run=True)
        self.assertEqual(result["action"], "preview")
        self.assertFalse(result["configuration_writes"])
        self.assertEqual(len(result["desired_topics"]), 6)
        subscriber.assert_not_called()
        self.publisher.assert_not_called()
        self.metric_reader.assert_not_called()
        self.assertFalse(Path(local["remote_controls"]["state_path"]).parent.exists())

    def test_controls_require_local_opt_in_identity_and_sufficient_power(self):
        local, subscriber = self.controls_fixture()
        local["remote_controls"]["enabled"] = False
        self.assertEqual(ha.run_controls(self.config)["action"], "wait")
        local["remote_controls"]["enabled"] = True
        local["remote_controls"]["device_id"] = "other"
        with self.assertRaisesRegex(ValueError, "identities"):
            ha.run_controls(self.config)
        local["remote_controls"]["device_id"] = self.config["device_id"]
        self.metrics.update(usb_input_status="BAD", battery_voltage=3.5)
        self.assertEqual(ha.run_controls(self.config)["action"], "wait")
        subscriber.assert_not_called()
        self.publisher.assert_not_called()

    def test_controls_report_only_durable_readback_and_retained_replay_is_idempotent(
        self,
    ):
        local, subscriber = self.controls_fixture()
        base = "pi_timelapse/test-camera"
        subscriber.return_value = [
            (f"{base}/desired/interval_seconds", b"600"),
            (f"{base}/desired/capture_enabled", b"true"),
        ]

        def confirm_persisted(_mqtt, outgoing, **_kwargs):
            persisted = ha_controls.effective_settings(local)
            reported = {
                topic: payload
                for topic, payload, retain in outgoing
                if "/reported/" in topic
            }
            self.assertEqual(
                reported,
                {
                    f"{base}/reported/{field}": value
                    for field, value in ha_controls.encode_reported(persisted).items()
                },
            )
            self.assertTrue(all(retain for _topic, _payload, retain in outgoing))

        self.publisher.side_effect = confirm_persisted
        result = ha.run_controls(self.config)
        self.assertEqual(
            set(result["changed_fields"]), {"capture_enabled", "interval_seconds"}
        )
        self.assertLess(self.publisher.call_args.args[0]["timeout_seconds"], 20)
        self.assertLess(subscriber.call_args.args[0]["timeout_seconds"], 20)
        self.assertEqual(len(subscriber.call_args.args[1]), 6)
        path = Path(local["remote_controls"]["state_path"])
        inode = path.stat().st_ino
        with patch.object(
            ha_controls,
            "atomic_json",
            side_effect=AssertionError("Replay rewrote state"),
        ):
            result = ha.run_controls(self.config)
        self.assertEqual(result["changed_fields"], [])
        self.assertEqual(path.stat().st_ino, inode)
        self.assertFalse((self.root / "state").exists())

    def test_invalid_control_batch_is_not_partially_applied_or_reported(self):
        local, subscriber = self.controls_fixture()
        base = "pi_timelapse/test-camera/desired"
        for invalid in (
            (f"{base}/interval_seconds", b"NaN"),
            (f"{base}/camera_command", b"/bin/sh"),
            ("pi_timelapse/other/desired/capture_enabled", b"true"),
        ):
            subscriber.return_value = [(f"{base}/capture_enabled", b"true"), invalid]
            with self.assertRaises(ValueError):
                ha.run_controls(self.config)
            self.assertFalse(Path(local["remote_controls"]["state_path"]).exists())
        self.publisher.assert_not_called()

    def test_failed_report_ack_preserves_applied_settings_and_retry_reports_them(self):
        local, subscriber = self.controls_fixture()
        subscriber.return_value = [
            ("pi_timelapse/test-camera/desired/jpeg_quality", b"75")
        ]
        self.publisher.side_effect = RuntimeError("PUBACK missing")
        with self.assertRaises(RuntimeError):
            ha.run_controls(self.config)
        self.assertEqual(ha_controls.effective_settings(local)["jpeg_quality"], 75)
        self.publisher.side_effect = None
        with patch.object(
            ha_controls,
            "atomic_json",
            side_effect=AssertionError("Replay rewrote state"),
        ):
            result = ha.run_controls(self.config)
        self.assertEqual(result["settings"]["jpeg_quality"], 75)

    def test_failed_controls_durability_never_reports_desired_state(self):
        self.controls_fixture()
        with patch("timelapse.ha.apply_desired", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError):
                ha.run_controls(self.config)
        self.publisher.assert_not_called()

    def test_preview_performs_no_network_or_state_writes(self):
        result = ha.run(self.config, "telemetry", dry_run=True)
        self.assertEqual(result["action"], "preview")
        self.publisher.assert_not_called()
        self.assertFalse((self.root / "state").exists())

    def test_metrics_discovery_retained_but_state_not_retained(self):
        result = ha.run(self.config, "telemetry", now=1000)
        self.assertEqual(result["action"], "published")
        messages = self.publisher.call_args.args[1]
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[0][2])
        self.assertFalse(messages[1][2])
        self.assertEqual(json.loads(messages[1][1]), self.metrics)
        self.assertTrue(
            self.publisher.call_args.kwargs["connection_topic"].endswith("/telemetry")
        )
        self.assertEqual(ha.run(self.config, "telemetry", now=1100)["action"], "wait")
        self.assertEqual(self.publisher.call_count, 1)

    def test_new_sample_publishes_only_state_until_discovery_refresh(self):
        ha.run(self.config, "telemetry", now=1000)
        self.metrics["sample_index"] = 2
        ha.run(self.config, "telemetry", now=1300)
        messages = self.publisher.call_args.args[1]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0][0].endswith("/state"))
        ha.run(self.config, "telemetry", now=4600)
        messages = self.publisher.call_args.args[1]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0][0].endswith("/config"))

    def test_failed_ack_does_not_checkpoint_and_next_run_retries(self):
        self.publisher.side_effect = RuntimeError("Acknowledgment timed out")
        with self.assertRaises(RuntimeError):
            ha.run(self.config, "telemetry", now=1000)
        self.assertFalse((self.root / "state/telemetry.json").exists())
        self.publisher.side_effect = None
        self.assertEqual(
            ha.run(self.config, "telemetry", now=1300)["action"], "published"
        )

    def test_low_battery_bad_solar_skips_network(self):
        self.metrics.update(usb_input_status="BAD", battery_voltage=3.6)
        self.assertEqual(ha.run(self.config, "telemetry")["action"], "wait")
        self.publisher.assert_not_called()
        self.assertFalse((self.root / "state/telemetry.json").exists())
        self.metrics["battery_voltage"] = None
        self.assertEqual(ha.run(self.config, "telemetry")["action"], "wait")

    def test_archive_photo_retained_and_deduplicated_without_processing(self):
        photo = {
            "payload": b"\xff\xd8fixture\xff\xd9",
            "metadata": {
                "sha256": "a" * 64,
                "captured_at_utc": "2026-09-08T06:00:00+00:00",
            },
        }
        with patch("timelapse.ha.latest_photo", return_value=photo):
            result = ha.run(self.config, "photos", now=1000)
            self.assertEqual(result["action"], "published")
            messages = self.publisher.call_args.args[1]
            self.assertEqual(messages[1][1], photo["payload"])
            self.assertTrue(all(message[2] for message in messages))
            self.assertEqual(ha.run(self.config, "photos", now=1100)["action"], "wait")
            self.assertEqual(
                ha.run(self.config, "photos", now=4600)["action"], "published"
            )
            self.assertEqual(self.publisher.call_count, 2)
        self.metric_reader.assert_not_called()

    def test_role_state_files_and_client_ids_do_not_collide(self):
        ha.run(self.config, "telemetry", now=1000)
        telemetry_id = self.publisher.call_args.kwargs["client_id"]
        with patch(
            "timelapse.ha.latest_photo",
            return_value={"payload": b"x", "metadata": {"sha256": "b" * 64}},
        ):
            ha.run(self.config, "photos", now=1000)
        self.assertNotEqual(telemetry_id, self.publisher.call_args.kwargs["client_id"])
        self.assertTrue((self.root / "state/telemetry.json").is_file())
        self.assertTrue((self.root / "state/photos.json").is_file())

    def test_identical_pixels_with_new_capture_metadata_are_published(self):
        photo = {
            "payload": b"jpeg",
            "metadata": {
                "sha256": "a" * 64,
                "captured_at_utc": "2026-09-08T06:00:00+00:00",
            },
        }
        with patch("timelapse.ha.latest_photo", return_value=photo):
            ha.run(self.config, "photos", now=1000)
            photo["metadata"]["captured_at_utc"] = "2026-09-08T06:05:00+00:00"
            self.assertEqual(
                ha.run(self.config, "photos", now=1300)["action"], "published"
            )
        self.assertEqual(self.publisher.call_count, 2)

    def test_registration_and_removal_use_each_roles_acl_topics(self):
        for role, endings in (
            ("telemetry", ("/config", "/state")),
            ("photos", ("/config", "/image", "/image_metadata")),
        ):
            ha.register(self.config, role)
            self.assertTrue(
                self.publisher.call_args.kwargs["connection_topic"].endswith("/" + role)
            )
            ha.remove(self.config, role)
            messages = self.publisher.call_args.args[1]
            self.assertEqual(len(messages), len(endings))
            for message, ending in zip(messages, endings):
                self.assertTrue(message[0].endswith(ending))
                self.assertEqual(message[1:], (b"", True))
            self.assertTrue(
                self.publisher.call_args.kwargs["connection_topic"].endswith("/" + role)
            )

    def test_concurrent_session_is_rejected(self):
        with ha.state_lock(self.config["state_directory"], "telemetry"):
            with self.assertRaises(BlockingIOError):
                ha.run(self.config, "telemetry", now=1000)
        self.publisher.assert_not_called()

    def test_broker_change_requires_fresh_discovery_and_content(self):
        ha.run(self.config, "telemetry", now=1000)
        changed = copy.deepcopy(self.config)
        changed["mqtt"]["host"] = "replacement.test"
        result = ha.run(changed, "telemetry", now=1100)
        self.assertEqual(result["action"], "published")
        self.assertEqual(len(self.publisher.call_args.args[1]), 2)

    def test_backward_wall_clock_republishes_discovery(self):
        ha.run(self.config, "telemetry", now=1000)
        self.assertEqual(
            ha.run(self.config, "telemetry", now=900)["action"], "published"
        )


if __name__ == "__main__":
    unittest.main()
