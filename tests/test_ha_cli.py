import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from timelapse import ha
from timelapse.ha_config import validate_config


class HomeAssistantPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
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
