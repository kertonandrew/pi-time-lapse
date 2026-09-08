# Capture and transfer runbook

This application captures original JPEGs, keeps a durable local queue and transfers
bounded batches to an SSH server when configured power conditions permit it. Image
processing is deferred. It does not program PiJuice alarms, halt Linux or cut power;
the timers run only while the Pi is powered.

The battery profile, external power sensing and physical off/wake commissioning
remain dependencies described in the [hardware baseline](../hardware/README.md).
HAT status alone cannot measure solar-panel input watts. See the
[solar transfer design](solar-transfer-design.md) for measurement boundaries,
full-battery behavior and future off-Pi probing.

## Install and inspect

Run these from the repository root on the Pi:

```sh
sudo python3 deploy/install.py
sudo python3 deploy/install.py --apply
cd /usr/local/lib/pi-timelapse
sudo python3 -m timelapse --config /etc/pi-timelapse.json capture --dry-run
sudo python3 -m timelapse --config /etc/pi-timelapse.json status
```

The installer previews by default. Applying installs the package under
`/usr/local/lib/pi-timelapse/timelapse`, four systemd units, and a mode-0600
`/etc/pi-timelapse.json` only if it is absent. An existing configuration is preserved.
Initial timers remain disabled; updates preserve their previous enabled/active state.
The application installer accepts ARM Raspberry Pi hardware on Debian/Raspberry Pi
OS Bookworm or Trixie with Python 3.11 or newer and systemd. Physical measurements
cover the original Pi Zero W only. The separate hardware installer remains limited
to that reviewed Zero W/Bookworm target. A working hardware recorder is required
before real capture.

Start with the private configuration generator in [the quickstart](../README.md).
For a fresh installation, edit its `local/camera.json`, validate it with `doctor`,
and install it as `/etc/pi-timelapse.json`, owner root, mode 0600. Keep an existing
installation's configuration when upgrading. JSON files must be regular files, not
symlinks, and at most 64 KiB. Configuration can supply a partial object; omitted
values use the documented defaults.

`capture --dry-run` displays the requested capture without reading hardware or
creating the image queue. A normal capture checks the latest hardware record before
starting the camera. The record must be from the current boot and no more than
420 seconds old, allowing for the recorder's five-minute database commits. With a
battery present, capture additionally requires the verified-profile setting and
configured reserve. With no battery, a confirmed external input is required.

To capture one photograph and inspect the result:

```sh
cd /usr/local/lib/pi-timelapse
sudo python3 -m timelapse --config /etc/pi-timelapse.json capture
sudo python3 -m timelapse --config /etc/pi-timelapse.json status
sudo journalctl -u pi-timelapse-capture.service -n 30 --no-pager
```

Capture emits its filename, SHA-256, size, timestamp, time-quality label, boot ID and
duration. Fresh defaults use the camera's native resolution (`width: 0`, `height: 0`),
rotation 0 and JPEG quality 90. Set both dimensions together for a fixed size.
Existing configured dimensions and rotation are preserved on upgrade. Confirm the
image visually after mounting. A 45-second process
timeout bounds camera execution; failed or incomplete output is not acknowledged as
a successful capture.

## Timers and retention

After checking one real image, set `schedule.enabled` to `true` and
`schedule.interval_seconds` to your desired interval (default 300), then enable
the timer:

```sh
sudo systemctl enable --now pi-timelapse-capture.timer
systemctl list-timers 'pi-timelapse-*' --no-pager
```

The timer first runs two minutes after boot and checks again one minute after each
invocation finishes while powered.
`scheduled-capture` checks the configured interval (60–86400 seconds) and takes at
most one photograph when due. Attempts, including rejected or failed captures, are
spaced by that interval. It does not replay missed photographs after downtime.
The scheduler uses monotonic time within a boot and serializes concurrent attempts.
Manual `capture` remains available when scheduled capture is disabled.

Intervals are minimum spacing, not exact wall-clock appointments: the next check
can be delayed by capture duration and the timer interval. Scheduling from service
completion also handles an invocation that outlasts a one-minute tick. See
[systemd timer semantics](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml).

