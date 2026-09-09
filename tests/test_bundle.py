import copy
import gzip
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from deploy import bundle


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        for name in bundle.BUNDLE_PATHS:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"public contents of {name}\n")
        self.artifact = self.root / "application.tar.gz"

    def build(self):
        return bundle.build_bundle(self.source, self.artifact)

    def members(self):
        with tarfile.open(self.artifact, "r:gz") as archive:
            return [
                (copy.copy(member), archive.extractfile(member).read())
                for member in archive
            ]

    def rewrite(self, members):
        with tarfile.open(self.artifact, "w:gz") as archive:
            for member, content in members:
                if member.isfile():
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                else:
                    member.size = 0
                    archive.addfile(member)

    def test_bundle_round_trip_contains_only_explicit_application_files(self):
        private = "not-for-publication-" + "x" * 24
        for name in (
            "local/setup.json",
            ".env",
            ".git/config",
            "photos/frame.jpg",
            "hardware/config.json",
            "deploy/home-assistant/config.json",
            "timelapse/private.json",
            "__init__.py",
            "untracked.py",
        ):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(private)
        result = self.build()
        manifest = bundle.verify_bundle(self.artifact, result["sha256"])
        destination = self.root / "extracted"
        self.assertEqual(
            bundle.extract_bundle(self.artifact, destination, result["sha256"]),
            manifest,
        )
        self.assertEqual(manifest["release_id"], result["release_id"])
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        self.assertNotIn(str(self.source), json.dumps(manifest))
        self.assertNotIn(private.encode(), gzip.decompress(self.artifact.read_bytes()))
        self.assertEqual(
            {
                str(path.relative_to(destination))
                for path in destination.rglob("*")
                if path.is_file()
            },
            {*bundle.BUNDLE_PATHS, bundle.MANIFEST_PATH},
        )
        for name in bundle.BUNDLE_PATHS:
            self.assertEqual(
                (destination / name).read_bytes(), (self.source / name).read_bytes()
            )
            self.assertEqual(stat.S_IMODE((destination / name).stat().st_mode), 0o644)

    def test_build_is_deterministic_across_paths_modes_and_timestamps(self):
        first = self.build()
        original = self.artifact.read_bytes()
        for name in bundle.BUNDLE_PATHS:
            path = self.source / name
            path.chmod(0o700)
            os.utime(path, (10, 10))
        second_path = self.root / "different-name.tar.gz"
        second = bundle.build_bundle(self.source, second_path)
        self.assertEqual(original, second_path.read_bytes())
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["release_id"], second["release_id"])
        self.assertTrue(
            all(
                member.mtime == 0 and member.uid == member.gid == 0
                for member, _ in self.members()
            )
        )

    def test_content_change_changes_release_identifier(self):
        original = self.build()
        (self.source / bundle.BUNDLE_PATHS[0]).write_text("updated\n")
        changed = self.build()
        self.assertNotEqual(original["release_id"], changed["release_id"])

    def test_source_commit_is_optional_and_does_not_define_release_identifier(self):
        with patch.object(bundle, "source_commit", return_value="a" * 40):
            original = self.build()
        manifest = bundle.verify_bundle(self.artifact)
        self.assertEqual(manifest["source_commit"], "a" * 40)
        with patch.object(bundle, "source_commit", return_value="b" * 40):
            changed = self.build()
        self.assertEqual(original["release_id"], changed["release_id"])
        self.assertNotEqual(original["sha256"], changed["sha256"])

    def test_source_file_and_directory_symlinks_are_rejected(self):
        path = self.source / bundle.BUNDLE_PATHS[0]
        outside = self.root / "outside"
        outside.write_text("private content")
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises((bundle.BundleError, OSError)):
            self.build()
        path.unlink()
        path.write_text("restored")
        real = self.source / "deploy"
        moved = self.root / "moved"
        real.rename(moved)
        real.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(bundle.BundleError, "symlinks"):
            self.build()
        self.assertFalse(self.artifact.exists())

    def test_source_cannot_be_overwritten_by_bundle_output(self):
        path = self.source / bundle.BUNDLE_PATHS[0]
        original = path.read_bytes()
        with self.assertRaisesRegex(bundle.BundleError, "overwrite"):
            bundle.build_bundle(self.source, path)
        self.assertEqual(path.read_bytes(), original)

    def test_source_size_limit_is_enforced_before_writing_artifact(self):
        with patch.object(bundle, "MAX_FILE_BYTES", 8):
            with self.assertRaisesRegex(bundle.BundleError, "size limit"):
                self.build()
        self.assertFalse(self.artifact.exists())

    def test_wrong_trusted_archive_checksum_writes_nothing(self):
        self.build()
        destination = self.root / "extracted"
        with self.assertRaisesRegex(bundle.BundleError, "trusted checksum"):
            bundle.extract_bundle(self.artifact, destination, "0" * 64)
        self.assertFalse(destination.exists())

    def test_changed_file_without_updated_manifest_is_rejected(self):
        self.build()
        members = self.members()
        member, content = members[0]
        members[0] = (member, b"changed" + content)
        self.rewrite(members)
        with self.assertRaisesRegex(bundle.BundleError, "manifest checksum"):
            bundle.verify_bundle(self.artifact)

    def test_traversal_absolute_unknown_and_duplicate_members_are_rejected(self):
        self.build()
        original = self.members()
        for name in (
            "../escaped",
            "/tmp/escaped",
            "deploy/../../escaped",
            "timelapse/private.json",
            original[0][0].name,
        ):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                self.rewrite([*original, (member, b"untrusted")])
                destination = self.root / "extracted"
                with self.assertRaises(bundle.BundleError):
                    bundle.extract_bundle(self.artifact, destination)
                self.assertFalse(destination.exists())
        self.assertFalse((self.root / "escaped").exists())

    def test_symbolic_hard_links_devices_and_directories_are_rejected(self):
        self.build()
        original = self.members()
        for kind in (
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
            tarfile.CHRTYPE,
            tarfile.BLKTYPE,
            tarfile.FIFOTYPE,
            tarfile.DIRTYPE,
        ):
            with self.subTest(kind=kind):
                member = copy.copy(original[0][0])
                member.type = kind
                member.linkname = "../outside"
                self.rewrite([(member, b""), *original[1:]])
                with self.assertRaisesRegex(bundle.BundleError, "regular files"):
                    bundle.verify_bundle(self.artifact)

    def test_manifest_missing_paths_duplicate_paths_bad_versions_and_release_id_fail(
        self,
    ):
        self.build()
        original = self.members()
        index = next(
            i
            for i, (member, _) in enumerate(original)
            if member.name == bundle.MANIFEST_PATH
        )
        manifest = json.loads(original[index][1])
        for variant in (
            "missing",
            "duplicate",
            "version",
            "release",
            "extra",
            "size",
            "checksum",
        ):
            with self.subTest(variant=variant):
                changed = copy.deepcopy(manifest)
                if variant == "missing":
                    changed["files"].pop()
                elif variant == "duplicate":
                    changed["files"][1] = changed["files"][0]
                elif variant == "version":
                    changed["schema_version"] = True
                elif variant == "release":
                    changed["release_id"] = "0" * 64
                elif variant == "extra":
                    changed["private"] = "unexpected"
                elif variant == "size":
                    changed["files"][0]["size"] = -1
                else:
                    changed["files"][0]["sha256"] = "not a checksum"
                members = list(original)
                members[index] = (
                    copy.copy(original[index][0]),
                    json.dumps(changed).encode(),
                )
                self.rewrite(members)
                with self.assertRaises(bundle.BundleError):
                    bundle.verify_bundle(self.artifact)

    def test_duplicate_manifest_json_fields_are_rejected(self):
        self.build()
        members = self.members()
        for index, (member, content) in enumerate(members):
            if member.name == bundle.MANIFEST_PATH:
                members[index] = (member, b'{"schema_version":1,' + content[1:])
        self.rewrite(members)
        with self.assertRaisesRegex(bundle.BundleError, "Duplicate manifest field"):
            bundle.verify_bundle(self.artifact)

    def test_truncated_and_oversized_compressed_input_are_rejected(self):
        self.build()
        original = self.artifact.read_bytes()
        self.artifact.write_bytes(original[:-12])
        with self.assertRaisesRegex(bundle.BundleError, "truncated"):
            bundle.verify_bundle(self.artifact)
        self.artifact.write_bytes(original)
        with patch.object(bundle, "MAX_ARCHIVE_BYTES", len(original) - 1):
            with self.assertRaisesRegex(bundle.BundleError, "size limit"):
                bundle.verify_bundle(self.artifact)

    def test_compression_bomb_is_bounded(self):
        self.artifact.write_bytes(gzip.compress(b"0" * 4096))
        with patch.object(bundle, "MAX_TAR_BYTES", 1024):
            with self.assertRaisesRegex(bundle.BundleError, "Expanded archive exceeds"):
                bundle.verify_bundle(self.artifact)

    def test_existing_extraction_directory_and_symlink_are_never_overwritten(self):
        self.build()
        destination = self.root / "existing"
        destination.mkdir()
        saved = destination / "keep"
        saved.write_text("keep")
        with self.assertRaises(FileExistsError):
            bundle.extract_bundle(self.artifact, destination)
        link = self.root / "link"
        link.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            bundle.extract_bundle(self.artifact, link)
        self.assertEqual(saved.read_text(), "keep")
        self.assertEqual(list(destination.iterdir()), [saved])


if __name__ == "__main__":
    unittest.main()
