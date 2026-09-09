"""Build and inspect bounded application bundles without local configuration."""

import argparse
import gzip
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

INSTALLER_MODULE = importlib.import_module(
    f"{__package__}.install" if __package__ else "install"
)
PACKAGE_FILES = INSTALLER_MODULE.PACKAGE_FILES
UNITS = INSTALLER_MODULE.UNITS


BUNDLE_PATHS = tuple(
    sorted(
        [f"timelapse/{name}" for name in PACKAGE_FILES]
        + [f"deploy/{name}" for name in UNITS]
        + ["deploy/install.py"]
    )
)
MANIFEST_PATH = "manifest.json"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_TAR_BYTES = MAX_PAYLOAD_BYTES + 1024 * 1024
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}\Z")


class BundleError(ValueError):
    pass


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_regular(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode):
            raise BundleError("Bundle inputs must be regular files")
        if details.st_size > limit:
            raise BundleError("Bundle input exceeds size limit")
        content = stream.read(limit + 1)
        if len(content) > limit:
            raise BundleError("Bundle input exceeds size limit")
        return content


def source_commit(source_root):
    try:
        root = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        if Path(root).resolve() != source_root:
            return None
        tracked = (
            subprocess.run(
                ["git", "-C", str(source_root), "ls-files", "-z", "--", *BUNDLE_PATHS],
                check=True,
                capture_output=True,
                timeout=10,
            )
            .stdout.decode()
            .rstrip("\0")
            .split("\0")
        )
        if sorted(tracked) != list(BUNDLE_PATHS):
            return None
        subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "diff",
                "--quiet",
                "HEAD",
                "--",
                *BUNDLE_PATHS,
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        commit = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    return commit if re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit) else None


def build_bundle(source_root, output):
    """Write a deterministic archive of explicitly managed application files."""
    source_root = Path(source_root).resolve()
    output = Path(output)
    if output.resolve() in {source_root / name for name in BUNDLE_PATHS}:
        raise BundleError("Artifact must not overwrite application sources")
    contents = {}
    for name in BUNDLE_PATHS:
        path = source_root / name
        if any(
            parent.is_symlink()
            for parent in path.parents
            if parent != source_root and source_root in parent.parents
        ):
            raise BundleError("Bundle source directories must not be symlinks")
        contents[name] = read_regular(path, MAX_FILE_BYTES)
        if sum(map(len, contents.values())) > MAX_PAYLOAD_BYTES:
            raise BundleError("Bundle payload exceeds size limit")
    files = [
        {"path": name, "size": len(content), "sha256": digest(content)}
        for name, content in contents.items()
    ]
    manifest = {
        "schema_version": 1,
        "files": files,
        "release_id": digest(canonical_json(files)),
    }
    commit = source_commit(source_root)
    if commit:
        manifest["source_commit"] = commit
    contents[MANIFEST_PATH] = canonical_json(manifest) + b"\n"
    buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="", fileobj=buffer, mode="wb", mtime=0, compresslevel=9
    ) as compressed:
        with tarfile.open(
            fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT
        ) as archive:
            for name, content in sorted(contents.items()):
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(content))
    content = buffer.getvalue()
    if len(content) > MAX_ARCHIVE_BYTES:
        raise BundleError("Compressed bundle exceeds size limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pi-bundle-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {
        "artifact": str(output),
        "sha256": digest(content),
        "release_id": manifest["release_id"],
    }


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) not in (
        {"schema_version", "release_id", "files"},
        {"schema_version", "release_id", "files", "source_commit"},
    ):
        raise BundleError("Invalid bundle manifest fields")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise BundleError("Unsupported bundle manifest version")
    if "source_commit" in manifest and (
        not isinstance(manifest["source_commit"], str)
        or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", manifest["source_commit"])
    ):
        raise BundleError("Invalid source commit")
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != len(BUNDLE_PATHS):
        raise BundleError("Manifest must contain the complete application allowlist")
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise BundleError("Invalid bundle file entry")
        if not isinstance(entry["path"], str) or entry["path"] not in BUNDLE_PATHS:
            raise BundleError("Manifest contains an unmanaged path")
        if type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_FILE_BYTES:
            raise BundleError("Invalid bundle file size")
        if not isinstance(entry["sha256"], str) or not SHA256_PATTERN.fullmatch(
            entry["sha256"]
        ):
            raise BundleError("Invalid bundle file checksum")
    if [entry["path"] for entry in files] != list(BUNDLE_PATHS):
        raise BundleError("Manifest paths must be unique and sorted")
    if sum(entry["size"] for entry in files) > MAX_PAYLOAD_BYTES:
        raise BundleError("Bundle payload exceeds size limit")
    if manifest["release_id"] != digest(canonical_json(files)):
        raise BundleError("Bundle release identifier does not match content")
    return manifest


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("Duplicate manifest field")
        result[key] = value
    return result


