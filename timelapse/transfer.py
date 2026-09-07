"""Transfer bounded batches of immutable JPEGs with durable server receipts."""

import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import time

from .receiver import (
    DEVICE_PATTERN,
    MAX_FILES,
    MAX_MANIFEST_BYTES,
    PROTOCOL_VERSION,
    validate_metadata,
)


REMOTE_PATH_PATTERN = re.compile(r"/[A-Za-z0-9_./-]+\Z")
HOST_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]*\Z")
USER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")


class TransferStopped(RuntimeError):
    """The transfer lost power eligibility or exhausted its time allowance."""


def _positive_seconds(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def _validate_config(config):
    result = dict(config)
    for name, pattern in (
        ("host", HOST_PATTERN),
        ("user", USER_PATTERN),
        ("device_id", DEVICE_PATTERN),
    ):
        if not isinstance(result.get(name), str) or not pattern.fullmatch(result[name]):
            raise ValueError(f"Invalid transfer {name}")
    for name in ("remote_root", "receiver_path"):
        path = result.get(name)
        if (
            not isinstance(path, str)
            or not REMOTE_PATH_PATTERN.fullmatch(path)
            or ".." in Path(path).parts
            or "//" in path
        ):
            raise ValueError(
                f"Invalid transfer {name}; use an absolute path without spaces or traversal"
            )
        result[name] = path.rstrip("/")
        if not result[name]:
            raise ValueError(f"Transfer {name} cannot be the filesystem root")
    for name in ("identity_file", "known_hosts_file"):
        value = result.get(name)
        if (
            not isinstance(value, str)
            or not Path(value).is_absolute()
            or not Path(value).is_file()
        ):
            raise ValueError(f"Transfer {name} must be an existing absolute file path")
    port = result.get("port", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid transfer port")
    result["port"] = port
    result["file_timeout_seconds"] = _positive_seconds(
        result.get("file_timeout_seconds", 60), "file_timeout_seconds"
    )
    return result


def _ssh_options(config):
    return [
        "-F",
        "/dev/null",
        "-p",
        str(config["port"]),
        "-i",
        config["identity_file"],
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={config['known_hosts_file']}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=1",
        "-o",
        "Compression=no",
        "-o",
        "RequestTTY=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
    ]


def _stop_process(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)


def _run_process(arguments, input_data, timeout, eligible):
    deadline = time.monotonic() + timeout
    if not eligible():
        raise TransferStopped("power_ineligible")
    next_eligibility_check = time.monotonic() + 1
    stdout = bytearray()
    stderr = bytearray()
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            for stream, tag in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, tag)
            remaining_input = memoryview(input_data or b"")
            if remaining_input:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TransferStopped("time_budget")
                if time.monotonic() >= next_eligibility_check:
                    if not eligible():
                        raise TransferStopped("power_ineligible")
                    next_eligibility_check = time.monotonic() + 1
                wait_seconds = max(
                    0, min(deadline, next_eligibility_check) - time.monotonic()
                )
                for key, _ in selector.select(timeout=wait_seconds):
                    if key.data == "stdin":
                        try:
                            written = os.write(key.fd, remaining_input)
                        except BrokenPipeError:
                            remaining_input = remaining_input[len(remaining_input) :]
                        else:
                            remaining_input = remaining_input[written:]
                        if not remaining_input:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        output = stdout if key.data == "stdout" else stderr
                        output.extend(data)
                        if len(output) > MAX_MANIFEST_BYTES:
                            raise RuntimeError(
                                "Transfer subprocess output exceeds byte limit"
                            )
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise TransferStopped("time_budget")
                if time.monotonic() >= next_eligibility_check:
                    if not eligible():
                        raise TransferStopped("power_ineligible")
                    next_eligibility_check = time.monotonic() + 1
                try:
                    process.wait(
                        timeout=max(
                            0.001,
                            min(deadline, next_eligibility_check) - time.monotonic(),
                        )
                    )
                except subprocess.TimeoutExpired:
                    pass
        if process.returncode:
            detail = bytes(stderr).decode("utf-8", errors="replace")[-2000:].strip()
            raise RuntimeError(
                f"Transfer subprocess exited {process.returncode}: {detail}"
            )
        return bytes(stdout)
    finally:
        _stop_process(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _receiver(config, action, payload, deadline, eligible):
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError("Transfer manifest exceeds byte limit")
    command = shlex.join(
        [
            "python3",
            config["receiver_path"],
            action,
            "--root",
            config["remote_root"],
            "--device",
            config["device_id"],
        ]
    )
    arguments = [
        "ssh",
        *_ssh_options(config),
        f"{config['user']}@{config['host']}",
        command,
    ]
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TransferStopped("time_budget")
    response = _run_process(
        arguments, body, min(remaining, config["file_timeout_seconds"]), eligible
    )
    parsed = json.loads(response)
    if not isinstance(parsed, dict) or parsed.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("Invalid receiver response protocol")
    return parsed


def _validate_receipt(receipt, metadata, device_id):
    if (
        not isinstance(receipt, dict)
        or receipt.get("protocol") != PROTOCOL_VERSION
        or receipt.get("device_id") != device_id
    ):
        raise ValueError("Invalid receiver receipt")
    if type(receipt.get("size_bytes")) is not int or any(
        receipt.get(name) != metadata[name]
        for name in ("filename", "size_bytes", "sha256")
    ):
        raise ValueError("Receiver receipt does not match local image")
    return receipt


def transfer(spool, config, eligible, max_bytes=33554432, max_seconds=120):
    """Upload a finite batch, preserving local files and recording verified receipts.

    The byte allowance counts original selected image sizes, including resumed or
    already published images. The deadline includes connection and verification.
    Eligibility is checked during subprocess execution approximately once a second.
    file_timeout_seconds caps each subprocess, including the whole batch rsync.
    """
    started = time.monotonic()
    seconds = _positive_seconds(max_seconds, "max_seconds")
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    result = {
        "status": "disabled",
        "reason": "destination_unset",
        "uploaded_files": 0,
        "uploaded_bytes": 0,
        "recovered_receipts": 0,
        "selected_bytes": 0,
        "skipped_oversized_files": 0,
        "errors": [],
    }
    if not config or config.get("enabled") is False or not config.get("host"):
        return result
    config = _validate_config(config)
    deadline = started + seconds
    selected = []
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError("Transfer deadline expired before spool inspection")
        pending = spool.list_pending(
            timeout_seconds=max(0, deadline - time.monotonic())
        )
    except TimeoutError:
        result.update(
            status="deferred",
            reason="time_budget",
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        return result
    for item in pending:
        metadata = validate_metadata(item)
        if metadata["size_bytes"] > max_bytes - result["selected_bytes"]:
            result["skipped_oversized_files"] += 1
            continue
        selected.append(metadata)
        result["selected_bytes"] += metadata["size_bytes"]
        if len(selected) == MAX_FILES:
            break
    if not selected:
        result.update(
            status="deferred" if result["skipped_oversized_files"] else "idle",
            reason="byte_budget"
            if result["skipped_oversized_files"]
            else "no_pending_images",
        )
        return result
    current_filename = None
    try:
        initialized = _receiver(
            config,
            "init",
            {"protocol": PROTOCOL_VERSION, "files": selected},
            deadline,
            eligible,
        )
        incoming = f"{config['remote_root']}/{config['device_id']}/incoming"
        if (
            initialized.get("device_id") != config["device_id"]
            or initialized.get("incoming_path") != incoming
        ):
            raise ValueError("Receiver initialized an unexpected destination")
        receipts = initialized.get("receipts")
        if not isinstance(receipts, list) or len(receipts) > len(selected):
            raise ValueError("Invalid receiver receipt list")
        by_filename = {item["filename"]: item for item in selected}
        recovered = set()
        for receipt in receipts:
            current_filename = (
                receipt.get("filename") if isinstance(receipt, dict) else None
            )
            if current_filename not in by_filename or current_filename in recovered:
                raise ValueError("Receiver returned an unexpected or duplicate receipt")
            metadata = by_filename[current_filename]
            spool.record_receipt(
                current_filename,
                _validate_receipt(receipt, metadata, config["device_id"]),
                timeout_seconds=max(0, deadline - time.monotonic()),
            )
            recovered.add(current_filename)
            result["recovered_receipts"] += 1
        remaining_files = [
            metadata for metadata in selected if metadata["filename"] not in recovered
        ]
        source_directory = None
        for metadata in remaining_files:
            current_filename = metadata["filename"]
            if not eligible():
                raise TransferStopped("power_ineligible")
            if time.monotonic() >= deadline:
                raise TransferStopped("time_budget")
            image = spool.image_path(current_filename)
            if image.stat().st_size != metadata["size_bytes"]:
                raise ValueError("Local image size changed after capture")
            if source_directory is not None and image.parent != source_directory:
                raise ValueError("Spool images must share a source directory")
            source_directory = image.parent
        if remaining_files:
            current_filename = None
            host = f"[{config['host']}]" if ":" in config["host"] else config["host"]
            destination = f"{config['user']}@{host}:{incoming}/"
            arguments = [
                "rsync",
                "--partial",
                "--partial-dir=.rsync-partial",
                "--protect-args",
                "--checksum",
                "--from0",
                "--files-from=-",
                "--timeout=15",
                "-e",
                shlex.join(["ssh", *_ssh_options(config)]),
                "--",
                str(source_directory) + "/",
                destination,
            ]
            files_input = b"".join(
                metadata["filename"].encode("ascii") + b"\0"
                for metadata in remaining_files
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransferStopped("time_budget")
            _run_process(
                arguments,
                files_input,
                min(remaining, config["file_timeout_seconds"]),
                eligible,
            )
            committed = _receiver(
                config,
                "commit-batch",
                {"protocol": PROTOCOL_VERSION, "files": remaining_files},
                deadline,
                eligible,
            )
            receipts = committed.get("receipts")
            if not isinstance(receipts, list) or len(receipts) != len(remaining_files):
                raise ValueError(
                    "Receiver did not acknowledge the complete uploaded batch"
                )
            pending_by_filename = {
                metadata["filename"]: metadata for metadata in remaining_files
            }
            validated = {}
            for receipt in receipts:
                current_filename = (
                    receipt.get("filename") if isinstance(receipt, dict) else None
                )
                if (
                    current_filename not in pending_by_filename
                    or current_filename in validated
                ):
                    raise ValueError(
                        "Receiver returned an unexpected or duplicate receipt"
                    )
                validated[current_filename] = _validate_receipt(
                    receipt, pending_by_filename[current_filename], config["device_id"]
                )
            for current_filename, receipt in validated.items():
                spool.record_receipt(
                    current_filename,
                    receipt,
                    timeout_seconds=max(0, deadline - time.monotonic()),
                )
                result["uploaded_files"] += 1
                result["uploaded_bytes"] += receipt["size_bytes"]
        result.update(status="complete", reason="batch_complete")
    except TransferStopped as exc:
        result.update(status="deferred", reason=str(exc))
    except TimeoutError:
        result.update(status="deferred", reason="time_budget")
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        result.update(status="error", reason="transfer_failed")
        result["errors"].append({"filename": current_filename, "error": str(exc)})
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result
