import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from timelapse.capture import CaptureError, capture
from timelapse.spool import CapacityError, MAX_IMAGE_BYTES, Spool


JPEG = b"\xff\xd8photograph\xff\xd9"


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.spool = Spool(Path(self.directory.name) / "spool", min_free_bytes=0)

    def fake_camera(self, command, **kwargs):
        Path(command[command.index("--output") + 1]).write_bytes(JPEG)
        return SimpleNamespace(returncode=0)

    def test_success_publishes_jpeg_and_durable_metadata(self):
        with patch(
            "timelapse.capture.subprocess.run", side_effect=self.fake_camera
        ) as run:
            result = capture(self.spool, time_source="RTC")
        self.assertEqual(self.spool.image_path(result["filename"]).read_bytes(), JPEG)
        self.assertEqual(self.spool.list_pending(), [result])
        self.assertEqual(
            json.loads(self.spool.metadata_path(result["filename"]).read_text()), result
        )
        self.assertEqual(result["size_bytes"], len(JPEG))
        self.assertEqual(result["time_source"], "RTC")
        self.assertEqual(
            datetime.fromisoformat(result["captured_at_utc"])
            .utcoffset()
            .total_seconds(),
            0,
        )
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--encoding") + 1], "jpg")
        self.assertEqual(command[command.index("--rotation") + 1], "180")
        self.assertEqual(run.call_args.kwargs["timeout"], 45)
        self.assertEqual(list(self.spool.images.glob(".*.part")), [])

    def test_equal_timestamps_produce_distinct_images(self):
        timestamp = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
        with patch("timelapse.capture.datetime") as clock:
            clock.now.return_value = timestamp
            with patch(
                "timelapse.capture.subprocess.run", side_effect=self.fake_camera
            ):
                first = capture(self.spool)
                second = capture(self.spool)
        self.assertNotEqual(first["filename"], second["filename"])
        self.assertEqual(first["captured_at_utc"], second["captured_at_utc"])
        self.assertEqual(len(self.spool.list_pending()), 2)

    def test_camera_failure_preserves_older_pending_images(self):
        with patch("timelapse.capture.subprocess.run", side_effect=self.fake_camera):
            original = capture(self.spool)

        def failure(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(b"partial")
            kwargs["stderr"].write(b"camera disconnected")
            return SimpleNamespace(returncode=1)

        with patch("timelapse.capture.subprocess.run", side_effect=failure):
            with self.assertRaisesRegex(CaptureError, "camera disconnected"):
                capture(self.spool)
        self.assertEqual(self.spool.list_pending(), [original])
        self.assertEqual(self.spool.image_path(original["filename"]).read_bytes(), JPEG)
        self.assertEqual(list(self.spool.images.glob(".*.part")), [])

    def test_camera_timeout_leaves_no_committed_partial_image(self):
        def timeout(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(b"partial")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with patch("timelapse.capture.subprocess.run", side_effect=timeout):
            with self.assertRaisesRegex(CaptureError, "exceeded"):
                capture(self.spool, timeout_seconds=0.25)
        self.assertEqual(self.spool.list_pending(), [])
        self.assertEqual(list(self.spool.images.iterdir()), [])

    def test_real_process_timeout_kills_capture_child(self):
        executable = Path(self.directory.name) / "slow-camera"
        executable.write_text("#!/bin/sh\nexec sleep 10\n")
        executable.chmod(0o700)
        with self.assertRaisesRegex(CaptureError, "exceeded"):
            capture(self.spool, camera_command=str(executable), timeout_seconds=0.05)
        self.assertEqual(self.spool.list_pending(), [])

    def test_invalid_jpeg_is_not_published(self):
        for payload in (b"", b"not-a-jpeg", b"\xff\xd8partial"):

            def incomplete(command, **kwargs):
                Path(command[command.index("--output") + 1]).write_bytes(payload)
                return SimpleNamespace(returncode=0)

            with (
                self.subTest(payload=payload),
                patch("timelapse.capture.subprocess.run", side_effect=incomplete),
            ):
                with self.assertRaises(CaptureError):
                    capture(self.spool)
            self.assertEqual(self.spool.list_pending(), [])

    def test_oversized_camera_output_is_refused_without_queued_data_loss(self):
        with patch("timelapse.capture.subprocess.run", side_effect=self.fake_camera):
            original = capture(self.spool)

        def oversized(command, **kwargs):
            path = Path(command[command.index("--output") + 1])
            with path.open("wb") as stream:
                stream.truncate(MAX_IMAGE_BYTES + 1)
            return SimpleNamespace(returncode=0)

        with patch("timelapse.capture.subprocess.run", side_effect=oversized):
            with self.assertRaises(CaptureError):
                capture(self.spool)
        self.assertEqual(self.spool.list_pending(), [original])

    def test_capacity_refusal_happens_before_camera_starts(self):
        with patch("timelapse.capture.subprocess.run", side_effect=self.fake_camera):
            original = capture(self.spool)
        self.spool.max_bytes = MAX_IMAGE_BYTES
        with patch("timelapse.capture.subprocess.run") as run:
            with self.assertRaises(CapacityError):
                capture(self.spool)
        run.assert_not_called()
        self.assertEqual(self.spool.list_pending(), [original])

    def test_actual_free_space_exhaustion_discards_only_uncommitted_capture(self):
        with patch("timelapse.capture.subprocess.run", side_effect=self.fake_camera):
            original = capture(self.spool)
        with patch.object(
            self.spool,
            "check_capacity",
            side_effect=[None, CapacityError("disk filled")],
        ):
            with patch(
                "timelapse.capture.subprocess.run", side_effect=self.fake_camera
            ):
                with self.assertRaises(CapacityError):
                    capture(self.spool)
        self.assertEqual(self.spool.list_pending(), [original])
        self.assertEqual(list(self.spool.images.glob(".*.part")), [])

    def test_metadata_failure_retains_recoverable_new_capture(self):
        from timelapse import spool as spool_module

        original_atomic_json = spool_module.atomic_json

        def fail_metadata(path, value):
            if not path.name.startswith("."):
                raise OSError("injected metadata failure")
            original_atomic_json(path, value)

        with patch("timelapse.capture.subprocess.run", side_effect=self.fake_camera):
            with patch("timelapse.spool.atomic_json", side_effect=fail_metadata):
                with self.assertRaisesRegex(CaptureError, "preserve"):
                    capture(self.spool)
        committed = list(self.spool.images.glob("*.jpg"))
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0].read_bytes(), JPEG)
        recovered = self.spool.list_pending()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["filename"], committed[0].name)

    def test_missing_camera_is_reported_without_image_publication(self):
        with self.assertRaisesRegex(CaptureError, "Unable to start camera"):
            capture(
                self.spool,
                camera_command=str(Path(self.directory.name) / "does-not-exist"),
            )
        self.assertEqual(self.spool.list_pending(), [])


if __name__ == "__main__":
    unittest.main()
