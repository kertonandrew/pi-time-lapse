"""Exercise the transport against a temporary loopback-only SSH receiver on the Pi."""

import argparse
import hashlib
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from timelapse.spool import Spool
from timelapse.transfer import transfer


def run(user):
    if os.geteuid() != 0:
        raise RuntimeError("The isolated SSH daemon requires root")
    account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise ValueError("Use an unprivileged receiver account")
    with tempfile.TemporaryDirectory(
        prefix="pi-transfer-bench-", dir="/run"
    ) as temporary:
        base = Path(temporary)
        base.chmod(0o755)
        for name in ("host", "client"):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(base / name)],
                check=True,
            )
        (base / "authorized_keys").write_text((base / "client.pub").read_text())
        (base / "authorized_keys").chmod(0o644)
        receiver = base / "receiver.py"
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "timelapse" / "receiver.py", receiver
        )
        receiver.chmod(0o644)
        destination = base / "server"
        destination.mkdir(mode=0o700)
        os.chown(destination, account.pw_uid, account.pw_gid)
        with socket.socket() as endpoint:
            endpoint.bind(("127.0.0.1", 0))
            port = endpoint.getsockname()[1]
        (base / "known_hosts").write_text(
            f"[127.0.0.1]:{port} {(base / 'host.pub').read_text()}"
        )
        configuration = base / "sshd_config"
        configuration.write_text(
            "\n".join(
                (
                    f"Port {port}",
                    "ListenAddress 127.0.0.1",
                    f"HostKey {base / 'host'}",
                    f"PidFile {base / 'sshd.pid'}",
                    f"AuthorizedKeysFile {base / 'authorized_keys'}",
                    "PasswordAuthentication no",
                    "KbdInteractiveAuthentication no",
                    "UsePAM no",
                    "PermitRootLogin no",
                    "PubkeyAuthentication yes",
                    "StrictModes yes",
                    "AuthenticationMethods publickey",
                    f"AllowUsers {user}",
                    "AllowTcpForwarding no",
                    "AllowAgentForwarding no",
                    "X11Forwarding no",
                    "PermitTunnel no",
                    "UseDNS no",
                    "LogLevel ERROR",
                    "",
                )
            )
        )
        subprocess.run(["/usr/sbin/sshd", "-t", "-f", str(configuration)], check=True)
        with (base / "sshd.log").open("w+") as log:
            daemon = subprocess.Popen(
                ["/usr/sbin/sshd", "-D", "-e", "-f", str(configuration)],
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 10
                while True:
                    if daemon.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        raise RuntimeError(
                            f"Bench SSH listener failed: {log.read()[-2000:]}"
                        )
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)
                spool = Spool(base / "spool", min_free_bytes=64 * 1024 * 1024)
                for index in range(3):
                    payload = b"\xff\xd8" + os.urandom(1024 * 1024) + b"\xff\xd9"
                    pending = spool.images / f".capture-bench{index}.part"
                    pending.write_bytes(payload)
                    with pending.open("rb") as stream:
                        os.fsync(stream.fileno())
                    metadata = {
                        "filename": f"bench-{index}.jpg",
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                        "time_source": "NTP",
                        "boot_id": "bench",
                        "capture_duration_seconds": 0,
                    }
                    with spool.lock():
                        spool.publish(pending, metadata)
                config = {
                    "host": "127.0.0.1",
                    "port": port,
                    "user": user,
                    "remote_root": str(destination),
                    "device_id": "transport-bench",
                    "identity_file": str(base / "client"),
                    "known_hosts_file": str(base / "known_hosts"),
                    "receiver_path": str(receiver),
                    "file_timeout_seconds": 60,
                }
                first = transfer(spool, config, lambda: True, max_seconds=120)
                if (
                    first["status"] != "complete"
                    or first["uploaded_files"] != 3
                    or spool.list_pending()
                ):
                    log.seek(0)
                    raise RuntimeError(
                        json.dumps({"transfer": first, "sshd": log.read()[-2000:]})
                    )
                retry = transfer(spool, config, lambda: True, max_seconds=30)
                if retry["status"] != "idle":
                    raise RuntimeError(f"Unexpected completed-batch retry: {retry}")
                (spool.receipts / "bench-0.json").unlink()
                recovery = transfer(spool, config, lambda: True, max_seconds=60)
                if (
                    recovery["recovered_receipts"] != 1
                    or recovery["uploaded_files"] != 0
                    or spool.list_pending()
                ):
                    raise RuntimeError(f"Receipt recovery failed: {recovery}")
                return {
                    "first": first,
                    "idle_retry": retry,
                    "lost_receipt_recovery": recovery,
                    "retained_local_images": len(list(spool.images.glob("*.jpg"))),
                    "scope": "Loopback SSH transport and receiver; synthetic bytes, no solar or image processing",
                }
            finally:
                daemon.terminate()
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--user", required=True)
    args = parser.parse_args()
    if not args.run:
        print(
            "Will use temporary keys, an isolated loopback SSH daemon, three synthetic files, and remove the complete bench directory on exit."
        )
        return 0
    print(json.dumps(run(args.user), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