On upgrade, an older configuration with no `schedule` section now defaults to
disabled scheduled capture: opt in explicitly. Remote settings, when enabled, are
read once per attempt; an in-progress photograph finishes with its original
settings. The [Home Assistant guide](home-assistant.md) explains remote opt-in and
the additional bounded polling service. A Home Assistant schedule switch cannot
start an operating-system timer that an administrator has disabled.

The default spool is `/var/lib/pi-timelapse`:

| Path | Content |
| --- | --- |
| `images/` | Original JPEGs, including already acknowledged images |
| `metadata/` | Immutable capture manifests |
| `receipts/` | Verified server acknowledgments |
| `power-history.sqlite3` | Observations and admission state |
| `transfer-state.json` | Attempt/probe cooldown state |
| `schedule-state.json` | Last scheduled attempt and boot identity |

Images, manifests and receipts have a combined 2 GiB limit. Capture also preserves
512 MiB of filesystem free space and reserves room for the largest permitted image
before starting. Consequently, it can stop before the measured folder size reaches
exactly 2 GiB. Existing files remain intact when capacity is reached.

**Nothing is automatically deleted after upload.** `pending_images: 0` means the
server acknowledged the queue; it does not mean local space was reclaimed. Operator
retention tooling is still pending. Before any manual cleanup, verify the server
copy and its matching receipt, and retain any unacknowledged originals. Inspect
usage with `sudo du -sh /var/lib/pi-timelapse` and `df -h /var/lib/pi-timelapse`.

The transfer timer is a separate opt-in. After its prerequisites are commissioned,
it first runs four minutes after boot and then every five minutes. Each invocation
records an observation through `probe`, then considers an upload. Do not enable it
merely to obtain battery monitoring; `pi-hardware.service` already provides that.

```sh
sudo systemctl enable --now pi-timelapse-transfer.timer
sudo journalctl -u pi-timelapse-transfer.service -n 50 --no-pager
```

Stop scheduled work with:

```sh
sudo systemctl disable --now pi-timelapse-capture.timer pi-timelapse-transfer.timer
```

Stopping timers prevents future activations; a currently running service may finish.
The uploader independently limits its batch and stops on lost power eligibility.

## Configuration

The installed file begins with the following defaults. Percentages, dwell/gap,
freshness and transfer budgets are commissioning starting points, not measured safe
limits for a particular battery. Automatic upload remains unavailable until
the exact pack profile and the power thresholds have been validated. Unknown keys and invalid
numeric values are rejected.

```json
{
  "spool": "/var/lib/pi-timelapse",
  "max_spool_bytes": 2147483648,
  "min_free_bytes": 536870912,
  "hardware_database": "/var/lib/pi-hardware/metrics.sqlite3",
  "minimum_capture_charge_percent": 20,
  "camera": {
    "camera_command": "/usr/bin/rpicam-still",
    "timeout_seconds": 45,
    "settle_ms": 1000,
    "width": 0,
    "height": 0,
    "rotation": 0,
    "quality": 90
  },
  "schedule": {
    "enabled": false,
    "interval_seconds": 300
  },
  "remote_controls": {
    "enabled": false,
    "device_id": null,
    "state_path": "/var/lib/pi-timelapse-controls/settings.json"
  },
  "power": {
    "sensor_path": "/run/pi-power/latest.json",
    "max_age_seconds": 90,
    "battery_profile_verified": false,
    "minimum_input_w": null,
    "stop_input_w": null,
    "maximum_battery_discharge_w": null,
    "start_charge_percent": 85,
    "stop_charge_percent": 75,
    "start_peak_fraction": 0.8,
    "continue_peak_fraction": 0.65,
    "sustained_samples": 3,
    "maximum_observation_gap_seconds": 900,
    "allow_load_probe": false,
    "probe_minimum_input_w": null,
    "probe_cooldown_seconds": 1800
  },
  "transfer": {
    "max_bytes": 33554432,
    "max_seconds": 120,
    "probe_max_bytes": 2097152,
    "probe_max_seconds": 15,
    "retry_cooldown_seconds": 1800
  },
  "server": null
}
```

