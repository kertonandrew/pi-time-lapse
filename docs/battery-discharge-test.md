# Bounded battery discharge trial

This opt-in diagnostic records a five-minute battery-only trial: two minutes idle,
up to 30 seconds of CPU work, then recovery. It never enables charging or changes
the battery profile. It is a first operating check, not a capacity test.

Use a regulated USB supply into the PiJuice, an attached battery and no separate
power connection into the Pi. Disable scheduled captures and transfers first.
Verify that charging is disabled, battery readings are credible, and RTC or other
wake sources will not restart the Pi after the test. A battery start requires at
least 3.90 V. The experiment stops below 3.80 V, outside 10–35°C reported battery
temperature, outside 4.80–5.25 V at the Pi rail, or on invalid sensor/source data.
CPU work also stops at 70°C CPU temperature.

Install `hardware/battery_test.py` as
`/usr/local/lib/pi-time-lapse/battery_test.py` and the service as
`/etc/systemd/system/pi-battery-test.service`, owned by root. Create
`/etc/default/pi-battery-test`, mode 0600, with a new absolute output directory
for every run:

```sh
BATTERY_TEST_OUTPUT=/var/log/pi-battery-tests/trial-001
```

The output directory must not already exist. Start the single fixed unit:

```sh
sudo systemctl daemon-reload
sudo systemctl start pi-battery-test.service
sudo journalctl -u pi-battery-test.service
```

The unit waits up to 15 minutes for USB removal while recording readings every
two seconds. Unplug only the USB cable into the PiJuice; leave its battery
connected. CPU work starts only after the HAT reports battery-only power.
Reconnect USB to stop the trial early. If USB is present when the trial stops,
the Pi stays running. Otherwise it shuts down and asks the HAT to cut Pi power;
press the PiJuice power button to restart after reconnecting USB if necessary.

The controller renews a HAT power-off countdown every 15 seconds and independently
reads it back. Countdown code 120 is approximately 123 seconds on firmware 1.6.
This provides a fallback for loss of the controller while discharging. It is
conditional on functioning HAT firmware and no external GPIO backfeed. The
systemd unit also bounds its runtime and invokes cleanup after the main process
exits. Both entry points share a lock, and cleanup rejects markers from an older
boot. Do not run overlapping units or manually reuse an old output directory.

`samples.jsonl` retains decoded API responses, source status, reported temperature,
CPU temperature, phase changes, monotonic timestamps and read durations.
Register 0x42 preserves the HAT's 0.1% charge-level resolution. `result.json`
records why the trial ended. Capture-phase measurements need a subsequent
dedicated photo trial; CPU work does not establish photo energy consumption.

PiJuice's charge level and battery-current readings are estimates. Reported
temperature can fall back to the MCU sensor. Pi-rail current readings outside
the trial's accepted range are flagged and excluded from power integration. Five
consecutive outliers stop the trial as a measurement failure; CPU work only starts
on a valid current reading without extending its fixed phase window. This
test does not establish full startup energy, cell capacity, charging limits or
solar input efficiency. The HAT power-off countdown does not disable charging.
