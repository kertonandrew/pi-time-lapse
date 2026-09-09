"""Allow only a fixed camera's receiver and incoming rsync commands over SSH."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import resource
import signal
import stat
import subprocess
import sys

from .receiver import DEVICE_PATTERN, MAX_IMAGE_BYTES


MAX_CONFIG_BYTES = 4096
MAX_COMMAND_BYTES = 2048
SAFE_PATH = re.compile(
    r"/(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*\Z"
)
RSYNC_FEATURES = {"-cRe.LsfxCIvu", "-cRe.iLsfxCIvu"}
RECEIVER_ACTIONS = {"init", "commit", "commit-batch"}
CLEAN_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "HOME": "/nonexistent",
}


def _path(value):
    if not isinstance(value, str) or not SAFE_PATH.fullmatch(value):
        raise ValueError("Gateway paths must be canonical absolute ASCII paths")
    return value


def validate_config(data):
    """Validate locally provisioned settings; remote commands cannot change them."""
    required = {"root", "device_id", "receiver_path", "python_path", "rsync_path"}
    if (
        not isinstance(data, dict)
        or not required <= data.keys()
        or data.keys() - required - {"timeout_seconds"}
    ):
        raise ValueError("Invalid gateway configuration fields")
    result = dict(data)
    for key in required - {"device_id"}:
        result[key] = _path(result[key])
    if not isinstance(result["device_id"], str) or not DEVICE_PATTERN.fullmatch(
        result["device_id"]
    ):
        raise ValueError("Invalid fixed gateway device")
    timeout = result.get("timeout_seconds", 120)
    if type(timeout) is not int or not 5 <= timeout <= 300:
        raise ValueError("Gateway timeout must be an integer from 5 to 300 seconds")
    result["timeout_seconds"] = timeout
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate gateway configuration field")
        result[key] = value
    return result


def _directory_fd(path, owner_ids):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = following
            info = os.fstat(descriptor)
            if info.st_uid not in owner_ids or info.st_mode & 0o022:
                raise ValueError(
                    "Gateway directory has unsafe ownership or permissions"
                )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def load_config(path):
    """Read a bounded root-owned configuration through immutable parent directories."""
    path = Path(_path(str(path)))
    parent = _directory_fd(path.parent, {0})
    try:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
                or info.st_size > MAX_CONFIG_BYTES
            ):
                raise ValueError(
                    "Gateway configuration must be a small root-owned regular file"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read(MAX_CONFIG_BYTES + 1)
            if len(payload) > MAX_CONFIG_BYTES:
                raise ValueError("Gateway configuration exceeds its byte limit")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    return validate_config(json.loads(payload, object_pairs_hook=_unique_object))


def command_plan(command, config):
    """Map one exact uploader command to a locally selected executable and arguments."""
    config = validate_config(config)
    if (
        not isinstance(command, str)
        or len(command) > MAX_COMMAND_BYTES
        or not command.isascii()
    ):
        raise ValueError("Invalid SSH command")
    tokens = command.split(" ")
    if any(
        not token
        or any(character in token for character in "\t\r\n\x00'\"\\;$`|&<>(){}[]")
        for token in tokens
    ):
        raise ValueError("SSH command must use the normalized uploader format")
    for action in RECEIVER_ACTIONS:
        expected = [
            "python3",
            config["receiver_path"],
            action,
            "--root",
            config["root"],
            "--device",
            config["device_id"],
        ]
        if tokens == expected:
            return "receiver", [
                config["python_path"],
                "-I",
                "-m",
                "timelapse.receiver",
                action,
                "--root",
                config["root"],
                "--device",
                config["device_id"],
            ]
    incoming = f"{config['root']}/{config['device_id']}/incoming/"
    if (
        len(tokens) != 8
        or tokens[:2] != ["rsync", "--server"]
        or tokens[2] not in RSYNC_FEATURES
        or tokens[3:]
        != ["--timeout=15", "--partial-dir", ".rsync-partial", ".", incoming]
    ):
        raise ValueError("SSH command is outside the camera upload allowlist")
    return "rsync", [
        config["rsync_path"],
        "--server",
        tokens[2],
        "--timeout=15",
        "--no-links",
        "--no-devices",
        "--no-specials",
        "--munge-links",
        "--safe-links",
        f"--max-size={MAX_IMAGE_BYTES}",
        f"--max-alloc={MAX_IMAGE_BYTES}",
        "--chmod=F600,D700",
    ]


@contextlib.contextmanager
def _device_lock(config):
    device = Path(config["root"]) / config["device_id"]
    directory = _directory_fd(device, {0, os.geteuid()})
    try:
        descriptor = os.open(
            ".ssh-gateway.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("Invalid gateway lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield directory
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _limits(directory, timeout):
    os.fchdir(directory)
    os.umask(0o077)
    for category, limit in (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_FSIZE, MAX_IMAGE_BYTES),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_CPU, timeout),
        (resource.RLIMIT_AS, 512 * 1024 * 1024),
    ):
        _, hard = resource.getrlimit(category)
        value = min(limit, hard) if hard != resource.RLIM_INFINITY else limit
        resource.setrlimit(category, (value, value))


def _run(arguments, directory, descriptors, timeout):
    process = subprocess.Popen(
        arguments,
        env=CLEAN_ENVIRONMENT,
        close_fds=True,
        pass_fds=tuple(descriptors),
        start_new_session=True,
        preexec_fn=lambda: _limits(directory, timeout),
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def _interrupted(signum, frame):
    raise SystemExit(128 + signum)


def execute(command, config):
    """Run an allowlisted process with fixed storage, limits, and a clean environment."""
    config = validate_config(config)
    kind, arguments = command_plan(command, config)
    if sys.platform != "linux" or os.geteuid() == 0:
        raise ValueError("The SSH gateway requires an unprivileged Linux account")
    with _device_lock(config) as directory:
        if kind == "receiver":
            return _run(arguments, directory, (directory,), config["timeout_seconds"])
        incoming = os.open(
            "incoming", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
        )
        try:
            info = os.fstat(incoming)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError(
                    "Incoming directory must be private to the ingest account"
                )
            try:
                os.mkdir(".rsync-partial", mode=0o700, dir_fd=incoming)
            except FileExistsError:
                pass
            partial = os.open(
                ".rsync-partial",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=incoming,
            )
            try:
                info = os.fstat(partial)
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ValueError(
                        "Partial directory must be private to the ingest account"
                    )
                arguments.extend(
                    ["--partial-dir", f"/proc/self/fd/{partial}", ".", "./"]
                )
                return _run(
                    arguments, incoming, (incoming, partial), config["timeout_seconds"]
                )
            finally:
                os.close(partial)
        finally:
            os.close(incoming)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    previous_handlers = {
        signum: signal.signal(signum, _interrupted)
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    }
    try:
        return execute(
            os.environ.get("SSH_ORIGINAL_COMMAND", ""), load_config(args.config)
        )
    except (OSError, ValueError, TypeError, RecursionError):
        print("Camera SSH command rejected", file=sys.stderr)
        return 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
