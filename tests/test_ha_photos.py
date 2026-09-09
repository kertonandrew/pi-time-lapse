import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from timelapse import ha_photos, receiver


JPEG = b"\xff\xd8original photograph\xff\xd9"
DEVICE = "zero-one"


class LatestPhotoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "archive"

    def commit(
        self,
        filename="photo.jpg",
        captured="2026-09-08T05:00:00Z",
        payload=JPEG,
        time_source="RTC",
        stored=None,
        extra=None,
    ):
        metadata = {
            "filename": filename,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "captured_at_utc": captured,
            "time_source": time_source,
            "boot_id": "one-boot",
            "capture_duration_seconds": 8.5,
        }
        if extra is not None:
            metadata.update(extra)
        receiver.handle_request("init", self.root, DEVICE, {"protocol": 1, "files": []})
        (self.root / DEVICE / "incoming" / filename).write_bytes(payload)
        atomic = receiver._atomic_json

        def store_record(path, value):
            if path.parent.name == "receipts" and stored is not None:
                value["stored_at_utc"] = stored
            return atomic(path, value)

        with patch.object(receiver, "_atomic_json", side_effect=store_record):
            result = receiver.handle_request(
                "commit", self.root, DEVICE, {"protocol": 1, "metadata": metadata}
            )
        return metadata, result["receipt"]

    def assert_selected_after_legacy_scan_and_reindex(self, filename):
        self.assertEqual(
            ha_photos.latest_photo(self.root, DEVICE)["metadata"]["filename"], filename
        )
        index = self.root / DEVICE / "latest.json"
        index.unlink()
        self.assertEqual(
            ha_photos.latest_photo(self.root, DEVICE)["metadata"]["filename"], filename
        )
        self.assertFalse(index.exists())
        result = receiver.handle_request("reindex", self.root, DEVICE, {"protocol": 1})
        self.assertEqual(result["latest_filename"], filename)
        self.assertEqual(
            ha_photos.latest_photo(self.root, DEVICE)["metadata"]["filename"], filename
        )

    def test_reads_complete_committed_receiver_fixture_without_modifying_it(self):
        metadata, receipt = self.commit()
        before = {
            str(path): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
            if path.is_file()
        }
        result = ha_photos.latest_photo(self.root, DEVICE)
        self.assertEqual(result["payload"], JPEG)
        self.assertEqual(
            result["metadata"], dict(metadata, uploaded_at_utc=receipt["stored_at_utc"])
        )
        after = {
            str(path): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_absent_or_empty_archive_returns_none_without_creating_paths(self):
        self.assertIsNone(ha_photos.latest_photo(self.root, DEVICE))
        self.assertFalse(self.root.exists())
        self.root.mkdir()
        self.assertIsNone(ha_photos.latest_photo(self.root, DEVICE))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_uncommitted_and_pending_uploads_are_ignored(self):
        receiver.handle_request("init", self.root, DEVICE, {"protocol": 1, "files": []})
        paths = self.root / DEVICE
        (paths / "incoming/unfinished.jpg").write_bytes(JPEG)
        (paths / "images/unreceipted.jpg").write_bytes(JPEG)
        (paths / "metadata/unreceipted.json").write_text("invalid pending metadata")
        (paths / "receipts/.pending-record").write_text("not complete")
        self.assertIsNone(ha_photos.latest_photo(self.root, DEVICE))

    def test_newest_capture_timestamp_wins_and_only_selected_image_is_hashed(self):
        self.commit("z-old.jpg", "2026-09-08T04:00:00+00:00")
        self.commit("a-new.jpg", "2026-09-08T05:00:00Z", b"\xff\xd8new photo\xff\xd9")
        (self.root / DEVICE / "images/z-old.jpg").write_bytes(b"old corruption")
        with patch.object(ha_photos.hashlib, "sha256", wraps=hashlib.sha256) as digest:
            result = ha_photos.latest_photo(self.root, DEVICE)
        self.assertEqual(result["metadata"]["filename"], "a-new.jpg")
        digest.assert_called_once_with(b"\xff\xd8new photo\xff\xd9")

    def test_equal_capture_timestamps_use_filename_as_deterministic_tiebreaker(self):
        self.commit("a.jpg")
        self.commit("z.jpg")
        self.assertEqual(
            ha_photos.latest_photo(self.root, DEVICE)["metadata"]["filename"], "z.jpg"
        )

    def test_trusted_photo_displaces_future_unsync_capture_and_cannot_be_pinned(self):
        self.commit("future.jpg", "2099-01-01T00:00:00Z", time_source="UNSYNC")
        self.commit("trusted.jpg", time_source="NTP")
        self.commit("later-unsync.jpg", "2100-01-01T00:00:00Z", time_source="UNSYNC")
        self.assert_selected_after_legacy_scan_and_reindex("trusted.jpg")
        self.commit("newer-trusted.jpg", "2026-09-09T05:00:00Z", time_source="RTC")
        self.assert_selected_after_legacy_scan_and_reindex("newer-trusted.jpg")

    def test_unsync_only_uses_receipt_time_before_filename_and_ignores_capture_time(
        self,
    ):
        self.commit(
            "z-future.jpg",
            "2099-01-01T00:00:00Z",
            time_source="UNSYNC",
            stored="2026-09-08T05:00:00Z",
        )
        self.commit(
            "a-later.jpg",
            "1970-01-01T00:00:00Z",
            time_source="UNSYNC",
            stored="2026-09-08T05:01:00Z",
        )
        self.assert_selected_after_legacy_scan_and_reindex("a-later.jpg")
        self.commit(
            "z-tie.jpg",
            "1970-01-01T00:00:00Z",
            time_source="UNSYNC",
            stored="2026-09-08T05:01:00Z",
        )
        self.assert_selected_after_legacy_scan_and_reindex("z-tie.jpg")

    def test_metadata_exact_byte_limit_is_readable_after_oversized_upload_rejection(
        self,
    ):
        metadata, _receipt = self.commit()
        boundary = dict(metadata, filename="z-boundary.jpg", extra="\u26a1", padding="")
        boundary["padding"] = "x" * (
            receiver.MAX_INDEX_RECORD_BYTES - len(receiver._json_bytes(boundary))
        )
        expected, receipt = self.commit("z-boundary.jpg", extra=boundary)
        result = ha_photos.latest_photo(self.root, DEVICE)
        self.assertEqual(
            result["metadata"], dict(expected, uploaded_at_utc=receipt["stored_at_utc"])
        )
        with self.assertRaisesRegex(ValueError, "metadata exceeds its byte limit"):
            receiver.handle_request(
                "commit",
                self.root,
                DEVICE,
                {
                    "protocol": 1,
                    "metadata": dict(boundary, padding=boundary["padding"] + "x"),
                },
            )
        self.assertEqual(ha_photos.latest_photo(self.root, DEVICE), result)
        self.commit("zz-healthy.jpg")
        self.assert_selected_after_legacy_scan_and_reindex("zz-healthy.jpg")

    def test_selected_image_corruption_is_rejected(self):
        self.commit()
        image = self.root / DEVICE / "images/photo.jpg"
        image.write_bytes(JPEG.replace(b"original", b"tampered"))
        with self.assertRaisesRegex(ValueError, "SHA256"):
            ha_photos.latest_photo(self.root, DEVICE)

    def test_invalid_jpeg_markers_are_rejected_even_with_matching_receipt(self):
        self.commit(payload=b"not actually a jpeg")
        with self.assertRaisesRegex(ValueError, "complete JPEG"):
            ha_photos.latest_photo(self.root, DEVICE)

    def test_receipt_or_metadata_corruption_does_not_fall_back_to_older_image(self):
        self.commit("old.jpg", "2026-09-08T04:00:00Z")
        self.commit()
        receipt_path = self.root / DEVICE / "receipts/photo.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["device_id"] = "different-device"
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "receipt"):
            ha_photos.latest_photo(self.root, DEVICE)

    def test_receipt_paths_and_protocol_types_are_verified(self):
        self.commit()
        path = self.root / DEVICE / "receipts/photo.json"
        original = json.loads(path.read_text())
        for change in (
            {"protocol": True},
            {"image_relative_path": "../../secret"},
            {"stored_at_utc": "yesterday"},
        ):
            path.write_text(json.dumps(dict(original, **change)))
            with self.subTest(change=change), self.assertRaises(ValueError):
                ha_photos.latest_photo(self.root, DEVICE)

    def test_symlinked_images_metadata_receipts_or_directories_are_rejected(self):
        self.commit()
        for relative in (
            "images/photo.jpg",
            "metadata/photo.json",
            "receipts/photo.json",
        ):
            path = self.root / DEVICE / relative
            content = path.read_bytes()
            target = self.directory / "outside"
            target.write_bytes(content)
            path.unlink()
            path.symlink_to(target)
            with (
                self.subTest(relative=relative),
                self.assertRaises((OSError, ValueError)),
            ):
                ha_photos.latest_photo(self.root, DEVICE)
            path.unlink()
            path.write_bytes(content)
        link = self.directory / "linked-root"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            ha_photos.latest_photo(link, DEVICE)

    def test_nonregular_image_does_not_block_reader(self):
        self.commit()
        path = self.root / DEVICE / "images/photo.jpg"
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, "type or size"):
            ha_photos.latest_photo(self.root, DEVICE)

    def test_oversized_selected_image_or_metadata_is_rejected(self):
        self.commit()
        with self.assertRaisesRegex(ValueError, "byte limit"):
            ha_photos.latest_photo(self.root, DEVICE, max_bytes=4)
        path = self.root / DEVICE / "metadata/photo.json"
        path.write_bytes(b" " * (ha_photos.MAX_RECORD_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "type or size"):
            ha_photos.latest_photo(self.root, DEVICE)

    def test_image_replacement_during_read_is_rejected(self):
        self.commit()
        path = self.root / DEVICE / "images/photo.jpg"
        inode = path.stat().st_ino
        original_fstat = os.fstat
        reads = 0

        def changed(descriptor):
            nonlocal reads
            details = original_fstat(descriptor)
            if details.st_ino == inode:
                reads += 1
                if reads == 2:
                    replacement = path.with_suffix(".replacement")
                    replacement.write_bytes(JPEG)
                    replacement.replace(path)
            return details

        with patch.object(ha_photos.os, "fstat", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "changed"):
                ha_photos.latest_photo(self.root, DEVICE)

    def test_scan_and_parameter_bounds(self):
        self.commit()
        (self.root / DEVICE / "latest.json").unlink()
        with patch.object(ha_photos, "MAX_SCAN_ENTRIES", 0):
            with self.assertRaisesRegex(
                ValueError, "timelapse.receiver reindex.*max_entries"
            ):
                ha_photos.latest_photo(self.root, DEVICE)
        for maximum in (True, 3, 33554433):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                ha_photos.latest_photo(self.root, DEVICE, maximum)
        with self.assertRaises(ValueError):
            ha_photos.latest_photo(self.root, "../other")

    def test_latest_index_avoids_archive_scan_and_unrelated_old_corruption(self):
        self.commit("old.jpg", "2026-09-08T04:00:00Z")
        self.commit()
        (self.root / DEVICE / "metadata/old.json").write_text("old corrupted metadata")
        with patch.object(
            ha_photos.os,
            "scandir",
            side_effect=AssertionError("unexpected archive scan"),
        ):
            result = ha_photos.latest_photo(self.root, DEVICE)
        self.assertEqual(result["metadata"]["filename"], "photo.jpg")

    def test_index_cross_file_mismatch_fails_instead_of_falling_back(self):
        self.commit()
        path = self.root / DEVICE / "latest.json"
        index = json.loads(path.read_text())
        index["metadata"]["capture_duration_seconds"] = 100
        path.write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError, "index does not match"):
            ha_photos.latest_photo(self.root, DEVICE)
        path.write_text("not valid JSON")
        with self.assertRaises(ValueError):
            ha_photos.latest_photo(self.root, DEVICE)

    def test_legacy_reader_still_selects_newest_without_writing_index(self):
        self.commit("z-old.jpg", "2026-09-08T04:00:00Z")
        self.commit("a-new.jpg")
        path = self.root / DEVICE / "latest.json"
        path.unlink()
        result = ha_photos.latest_photo(self.root, DEVICE)
        self.assertEqual(result["metadata"]["filename"], "a-new.jpg")
        self.assertFalse(path.exists())

    def test_empty_rebuilt_index_returns_none(self):
        receiver.handle_request("reindex", self.root, DEVICE, {"protocol": 1})
        self.assertIsNone(ha_photos.latest_photo(self.root, DEVICE))


if __name__ == "__main__":
    unittest.main()
