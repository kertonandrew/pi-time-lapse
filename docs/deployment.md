# Home camera deployment

Use a trusted laptop or home server to push tested application bundles over SSH.
The Pi runs systemd jobs and retains local configuration, photographs and metrics.
It needs no deployment daemon, Git checkout, build tools or cloud credentials.
For one or a few home cameras, this provides a practical maintenance workflow
without adding an always-running service to a battery-powered Pi Zero.
The operator tools require Python 3.11 or newer and OpenSSH on Linux or macOS;
Windows operators should use a Linux environment such as WSL.

## Private setup

`local/deployment/` is the default operator workspace. It is ignored by Git;
directories are private and configuration files use mode 0600.

| File | Purpose |
| --- | --- |
| `profile.json` | Device identity, SSH connection and staging/disk settings |
| `camera.json` | Camera base settings, transfer destination and power-policy settings |
| `home-assistant.json` | MQTT identity and references to credential files |
| `hardware.env` | Recorder environment, including its authoritative telemetry database |
| `SETUP.md`, `inventory.json`, `status.json` | Optional operator notes and observed device state |
| `runs/` | Private deployment receipts and SSH diagnostics |
| `releases/` | Local copies of tested deployment archives |

Existing device snapshots are observations, not automatic deployment inputs.
An unavailable broker, server or credential must remain unconfigured. Private
keys stay in the SSH agent or an existing key file; the deployment profile holds
references and never embeds a private key. Keep an encrypted backup of this folder
and the credential files separately from the public repository.

For a new device, create the folder with explicit identity and connection values:

```sh
python3 deploy/manage.py init --directory local/deployment \
  --host camera.example.com --user camera --device-id garden-camera
python3 deploy/manage.py validate
```

`init` refuses to overwrite an existing setup. It creates inert camera and MQTT
templates and a recorder environment template. `validate` checks existing files
without contacting the device. A reserved but absent configuration file is
reported as missing, rather than filled with invented settings. The public
[`profile.example.json`](../deploy/profile.example.json) documents the profile shape.

Use `--profile local/another-camera/profile.json` before the command to select
another device. SSH uses strict host-key checking, noninteractive authentication
and disabled agent forwarding. Establish the device's trusted host key during
commissioning. If connecting by a reserved IP, `host_key_alias` can retain the
trusted hostname. Relative key/known-hosts references resolve beside the profile.
SSH agent authentication and the existing known-hosts file are the defaults.

## Build, inspect and deploy

Run the tests before building a release, or download the archive produced by a
successful CI run for the intended revision. Check the CI artifact digest when
downloading; the bundle's own checksums provide integrity, not publisher identity.
Only use a trusted source or CI run. The SSH transport authenticates the device.

```sh
python3 -m unittest discover -s tests -q
python3 -m unittest discover -s hardware -p 'test_*.py' -q
python3 deploy/bundle.py --output local/deployment/releases/camera.tar.gz
python3 deploy/manage.py status
python3 deploy/manage.py plan --bundle local/deployment/releases/camera.tar.gz
python3 deploy/manage.py deploy --bundle local/deployment/releases/camera.tar.gz \
  --apply --stable-power --wait 180
```

The release archive contains only the maintained application, four camera/transfer
systemd units, and the application installer. An explicit allowlist excludes local
configuration, keys, photographs, databases, history and arbitrary untracked files.
The manifest records each file's digest and size and a content-derived release ID.
Archive verification rejects links, traversal, unexpected files, duplicate entries,
tampering and excessive sizes. Identical content and source metadata produce the
same archive. An uncommitted source tree does not claim a clean commit identity.

`plan` uploads into a temporary private staging directory and reports changed
destinations and prerequisites. It makes no managed application/configuration or
systemd changes and executes no Python from the candidate bundle. Omit `--apply`
from `deploy`, `rollback` or `recover` to preview those operations too.

`--stable-power` records the operator's confirmation of reliable external power.
The device also requires fresh external-input telemetry and free space on the
staging, application and backup filesystems. The default minimum is 128 MiB.
Telemetry must belong to this boot and be no more than 420 seconds old by both
wall clock and uptime; the window accommodates the recorder's five-minute flush.
USB presence cannot distinguish mains power from intermittent solar. Do maintenance
on the bench with reliable power. These checks do not qualify a battery or charger.

After dispatch, a transient systemd job finishes independently of the SSH session.
The installer locks against concurrent deployments, stops active application jobs,
saves a private snapshot, writes files with fsync/rename, and validates installed
imports and configuration before restoring timer states. A failed health check
triggers restoration. Disabled timers remain disabled. A no-op deployment still
runs the health check.

