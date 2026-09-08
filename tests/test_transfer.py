import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from timelapse import receiver
from timelapse import transfer as uploader


class FakeSpool:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir()
        self.metadata = []
        self.receipts = {}
        self.fail_receipt = False

    def add(
        self,
        filename="20260907T120000-test.jpg",
        content=b"\xff\xd8synthetic-camera-data\xff\xd9",
    ):
        (self.root / filename).write_bytes(content)
        metadata = {
            "filename": filename,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "captured_at_utc": "2026-09-07T12:00:00+00:00",
            "time_source": "RTC",
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "capture_duration_seconds": 1.25,
        }
        self.metadata.append(metadata)
        return metadata

    def list_pending(self, timeout_seconds=None):
        return [item for item in self.metadata if item["filename"] not in self.receipts]

    def image_path(self, filename):
        return self.root / filename

    def record_receipt(self, filename, receipt, timeout_seconds=None):
        if self.fail_receipt:
            raise OSError("Simulated local receipt disk failure")
        self.receipts[filename] = receipt


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "server"
        self.spool = FakeSpool(self.base / "spool")
        self.metadata = self.spool.add()

    def initialize(self, device="zero-one", files=None):
        return receiver.handle_request(
            "init", self.root, device, {"protocol": 1, "files": files or []}
        )

    def stage(self, metadata=None, device="zero-one"):
        metadata = metadata or self.metadata
        self.initialize(device)
        destination = self.root / device / "incoming" / metadata["filename"]
        shutil.copyfile(self.spool.image_path(metadata["filename"]), destination)
        return destination

    def commit(self, metadata=None, device="zero-one"):
        return receiver.handle_request(
            "commit",
            self.root,
            device,
            {"protocol": 1, "metadata": metadata or self.metadata},
        )

    def test_cli_commits_verified_image_and_exact_offline_metadata(self):
        self.stage()
        command = [
            sys.executable,
            str(Path(receiver.__file__)),
            "commit",
            "--root",
            str(self.root),
            "--device",
            "zero-one",
        ]
        completed = subprocess.run(
            command,
            input=json.dumps({"protocol": 1, "metadata": self.metadata}),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads(completed.stdout)["receipt"]
        self.assertEqual(receipt["sha256"], self.metadata["sha256"])
        self.assertEqual(
            (self.root / "zero-one/images" / self.metadata["filename"]).read_bytes(),
            self.spool.image_path(self.metadata["filename"]).read_bytes(),
        )
        metadata_path = (
            self.root
            / "zero-one/metadata"
            / (Path(self.metadata["filename"]).stem + ".json")
        )
        self.assertEqual(json.loads(metadata_path.read_text()), self.metadata)
        self.assertEqual(
            json.loads(
                (self.root / "zero-one/receipts" / metadata_path.name).read_text()
            ),
            receipt,
        )
        self.assertFalse(
            (self.root / "zero-one/incoming" / self.metadata["filename"]).exists()
        )

    def test_truncated_and_same_size_corrupt_files_never_receive_receipt(self):
        destination = self.stage()
        for content in (b"short", b"x" * self.metadata["size_bytes"]):
            with self.subTest(content=content):
                destination.write_bytes(content)
                with self.assertRaises(ValueError):
                    self.commit()
                self.assertEqual(list((self.root / "zero-one/receipts").iterdir()), [])
                self.assertEqual(list((self.root / "zero-one/images").iterdir()), [])

    def test_retries_recover_durable_receipt_without_staged_image(self):
        self.stage()
        first = self.commit()["receipt"]
        self.assertEqual(self.commit()["receipt"], first)
        initialized = self.initialize(files=[self.metadata])
        self.assertEqual(initialized["receipts"], [first])

    def latest(self):
        return json.loads((self.root / "zero-one/latest.json").read_text())

    def test_latest_index_contains_exact_metadata_and_committed_receipt(self):
        self.stage()
        receipt = self.commit()["receipt"]
        self.assertEqual(
            self.latest(),
            {
                "protocol": 1,
                "device_id": "zero-one",
                "metadata": self.metadata,
                "receipt": receipt,
            },
        )

    def test_oversized_metadata_rejects_every_upload_action_before_layout_changes(self):
        oversized = dict(self.metadata, extra="x" * receiver.MAX_INDEX_RECORD_BYTES)
        requests = {
            "init": {"protocol": 1, "files": [self.metadata, oversized]},
            "commit-batch": {"protocol": 1, "files": [self.metadata, oversized]},
            "commit": {"protocol": 1, "metadata": oversized},
        }
        for action, request in requests.items():
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(ValueError, "metadata exceeds its byte limit"),
            ):
                receiver.handle_request(action, self.root, "zero-one", request)
            self.assertFalse(self.root.exists())

    def test_metadata_byte_boundary_keeps_acknowledged_index_readable_after_rejection(
        self,
    ):
        self.metadata["extra"] = "\u26a1"
        self.metadata["padding"] = ""
        self.metadata["padding"] = "x" * (
            receiver.MAX_INDEX_RECORD_BYTES - len(receiver._json_bytes(self.metadata))
        )
        self.stage()
        receipt = self.commit()["receipt"]
        metadata_path = (
            self.root
            / "zero-one/metadata"
            / f"{Path(self.metadata['filename']).stem}.json"
        )
        self.assertEqual(metadata_path.stat().st_size, receiver.MAX_INDEX_RECORD_BYTES)
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        oversized = dict(self.metadata, padding=self.metadata["padding"] + "x")
        with self.assertRaisesRegex(ValueError, "metadata exceeds its byte limit"):
            self.commit(oversized)
        self.assertEqual(
            {
                path: path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            },
            before,
        )
        self.assertEqual(self.commit()["receipt"], receipt)
        healthy = self.spool.add("z-healthy.jpg")
        self.stage(healthy)
        self.assertEqual(self.commit(healthy)["receipt"], self.latest()["receipt"])
        self.assertEqual(self.latest()["metadata"], healthy)

    def test_latest_index_rejects_oversized_receipt_below_total_index_limit(self):
        self.stage()
        self.commit()
        index = self.latest()
        index["receipt"]["extra"] = "x" * receiver.MAX_INDEX_RECORD_BYTES
        self.assertLess(
            len(receiver._json_bytes(index)), receiver.MAX_LATEST_INDEX_BYTES
        )
        with self.assertRaisesRegex(ValueError, "receipt exceeds its byte limit"):
            receiver.validate_latest_index(index, "zero-one")

    def test_latest_index_rejects_oversized_empty_record(self):
        index = {
            "protocol": 1,
            "device_id": "zero-one",
            "metadata": None,
            "receipt": None,
            "extra": "x" * receiver.MAX_LATEST_INDEX_BYTES,
        }
        with self.assertRaisesRegex(ValueError, "index exceeds its byte limit"):
            receiver.validate_latest_index(index, "zero-one")

    def test_older_out_of_order_upload_never_regresses_latest_index(self):
        self.metadata["captured_at_utc"] = "2026-09-07T13:00:00Z"
        self.stage()
        self.commit()
        previous = self.latest()
        older = self.spool.add("older.jpg")
        self.stage(older)
        with patch.object(
            receiver,
            "_scan_latest",
            side_effect=AssertionError("unexpected archive scan"),
        ):
            self.commit(older)
        self.assertEqual(self.latest(), previous)

    def test_missing_legacy_index_bootstrap_preserves_newer_existing_capture(self):
        self.metadata["captured_at_utc"] = "2026-09-07T13:00:00Z"
        self.stage()
        self.commit()
        previous = self.latest()
        (self.root / "zero-one/latest.json").unlink()
        older = self.spool.add("older.jpg")
        self.stage(older)
        self.commit(older)
        self.assertEqual(self.latest(), previous)

    def test_equal_capture_times_choose_lexically_later_filename(self):
        later = self.spool.add("z.jpg")
        earlier = self.spool.add("a.jpg")
        self.stage(later)
        self.commit(later)
        self.stage(earlier)
        self.commit(earlier)
        self.assertEqual(self.latest()["metadata"]["filename"], "z.jpg")

    def test_failed_latest_index_write_cannot_acknowledge_until_retry(self):
        self.stage()
        atomic = receiver._atomic_json

        def fail_index(path, value):
            if path.name == "latest.json":
                raise OSError("Index write failed")
            return atomic(path, value)

        with patch.object(receiver, "_atomic_json", side_effect=fail_index):
            with self.assertRaisesRegex(OSError, "Index write failed"):
                self.commit()
        device = self.root / "zero-one"
        self.assertTrue((device / "images" / self.metadata["filename"]).exists())
        self.assertEqual(len(list((device / "receipts").iterdir())), 1)
        self.assertFalse((device / "latest.json").exists())
        recovered = self.initialize(files=[self.metadata])["receipts"]
        self.assertEqual(recovered, [self.latest()["receipt"]])

    def test_index_cross_file_mismatch_prevents_acknowledgement(self):
        self.stage()
        self.commit()
        index = self.latest()
        index["metadata"]["capture_duration_seconds"] = 999
        (self.root / "zero-one/latest.json").write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError, "does not match committed"):
            self.commit()

    def test_latest_index_directory_sync_failure_requires_retry_before_ack(self):
        self.stage()
        device = self.root / "zero-one"
        sync = receiver._sync_directory

        def fail_index_sync(path):
            if path == device:
                raise OSError("Index directory sync failed")
            return sync(path)

        with patch.object(receiver, "_sync_directory", side_effect=fail_index_sync):
            with self.assertRaisesRegex(OSError, "Index directory sync failed"):
                self.commit()
        self.assertTrue((device / "latest.json").exists())
        with patch.object(receiver, "_sync_directory", wraps=sync) as synchronized:
            recovered = self.commit()["receipt"]
        self.assertEqual(recovered, self.latest()["receipt"])
        self.assertTrue(
            any(
                arguments.args == (device,) for arguments in synchronized.call_args_list
            )
        )

    def test_bootstrap_limit_failure_retains_photos_without_acknowledgement(self):
        self.stage()
        self.commit()
        (self.root / "zero-one/latest.json").unlink()
        other = self.spool.add("other.jpg")
        self.stage(other)
        with patch.object(receiver, "MAX_REINDEX_ENTRIES", 1):
            with self.assertRaisesRegex(ValueError, "exceeded max_entries"):
                self.commit(other)
        self.assertFalse((self.root / "zero-one/latest.json").exists())
        result = receiver.handle_request(
            "reindex", self.root, "zero-one", {"protocol": 1, "max_entries": 2}
        )
        self.assertEqual(result["indexed_entries"], 2)
        self.assertEqual(result["latest_filename"], "other.jpg")

    def test_reindex_rebuilds_legacy_or_corrupt_index_with_strict_request_limits(self):
        for maximum in (0, True, 1.5, float("inf"), "10", 1000001):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                receiver.handle_request(
                    "reindex",
                    self.root,
                    "zero-one",
                    {"protocol": 1, "max_entries": maximum},
                )
        self.stage()
        receipt = self.commit()["receipt"]
        (self.root / "zero-one/latest.json").write_text("corrupt")
        result = receiver.handle_request(
            "reindex", self.root, "zero-one", {"protocol": 1}
        )
        self.assertEqual(result["latest_filename"], self.metadata["filename"])
        self.assertEqual(self.latest()["receipt"], receipt)

    def test_reindex_rejects_corrupt_or_oversized_committed_records_without_replacing_index(
        self,
    ):
        self.stage()
        self.commit()
        original = self.latest()
        path = (
            self.root
            / "zero-one/receipts"
            / f"{Path(self.metadata['filename']).stem}.json"
        )
        bad = dict(original["receipt"], device_id="wrong-device")
        for content in (json.dumps(bad), " " * (receiver.MAX_INDEX_RECORD_BYTES + 1)):
            path.write_text(content)
            with self.assertRaises(ValueError):
                receiver.handle_request(
                    "reindex", self.root, "zero-one", {"protocol": 1}
                )
            self.assertEqual(self.latest(), original)

    def test_reindex_empty_archive_index_is_replaced_by_first_commit(self):
        result = receiver.handle_request(
            "reindex", self.root, "zero-one", {"protocol": 1}
        )
        self.assertIsNone(result["latest_filename"])
        self.assertIsNone(self.latest()["metadata"])
        self.stage()
        with patch.object(
            receiver,
            "_scan_latest",
            side_effect=AssertionError("unexpected archive scan"),
        ):
            self.commit()
        self.assertEqual(self.latest()["metadata"], self.metadata)

    def test_receipt_write_failure_recovers_after_image_publication(self):
        self.stage()
        original = receiver._atomic_json

        def fail_receipt(path, value):
            if path.parent.name == "receipts":
                raise OSError("Simulated receiver receipt failure")
            return original(path, value)

        with patch.object(receiver, "_atomic_json", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.commit()
        self.assertTrue(
            (self.root / "zero-one/images" / self.metadata["filename"]).exists()
        )
        self.assertEqual(list((self.root / "zero-one/receipts").iterdir()), [])
        self.assertEqual(len(self.initialize(files=[self.metadata])["receipts"]), 1)

    def test_sync_failure_cannot_emit_receipt(self):
        self.stage()
        with patch.object(
            receiver.os, "fsync", side_effect=OSError("Simulated fsync failure")
        ):
            with self.assertRaises(OSError):
                self.commit()
        self.assertEqual(list((self.root / "zero-one/receipts").iterdir()), [])

    def test_retry_syncs_image_directory_after_interrupted_publication(self):
        self.stage()
        original_sync = receiver._sync_directory

        def fail_images_directory(path):
            if path.name == "images":
                raise OSError("Simulated image directory sync failure")
            return original_sync(path)

        with patch.object(
            receiver, "_sync_directory", side_effect=fail_images_directory
        ):
            with self.assertRaises(OSError):
                self.commit()
        self.assertTrue(
            (self.root / "zero-one/images" / self.metadata["filename"]).exists()
        )
        self.assertEqual(list((self.root / "zero-one/receipts").iterdir()), [])
        events = []
        original_atomic = receiver._atomic_json

        def track_sync(path):
            events.append(("sync", path.name))
            return original_sync(path)

        def track_atomic(path, value):
            events.append(("write", path.parent.name))
            return original_atomic(path, value)

        with (
            patch.object(receiver, "_sync_directory", side_effect=track_sync),
            patch.object(receiver, "_atomic_json", side_effect=track_atomic),
        ):
            self.commit()
        self.assertLess(
            events.index(("sync", "images")), events.index(("write", "receipts"))
        )

    def test_traversal_and_shell_like_names_are_rejected(self):
        for filename in (
            "../outside.jpg",
            "/tmp/outside.jpg",
            "x/../../outside.jpg",
            "$(touch-owned).jpg",
            "a\n.jpg",
            ".hidden.jpg",
        ):
            with self.subTest(filename=filename):
                metadata = dict(self.metadata, filename=filename)
                with self.assertRaises(ValueError):
                    self.commit(metadata)
        for device in ("../outside", "/outside", "zero;touch", "zero/one"):
            with self.subTest(device=device):
                with self.assertRaises(ValueError):
                    self.initialize(device)

    def test_symlinked_images_and_device_directories_are_rejected(self):
        self.initialize()
        outside = self.base / "outside.jpg"
        outside.write_bytes(
            self.spool.image_path(self.metadata["filename"]).read_bytes()
        )
        (self.root / "zero-one/incoming" / self.metadata["filename"]).symlink_to(
            outside
        )
        with self.assertRaises(OSError):
            self.commit()
        (self.root / "other-zero").symlink_to(
            self.root / "zero-one", target_is_directory=True
        )
        with self.assertRaises(ValueError):
            self.initialize("other-zero")
        self.assertTrue(outside.exists())

    def test_devices_with_same_filename_cannot_overwrite_each_other(self):
        self.stage(device="zero-one")
        self.commit(device="zero-one")
        second = dict(self.metadata)
        second_content = b"z" * second["size_bytes"]
        second["sha256"] = hashlib.sha256(second_content).hexdigest()
        self.initialize("zero-two")
        (self.root / "zero-two/incoming" / second["filename"]).write_bytes(
            second_content
        )
        self.commit(second, device="zero-two")
        self.assertNotEqual(
            (self.root / "zero-one/images" / second["filename"]).read_bytes(),
            second_content,
        )
        self.assertEqual(
            (self.root / "zero-two/images" / second["filename"]).read_bytes(),
            second_content,
        )

    def test_existing_filename_content_and_metadata_conflicts_fail_closed(self):
        self.stage()
        self.commit()
        for changed in (
            dict(self.metadata, sha256="0" * 64),
            dict(self.metadata, time_source="NTP"),
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    self.commit(changed)

    def test_manifest_limit_is_enforced_before_creating_destination(self):
        command = [
            sys.executable,
            str(Path(receiver.__file__)),
            "init",
            "--root",
            str(self.root),
            "--device",
            "zero-one",
        ]
        completed = subprocess.run(
            command,
            input=b" " * (receiver.MAX_MANIFEST_BYTES + 1),
            capture_output=True,
            timeout=5,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"byte limit", completed.stderr)
        self.assertFalse(self.root.exists())


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.spool = FakeSpool(self.base / "spool")
        self.metadata = self.spool.add()
        identity = self.base / "identity"
        identity.write_text("synthetic-test-identity")
        hosts = self.base / "known_hosts"
        hosts.write_text("synthetic-test-host")
        self.config = {
            "host": "images.example.test",
            "user": "camera",
            "remote_root": str(self.base / "server"),
            "device_id": "zero-one",
            "identity_file": str(identity),
            "known_hosts_file": str(hosts),
            "receiver_path": "/opt/timelapse/receiver.py",
            "port": 2222,
        }
        self.commands = []

    def local_process(self, arguments, input_data, timeout, eligible):
        self.commands.append(arguments)
        self.assertGreater(timeout, 0)
        self.assertTrue(eligible())
        if arguments[0] == "ssh":
            request = json.loads(input_data)
            action = shlex.split(arguments[-1])[2]
            result = receiver.handle_request(
                action, self.config["remote_root"], self.config["device_id"], request
            )
            return json.dumps(result).encode()
        self.assertEqual(arguments[0], "rsync")
        for filename in input_data.decode("ascii").rstrip("\0").split("\0"):
            source = Path(arguments[-2]) / filename
            destination = (
                Path(self.config["remote_root"])
                / self.config["device_id"]
                / "incoming"
                / source.name
            )
            shutil.copyfile(source, destination)
        return b""

    def run_transfer(self, **kwargs):
        with patch.object(uploader, "_run_process", side_effect=self.local_process):
            return uploader.transfer(self.spool, self.config, lambda: True, **kwargs)

    def test_unset_destination_never_executes_network_or_requires_credentials(self):
        with patch.object(uploader, "_run_process") as execute:
            for config in (
                {},
                {"host": ""},
                {"enabled": False, "host": "invalid host"},
            ):
                self.assertEqual(
                    uploader.transfer(self.spool, config, lambda: True)["status"],
                    "disabled",
                )
            execute.assert_not_called()

    def test_success_preserves_local_image_and_protects_ssh_and_rsync(self):
        result = self.run_transfer()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["uploaded_files"], 1)
        self.assertTrue(self.spool.image_path(self.metadata["filename"]).exists())
        self.assertEqual(
            self.spool.receipts[self.metadata["filename"]]["sha256"],
            self.metadata["sha256"],
        )
        ssh = self.commands[0]
        self.assertIn("StrictHostKeyChecking=yes", ssh)
        self.assertIn("BatchMode=yes", ssh)
        self.assertIn("IdentitiesOnly=yes", ssh)
        self.assertIn("UpdateHostKeys=no", ssh)
        self.assertEqual(ssh[ssh.index("-p") + 1], "2222")
        rsync = next(command for command in self.commands if command[0] == "rsync")
        self.assertIn("--partial-dir=.rsync-partial", rsync)
        self.assertIn("--checksum", rsync)
        self.assertIn("--protect-args", rsync)
        self.assertIn("--from0", rsync)
        self.assertIn("--files-from=-", rsync)
        self.assertNotIn("--compress", rsync)
        self.assertNotIn("--delete", rsync)
        self.assertNotIn("--remove-source-files", rsync)

    def test_many_images_use_one_rsync_and_two_receiver_connections(self):
        self.spool.add("20260907T120001-second.jpg", b"second")
        self.spool.add("20260907T120002-third.jpg", b"third")
        result = self.run_transfer()
        self.assertEqual(result["uploaded_files"], 3)
        self.assertEqual(
            [command[0] for command in self.commands], ["ssh", "rsync", "ssh"]
        )
        self.assertEqual(shlex.split(self.commands[-1][-1])[2], "commit-batch")

    def test_batch_verification_failure_recovers_successful_prefix(self):
        second = self.spool.add("20260907T120001-second.jpg", b"second")
        original = self.local_process

        def corrupt_second(arguments, input_data, timeout, eligible):
            result = original(arguments, input_data, timeout, eligible)
            if arguments[0] == "rsync":
                destination = (
                    Path(self.config["remote_root"])
                    / "zero-one/incoming"
                    / second["filename"]
                )
                destination.write_bytes(b"x" * second["size_bytes"])
            return result

        with patch.object(uploader, "_run_process", side_effect=corrupt_second):
            result = uploader.transfer(self.spool, self.config, lambda: True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.spool.receipts, {})
        first_receipt = (
            Path(self.config["remote_root"])
            / "zero-one/receipts"
            / (Path(self.metadata["filename"]).stem + ".json")
        )
        self.assertTrue(first_receipt.exists())
        second_result = self.run_transfer()
        self.assertEqual(second_result["status"], "complete")
        self.assertEqual(second_result["recovered_receipts"], 1)
        self.assertEqual(second_result["uploaded_files"], 1)

    def test_spool_lock_timeout_is_deferred_without_network(self):
        with patch.object(
            self.spool, "list_pending", side_effect=TimeoutError("Busy spool")
        ) as pending:
            result = self.run_transfer(max_seconds=15)
        self.assertEqual(result["reason"], "time_budget")
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(self.commands, [])
        self.assertLessEqual(pending.call_args.kwargs["timeout_seconds"], 15)

    def test_receipt_lock_timeout_retains_pending_and_recovers(self):
        with patch.object(
            self.spool, "record_receipt", side_effect=TimeoutError("Busy spool")
        ) as receipt:
            result = self.run_transfer(max_seconds=15)
        self.assertEqual(result["reason"], "time_budget")
        self.assertEqual(self.spool.receipts, {})
        self.assertLessEqual(receipt.call_args.kwargs["timeout_seconds"], 15)
        self.assertEqual(self.run_transfer()["recovered_receipts"], 1)

    @unittest.skipUnless(shutil.which("rsync"), "rsync is unavailable")
    def test_real_rsync_uses_partial_data_and_reconstructs_exact_batch_image(self):
        source = self.base / "resume-source"
        destination = self.base / "resume-destination"
        source.mkdir()
        destination.mkdir()
        partial = destination / ".rsync-partial"
        partial.mkdir()
        payload = os.urandom(1024 * 1024)
        (source / "resume.jpg").write_bytes(payload)
        (partial / "resume.jpg").write_bytes(payload[: 512 * 1024])
        arguments = [
            "rsync",
            "--partial",
            "--partial-dir=.rsync-partial",
            "--checksum",
            "--no-whole-file",
            "--stats",
            "--from0",
            "--files-from=-",
            str(source) + "/",
            str(destination) + "/",
        ]
        completed = subprocess.run(
            arguments, input=b"resume.jpg\0", capture_output=True, timeout=10
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual((destination / "resume.jpg").read_bytes(), payload)
        matched = next(
            line
            for line in completed.stdout.decode().splitlines()
            if line.startswith("Matched data:")
        )
        matched_bytes = int(
            matched.split(":", 1)[1].strip().split()[0].replace(",", "")
        )
        self.assertGreater(matched_bytes, 500000)

    def test_local_receipt_failure_retries_without_retransmitting_image(self):
        self.spool.fail_receipt = True
        first = self.run_transfer()
        self.assertEqual(first["status"], "error")
        self.assertEqual(len(self.spool.list_pending()), 1)
        self.spool.fail_receipt = False
        self.commands.clear()
        second = self.run_transfer()
        self.assertEqual(second["status"], "complete")
        self.assertEqual(second["recovered_receipts"], 1)
        self.assertEqual(second["uploaded_files"], 0)
        self.assertFalse(any(command[0] == "rsync" for command in self.commands))

    def test_byte_budget_skips_large_image_and_selects_smaller_pending_image(self):
        small = self.spool.add("20260907T120001-small.jpg", b"tiny")
        result = self.run_transfer(max_bytes=small["size_bytes"])
        self.assertEqual(result["selected_bytes"], small["size_bytes"])
        self.assertEqual(result["skipped_oversized_files"], 1)
        self.assertIn(small["filename"], self.spool.receipts)
        self.assertNotIn(self.metadata["filename"], self.spool.receipts)

    def test_budget_smaller_than_all_images_never_connects(self):
        result = self.run_transfer(max_bytes=1)
        self.assertEqual(result["reason"], "byte_budget")
        self.assertEqual(self.commands, [])

    def test_receiver_initialization_consumes_the_overall_time_budget(self):
        original = self.local_process

        def slow_init(arguments, input_data, timeout, eligible):
            response = original(arguments, input_data, timeout, eligible)
            time.sleep(0.03)
            return response

        with patch.object(uploader, "_run_process", side_effect=slow_init):
            result = uploader.transfer(
                self.spool, self.config, lambda: True, max_seconds=0.02
            )
        self.assertEqual(result["reason"], "time_budget")
        self.assertEqual([command[0] for command in self.commands], ["ssh"])
        self.assertEqual(self.spool.receipts, {})

    def test_failed_remote_hash_verification_keeps_local_pending(self):
        original = self.local_process

        def corrupt(arguments, input_data, timeout, eligible):
            output = original(arguments, input_data, timeout, eligible)
            if arguments[0] == "rsync":
                destination = (
                    Path(self.config["remote_root"])
                    / "zero-one/incoming"
                    / self.metadata["filename"]
                )
                destination.write_bytes(b"x" * self.metadata["size_bytes"])
            return output

        with patch.object(uploader, "_run_process", side_effect=corrupt):
            result = uploader.transfer(self.spool, self.config, lambda: True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(self.spool.list_pending()), 1)
        self.assertTrue(self.spool.image_path(self.metadata["filename"]).exists())

    def test_power_loss_between_upload_and_commit_leaves_image_pending(self):
        permitted = True
        original = self.local_process

        def lose_power(arguments, input_data, timeout, eligible):
            nonlocal permitted
            if not eligible():
                raise uploader.TransferStopped("power_ineligible")
            output = original(arguments, input_data, timeout, eligible)
            if arguments[0] == "rsync":
                permitted = False
            return output

        with patch.object(uploader, "_run_process", side_effect=lose_power):
            result = uploader.transfer(self.spool, self.config, lambda: permitted)
        self.assertEqual(result["reason"], "power_ineligible")
        self.assertEqual(self.spool.receipts, {})
        self.assertEqual(self.run_transfer()["uploaded_files"], 1)

    def test_wrong_server_receipt_never_acknowledges_local_image(self):
        original = self.local_process

        def wrong_receipt(arguments, input_data, timeout, eligible):
            output = original(arguments, input_data, timeout, eligible)
            if (
                arguments[0] == "ssh"
                and shlex.split(arguments[-1])[2] == "commit-batch"
            ):
                value = json.loads(output)
                value["receipts"][0]["sha256"] = "0" * 64
                return json.dumps(value).encode()
            return output

        with patch.object(uploader, "_run_process", side_effect=wrong_receipt):
            result = uploader.transfer(self.spool, self.config, lambda: True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.spool.receipts, {})

    def test_shell_metacharacters_are_rejected_before_process_execution(self):
        with patch.object(uploader, "_run_process") as execute:
            for name, value in (
                ("host", "server; touch bad"),
                ("user", "-evil"),
                ("remote_root", "/srv/$(touch-bad)"),
                ("receiver_path", "/srv/a;bad"),
                ("device_id", "../../bad"),
            ):
                with self.subTest(name=name):
                    config = dict(self.config, **{name: value})
                    with self.assertRaises(ValueError):
                        uploader.transfer(self.spool, config, lambda: True)
            execute.assert_not_called()

    def test_real_subprocess_stops_when_eligibility_drops(self):
        started = time.monotonic()
        with self.assertRaisesRegex(uploader.TransferStopped, "power_ineligible"):
            uploader._run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                None,
                10,
                lambda: time.monotonic() - started < 0.2,
            )
        self.assertLess(time.monotonic() - started, 3)

    def test_real_subprocess_deadline_and_output_limit_are_bounded(self):
        started = time.monotonic()
        with self.assertRaisesRegex(uploader.TransferStopped, "time_budget"):
            uploader._run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                None,
                0.2,
                lambda: True,
            )
        self.assertLess(time.monotonic() - started, 3)
        with self.assertRaisesRegex(RuntimeError, "output exceeds"):
            uploader._run_process(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * 2000000)"],
                None,
                5,
                lambda: True,
            )

    def test_real_subprocess_bounded_stdin_round_trip(self):
        payload = b"payload" * 20000
        output = uploader._run_process(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            payload,
            5,
            lambda: True,
        )
        self.assertEqual(output, payload)


if __name__ == "__main__":
    unittest.main()
