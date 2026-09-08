# Restricted SSH receiver

The optional SSH gateway limits one camera key to that camera's upload protocol.
It accepts the uploader's exact `init`, `commit`, and `commit-batch` requests and a
small rsync upload command profile. It rejects shells, downloads, deletion options,
other devices, caller-selected executables, and caller-selected storage paths.

This is a command boundary, not a filesystem sandbox. Run it as a separate
unprivileged account per camera on a maintained Linux server. A compromised rsync
or Python process still has that account's filesystem permissions, including its
committed archive. Use a dedicated filesystem/container or OS confinement for a
stronger boundary. Configure storage quotas and SSH connection limits separately;
per-command limits do not bound lifetime disk use or repeated connections.

## Install the receiver package

Use a reviewed checkout or wheel on the receiving server:

```sh
sudo python3 -m venv /opt/pi-timelapse-receiver
sudo /opt/pi-timelapse-receiver/bin/pip install --no-deps .
sudo /opt/pi-timelapse-receiver/bin/python3 -I -m timelapse.ssh_gateway --help
```

The virtual environment, interpreter, installed package, and their parent
directories must be root-owned and not writable by the ingest account or other
unprivileged users. Install updated distro OpenSSH security packages. Both the
camera and receiver need rsync 3.5.0 or a vendor package with the applicable
[rsync security fixes](https://rsync.samba.org/security.html) backported. Check the
vendor advisory against the full installed package revision.

The gateway needs Linux `/proc/self/fd` and POSIX resource limits. The recorded
compatibility test used rsync 3.4.1; rerun the receiver acceptance checks with the
patched package before deployment. Apple's openrsync is not this server profile.

Unlike the older standalone `receiver.py` installation, this setup needs the
complete installed `timelapse` package. Both gateway and receiver use isolated
Python (`-I -m`), which excludes the working directory, user site-packages, and
`PYTHONPATH` from import resolution. Never run from an ingest-writable checkout.

## Create an account for one camera

Replace `camera01` consistently with your configured device identifier. These
commands create a new account and archive; adapt ownership carefully for an
existing archive rather than recursively changing an unrelated directory.

```sh
sudo useradd --system --no-create-home --home-dir /var/empty/timelapse_camera01 --shell /bin/dash timelapse_camera01
sudo usermod --password '*' timelapse_camera01
sudo install -d -o root -g root -m 0755 /var/empty/timelapse_camera01
sudo install -d -o root -g root -m 0755 /srv/pi-timelapse
sudo install -d -o timelapse_camera01 -g timelapse_camera01 -m 0700 /srv/pi-timelapse/camera01
sudo install -d -o root -g root -m 0755 /etc/pi-timelapse-receiver
sudo install -d -o root -g root -m 0755 /etc/ssh/pi-timelapse-keys
```

Give this account no sudo rights, supplementary access to other archives, writable
home directory, or other login mechanism. `dash` is needed because OpenSSH invokes
forced commands through the account's shell; `/usr/sbin/nologin` prevents the
gateway from starting. The SSH configuration below denies interactive access.
Avoid a shell that sources writable startup files before the forced command.

Provision an account or filesystem quota appropriate for retention, plus a server
SSH connection-rate policy. The gateway permits only one active command per
device, rejecting competing commands so the uploader can retry later. The
device directory must already exist; the first allowed `init` creates its private
`incoming`, `images`, `metadata`, and `receipts` subdirectories.

## Fixed local gateway configuration

Create `/etc/pi-timelapse-receiver/camera01.json`, owned by root, mode 0644:

```json
{
  "root": "/srv/pi-timelapse",
  "device_id": "camera01",
  "receiver_path": "/usr/local/lib/pi-timelapse-receiver/receiver.py",
  "python_path": "/opt/pi-timelapse-receiver/bin/python3",
  "rsync_path": "/usr/bin/rsync",
  "timeout_seconds": 120
}
```

`receiver_path` is the exact receiver command token configured on the camera. It
is compared with the SSH request, then replaced with the fixed installed module;
the gateway never executes that script path. The root, device, interpreter, and
rsync executable come only from this local file. Configuration and its parents
must be root-owned, without group/world write access or symlinks. It contains no
secret and must be readable by the ingest account. Configuration is capped at
4096 bytes and rejects unknown or duplicate keys.

## Restrict SSH authentication and commands

Put the camera's dedicated public key into
`/etc/ssh/pi-timelapse-keys/timelapse_camera01`, root-owned, mode 0644. Prefix its
single key line with `restrict`:

```text
restrict ssh-ed25519 REPLACE_WITH_CAMERA_PUBLIC_KEY camera01
```

`restrict` alone does not restrict remote commands. Add this matching-user block
to the server's SSH configuration, using its supported include mechanism:

```text
Match User timelapse_camera01
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AuthorizedKeysFile /etc/ssh/pi-timelapse-keys/%u
    ForceCommand /opt/pi-timelapse-receiver/bin/python3 -I -m timelapse.ssh_gateway --config /etc/pi-timelapse-receiver/camera01.json
    DisableForwarding yes
    PermitTunnel no
    PermitTTY no
    PermitUserRC no
    MaxSessions 1
```

Keep the global `PermitUserEnvironment` setting disabled. Check `sshd -t` and the
effective matching-user settings with `sshd -T -C user=timelapse_camera01,host=localhost,addr=127.0.0.1`
before reloading SSH. Keep an existing administrator connection open while
verifying the new configuration. A server-wide forced command also constrains
future keys or certificates accepted for that user; a key prefix alone constrains
only that particular key.

Keep `PermitTunnel no` explicit: older OpenSSH versions do not consistently apply
`restrict` or `DisableForwarding` to tunnel forwarding. See the
[OpenSSH 10.5 security fixes](https://www.openssh.org/txt/release-10.5).

An equivalent `command="/opt/pi-timelapse-receiver/bin/python3 -I -m timelapse.ssh_gateway --config /etc/pi-timelapse-receiver/camera01.json"`
can be combined with `restrict` on an individual key when server configuration
cannot be changed. In that case, ensure no other key or authentication method
grants the same account a shell, and keep the authorized-keys file root-controlled.

Continue to pin the server host key on the camera as described in
[the camera workflow](camera-workflow.md). The gateway does not replace host-key
verification or protect a client connected to an impostor server.

## Enable the uploader's gateway mode

In the camera's local `server` configuration, set:

```json
{
  "host": "images.example.net",
  "user": "timelapse_camera01",
  "port": 22,
  "device_id": "camera01",
  "remote_root": "/srv/pi-timelapse",
  "receiver_path": "/usr/local/lib/pi-timelapse-receiver/receiver.py",
  "identity_file": "/etc/pi-timelapse/ssh/id_ed25519",
  "known_hosts_file": "/etc/pi-timelapse/ssh/known_hosts",
  "ssh_gateway": true
}
```

The default remains `false` for existing unrestricted receiver installations.
Gateway mode explicitly passes `--no-protect-args`: all server options and the
destination must be visible to the gateway. Existing strict ASCII destination
and filename validation remains in force. `--protect-args` would hide additional
options inside the rsync protocol and is rejected, as it is by upstream
[restricted rsync](https://download.samba.org/pub/rsync/rrsync.1).

The tested command generated by rsync 3.4.1 is:

```text
rsync --server -cRe.LsfxCIvu --timeout=15 --partial-dir .rsync-partial . /srv/pi-timelapse/camera01/incoming/
```

The gateway also recognizes the `-cRe.iLsfxCIvu` compatibility form. Every other
argument sequence fails closed. A client upgrade that changes the sequence needs
a reviewed compatibility test; do not add broad flag or shell passthrough.

## Storage and execution limits

The gateway rebuilds subprocess arguments, clears inherited environment variables,
never executes a shell, and refuses to run as root. Rsync uses a pinned incoming
directory as its working directory and a pinned private partial directory. It
disables link, device, and special-file preservation and does not allow inplace
writes, backup paths, log paths, remote file lists, deletion, or sender mode.
Individual files are limited to 64 MiB, and workers have CPU, memory, descriptor,
core-dump, and wall-time limits. Each command defaults to 120 seconds; local
configuration can choose 5–300 seconds. The existing rsync inactivity limit is
15 seconds. Interrupted gateway processes clean up their worker process group.

The rsync upload protocol still exchanges information used for resuming existing
incoming files. It is not a general read API. Safety against malicious binary
rsync protocol messages also depends on a patched rsync implementation; the
gateway does not independently parse that protocol. Only receiver commits
validate JPEG filenames, metadata, and hashes before acknowledgement. Raw uploads
can consume uncommitted incoming storage, so quotas remain necessary.

No direct SSH `reindex` operation is allowed. An administrator repairs or migrates
the archive locally using the installed receiver, under the archive's owning
account. Preserve the existing durable-receipt rule: never delete a camera's
local photo because an rsync process alone reported success.

The isolated Linux verification uses synthetic keys and files with networking
disabled except container loopback. It exercises the actual uploader through
OpenSSH, rsync, and durable receipt verification, then checks rejection of shell,
read, delete, another-device, and hidden-argument commands. This does not certify
an untested deployment's SSH configuration, package updates, or filesystem quotas.

See [OpenSSH authorized-key restrictions](https://man.openbsd.org/sshd#AUTHORIZED_KEYS_FILE_FORMAT)
for the distinction between `restrict` and a forced command.