def read_bundle(artifact, expected_sha256=None):
    content = read_regular(Path(artifact), MAX_ARCHIVE_BYTES)
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or not SHA256_PATTERN.fullmatch(expected_sha256)
        or digest(content) != expected_sha256
    ):
        raise BundleError("Artifact checksum does not match the trusted checksum")
    contents = {}
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content), mode="rb") as compressed:
            raw = compressed.read(MAX_TAR_BYTES + 1)
        if len(raw) > MAX_TAR_BYTES:
            raise BundleError("Expanded archive exceeds size limit")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive:
                if member.name not in {*BUNDLE_PATHS, MANIFEST_PATH}:
                    raise BundleError("Archive contains an unmanaged path")
                if member.name in contents:
                    raise BundleError("Archive contains duplicate members")
                if (
                    not member.isfile()
                    or member.pax_headers
                    or member.sparse is not None
                ):
                    raise BundleError("Archive members must be regular files")
                if not 0 <= member.size <= MAX_FILE_BYTES:
                    raise BundleError("Archive member exceeds size limit")
                stream = archive.extractfile(member)
                if stream is None:
                    raise BundleError("Missing archive member contents")
                with stream:
                    payload = stream.read(MAX_FILE_BYTES + 1)
                if len(payload) != member.size:
                    raise BundleError("Archive member size mismatch")
                contents[member.name] = payload
    except (EOFError, OSError, tarfile.TarError) as error:
        raise BundleError("Invalid or truncated compressed archive") from error
    if set(contents) != {*BUNDLE_PATHS, MANIFEST_PATH}:
        raise BundleError("Archive must contain the complete application allowlist")
    try:
        manifest = validate_manifest(
            json.loads(contents.pop(MANIFEST_PATH), object_pairs_hook=unique_object)
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BundleError("Invalid manifest JSON") from error
    for entry in manifest["files"]:
        payload = contents[entry["path"]]
        if len(payload) != entry["size"] or digest(payload) != entry["sha256"]:
            raise BundleError("Archive file does not match its manifest checksum")
    return manifest, contents


def verify_bundle(artifact, expected_sha256=None):
    """Verify content; authenticity requires a checksum from a trusted source."""
    return read_bundle(artifact, expected_sha256)[0]


def extract_bundle(artifact, destination, expected_sha256=None):
    """Verify completely before writing into a new private destination directory."""
    manifest, contents = read_bundle(artifact, expected_sha256)
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    try:
        for name, content in contents.items():
            target = destination / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content)
            target.chmod(0o644)
        (destination / MANIFEST_PATH).write_bytes(canonical_json(manifest) + b"\n")
        (destination / MANIFEST_PATH).chmod(0o644)
    except Exception:
        shutil.rmtree(destination)
        raise
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = build_bundle(arguments.source_root, arguments.output)
        verify_bundle(arguments.output, result["sha256"])
    except (BundleError, OSError) as error:
        print(f"Bundle failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