Set `minimum_input_w` to the measured start threshold and `stop_input_w` to the
separately validated lower stop threshold. Both are delivered HAT-input watts.
`maximum_battery_discharge_w` is the calibrated permitted battery contribution, with
positive power meaning discharge. It is not a charger-current setting. A full
battery with zero charging current can still qualify.

Entry requires distinct sustained observations, sufficient reserve and high input
relative to the observed peak of the current admission history. The active transfer
uses the lower stop and relative-peak thresholds. Missing/replayed/stale data stops
eligibility. `allow_load_probe` permits a small real upload below the normal start
threshold; enable it only after calibrating the probe floor, budget and battery
reserve. The separate CLI `probe` command records and evaluates an observation; it
does not send images by itself.

Custom spool locations also require corresponding systemd `ReadWritePaths` changes.
The supplied services permit writes to the default spool only. They run as root to
read the hardware database and configured transfer key, with filesystem restrictions
and reduced CPU/I/O priority.

## Sensor adapter contract

The adapter and calibrated external sensors are not provided by the current camera
package. They must atomically publish a JSON object at `power.sensor_path`. The
following describes every required field; these are example values, not readings
to install or replay:

```json
{
  "sample_id": "unique-observation-id",
  "sequence": 42,
  "timestamp_utc": "2026-09-07T12:00:00+00:00",
  "observed_uptime_seconds": 1234.5,
  "pi_boot_id": "the-current-proc-kernel-boot-id",
  "sensor_session_id": "new-id-after-each-sensor-reset",
  "calibration_id": "verified-calibration-revision",
  "quality": "calibrated",
  "source": "solar",
  "time_source": "RTC",
  "input_healthy": true,
  "battery_healthy": true,
  "errors": [],
  "solar_input_w": 4.0,
  "battery_power_w": 0.0,
  "battery_charge_percent": 95.0
}
```

`sample_id` is globally unique and immutable. `sequence` strictly increases within
the sensor session; it is a nonnegative signed-64-bit integer. Session and calibration
changes clear admission history. All identifiers must be nonempty strings of at
most 128 characters. Use the actual `/proc/sys/kernel/random/boot_id` and acquisition
uptime from `/proc/uptime`; do not reset acquisition time when a consumer rereads the
file. Both UTC and monotonic ages must be within `max_age_seconds`, with no future
timestamp. Valid `time_source` values are `NTP` and `RTC`.

The trusted adapter must derive `input_healthy` and `battery_healthy` from verified
wiring, calibration, the exact battery profile, valid voltage/temperature limits and
fault status. These booleans are evidence supplied by the adapter, not extra checks
that the camera package can derive from the three numeric fields. Unknown or failed
measurements must set errors/health accordingly, never fabricate a zero.

The adapter must refresh fast enough to observe a short load probe; a 60-second
sample interval can miss the entire default 15-second probe. Commission a suitable
under-load sampling rate and freshness limit with the sensors. Rechecking an old
record once per second does not create a new physical measurement.

`solar_input_w` is nonnegative calibrated power delivered to the HAT through the
known solar feed. `battery_power_w` is calibrated signed battery-terminal power:
positive for discharge, negative for charge. Charge percentage remains an estimate
from the validated fuel-gauge/profile arrangement, not a direct solar reading.
PiJuice's existing current estimate cannot substitute for a calibrated battery
power measurement in this contract.

Buffered off-Pi samples must retain their original age and session; importing a
backlog cannot relabel it as fresh. The current-boot monotonic fields need an adapter
mapping that conservatively includes acquisition age and transport delay. The current
SQLite hardware history is deliberately batched and is not this live sensor feed.

Inspect admission without sending images:

```sh
cd /usr/local/lib/pi-timelapse
sudo python3 -m timelapse --config /etc/pi-timelapse.json probe
sudo python3 -m timelapse --config /etc/pi-timelapse.json upload --dry-run
```

These commands may create/update local observation history. `upload --dry-run`
does not contact the server. Invalid observations create a persistent admission
barrier; repeatedly reading the last valid sample cannot rebuild a sustained streak.
An unchanged current sample creates no new history record. Profile activity counts
separate observations first recorded while idle, probing or uploading; the reported
strongest half-hour is an observed, demand-biased interval, not the daily solar maximum.

## Provision an SSH receiver

