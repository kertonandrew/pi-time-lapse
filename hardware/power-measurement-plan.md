# Power measurement design

Start with externally powered bench instrumentation; select permanent monitoring only
after measuring complete capture cycles and the cost of monitoring itself.

**Measurement boundaries**

Use three bidirectional high-side shunts, with common ground and Kelvin sense wiring:

| Channel | Physical placement and positive direction | Voltage measurement point |
| --- | --- | --- |
| Solar input | Solar supply/cable → shunt → PiJuice USB VBUS | HAT side of shunt, at USB input |
| Battery | Protected pack positive → shunt → HAT battery positive | Pack side of shunt; preserve negative and NTC connections |
| Pi output | HAT 5 V → shunt → **both** Pi GPIO 5 V pins | Pi side of shunt |

Solar input measures power delivered to the HAT. A panel with an integrated USB
regulator has already incurred conversion losses here; raw photovoltaic power needs
measurement before that regulator. Identify panel voltage/current limits before wiring.
Pi output requires an interposer that routes every HAT-to-Pi 5 V path through its
shunt; merely adding a parallel wire measures nothing useful. Camera and peripherals
powered by the Pi are included; separate VSYS loads are excluded.

Remove direct Pi USB power for whole-load tests, including power from a USB data host.
Otherwise instrument that feed separately and account for its signed contribution.
PiJuice's existing GPIO reading covers only the HAT boundary; its battery current is
estimated from charge level or a voltage model, and USB input watts are unavailable.
[PiJuice API](https://github.com/PiSupply/PiJuice/blob/master/Software/Source/pijuice.py),
[battery estimator](https://github.com/PiSupply/PiJuice/blob/master/Firmware/Sources-V1.6_2021_09_10/Src/fuel_gauge_lc709203f.c#L298).

**Two sensor options**

Values below are chip specifications, not breakout-board consumption. Both need a
2.7–5.5 V supply independent of the measured rail.

| Sensor | Active / shutdown current at 25°C, typical (maximum) | Conversion time per input | Relevant capability |
| --- | --- | --- | --- |
| INA226 | 330 (420) µA / 0.5 (2) µA | 140 µs–8.244 ms | 36 V bus; ±81.92 mV shunt; 2.5 µV shunt steps; host integrates energy |
| INA228 | 640 (750) µA / 2.8 (5) µA | 50 µs–4.12 ms | 85 V bus; ±40.96/163.84 mV shunt; 78.125/312.5 nV steps; hardware energy/charge counters |

Both support 1–1024 averages. Longer conversions/averaging trade temporal detail for
lower noise. INA226 uses less supply current; INA228 is the stronger candidate for
standby measurements, subject to measured noise. Its 20-bit output is not 20-bit accuracy.
[INA226 datasheet, §§5.5, 6.3](https://www.ti.com/lit/ds/symlink/ina226.pdf),
[INA228 datasheet, §§6.5, 7.3, 8.1](https://www.ti.com/lit/ds/symlink/ina228.pdf).

For a **2 A worked example**, not a claimed operating current:

| Shunt | Drop / dissipation at 2 A | INA226 current step | INA228 step, ±40.96 mV range |
| --- | --- | --- | --- |
| 10 mΩ | 20 mV / 40 mW | 250 µA | 7.8125 µA |
| 20 mΩ | 40 mV / 80 mW | 125 µA | 3.90625 µA |

These follow from `V = IR`, `P = I²R`, and `current step = voltage step / R`.
At 10 mΩ the maximum specified input offsets correspond to 1 mA for INA226 and
0.1 mA for INA228: neither guarantees accurate sub-mA readings without calibration.
Choose resistance from measured peak current, permitted voltage drop, ADC headroom
and resistor temperature/pulse ratings. The INA228 narrow range with 20 mΩ reaches
full scale at only 2.048 A. Use measured shunt resistance, low temperature coefficient
and short Kelvin traces; verify zero and several known loads, including standby.

**Keeping the Pi off**

Three continuously active sensors at 3.3 V consume approximately **0.078 Wh/day
(INA226)** or **0.152 Wh/day (INA228)** before logger, regulator and board losses.
As a duty-cycle illustration, one INA226 shunt/bus pair at 1.1 ms each plus 40 µs
wake-up, once per second then shutdown, averages about **1.24 µA** at its supply.
That excludes communication, input loading and board overhead; it also misses events
between samples. Measure the assembled logger's battery consumption.

An always-powered, sleeping MCU is required for timestamped histories and separate
battery charge/discharge Wh while Linux is off. It should own sampling/integration,
buffer records and transfer them when the Pi wakes. Supply it from a protected,
unswitched source with suitable undervoltage handling. Count its draw inside the
battery measurement boundary; prevent its I2C pull-ups from back-powering the off Pi.
Do not use the Pi's switched 3.3 V rail for off-time instrumentation.

An INA228 left powered in continuous mode can retain aggregate counters without an
awake host, until reset/power loss or overflow. Triggered-mode accumulation is invalid.
Its energy counter adds **absolute** energy in either direction; signed charge alone
cannot reconstruct charge/discharge Wh at changing voltage. A host must observe
direction sufficiently often to separate them.
[TI polarity clarification](https://e2e.ti.com/support/amplifiers-group/amplifiers/f/amplifiers-forum/1323511/ina228-energy-counter-polarity).

**Bench sequence and sizing**

1. Verify polarity, calibration, supply minimums and added drop with known loads.
   Log boot, camera initialization, capture, file flush, Wi-Fi transfer, shutdown and
   complete power-off. Start at 100 samples/s; increase until cycle Wh converges
   within a chosen error budget, initially 2%. Compare slow standby sampling against
   continuous reference data before using duty cycling in deployment.
2. Integrate signed `V × I × elapsed_seconds / 3600`. Keep battery charge and discharge
   totals separately; flag resets, saturation and gaps. Repeat representative cycles
   and measure off-state power with the final monitoring hardware included.
3. On battery-only tests, calculate daily demand as `N × E_cycle + P_off × off_hours`
   plus any separately scheduled transfer energy. Battery-terminal measurements
   already include downstream conversion losses; do not add them again.
4. Required nominal battery Wh is at least `daily_demand × autonomy_days / usable_fraction`.
   Establish usable fraction at the intended temperature, cutoff and aging allowance.
   For conservative solar sizing through storage, use
   `panel_W ≥ daily_demand / (worst-season HAT-input Wh per panel_W per day × storage_path_efficiency)`.
   Measure storage efficiency through a full charge/discharge cycle; include recharge
   margin and site shading. Panel nameplate watts and battery label Ah are insufficient.
