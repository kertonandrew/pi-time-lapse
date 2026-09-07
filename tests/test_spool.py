import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from timelapse.spool import CapacityError, Spool, SpoolError, atomic_json


JPEG = b"\xff\xd8photograph\xff\xd9"


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.spool = Spool(Path(self.directory.name) / "spool", min_free_bytes=0)

    def metadata(
        self,
        filename="capture.jpg",
        timestamp="2026-09-07T12:00:00+00:00",
        payload=JPEG,
    ):
        return {
            "filename": filename,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "captured_at_utc": timestamp,
            "time_source": "RTC",
            "boot_id": "boot-one",
            "capture_duration_seconds": 1.25,
        }

    def publish(self, filename="capture.jpg", timestamp="2026-09-07T12:00:00+00:00"):
        metadata = self.metadata(filename, timestamp)
        temporary = self.spool.images / ".capture-test.part"
        temporary.write_bytes(JPEG)
        with self.spool.lock():
            self.spool.publish(temporary, metadata)
        return metadata

    def test_pending_is_chronological_and_receipted_images_remain_local(self):
        newer = self.publish("a-new.jpg", "2026-09-08T12:00:00+00:00")
        older = self.publish("z-old.jpg")
        self.assertEqual(self.spool.list_pending(), [older, newer])
        self.spool.record_receipt("z-old.jpg", {"server": "image-server"})
        self.assertEqual(self.spool.list_pending(), [newer])
        self.assertEqual(self.spool.image_path("z-old.jpg").read_bytes(), JPEG)
        receipt = json.loads(self.spool.receipt_path("z-old.jpg").read_text())
        self.assertEqual(receipt["sha256"], older["sha256"])
        self.assertEqual(receipt["server"], "image-server")

    def test_interrupted_metadata_publication_recovers_original_image(self):
        original_atomic_json = atomic_json
        metadata = self.metadata()
        temporary = self.spool.images / ".capture-interrupted.part"
        temporary.write_bytes(JPEG)

        def fail_metadata(path, value):
            if path == self.spool.metadata_path("capture.jpg"):
                raise OSError("injected metadata write failure")
            original_atomic_json(path, value)

        with patch("timelapse.spool.atomic_json", side_effect=fail_metadata):
            with self.spool.lock(), self.assertRaises(OSError):
                self.spool.publish(temporary, metadata)
        self.assertEqual(self.spool.image_path("capture.jpg").read_bytes(), JPEG)
        restarted = Spool(self.spool.root, min_free_bytes=0)
        self.assertEqual(restarted.recover(), [metadata])
        self.assertEqual(restarted.list_pending(), [metadata])
        self.assertFalse(temporary.exists())
        self.assertEqual(list(self.spool.metadata.glob(".*.pending.json")), [])

    def test_interruption_before_image_link_recovers_from_durable_journal(self):
        temporary = self.spool.images / ".capture-beforelink.part"
        temporary.write_bytes(JPEG)
        metadata = self.metadata()
        with patch(
            "timelapse.spool.os.link", side_effect=OSError("injected link failure")
        ):
            with self.spool.lock(), self.assertRaises(OSError):
                self.spool.publish(temporary, metadata)
        self.assertFalse(self.spool.image_path("capture.jpg").exists())
        self.assertTrue(temporary.exists())
        self.assertEqual(self.spool.recover(), [metadata])
        self.assertEqual(self.spool.image_path("capture.jpg").read_bytes(), JPEG)

    def test_receipt_failure_keeps_original_receipt_and_pending_state(self):
        metadata = self.publish()
        receipt_path = self.spool.receipt_path("capture.jpg")
        receipt_path.write_text("{incomplete")
        with patch(
            "timelapse.spool.os.replace", side_effect=OSError("injected rename failure")
        ):
            with self.assertRaises(OSError):
                self.spool.record_receipt("capture.jpg", {"server": "host"})
        self.assertEqual(receipt_path.read_text(), "{incomplete")
        self.assertEqual(self.spool.list_pending(), [metadata])
        self.assertEqual(self.spool.image_path("capture.jpg").read_bytes(), JPEG)
        self.assertEqual(list(self.spool.receipts.glob("*.tmp")), [])

    def test_completed_receipt_survives_failed_receipt_replacement(self):
        self.publish()
        self.spool.record_receipt("capture.jpg", {"server": "old-server"})
        before = self.spool.receipt_path("capture.jpg").read_bytes()
        with patch(
            "timelapse.spool.os.replace", side_effect=OSError("injected rename failure")
        ):
            with self.assertRaises(OSError):
                self.spool.record_receipt("capture.jpg", {"server": "new-server"})
        self.assertEqual(self.spool.receipt_path("capture.jpg").read_bytes(), before)
        self.assertEqual(self.spool.list_pending(), [])

    def test_wrong_receipt_identity_cannot_hide_an_image(self):
        metadata = self.publish()
        with self.assertRaises(SpoolError):
            self.spool.record_receipt("capture.jpg", {"sha256": "0" * 64})
        wrong = dict(metadata, sha256="0" * 64)
        self.spool.receipt_path("capture.jpg").write_text(json.dumps(wrong))
        self.assertEqual(self.spool.list_pending(), [metadata])

    def test_capacity_rejection_keeps_all_pending_images(self):
        metadata = self.publish()
        self.spool.max_bytes = self.spool.used_bytes()
        with self.assertRaises(CapacityError):
            self.spool.check_capacity(1)
        self.assertEqual(self.spool.list_pending(), [metadata])
        self.assertEqual(self.spool.image_path("capture.jpg").read_bytes(), JPEG)

    def test_free_space_floor_refuses_reservation(self):
        self.spool.min_free_bytes = 100
        with patch(
            "timelapse.spool.shutil.disk_usage", return_value=SimpleNamespace(free=110)
        ):
            with self.assertRaises(CapacityError):
                self.spool.check_capacity(11)
            self.spool.check_capacity(10)

    def test_interrupted_unjournaled_temporary_is_retained_and_counted(self):
        temporary = self.spool.images / ".capture-crashed.part"
        temporary.write_bytes(JPEG)
        self.assertEqual(self.spool.recover(), [])
        self.assertEqual(self.spool.list_pending(), [])
        self.assertEqual(temporary.read_bytes(), JPEG)
        self.assertEqual(self.spool.used_bytes(), len(JPEG))

    def test_existing_image_is_never_overwritten(self):
        original = self.publish()
        temporary = self.spool.images / ".capture-duplicate.part"
        temporary.write_bytes(JPEG + b"different")
        with self.spool.lock(), self.assertRaises(SpoolError):
            self.spool.publish(temporary, self.metadata(payload=JPEG + b"different"))
        self.assertEqual(self.spool.image_path("capture.jpg").read_bytes(), JPEG)
        self.assertEqual(self.spool.list_pending(), [original])

    def test_invalid_or_path_escaping_names_are_rejected(self):
        for name in (
            "../photo.jpg",
            "/photo.jpg",
            "a/b.jpg",
            "name.png",
            ".hidden.jpg",
            "x\n.jpg",
            "a" * 201 + ".jpg",
            None,
        ):
            with self.subTest(name=name), self.assertRaises(SpoolError):
                self.spool.image_path(name)
        self.assertEqual(self.spool.image_path("safe_1-2.3.jpg").name, "safe_1-2.3.jpg")

    def test_symbolic_link_images_and_receipts_do_not_hide_originals(self):
        metadata = self.publish()
        outside = Path(self.directory.name) / "outside.json"
        outside.write_text(json.dumps(metadata))
        self.spool.receipt_path("capture.jpg").symlink_to(outside)
        self.assertEqual(self.spool.list_pending(), [metadata])
        self.spool.image_path("capture.jpg").unlink()
        (self.spool.images / "capture.jpg").symlink_to(outside)
        with self.assertRaises(SpoolError):
            self.spool.list_pending()

    def test_corrupt_recovery_cannot_replace_original_image(self):
        metadata = self.publish()
        temporary = self.spool.images / ".capture-wrong.part"
        temporary.write_bytes(b"wrong")
        journal = self.spool.metadata / ".capture.pending.json"
        atomic_json(
            journal,
            {
                "metadata": self.metadata(payload=b"wrong"),
                "temporary_name": temporary.name,
            },
        )
        with self.assertRaises(SpoolError):
            self.spool.recover()
        self.assertEqual(self.spool.image_path("capture.jpg").read_bytes(), JPEG)
        self.assertEqual(
            json.loads(self.spool.metadata_path("capture.jpg").read_text()), metadata
        )
        self.assertTrue(temporary.exists())

    def test_missing_or_truncated_committed_images_are_reported(self):
        self.publish()
        self.spool.image_path("capture.jpg").write_bytes(b"short")
        with self.assertRaises(SpoolError):
            self.spool.list_pending()
        self.spool.image_path("capture.jpg").unlink()
        with self.assertRaises(SpoolError):
            self.spool.list_pending()

    def test_other_process_cannot_exceed_lock_budget_during_capture(self):
        metadata = self.publish()
        script = (
            "import sys,time\n"
            "from pathlib import Path\n"
            "from timelapse.spool import Spool\n"
            "spool=Spool(Path(sys.argv[1]),min_free_bytes=0)\n"
            "started=time.monotonic()\n"
            "try:\n"
            "    spool.list_pending(timeout_seconds=0.1)\n"
            "except TimeoutError:\n"
            "    print(time.monotonic()-started)\n"
            "else:\n"
            "    raise SystemExit('unexpected lock acquisition')\n"
        )
        with self.spool.lock():
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.spool.root)],
                capture_output=True,
                text=True,
                timeout=3,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(float(result.stdout.strip()), 0.09)
        self.assertLess(float(result.stdout.strip()), 1)
        self.assertEqual(self.spool.list_pending(timeout_seconds=0.1), [metadata])

    def test_receipt_lock_timeout_keeps_image_pending_and_retry_succeeds(self):
        metadata = self.publish()
        started = time.monotonic()
        with self.spool.lock():
            with self.assertRaises(TimeoutError):
                self.spool.record_receipt(
                    "capture.jpg", {"server": "host"}, timeout_seconds=0.05
                )
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(self.spool.receipt_path("capture.jpg").exists())
        self.assertEqual(self.spool.list_pending(), [metadata])
        self.spool.record_receipt(
            "capture.jpg", {"server": "host"}, timeout_seconds=0.1
        )
        self.assertEqual(self.spool.list_pending(), [])

    def test_invalid_lock_budgets_fail_before_mutation(self):
        self.publish()
        for budget in (-1, float("nan"), float("inf"), float("-inf"), True, False, "1"):
            with self.subTest(budget=budget):
                with self.assertRaises(ValueError):
                    with self.spool.lock(timeout_seconds=budget):
                        self.fail("Invalid lock budget was accepted")
                with self.assertRaises(ValueError):
                    self.spool.list_pending(timeout_seconds=budget)
                with self.assertRaises(ValueError):
                    self.spool.record_receipt("capture.jpg", {}, timeout_seconds=budget)
        self.assertFalse(self.spool.receipt_path("capture.jpg").exists())

    def test_zero_lock_budget_expires_even_when_lock_is_free(self):
        with self.assertRaises(TimeoutError):
            with self.spool.lock(timeout_seconds=0):
                self.fail("Expired lock budget was accepted")
        self.assertEqual(self.spool.list_pending(timeout_seconds=0.1), [])


if __name__ == "__main__":
    unittest.main()
