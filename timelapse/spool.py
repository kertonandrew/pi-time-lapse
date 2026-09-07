import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
from datetime import datetime
from pathlib import Path


MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.jpg\Z")
HASH_PATTERN = re.compile(r"[a-f0-9]{64}\Z")


class SpoolError(RuntimeError):
    pass


class CapacityError(SpoolError):
    pass


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_directory(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o750, exist_ok=True)
        fsync_directory(directory.parent)
    if path.is_symlink() or not path.is_dir():
        raise SpoolError(f"Spool directory must be a real directory: {path}")


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SpoolError(f"Image is not a regular file: {path.name}")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    payload = (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        + b"\n"
    )
    if len(payload) > MAX_RECORD_BYTES:
        raise SpoolError("Metadata or receipt exceeds the record size limit")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SpoolError(f"Record is not a regular file: {path.name}")
        payload = stream.read(MAX_RECORD_BYTES + 1)
    if len(payload) > MAX_RECORD_BYTES:
        raise SpoolError(f"Record exceeds the size limit: {path.name}")
    try:
        result = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise SpoolError(f"Invalid JSON record: {path.name}") from error
    if not isinstance(result, dict):
        raise SpoolError(f"Record must contain an object: {path.name}")
    return result


class Spool:
    def __init__(
        self, root: Path, max_bytes: int = 536870912, min_free_bytes: int = 536870912
    ):
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        if (
            isinstance(min_free_bytes, bool)
            or not isinstance(min_free_bytes, int)
            or min_free_bytes < 0
        ):
            raise ValueError("min_free_bytes must be a nonnegative integer")
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.images = self.root / "images"
        self.metadata = self.root / "metadata"
        self.receipts = self.root / "receipts"
        for directory in (self.root, self.images, self.metadata, self.receipts):
            durable_directory(directory)

    @contextlib.contextmanager
    def lock(self, timeout_seconds: float | None = None):
        """Acquire a non-reentrant lock with an optional wait budget; zero expires immediately."""
        if timeout_seconds is not None:
            if (
                type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds)
                or timeout_seconds < 0
            ):
                raise ValueError(
                    "timeout_seconds must be a finite nonnegative number or None"
                )
            if timeout_seconds == 0:
                raise TimeoutError("Spool lock time budget exhausted")
        deadline = (
            None if timeout_seconds is None else time.monotonic() + timeout_seconds
        )
        descriptor = os.open(
            self.root / ".spool.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        acquired = False
        try:
            if deadline is None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            else:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Spool lock time budget exhausted")
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(min(0.05, remaining))
            acquired = True
            yield
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def image_path(self, filename: object) -> Path:
        if (
            not isinstance(filename, str)
            or len(filename) > 200
            or not FILENAME_PATTERN.fullmatch(filename)
        ):
            raise SpoolError("Invalid image filename")
        path = self.images / filename
        if path.is_symlink():
            raise SpoolError("Image must not be a symbolic link")
        return path

    def metadata_path(self, filename: str) -> Path:
        self.image_path(filename)
        return self.metadata / f"{Path(filename).stem}.json"

    def receipt_path(self, filename: str) -> Path:
        self.image_path(filename)
        return self.receipts / f"{Path(filename).stem}.json"

    def used_bytes(self) -> int:
        used = 0
        for directory in (self.images, self.metadata, self.receipts):
            for path in directory.iterdir():
                details = path.lstat()
                if not stat.S_ISREG(details.st_mode):
                    raise SpoolError(f"Unexpected non-regular spool entry: {path}")
                used += details.st_size
        return used

    def check_capacity(self, reserve_bytes: int = MAX_IMAGE_BYTES) -> None:
        if (
            isinstance(reserve_bytes, bool)
            or not isinstance(reserve_bytes, int)
            or reserve_bytes < 0
        ):
            raise ValueError("reserve_bytes must be a nonnegative integer")
        if self.used_bytes() + reserve_bytes > self.max_bytes:
            raise CapacityError("Spool capacity reached; existing images are retained")
        if shutil.disk_usage(self.root).free - reserve_bytes < self.min_free_bytes:
            raise CapacityError(
                "Insufficient free disk space; existing images are retained"
            )

    def _validate_metadata(self, metadata: object, filename: str | None = None) -> dict:
        if not isinstance(metadata, dict):
            raise SpoolError("Capture metadata must contain an object")
        name = metadata.get("filename")
        self.image_path(name)
        if filename is not None and name != filename:
            raise SpoolError("Metadata filename does not match its record")
        size = metadata.get("size_bytes")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 < size <= MAX_IMAGE_BYTES
        ):
            raise SpoolError("Invalid metadata image size")
        digest = metadata.get("sha256")
        if not isinstance(digest, str) or not HASH_PATTERN.fullmatch(digest):
            raise SpoolError("Invalid metadata image hash")
        try:
            timestamp = datetime.fromisoformat(metadata["captured_at_utc"])
        except (KeyError, TypeError, ValueError) as error:
            raise SpoolError("Invalid metadata capture timestamp") from error
        offset = timestamp.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise SpoolError("Capture timestamp must be timezone-aware UTC")
        if metadata.get("time_source") not in {"NTP", "RTC", "UNSYNC"}:
            raise SpoolError("Invalid metadata time source")
        if not isinstance(metadata.get("boot_id"), str) or not metadata["boot_id"]:
            raise SpoolError("Invalid metadata boot ID")
        duration = metadata.get("capture_duration_seconds")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < 0
        ):
            raise SpoolError("Invalid metadata capture duration")
        return metadata

    def _check_image(self, path: Path, metadata: dict) -> None:
        size, digest = file_digest(path)
        if size != metadata["size_bytes"] or digest != metadata["sha256"]:
            raise SpoolError(f"Image does not match durable metadata: {path.name}")

    def _finish_publication(self, journal: Path, record: dict) -> dict:
        metadata = self._validate_metadata(record.get("metadata"))
        filename = metadata["filename"]
        expected_journal = self.metadata / f".{Path(filename).stem}.pending.json"
        if journal != expected_journal:
            raise SpoolError("Capture journal filename mismatch")
        temporary_name = record.get("temporary_name")
        if not isinstance(temporary_name, str) or not re.fullmatch(
            r"\.capture-[A-Za-z0-9_-]+\.part", temporary_name
        ):
            raise SpoolError("Invalid capture journal temporary filename")
        temporary = self.images / temporary_name
        final = self.image_path(filename)
        metadata_path = self.metadata_path(filename)
        if final.exists():
            self._check_image(final, metadata)
        else:
            self._check_image(temporary, metadata)
            os.link(temporary, final, follow_symlinks=False)
        fsync_directory(self.images)
        if metadata_path.exists():
            if read_json(metadata_path) != metadata:
                raise SpoolError(f"Existing metadata differs from journal: {filename}")
        else:
            atomic_json(metadata_path, metadata)
        temporary.unlink(missing_ok=True)
        fsync_directory(self.images)
        journal.unlink()
        fsync_directory(self.metadata)
        return metadata

    def _recover(self) -> list[dict]:
        return [
            self._finish_publication(path, read_json(path))
            for path in sorted(self.metadata.glob(".*.pending.json"))
        ]

    def recover(self) -> list[dict]:
        with self.lock():
            return self._recover()

    def publish(self, temporary: Path, metadata: dict) -> dict:
        """Publish a durable capture while the caller holds lock()."""
        self._validate_metadata(metadata)
        if temporary.parent != self.images or not re.fullmatch(
            r"\.capture-[A-Za-z0-9_-]+\.part", temporary.name
        ):
            raise SpoolError(
                "Capture temporary file must be inside the image directory"
            )
        filename = metadata["filename"]
        if self.image_path(filename).exists() or self.metadata_path(filename).exists():
            raise SpoolError(f"Capture filename already exists: {filename}")
        self._check_image(temporary, metadata)
        journal = self.metadata / f".{Path(filename).stem}.pending.json"
        if journal.exists():
            raise SpoolError(f"Capture journal already exists: {filename}")
        atomic_json(journal, {"metadata": metadata, "temporary_name": temporary.name})
        return self._finish_publication(journal, read_json(journal))

    def list_pending(self, timeout_seconds: float | None = None) -> list[dict]:
        with self.lock(timeout_seconds=timeout_seconds):
            self._recover()
            pending = []
            for path in self.metadata.glob("*.json"):
                if path.name.startswith("."):
                    continue
                metadata = self._validate_metadata(read_json(path), f"{path.stem}.jpg")
                image = self.image_path(metadata["filename"])
                if not image.is_file():
                    raise SpoolError(
                        f"Committed image is missing: {metadata['filename']}"
                    )
                if image.stat().st_size != metadata["size_bytes"]:
                    raise SpoolError(
                        f"Committed image size changed: {metadata['filename']}"
                    )
                try:
                    receipt = read_json(self.receipt_path(metadata["filename"]))
                except (OSError, SpoolError):
                    receipt = {}
                if not all(
                    receipt.get(key) == metadata[key]
                    for key in ("filename", "size_bytes", "sha256")
                ):
                    pending.append(metadata)
            return sorted(
                pending, key=lambda item: (item["captured_at_utc"], item["filename"])
            )

    def record_receipt(
        self, filename: str, receipt: dict, timeout_seconds: float | None = None
    ) -> None:
        if not isinstance(receipt, dict):
            raise SpoolError("Receipt must contain an object")
        with self.lock(timeout_seconds=timeout_seconds):
            self._recover()
            metadata = self._validate_metadata(
                read_json(self.metadata_path(filename)), filename
            )
            result = dict(receipt)
            for key in ("filename", "size_bytes", "sha256"):
                if key in result and result[key] != metadata[key]:
                    raise SpoolError(f"Receipt {key} does not match the capture")
                result[key] = metadata[key]
            atomic_json(self.receipt_path(filename), result)
