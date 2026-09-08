import os
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .spool import (
    MAX_IMAGE_BYTES,
    MAX_RECORD_BYTES,
    Spool,
    SpoolError,
    file_digest,
    fsync_directory,
)


class CaptureError(RuntimeError):
    pass


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def run_cancellable(command, errors, timeout_seconds, cancelled):
    if cancelled():
        raise CaptureError("Camera capture cancelled by its power guard")
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=errors,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            if cancelled():
                raise CaptureError("Camera capture cancelled by its power guard")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaptureError(
                    f"Camera capture exceeded {timeout_seconds:g} seconds"
                )
            try:
                process.wait(timeout=min(0.2, remaining))
                return process
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)


def capture(
    spool: Spool,
    camera_command: str = "rpicam-still",
    timeout_seconds: float = 45,
    settle_ms: int = 1000,
    width: int = 4608,
    height: int = 2592,
    rotation: int = 180,
    time_source: str = "UNSYNC",
    cancelled=None,
    lock_timeout_seconds=None,
) -> dict:
    if (
        not isinstance(camera_command, str)
        or not camera_command
        or "\x00" in camera_command
    ):
        raise ValueError("camera_command must be an executable name or path")
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 0 < timeout_seconds <= 3600
    ):
        raise ValueError("timeout_seconds must be between 0 and 3600")
    for name, value in (("width", width), ("height", height), ("settle_ms", settle_ms)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if rotation not in (0, 180):
        raise ValueError("rotation must be 0 or 180 degrees")
    if time_source not in {"NTP", "RTC", "UNSYNC"}:
        raise ValueError("time_source must be NTP, RTC, or UNSYNC")
    if cancelled is not None and not callable(cancelled):
        raise ValueError("cancelled must be callable or None")
    if cancelled is not None and cancelled():
        raise CaptureError("Camera capture cancelled by its power guard")
    with spool.lock(timeout_seconds=lock_timeout_seconds):
        spool._recover()
        spool.check_capacity(MAX_IMAGE_BYTES + 2 * MAX_RECORD_BYTES)
        timestamp = datetime.now(timezone.utc)
        filename = f"{timestamp:%Y%m%dT%H%M%S.%fZ}-{uuid.uuid4().hex}.jpg"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".capture-", suffix=".part", dir=spool.images
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        publication_started = False
        started = time.monotonic()
        try:
            command = [
                camera_command,
                "--nopreview",
                "--timeout",
                str(settle_ms),
                "--width",
                str(width),
                "--height",
                str(height),
                "--rotation",
                str(rotation),
                "--encoding",
                "jpg",
                "--output",
                str(temporary),
            ]
            with tempfile.TemporaryFile() as errors:
                try:
                    if cancelled is None:
                        process = subprocess.run(
                            command,
                            stdout=subprocess.DEVNULL,
                            stderr=errors,
                            timeout=timeout_seconds,
                            check=False,
                        )
                    else:
                        process = run_cancellable(
                            command, errors, timeout_seconds, cancelled
                        )
                except subprocess.TimeoutExpired as error:
                    raise CaptureError(
                        f"Camera capture exceeded {timeout_seconds:g} seconds"
                    ) from error
                except OSError as error:
                    raise CaptureError(f"Unable to start camera: {error}") from error
                if process.returncode:
                    errors.seek(0, os.SEEK_END)
                    errors.seek(max(0, errors.tell() - 4096))
                    detail = errors.read().decode("utf-8", errors="replace").strip()
                    raise CaptureError(
                        f"Camera exited with status {process.returncode}: {detail}"
                    )
            if cancelled is not None and cancelled():
                raise CaptureError("Camera capture cancelled by its power guard")
            duration = time.monotonic() - started
            descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as image:
                details = os.fstat(image.fileno())
                if (
                    not stat.S_ISREG(details.st_mode)
                    or not 4 <= details.st_size <= MAX_IMAGE_BYTES
                ):
                    raise CaptureError(
                        "Camera produced an empty, oversized, or non-regular image"
                    )
                start = image.read(2)
                image.seek(-2, os.SEEK_END)
                if start != b"\xff\xd8" or image.read(2) != b"\xff\xd9":
                    raise CaptureError("Camera did not produce a complete JPEG")
                os.fsync(image.fileno())
            fsync_directory(spool.images)
            size, digest = file_digest(temporary)
            metadata = {
                "filename": filename,
                "size_bytes": size,
                "sha256": digest,
                "captured_at_utc": timestamp.isoformat(),
                "time_source": time_source,
                "boot_id": boot_id(),
                "capture_duration_seconds": duration,
            }
            spool.check_capacity(2 * MAX_RECORD_BYTES)
            if cancelled is not None and cancelled():
                raise CaptureError("Camera capture cancelled by its power guard")
            publication_started = True
            return spool.publish(temporary, metadata)
        except (CaptureError, SpoolError):
            raise
        except OSError as error:
            raise CaptureError(f"Unable to preserve camera image: {error}") from error
        finally:
            if not publication_started:
                temporary.unlink(missing_ok=True)
                fsync_directory(spool.images)
