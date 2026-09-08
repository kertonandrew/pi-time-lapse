"""Deploy verified camera application bundles over trusted SSH connections."""

import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.bundle import verify_bundle  # noqa: E402
from deploy.profiles import (  # noqa: E402
    initialize_profile,
    load_profile,
    private_file,
    profile_file,
    write_private,
)
from timelapse.config import load_config  # noqa: E402
from timelapse.ha_config import load_config as load_ha_config  # noqa: E402


class DeploymentError(RuntimeError):
    pass


def ssh_arguments(profile, profile_path):
    ssh = profile["ssh"]
    arguments = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-p",
        str(ssh["port"]),
        "-l",
        ssh["user"],
    ]
    if ssh["host_key_alias"]:
        arguments.extend(["-o", "HostKeyAlias=" + ssh["host_key_alias"]])
    for key, option in (
        ("identity_file", "-i"),
        ("known_hosts_file", "UserKnownHostsFile"),
    ):
        if ssh[key]:
            location = Path(ssh[key]).expanduser()
            if not location.is_absolute():
                location = Path(profile_path).absolute().parent / location
            if not location.is_file():
                raise ValueError(f"ssh.{key} does not identify an existing file")
            if key == "identity_file":
                arguments.extend([option, str(location), "-o", "IdentitiesOnly=yes"])
            else:
                arguments.extend(["-o", option + "=" + str(location)])
    arguments.extend(
        [
            ssh["host"],
            shlex.join(["sudo", "-n", profile["remote"]["python"], "-I", "-B", "-"]),
        ]
    )
    return arguments


def base_request(profile, operation):
    return {
        "operation": operation,
        "staging_dir": profile["remote"]["staging_dir"],
        "min_free_mb": profile["remote"]["min_free_mb"],
    }


def validate_local(profile_path, profile):
    files = {}
    for name, loader in (
        ("camera", load_config),
        ("home_assistant", load_ha_config),
        ("hardware_env", None),
    ):
        location = profile_file(profile_path, profile, name)
        files[name] = {"present": location.exists()}
        if location.exists():
            private_file(location)
            if loader is not None:
                loader(location)
            files[name]["valid"] = True
    ssh_arguments(profile, profile_path)
    return {
        "profile_valid": True,
        "device_id": profile["device_id"],
        "files": files,
        "network_contacted": False,
    }


def operation_request(profile_path, profile, arguments):
    request = base_request(profile, arguments.command)
    if arguments.command == "status":
        request["job"] = arguments.job
        return request
    artifact = Path(arguments.bundle)
    manifest = verify_bundle(artifact)
    content = artifact.read_bytes()
    if len(content) > 8 * 1024 * 1024:
        raise ValueError("Deployment archive exceeds 8 MiB")
    import hashlib

    request.update(
        {
            "artifact": base64.b64encode(content).decode("ascii"),
            "sha256": hashlib.sha256(content).hexdigest(),
            "release_id": manifest["release_id"],
            "stable_power": arguments.stable_power,
            "apply": arguments.apply,
            "job": uuid.uuid4().hex if arguments.apply else None,
            "tools": {
                name: base64.b64encode((ROOT / name).read_bytes()).decode("ascii")
                for name in (
                    "deploy/bundle.py",
                    "deploy/install.py",
                    "deploy/remote.py",
                    "timelapse/config.py",
                )
            },
        }
    )
    if arguments.command in {"rollback", "recover"}:
        request["snapshot"] = arguments.snapshot
    if arguments.include_camera_config:
        if arguments.command in {"rollback", "recover"}:
            raise ValueError("Configuration cannot be supplied to rollback or recovery")
        location = profile_file(profile_path, profile, "camera")
        data = private_file(location)
        load_config(location)
        request["camera_config"] = base64.b64encode(data).decode("ascii")
    if arguments.command == "plan" and arguments.apply:
        raise ValueError("Use deploy --apply to apply an application release")
    if arguments.apply and not arguments.stable_power:
        raise ValueError(
            "Apply requires --stable-power after confirming reliable external power"
        )
    return request


