"""Receive immutable timelapse images through a configured SSH account."""

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


PROTOCOL_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILES = 512
MAX_IMAGE_BYTES = 64 * 1024 * 1024
DEVICE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\.jpg\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def validate_metadata(metadata):
    """Validate one image manifest without interpreting its image contents."""
    if not isinstance(metadata, dict):
        raise ValueError("Image metadata must be an object")
    filename = metadata.get("filename")
    if not isinstance(filename, str) or not FILENAME_PATTERN.fullmatch(filename):
        raise ValueError("Invalid image filename")
    size = metadata.get("size_bytes")
    if type(size) is not int or not 1 <= size <= MAX_IMAGE_BYTES:
        raise ValueError("Invalid image size")
    digest = metadata.get("sha256")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        raise ValueError("Invalid image SHA256")
    captured = metadata.get("captured_at_utc")
    if not isinstance(captured, str) or len(captured) > 64:
        raise ValueError("Invalid capture timestamp")
    try:
        capture_time = datetime.datetime.fromisoformat(captured.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid capture timestamp") from exc
    if capture_time.tzinfo is None or capture_time.utcoffset() != datetime.timedelta(0):
        raise ValueError("Capture timestamp must include UTC timezone")
    if metadata.get("time_source") not in {"NTP", "RTC", "UNSYNC"}:
        raise ValueError("Invalid capture time source")
    boot_id = metadata.get("boot_id")
    if not isinstance(boot_id, str) or not 1 <= len(boot_id) <= 128:
        raise ValueError("Invalid boot identifier")
    duration = metadata.get("capture_duration_seconds")
    if (
        type(duration) not in (int, float)
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise ValueError("Invalid capture duration")
    return metadata


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _directory(path):
    try:
        path.mkdir(mode=0o700)
        _sync_directory(path.parent)
    except FileExistsError:
        pass
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("Receiver directory must not be a symlink or file")


def _layout(root, device_id):
    if not isinstance(device_id, str) or not DEVICE_PATTERN.fullmatch(device_id):
        raise ValueError("Invalid device identifier")
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("Receiver root must be an absolute path without traversal")
    _directory(root)
    device = root / device_id
    _directory(device)
    paths = {
        name: device / name for name in ("incoming", "images", "metadata", "receipts")
    }
    for path in paths.values():
        _directory(path)
    paths["device"] = device
    return paths


@contextlib.contextmanager
def _device_lock(paths):
    descriptor = os.open(
        paths["device"] / ".receiver.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Invalid receiver lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _json_bytes(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Receiver record must be a regular file")
        content = stream.read(MAX_MANIFEST_BYTES + 1)
        os.fsync(stream.fileno())
    if len(content) > MAX_MANIFEST_BYTES:
        raise ValueError("Receiver record is too large")
    return json.loads(content)


def _verify_image(path, metadata):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        initial_stat = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(initial_stat.st_mode)
            or initial_stat.st_size != metadata["size_bytes"]
        ):
            raise ValueError("Image size or file type does not match manifest")
        digest = hashlib.sha256()
        while data := stream.read(1024 * 1024):
            digest.update(data)
        final_stat = os.fstat(stream.fileno())
        path_stat = path.lstat()
        if (
            initial_stat.st_dev,
            initial_stat.st_ino,
            initial_stat.st_size,
            initial_stat.st_mtime_ns,
        ) != (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
        ) or (path_stat.st_dev, path_stat.st_ino) != (
            final_stat.st_dev,
            final_stat.st_ino,
        ):
            raise ValueError("Image changed during verification")
        if digest.hexdigest() != metadata["sha256"]:
            raise ValueError("Image SHA256 does not match manifest")
        os.fsync(stream.fileno())


def _receipt_matches(receipt, metadata, device_id):
    return (
        isinstance(receipt, dict)
        and receipt.get("protocol") == PROTOCOL_VERSION
        and receipt.get("device_id") == device_id
        and all(
            receipt.get(key) == metadata[key]
            for key in ("filename", "size_bytes", "sha256")
        )
    )


def _commit(paths, metadata, device_id):
    filename = metadata["filename"]
    image = paths["images"] / filename
    incoming = paths["incoming"] / filename
    try:
        _verify_image(image, metadata)
    except FileNotFoundError:
        _verify_image(incoming, metadata)
        os.link(incoming, image, follow_symlinks=False)
    _sync_directory(paths["images"])
    metadata_path = paths["metadata"] / (Path(filename).stem + ".json")
    try:
        existing_metadata = _read_json(metadata_path)
    except FileNotFoundError:
        _atomic_json(metadata_path, metadata)
    else:
        if existing_metadata != metadata:
            raise ValueError("Existing image metadata conflicts with manifest")
        _sync_directory(paths["metadata"])
    receipt_path = paths["receipts"] / (Path(filename).stem + ".json")
    try:
        receipt = _read_json(receipt_path)
    except FileNotFoundError:
        receipt = {
            "protocol": PROTOCOL_VERSION,
            "device_id": device_id,
            "filename": filename,
            "size_bytes": metadata["size_bytes"],
            "sha256": metadata["sha256"],
            "stored_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "image_relative_path": f"{device_id}/images/{filename}",
        }
        _atomic_json(receipt_path, receipt)
    else:
        if not _receipt_matches(receipt, metadata, device_id):
            raise ValueError("Existing image receipt conflicts with manifest")
        _sync_directory(paths["images"])
        _sync_directory(paths["receipts"])
    try:
        incoming.unlink()
    except FileNotFoundError:
        pass
    else:
        _sync_directory(paths["incoming"])
    return receipt


def handle_request(action, root, device_id, request):
    """Initialize a device or verify and durably publish one uploaded image."""
    if not isinstance(request, dict) or request.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("Unsupported manifest protocol")
    if action in {"init", "commit-batch"}:
        files = request.get("files", [])
        if not isinstance(files, list) or len(files) > MAX_FILES:
            raise ValueError("Too many image manifests")
        for metadata in files:
            validate_metadata(metadata)
        if len({metadata["filename"] for metadata in files}) != len(files):
            raise ValueError("Duplicate image filenames")
    elif action == "commit":
        metadata = validate_metadata(request.get("metadata"))
    else:
        raise ValueError("Unknown receiver action")
    paths = _layout(root, device_id)
    with _device_lock(paths):
        if action == "commit":
            return {
                "protocol": PROTOCOL_VERSION,
                "receipt": _commit(paths, metadata, device_id),
            }
        if action == "commit-batch":
            return {
                "protocol": PROTOCOL_VERSION,
                "receipts": [_commit(paths, metadata, device_id) for metadata in files],
            }
        receipts = []
        for metadata in files:
            if os.path.lexists(paths["images"] / metadata["filename"]):
                receipts.append(_commit(paths, metadata, device_id))
        return {
            "protocol": PROTOCOL_VERSION,
            "device_id": device_id,
            "incoming_path": str(paths["incoming"]),
            "receipts": receipts,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "commit", "commit-batch"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--device", required=True)
    args = parser.parse_args(argv)
    try:
        payload = sys.stdin.buffer.read(MAX_MANIFEST_BYTES + 1)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest exceeds byte limit")
        request = json.loads(payload)
        result = handle_request(args.action, args.root, args.device, request)
        sys.stdout.buffer.write(_json_bytes(result))
        sys.stdout.buffer.flush()
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
