# PiJuice hardware support

This directory provides clock recovery, power recording and an opt-in installer
for Raspberry Pi Zero W with 32-bit Raspberry Pi OS Bookworm and PiJuice.
Installation on other hardware or operating systems requires a reviewed setup;
read the installer's preflight report before applying changes.

## Install and configure

Install PiJuice software and enable I2C first. Preview installation on the Pi:

```sh
sudo python3 hardware/install.py --wifi-early-load
sudo python3 hardware/install.py --wifi-early-load --apply
```

Omit `--wifi-early-load` to preserve the existing early Wi-Fi loading configuration.
The installer preserves monitor environment overrides and unrelated firmware
settings. It saves a snapshot under `/var/backups/pi-hardware-installer/` before
changing managed files and services. Reboot separately to apply firmware,
user-session, watchdog and early-module settings.

The installer configures a headless system, disables selected unused audio,
display and Bluetooth services, and retains Wi-Fi, SSH, camera support and dynamic
CPU frequency control. Review the preview for the exact changes on your device.
It does not select or qualify a battery charging profile. Configure battery
chemistry, charge limits and thermistor parameters from the pack's specifications.

## Clock recovery

Keep the PiJuice RTC in UTC and configure Linux's display timezone independently.
The supported RTC overlay is:

```ini
dtoverlay=i2c-rtc,ds1307
```

PiJuice's maintainer recommends [DS1307 instead of DS1339](https://github.com/PiSupply/PiJuice/issues/770)
because DS1339 operations can clear wake alarms during clock updates.
`pijuice_clock.py` repairs a reset or drifting RTC only after the current boot's
NTP synchronization marker appears. It verifies UTC readback, can bind a previously
failed RTC device and does not program wake alarms. A valid RTC is preserved when
NTP is unavailable. Complete loss of HAT power can lose RTC time; a software clock
file cannot reconstruct elapsed time after that loss.

Clock recovery runs inside `pi-hardware.service`. The standalone
`pijuice-clock.timer` is disabled to avoid duplicate checks; its service remains
available for a manual check.

## Recorder configuration

`pi-hardware.service` uses the installed PiJuice library and Python's standard
library. Its environment file is `/etc/default/pi-hardware`:

```ini
PI_HARDWARE_INTERVAL=60
PI_HARDWARE_FLUSH_SECONDS=300
PI_HARDWARE_RETENTION=43200
PI_HARDWARE_DATABASE=/var/lib/pi-hardware/metrics.sqlite3
```

The default records once per minute while Linux is awake, retaining 43,200 samples.
A five-second interval is available for bounded diagnostics; restore normal
sampling afterward. Retention follows record order rather than wall-clock age.
Samples include raw API replies, errors, battery and input states, CPU temperature,
load, memory, boot ID, uptime and clock quality. Failed readings remain empty.

Pending records normally flush every five minutes. The first sample, power/fault
changes and graceful shutdown flush immediately. Sudden power loss can lose the
pending batch; a bounded queue retains records during temporary storage errors.
No energy is interpolated across power-off gaps.

Export and analyze data into the ignored private workspace:

```sh
mkdir -p local/metrics
ssh camera@camera.local 'sudo python3 /usr/local/lib/pi-time-lapse/power_monitor.py --export-csv' > local/metrics/power.csv
python3 hardware/analyze_metrics.py local/metrics/power.csv > local/metrics/report.json
```

Use your configured SSH identity and host. Exports contain committed records;
pending RAM samples appear after flushing. Use `--expected-interval 5` for an
export sampled every five seconds. Analysis separates boots and recorder sessions,
flags errors, gaps and clock jumps, and does not derive watt-hours from sparse data.

## Measurement boundaries

| Reading | Interpretation |
| --- | --- |
| GPIO/Pi voltage and current | Flow across the PiJuice-to-Pi 5 V connection |
| Battery voltage | Reported battery terminal voltage |
| Battery current and charge level | Firmware estimates, not calibrated throughput |
| Battery temperature | Depends on configured sensing and firmware fallback |
| USB/GPIO source status | Presence and quality, not USB input watts |

Direct USB power into the Pi bypasses the HAT's Pi-rail measurement boundary.
The standard API does not expose solar USB input voltage and current. Linux cannot
record before its recorder starts or while the Pi is powered off. See the
[measurement design](power-measurement-plan.md) for independent instrumentation,
energy accounting and solar/battery sizing.

## Watchdog, shutdown and wake

The optional systemd watchdog configuration uses a 15-second BCM2835 hardware
watchdog. Activation requires `systemctl daemon-reexec` or a reboot. It covers
systemd's operation, not early firmware boot, lost Wi-Fi or an individual stalled
camera process while systemd remains healthy. Orderly final shutdown disarms it.

This recorder does not implement hibernation, program RTC wake alarms, cut Pi power
or manage charging. Verify the complete power-off and automatic wake sequence with
your wiring before relying on a separate duty-cycle controller. A disk-image
backup is not a resumable RAM snapshot.

## Rollback and checks

Use the snapshot path printed by the corresponding installer run:

```sh
sudo python3 hardware/install.py --rollback /var/backups/pi-hardware-installer/SNAPSHOT
sudo python3 hardware/install.py --rollback /var/backups/pi-hardware-installer/SNAPSHOT --apply
```

Rollback restores the managed files and recorded service state immediately before
that installation and refuses to overwrite later edits. It does not reconstruct an
older operating-system image. Keep device snapshots and trial results outside Git.

```sh
python3 -m unittest discover -s hardware -p 'test_*.py' -v
```