class Connection:
    def __init__(self, profile_path, profile, run=subprocess.run):
        self.profile_path = Path(profile_path)
        self.profile = profile
        self.run = run
        self.logs = self.profile_path.absolute().parent / "runs"

    def prepare_logs(self):
        self.logs.mkdir(mode=0o700, exist_ok=True)
        if self.logs.is_symlink() or self.logs.stat().st_mode & 0o077:
            raise ValueError("Deployment log directory must be private")

    def request(self, request, timeout=120):
        source = (ROOT / "deploy/remote.py").read_text()
        script = (
            source
            + "\n\nprint(json.dumps(handle(json.loads("
            + repr(json.dumps(request))
            + ")), allow_nan=False))\n"
        )
        self.prepare_logs()
        stamp = str(time.time_ns())
        log = self.logs / (stamp + ".json")
        try:
            result = self.run(
                ssh_arguments(self.profile, self.profile_path),
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            write_private(
                log,
                (
                    json.dumps(
                        {
                            "operation": request["operation"],
                            "job": request.get("job"),
                            "timeout_seconds": timeout,
                        }
                    )
                    + "\n"
                ).encode(),
            )
            raise DeploymentError(
                f"SSH timed out; inspect the recorded job ID before retrying. Private diagnostics: {log}"
            ) from error
        write_private(
            log,
            (
                json.dumps(
                    {
                        "operation": request["operation"],
                        "job": request.get("job"),
                        "exit_code": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    },
                    indent=2,
                )
                + "\n"
            ).encode(),
        )
        if result.returncode:
            raise DeploymentError(
                f"Remote operation failed; private diagnostics: {log}"
            )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DeploymentError(
                f"Unexpected SSH response; private diagnostics: {log}"
            ) from error
        if not isinstance(response, dict):
            raise DeploymentError(
                f"Unexpected SSH response; private diagnostics: {log}"
            )
        if request["operation"] == "status" and request.get("job"):
            job = response.get("job")
            if (
                not isinstance(job, dict)
                or job.get("job_id") != request["job"]
                or job.get("state")
                not in {"queued", "running", "succeeded", "failed", "interrupted"}
            ):
                raise DeploymentError(f"Invalid job status; private diagnostics: {log}")
        if request.get("apply") and (
            response.get("job_id") != request["job"]
            or response.get("state") != "queued"
        ):
            raise DeploymentError(
                f"Invalid deployment acknowledgement; private diagnostics: {log}"
            )
        return response

    def wait(self, job, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(min(5, max(0, deadline - time.monotonic())))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            request = base_request(self.profile, "status")
            request["job"] = job
            try:
                result = self.request(request, timeout=min(120, remaining))
            except DeploymentError:
                continue
            state = result["job"]["state"]
            if state in {"succeeded", "failed", "interrupted"}:
                return result
        return {
            "job": {"job_id": job, "state": "unknown"},
            "message": "Wait expired; query status --job before starting another deployment",
        }


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--profile", type=Path, default=Path("local/deployment/profile.json")
    )
    commands = result.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser(
        "init",
        help="Create a private deployment profile and inert configuration templates",
    )
    initialize.add_argument("--directory", type=Path, default=Path("local/deployment"))
    initialize.add_argument("--host", required=True)
    initialize.add_argument("--user", required=True)
    initialize.add_argument("--device-id", required=True)
    initialize.add_argument("--broker")
    initialize.add_argument("--host-key-alias")
    commands.add_parser(
        "validate", help="Validate private files without network access"
    )
    status_parser = commands.add_parser(
        "status", help="Read device and deployment status"
    )
    status_parser.add_argument("--job")
    for operation in ("plan", "deploy", "rollback", "recover"):
        command = commands.add_parser(operation)
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--apply", action="store_true")
        command.add_argument("--stable-power", action="store_true")
        command.add_argument("--include-camera-config", action="store_true")
        command.add_argument("--wait", type=int, default=0, metavar="SECONDS")
        if operation in {"rollback", "recover"}:
            command.add_argument(
                "--snapshot",
                required=True,
                help="Snapshot directory name returned by a deployment",
            )
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    os.umask(0o077)
    try:
        if arguments.command == "init":
            output = initialize_profile(
                arguments.directory,
                arguments.host,
                arguments.user,
                arguments.device_id,
                arguments.broker,
                arguments.host_key_alias,
            )
        else:
            profile = load_profile(arguments.profile)
            if arguments.command == "validate":
                output = validate_local(arguments.profile, profile)
            else:
                if not 0 <= getattr(arguments, "wait", 0) <= 600:
                    raise ValueError("--wait must be between 0 and 600 seconds")
                request = operation_request(arguments.profile, profile, arguments)
                connection = Connection(arguments.profile, profile)
                if request.get("apply"):
                    connection.prepare_logs()
                    write_private(
                        connection.logs / (request["job"] + "-intent.json"),
                        (
                            json.dumps(
                                {
                                    "job_id": request["job"],
                                    "operation": request["operation"],
                                    "release_id": request["release_id"],
                                }
                            )
                            + "\n"
                        ).encode(),
                    )
                    print(
                        json.dumps({"job_id": request["job"], "state": "dispatching"}),
                        flush=True,
                    )
                output = connection.request(request)
                if request.get("apply") and arguments.wait:
                    output = connection.wait(request["job"], arguments.wait)
        print(json.dumps(output, indent=2, allow_nan=False))
        state = output.get("job", output).get("state")
        return (
            1 if state in {"failed", "interrupted"} else 2 if state == "unknown" else 0
        )
    except (DeploymentError, ValueError, OSError, TypeError) as error:
        print(
            json.dumps({"error": type(error).__name__, "message": str(error)}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
