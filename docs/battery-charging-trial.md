# Provisional CE06795 charging trial

This is an opt-in, five-minute compatibility experiment for the Core Electronics
CE06795 3.7 V / 1000 mAh pack and PiJuice firmware 1.6. It is not a general battery
profile or an unattended deployment setting. The experimental settings below
do not establish the pack's maximum ratings or a full-charge profile.

The trial uses 550 mA, 4.10 V and 50 mA termination, with reported-temperature
thresholds of 10/15/30/35°C for cold/cool/warm/hot. It retains the installed
1000 mAh profile's extended parameters and 10 kOhm / beta 3450 thermistor curve.
The USB input ceiling becomes 1.5 A. These are experimental settings; the 4.10 V
target leaves capacity unused and does not establish a full-charge profile.

Use regulated 5 V USB into the PiJuice. Stop the discharge trial and verify its
countdown is cancelled before preparing charging. Captures and transfers must be
inactive. Preparation refuses an unexpected firmware, incomplete battery profile
or active power-cut countdown. It preserves the original configuration before
writing a custom profile, checks the final selection and every configured value,
and leaves charging disabled.

Install `hardware/charge_test.py` as
`/usr/local/lib/pi-time-lapse/charge_test.py`, owned by root. Install the three
`hardware/pi-battery-charge-*` service/timer files in the systemd unit directory.
Create `/etc/default/pi-battery-charge-test`, mode 0600, naming a new private output
directory for each trial:

```sh
BATTERY_CHARGE_OUTPUT=/var/log/pi-battery-tests/charge-trial-001
```

Prepare and start the trial with the same output directory:

```sh
sudo python3 /usr/local/lib/pi-time-lapse/charge_test.py prepare --output /var/log/pi-battery-tests/charge-trial-001
sudo systemctl daemon-reload
sudo systemctl start pi-battery-charge-test.service
sudo systemctl status pi-battery-charge-test.service pi-battery-charge-deadline.timer
```

Start through the service so its independent deadline is armed. Each service
activation restarts the deadline timer. The controller records every two seconds
and stops on its five-minute deadline, reaching 4.10 V, changed configuration,
source loss, invalid API/numeric readings, new faults or a temperature
limit. Enablement is volatile; charging-disabled is the persistent resting state.
A single-use permit in `/run` prevents a stopped or previously used session from
enabling again. Every new trial needs a fresh preparation and output directory.

For a subsequent regulation-observation trial, add `--observe-regulation` to the
`prepare` command. That choice is recorded with the session. It allows observation
at the unchanged 4.10 V target until the five-minute deadline, retaining the
4.15 V measured-voltage stop. The baseline must still be below 4.10 V. Reaching
the target or observing a plateau does not establish full capacity.

Finite Pi-rail current outside the expected 0–500 mA idle range is retained with
quality flags and excluded from energy calculations. It is not a measurement of
cell charging current and does not alone stop charging. Persistent uncertainty in
this channel limits power conclusions even if voltage and charging behaviour are
otherwise normal.

Normal cleanup disables charging and checks both configuration and reported
battery state. `ExecStopPost` repeats that cleanup, while the separate 330-second
deadline stops the service and invokes charge-disable independently. Runtime and
stop timeouts bound the service processes. To stop early:

```sh
sudo systemctl start pi-battery-charge-stop.service
```

These are software controls. They do not guarantee that charging stops after a
total Linux, I2C or HAT failure. The PiJuice power-off countdown cuts Pi power but
does not disable its charger. Reported temperature can silently fall back to the
MCU sensor; selecting NTC does not provide independent pouch-temperature
validation. The original, more permissive profile is not restored automatically.

The output includes `original.json`, `prepared.json`, `baseline.json`, decoded
sample records, `stop-result.json` and `result.json`. A completed deadline does not
prove charging occurred: check `charge_status_observed` and the voltage/temperature
history. Keep battery-current and charge-level values labelled as estimates.
Use successful observations to choose the next trial; do not turn a short result
into a claim about maximum cell ratings, measured capacity or solar efficiency.
