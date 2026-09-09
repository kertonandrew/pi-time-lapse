"""Private operator profiles for SSH application deployment."""

import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from timelapse.setup import initialize


PROFILE_KEYS = {"schema_version", "device_id", "ssh", "remote", "files"}
SSH_KEYS = {
    "host",
    "user",
    "port",
    "host_key_alias",
    "identity_file",
    "known_hosts_file",
}
REMOTE_KEYS = {"staging_dir", "python", "min_free_mb"}
FILE_KEYS = {"camera", "home_assistant", "hardware_env"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}\Z")
USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")


def exact_keys(value, keys, name):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{name} must contain exactly: {', '.join(sorted(keys))}")


def validate_profile(value):
    exact_keys(value, PROFILE_KEYS, "Profile")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported deployment profile schema")
    if not isinstance(value["device_id"], str) or not IDENTIFIER.fullmatch(
        value["device_id"]
    ):
        raise ValueError("Invalid device_id")
    ssh = value["ssh"]
    exact_keys(ssh, SSH_KEYS, "ssh")
    for key, pattern in (("host", HOST), ("user", USER)):
        if not isinstance(ssh[key], str) or not pattern.fullmatch(ssh[key]):
            raise ValueError(f"Invalid ssh.{key}")
    if type(ssh["port"]) is not int or not 1 <= ssh["port"] <= 65535:
        raise ValueError("Invalid ssh.port")
    if ssh["host_key_alias"] is not None and (
        not isinstance(ssh["host_key_alias"], str)
        or not HOST.fullmatch(ssh["host_key_alias"])
    ):
        raise ValueError("Invalid ssh.host_key_alias")
    for key in ("identity_file", "known_hosts_file"):
        entry = ssh[key]
        if entry is not None and (
            not isinstance(entry, str) or not entry or any(ord(c) < 32 for c in entry)
        ):
            raise ValueError(f"Invalid ssh.{key}")
    remote = value["remote"]
    exact_keys(remote, REMOTE_KEYS, "remote")
    staging = remote["staging_dir"]
    if not isinstance(staging, str) or not re.fullmatch(
        r"/var/lib/[A-Za-z0-9][A-Za-z0-9_-]{0,63}", staging
    ):
        raise ValueError(
            "remote.staging_dir must be a dedicated directory under /var/lib"
        )
    if not isinstance(remote["python"], str) or not re.fullmatch(
        r"/usr/bin/python3(?:\.[0-9]+)?", remote["python"]
    ):
        raise ValueError("remote.python must be a system Python 3 executable")
    if (
        type(remote["min_free_mb"]) is not int
        or not 32 <= remote["min_free_mb"] <= 65536
    ):
        raise ValueError("remote.min_free_mb must be between 32 and 65536")
    exact_keys(value["files"], FILE_KEYS, "files")
    for name in value["files"].values():
        if not isinstance(name, str) or not name or any(ord(c) < 32 for c in name):
            raise ValueError("Profile file references must be relative paths")
        candidate = PurePosixPath(name)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in name:
            raise ValueError(
                "Profile file references must stay inside the profile directory"
            )
    return value


def private_file(path, maximum=65536):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Private configuration must not be a symlink")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_mode & 0o077
            or details.st_nlink != 1
        ):
            raise ValueError(
                "Private configuration must be a regular file with mode 0600"
            )
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Private configuration is too large")
    return data


def load_profile(path):
    path = Path(path).absolute()
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise ValueError("Deployment profile directory must be private (0700)")
    return validate_profile(json.loads(private_file(path)))


def profile_file(profile_path, profile, name):
    root = Path(profile_path).absolute().parent
    candidate = root / profile["files"][name]
    if root.resolve() not in candidate.resolve().parents or candidate.is_symlink():
        raise ValueError(
            "Profile file references must stay inside the profile directory"
        )
    return candidate


def write_private(path, data):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def initialize_profile(
    directory, host, user, device_id, broker=None, host_key_alias=None
):
    directory = Path(directory).absolute()
    profile = validate_profile(
        {
            "schema_version": 1,
            "device_id": device_id,
            "ssh": {
                "host": host,
                "user": user,
                "port": 22,
                "host_key_alias": host_key_alias,
                "identity_file": None,
                "known_hosts_file": None,
            },
            "remote": {
                "staging_dir": "/var/lib/pi-timelapse-deploy",
                "python": "/usr/bin/python3",
                "min_free_mb": 128,
            },
            "files": {
                "camera": "camera.json",
                "home_assistant": "home-assistant.json",
                "hardware_env": "hardware.env",
            },
        }
    )
    if directory.is_symlink() or (
        directory.exists()
        and (directory.stat().st_mode & 0o077 or any(directory.iterdir()))
    ):
        raise ValueError("Initialize into a new or empty private directory")
    created = initialize(directory, device_id, broker)
    write_private(
        directory / "profile.json", (json.dumps(profile, indent=2) + "\n").encode()
    )
    template = Path(__file__).resolve().parents[1] / "hardware/pi-hardware.default"
    write_private(directory / "hardware.env", template.read_bytes())
    return {
        "created": [
            *created["created"],
            str(directory / "profile.json"),
            str(directory / "hardware.env"),
        ],
        "device_id": device_id,
    }
