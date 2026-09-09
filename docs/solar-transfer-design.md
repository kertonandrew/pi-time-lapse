# Solar-aware image transfer

This deployment design does not establish solar operation. Battery profile
verification, calibrated input-power sensing and physical shutdown/wake
commissioning are required; see the [hardware setup](../hardware/README.md).
Image processing stays off the Pi and outside this phase.

The target is to capture reliably, retain originals locally, and send bounded
batches when the supply can support them while preserving the next capture and
shutdown reserve. The exact highest-power moment of a day is only identifiable
after the day, and only among the measurements actually collected. Sun position
provides a useful prior; passing clouds create rapid changes in an individual
panel's output. A prediction cannot guarantee the day's maximum.
[NREL, Integrating Variable Renewable Energy](https://docs.nrel.gov/docs/fy13osti/60451.pdf).

## What the available signals mean

| Signal | Useful decision | Limit |
| --- | --- | --- |
| Calibrated HAT-input voltage × current | Delivered input watts, including an observed change under transfer load | Not raw panel watts or unused generating capacity |
| Calibrated battery voltage × signed current | Whether the battery is supplying or absorbing energy during a transfer | Requires calibration, polarity and complete measurement boundaries |
| Calibrated Pi-output power | Capture and upload demand at the Pi rail | Does not include all HAT or upstream conversion losses |
| PiJuice battery current estimate | A secondary trend or anomaly signal | Not a shunt measurement and insufficient for precise Wh accounting |
| PiJuice charge level | An approximate reserve gate after pack/profile validation | Not solar production; charging can distort the estimate |
| PiJuice USB `PRESENT` | Input presence and quality at that connector | Neither source identity nor watts |
| Civil time or predicted solar noon | Which observations deserve attention | Cannot observe clouds, shading, supply limits or storage state |

PiJuice's documented input states distinguish absent, inadequate, limited and good
input. Its hardware documentation describes DPM limiting input demand when a weak
source would otherwise sag, and describes charge-level inaccuracies during charging.
The API exposes no USB-input voltage/current pair.
[PiJuice software](https://github.com/PiSupply/PiJuice/blob/master/Software/README.md#main-software-menu),
[PiJuice hardware](https://github.com/PiSupply/PiJuice/tree/master/Hardware#usb-micro-input),
[PiJuice API](https://github.com/PiSupply/PiJuice/blob/master/Software/Source/pijuice.py).

The full-battery case needs explicit treatment. The charger's current tapers and
charging terminates while its system output can still power the load. Consequently,
zero battery charge current can coexist with useful solar power. Input watts can
also fall because the system no longer demands the panel's available output.
This follows from the charger's power-path and termination behavior; an input shunt
alone cannot reveal the unused headroom.
[TI BQ2416xx datasheet, charging and power-path operation](https://www.ti.com/lit/ds/symlink/bq24160.pdf).

Direct Pi power bypasses the HAT-to-Pi measurement boundary. Establish the
final wiring and account for every feed before interpreting input/output
balance. Sensor placement, off-state
measurement and calibration are detailed in the
[power measurement plan](../hardware/power-measurement-plan.md).

## Admission and stopping policy

Use explicit operating modes:

| Mode | Entry requirement | Claim |
| --- | --- | --- |
| Disabled | Default until configured | Capture and retain; no automatic upload |
| Bench/manual | Deliberately selected with a known stable supply | Validates transport, not solar timing |
| Observed solar input | Verified solar wiring, calibrated fresh input data, reserve limits and measured thresholds | Input exceeded the configured observed-power threshold |
| Reserve fallback | Explicit opt-in after battery/profile verification; fresh healthy input plus validated reserve | An energy-reserve opportunity, not a solar-maximum measurement |

For automatic solar mode, require a pending queue and all of these conditions:

1. Battery identity/profile and supply topology are verified. Required observations
   are finite, calibrated, fresh, sequential and from their declared producer.
2. Input is healthy, battery temperature/voltage are inside the validated operating
   envelope, and no unresolved blocking fault is present. Do not silently clear
   historical fault flags to make admission succeed.
3. Reserve exceeds an explicit start threshold. Choose it from measured worst-case
   next-capture, shutdown and retry energy, with the pack's usable capacity and
   uncertainty. Do not invent a safe percentage from the label capacity.
4. Distinct observations exceed the configured input start threshold for a configured
   dwell period. Re-reading one observation never advances the streak.
5. The batch fits the remaining capture deadline, storage state and transfer budget.

During transfer, use a lower input stop threshold and a lower reserve stop threshold
than their corresponding start thresholds. Choose the gap and dwell from observed
noise and cloud transients. Stop admitting files when the conditions fail; retain
partial transfers for resumption. Bound the duration of the currently active file or
chunk so a large image cannot bypass the energy stop condition.

Missing, stale, regressed, non-finite, saturated or invalid readings deny a new batch.
Already admitted work must also have a bounded freshness lease; a sensor outage
cannot leave the upload running indefinitely. Clear the dwell history after a boot,
sensor reset, calibration change, long sample gap or invalid observation. A calendar
window never overrides these gates.

Automatic operation must remain disabled until bench measurements validate the
thresholds. This applies to watts, battery percentages, allowable battery discharge
during a probe, dwell, freshness age and transfer time/byte budgets. Any software
defaults are starting points for commissioning, not calibrated limits. These are
policy parameters, not battery charge settings. Record the calibration run and
uncertainty behind each value.

Observed-input admission is deliberately limited: it may miss a full-battery
opportunity because unloaded input watts are low. A later bounded load probe can
address this. With a validated high reserve and healthy known solar input, transfer
a small real queued item and observe calibrated input and battery response. Continue
only if the additional demand is supplied within the measured reserve budget. Cap
the probe's energy/time and back off on failure. This tests useful work under current
conditions; it is not an MPPT sweep or proof of the panel's absolute maximum power.
An MPPT controller exposing available-power telemetry would be a separate capability.

## Probe without spending the gain on probing

Initially, sample while awake for a scheduled photograph or upload. Reuse the existing
monitor process and avoid a separate interpreter, Wi-Fi connection or full Pi boot
just to ask whether the sun improved. Capture is the primary job; an unavailable
server or weak input must not prevent local capture.

Collect a small summary per time bin on the server or laptop: delivered input,
battery flow, reserve, whether transfer was active, and observation coverage. Separate
idle/full-battery observations from loaded probes. A day with no midday observation
does not prove midday power was low. Learn a broad promising interval from comparable
recent days, then refine the probe spacing when already-awake observations show
rising power. Expand/back off after weak conditions. Keep exploration bounded so
one clear historical afternoon does not permanently hide a better morning window.

For continuous observations while Linux is off, use the proposed independently
powered MCU and shunts. It should sleep between conversions, retain timestamped
summaries and wake requests, and hand history to the Pi at its next normal wake.
The MCU can request an extra upload wake only after measured reserve/input criteria
persist and a minimum wake interval has elapsed. Its own energy, regulator losses
and I2C isolation must be included in the measurement. The existing Pi monitor cannot
observe a powered-off Pi.

Decide between staying awake and another cold boot from measurements:

`additional idle energy = idle watts × waiting seconds / 3600`

Compare that with the measured extra shutdown, standby, boot, Wi-Fi association and
camera initialization energy. Include missed-capture risk. A faster boot does not by
itself prove lower energy per photograph.

## Fresh observation contract

The eventual external sensor adapter should provide these concepts, with an explicit
versioned schema in the implementation:

- Producer identity, reset/session ID, strictly increasing sequence, measurement
  timestamp, receiving Pi boot ID and monotonic receipt time.
- Physical measurement boundary and source identity; voltage in volts, signed
  current in amperes, power in watts, and the sign convention.
- Calibration identifier, range/saturation status, missing-channel errors and the
  actual observation interval. Unknown values remain absent rather than becoming zero.
- For buffered MCU records, original sample age plus transport delay; receiving an
  old backlog must not turn it into live evidence. Track clock synchronization quality.

Use monotonic age within one Pi boot. An RTC/NTP wall-clock adjustment must not make
old evidence fresh or erase a cooldown. After a reboot, do not reuse a prior boot's
monotonic timestamps. Preserve UTC separately for later cross-device analysis.

The current recorder samples every 60 seconds but normally commits its SQLite
history every 300 seconds. A consumer of that database must inspect the actual
sample age; the query time is not the measurement time. Do not count repeated reads
as distinct samples. A shared fresh observation or small atomic latest-sample file
can support the admission path while history remains batched. Writing/reading that
interface is separate implementation work and should preserve one I2C owner.

## Efficient and recoverable transfer

Use immutable JPEG originals and small metadata manifests. Keep full-resolution
rendering, timelapse assembly, thumbnails and other processing on the server later.
Use stable capture IDs independent of wall-clock uniqueness, and publish a local
capture to the queue only after the image and manifest have been durably written.

Batch enough files to amortize connection setup, but impose byte and wall-time
limits. Send oldest unacknowledged captures first. Keep one SSH session for a batch,
use verified server host keys, and use an upload account restricted to its ingestion
directory. Server discovery, credentials and host-key provisioning are explicit
deployment inputs; do not disable host-key checks to make unattended uploads work.

Rsync over SSH is a suitable initial transport: a private relative partial directory
supports resumption, and `--fsync` requests a flush of each received file. Avoid
`--inplace` for published objects. Disable transport compression for JPEG batches
unless a measured link/CPU benchmark shows a benefit; compression algorithms and
options vary by rsync version. Bound both connection setup and stalled I/O.
[Official rsync manual](https://download.samba.org/pub/rsync/rsync.1).

Transport success is not the retention contract. The receiver should verify the
expected size and SHA-256, durably publish the immutable object and acknowledge its
capture ID/hash. Repeated delivery of that same ID/hash succeeds idempotently; a
different hash at the same ID is a conflict. Mark local completion only after that
acknowledgment. Retain local originals until the separately configured retention
policy permits deletion. Do not use remote mirroring deletion or source removal as
a shortcut. Under storage pressure, fail visibly or use a deliberately chosen policy;
never discard unacknowledged originals silently.

Log transfer start/stop reasons, IDs, bytes acknowledged, duration, retries, signal
quality and power samples. Assess joules per durably acknowledged megabyte and total
daily energy, including connection failures and MCU overhead. A smaller byte count
or shorter elapsed time alone is insufficient evidence of energy savings.

## Camera and future sleep implementation details

The official camera documentation specifies `--encoding jpg` independently of the
output filename and supports `--rotation 0` or `--rotation 180`; 90° and 270° are not
supported. Select the orientation in the capture configuration and validate a real
image after mounting. `--nopreview` avoids a preview window. Keep a bounded process
timeout outside the camera command, and validate successful output before enqueueing.
[Raspberry Pi camera software](https://www.raspberrypi.com/documentation/computers/camera_software.html#rpicam-still).

PiJuice alarm values are matching fields, not a stored Unix deadline. The Python API
defaults omitted seconds to zero, masks omitted minutes, supports a numeric
day-of-month or separate weekday, and has no alarm month/year. `minute=0` with
`EVERY_HOUR` is hourly. Explicitly validate seconds/minutes as 0–59 even though the
API permits 60. `SetWakeUpOnCharge('DISABLED')` disables charge waking; zero is a
threshold, not disable.
[PiJuice alarm and power API](https://github.com/PiSupply/PiJuice/blob/master/Software/Source/pijuice.py).

`minute_period` is aligned to minutes within the hour; it is not a relative delay
from the call. The documented largest meaningful period is 30 minutes.
[PiJuice wakeup menu](https://github.com/PiSupply/PiJuice/blob/master/Software/README.md#wakeup-alarm-menu).

Future application logic should persist the next intended UTC deadline, derive its
alarm fields, and re-arm each cycle. Select a future deadline beyond the measured
shutdown margin and validate it again immediately before cutting power. Check every
write result, read back the alarm, clear stale fired status using the documented API,
enable wake and verify its control state. Keep RTC clock repair separate from alarm
ownership. A failed capture or failed alarm write must not proceed blindly into a
power cut. Actual battery-only halt, 5 V removal and wake must be observed physically
before enabling unattended sleep cycles.

## Evidence needed before field enablement

The minimum useful exercise covers cloud/supply drop during transfer, fresh versus
stale telemetry, full battery with negligible charging current, server unreachable,
an interrupted/resumed file, capture overrun, low storage, RTC correction and the
real battery-only off/wake cycle. Calibrate cycle energy with external instrumentation
and confirm that the chosen input/reserve thresholds preserve the next photograph.
Until then, local capture and deliberately selected bench transfer can be validated,
but field solar timing and energy savings remain unverified.