This pipeline updates the camera application only. It does not install the OS,
change charging/RTC settings, run APT, reboot, migrate databases, delete photos or
deploy the separate Home Assistant virtual environment. Initial hardware setup
uses the [hardware installer](../hardware/README.md); MQTT/receiver commissioning
uses the [Home Assistant](home-assistant.md) and [receiver](ssh-receiver.md) guides.

## Configuration and Home Assistant changes

Normal application deployment preserves `/etc/pi-timelapse.json`. To deliberately
apply the local base configuration, preview and then use the explicit flag:

```sh
python3 deploy/manage.py plan --bundle local/deployment/releases/camera.tar.gz \
  --include-camera-config
python3 deploy/manage.py deploy --bundle local/deployment/releases/camera.tar.gz \
  --include-camera-config --apply --stable-power --wait 180
```

The base configuration joins the same backup, validation and rollback transaction.
Changing the local snapshot alone never changes the device. Do not use an old
snapshot as a way to reset settings accidentally.

Home Assistant controls write the validated overlay referenced by
`remote_controls.state_path`; application deployments leave it intact. Each new
capture process reads the current base/overlay settings. Recorder environment
changes need a recorder restart, and MQTT configuration applies on the next
publisher/control invocation. The saved `hardware.env` and Home Assistant file are
commissioning inputs, not automatically uploaded by this camera release command.

## Status, rollback and interrupted updates

The CLI prints and saves a job ID before sending an apply request. If the connection
times out, query that job before retrying: the Pi may have completed the operation.
Private SSH diagnostics live in `local/deployment/runs/`; device details stay out of
public build artifacts.

```sh
python3 deploy/manage.py status --job JOB_ID
python3 deploy/manage.py rollback --bundle local/deployment/releases/camera.tar.gz \
  --snapshot SNAPSHOT_ID
python3 deploy/manage.py rollback --bundle local/deployment/releases/camera.tar.gz \
  --snapshot SNAPSHOT_ID --apply --stable-power --wait 180
python3 deploy/manage.py recover --bundle local/deployment/releases/camera.tar.gz \
  --snapshot SNAPSHOT_ID --apply --stable-power --wait 180
```

Successful jobs report their snapshot ID. `status` also lists recent and incomplete
snapshots so an interrupted installation can be recovered. `recover` accepts an
unfinished snapshot; normal `rollback` restores a completed deployment. Both reject
later file edits instead of overwriting them. An unfinished snapshot blocks a new
installation until recovery completes. A failed restoration leaves the snapshot
available and keeps timers stopped if restored application health cannot be proven.

On the device, job receipts are under the profile's staging directory, default
`/var/lib/pi-timelapse-deploy/JOB_ID/`; failures have a private `error.log`.
Snapshots live under `/var/backups/pi-timelapse/SNAPSHOT_ID/`. Keep the current and
previous successful release and its matching snapshots for rollback. There is no
automatic deletion of old evidence; check disk usage during maintenance before
removing completed historical jobs. Never remove an unfinished snapshot.

Per-file atomic replacement and recovery do not make this an atomic whole-system
update. An SD-card failure or power loss can still prevent Linux booting. Keep a
tested spare card/image and a backup of private provisioning data.

## CI and the next product stage

GitHub-hosted CI checks public contents, locked dependencies, application/hardware
tests and packaging on Python 3.11 and 3.13. Only after both jobs succeed does it
build, verify and upload the narrow application archive. Pull requests run checks;
archive publication runs on pushes/manual dispatch. Actions use verified immutable
commit pins and minimum permissions, with Dependabot maintaining Actions and uv.
The workflow contains no home-network credentials or automatic LAN deployment.
[GitHub's security guidance](https://docs.github.com/en/actions/reference/security/secure-use)
supports immutable action references and restricting privileged runner exposure.

For a few home devices, release one camera first, check its status and a separately
authorized functional capture, then deploy the same archive to the others. Keep OS
security maintenance separate from application releases. A home server can perform
the operator role when the laptop is away; introduce scheduled updates only after
power qualification and a maintenance-window policy have been tested.

For unattended units that must recover from a bad OS update, plan a new image with
signed updates, redundant system partitions, boot-attempt tracking and a separate
data partition. RAUC supports signed bundles and redundant system updates, but
requires partitioning and bootloader integration during provisioning; changing that
layout later is difficult. Evaluate it on a spare SD card for this exact Pi Zero
before using it remotely. [RAUC integration](https://rauc.readthedocs.io/en/latest/integration.html)
describes those requirements. Mender is an alternative when managing a larger fleet
and deployment service is useful; its exact device and storage requirements must be
checked against the chosen image. [Mender requirements](https://docs.mender.io/overview/requirements)

The current application pipeline is the first stage. A/B OS recovery, automatic
solar-window installation and a commissioned MQTT/archive server remain separate
acceptance work, rather than assumptions in the application deployment command.
