"""Read the newest verified, committed JPEG from a receiver archive."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat

from .receiver import (
    DEVICE_PATTERN,
    MAX_INDEX_RECORD_BYTES,
    MAX_LATEST_INDEX_BYTES,
    photo_order,
    validate_latest_index,
    validate_metadata,
)


MAX_RECORD_BYTES = MAX_INDEX_RECORD_BYTES
MAX_SCAN_ENTRIES = 10000
MAX_PHOTO_BYTES = 33554432


def _identity(details):
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
    )


def _read(directory, name, limit):
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
    )
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("Archived photo or record has an invalid type or size")
        payload = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            len(payload) > limit
            or len(payload) != before.st_size
            or _identity(before) != _identity(after)
            or _identity(before) != _identity(current)
            or not stat.S_ISREG(current.st_mode)
        ):
            raise ValueError("Archived photo or record changed during reading")
        return payload


def _invalid_constant(_value):
    raise ValueError("Archived JSON contains a nonfinite number")


def _record(directory, name, limit=MAX_RECORD_BYTES):
    value = json.loads(_read(directory, name, limit), parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Archived record must be an object")
    return value


def _directory(stack, name, parent=None):
    descriptor = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
    )
    stack.callback(os.close, descriptor)
    return descriptor


def _committed_record(receipts, metadata_directory, name, device_id):
    receipt = _record(receipts, name)
    metadata = validate_metadata(_record(metadata_directory, name))
    if name != f"{Path(metadata['filename']).stem}.json":
        raise ValueError("Archive record name does not match its image")
    validate_latest_index(
        {
            "protocol": 1,
            "device_id": device_id,
            "metadata": metadata,
            "receipt": receipt,
        },
        device_id,
    )
    return metadata, receipt


def _legacy_names(receipts, root, device_id):
    names = []
    with os.scandir(receipts) as entries:
        for count, entry in enumerate(entries, 1):
            if count > MAX_SCAN_ENTRIES:
                command = (
                    "python3 -m timelapse.receiver reindex --root "
                    f"{shlex.quote(str(root))} --device {shlex.quote(device_id)}"
                )
                raise ValueError(
                    "Legacy archive receipt scan exceeds 10000 entries. "
                    f"Run `{command}` once with stdin "
                    '{"protocol":1,"max_entries":1000000} to build latest.json.'
                )
            if not entry.name.startswith(".") and entry.name.endswith(".json"):
                names.append(entry.name)
    return names


def latest_photo(archive_root: Path, device_id: str, max_bytes: int = 8388608):
    """Return a verified committed JPEG and metadata, or None for an empty archive."""
    root = Path(archive_root)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("Archive root must be absolute without traversal")
    if not isinstance(device_id, str) or not DEVICE_PATTERN.fullmatch(device_id):
        raise ValueError("Invalid device identifier")
    if type(max_bytes) is not int or not 4 <= max_bytes <= MAX_PHOTO_BYTES:
        raise ValueError("Photo byte limit must be between four and 32 MiB")
    with contextlib.ExitStack() as stack:
        try:
            directory = _directory(stack, root.anchor)
            for part in root.parts[1:]:
                directory = _directory(stack, part, directory)
            device = _directory(stack, device_id, directory)
        except FileNotFoundError:
            return None
        try:
            index = _record(device, "latest.json", MAX_LATEST_INDEX_BYTES)
        except FileNotFoundError:
            index = None
        if index is not None:
            validate_latest_index(index, device_id)
            if index["metadata"] is None:
                return None
            receipts = _directory(stack, "receipts", device)
            metadata_directory = _directory(stack, "metadata", device)
            name = f"{Path(index['metadata']['filename']).stem}.json"
            metadata, receipt = _committed_record(
                receipts, metadata_directory, name, device_id
            )
            if metadata != index["metadata"] or receipt != index["receipt"]:
                raise ValueError(
                    "Latest-photo index does not match committed archive records"
                )
        else:
            try:
                receipts = _directory(stack, "receipts", device)
            except FileNotFoundError:
                return None
            names = _legacy_names(receipts, root, device_id)
            if not names:
                return None
            metadata_directory = _directory(stack, "metadata", device)
            selected = None
            for name in names:
                metadata, receipt = _committed_record(
                    receipts, metadata_directory, name, device_id
                )
                ordering = photo_order(metadata, receipt)
                if selected is None or ordering > selected[0]:
                    selected = (ordering, metadata, receipt)
            if selected is None:
                return None
            metadata, receipt = selected[1], selected[2]
        images = _directory(stack, "images", device)
        if metadata["size_bytes"] > max_bytes:
            raise ValueError("Newest committed photo exceeds the configured byte limit")
        payload = _read(images, metadata["filename"], max_bytes)
        if len(payload) != metadata["size_bytes"]:
            raise ValueError("Committed photo size does not match its metadata")
        if hashlib.sha256(payload).hexdigest() != metadata["sha256"]:
            raise ValueError("Committed photo SHA256 does not match its metadata")
        if (
            len(payload) < 4
            or not payload.startswith(b"\xff\xd8")
            or not payload.endswith(b"\xff\xd9")
        ):
            raise ValueError("Committed photo is not a complete JPEG")
        return {
            "payload": payload,
            "metadata": dict(metadata, uploaded_at_utc=receipt["stored_at_utc"]),
        }