Use a maintained Linux server with Python 3.11 or newer, OpenSSH and rsync. Follow
[the restricted receiver guide](ssh-receiver.md) to install the complete package,
create a separate unprivileged account per camera and restrict accepted commands.
The `restrict` key option alone permits arbitrary remote commands; the supplied
gateway adds a fixed device, destination and command allowlist.

Use `camera01` consistently in that guide and your local configuration, or replace
it everywhere with your device ID. On the Pi, generate a dedicated transfer key
only if one does not already exist:

```sh
sudo install -d -m 0700 /etc/pi-timelapse/ssh
sudo ssh-keygen -t ed25519 -N '' -f /etc/pi-timelapse/ssh/id_ed25519
sudo cat /etc/pi-timelapse/ssh/id_ed25519.pub
```

Install that public key through a trusted server administration connection as
described in the receiver guide. Also obtain the server's host public key and
verify its fingerprint through that trusted connection. Create the Pi's
`/etc/pi-timelapse/ssh/known_hosts` with mode 0600. For port 22 its line is
`images.example.net ssh-ed25519 REPLACE_WITH_VERIFIED_HOST_PUBLIC_KEY`;
for another port use `[images.example.net]:PORT`. Do not accept an unknown host key
unattended or disable host-key verification.

Replace the local `server: null` value with:

```json
{
  "host": "images.example.net",
  "port": 22,
  "user": "timelapse_camera01",
  "remote_root": "/srv/pi-timelapse",
  "device_id": "camera01",
  "identity_file": "/etc/pi-timelapse/ssh/id_ed25519",
  "known_hosts_file": "/etc/pi-timelapse/ssh/known_hosts",
  "receiver_path": "/usr/local/lib/pi-timelapse-receiver/receiver.py",
  "file_timeout_seconds": 60,
  "ssh_gateway": true
}
```

`receiver_path` must match the gateway's configured command token; the gateway
executes its installed module instead. Server paths must be absolute without
spaces or traversal. The identity and known-hosts files must exist before a real
transfer. Destination setup alone does not enable upload; the calibrated sensor
and power gates must also qualify. Keep the transfer timer disabled until then.

Published originals appear in `/srv/pi-timelapse/camera01/images`, with manifests and
receipts in sibling directories. Each upload batch normally uses **three SSH
connections**: initialize/recover receipts, one rsync transfer, then commit the batch.
An already committed batch can recover missing local receipts during initialization
without retransmitting its images.

Rsync resumes private partial files and transport compression is disabled. The
receiver verifies size and SHA-256, publishes durably and returns matching receipts
before local completion is recorded. Neither the uploader nor receiver renders or
processes images. The 32 MiB batch allowance counts selected original sizes, including
resumed files; it is not an exact wire-byte or energy limit. The overall default is
120 seconds and each subprocess is capped at 60 seconds. Eligibility is rechecked
approximately once per second during transfer.

## Bench scope and rollback

The isolated transport exercise runs from the Pi's source checkout:

```sh
sudo python3 -m ops.bench_transfer --user example.user
sudo python3 -m ops.bench_transfer --user example.user --run
```

It uses temporary keys and a loopback-only SSH listener to transfer three synthetic
payloads, test an idle retry and recover a deliberately removed local receipt. The
temporary receiver and spool are removed afterward. This tests the transport and
durability protocol; it does not measure Wi-Fi energy, exercise solar admission or
prove the bytes are decodable photographs. Use a real camera capture separately.

An installer apply prints a snapshot directory under `/var/backups/pi-timelapse`.
From the same source checkout, preview and apply a specific rollback using that exact
reported directory:

```sh
sudo python3 deploy/install.py --rollback /var/backups/pi-timelapse/TIMESTAMP_UUID
sudo python3 deploy/install.py --rollback /var/backups/pi-timelapse/TIMESTAMP_UUID --apply
```

Rollback restores the managed application/configuration/unit files and recorded timer
states, while refusing to overwrite later edits. It does not delete captures,
manifests, receipts or power history. Keep a separate private backup before
changing hardware, firmware or operating-system configuration. Restore those
changes from the matching hardware installation backup; application rollback
does not restore firmware or operating-system configuration.
