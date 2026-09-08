# Pi timelapse

A Raspberry Pi camera that stores durable JPEGs, transfers verified batches to an
SSH archive when power permits, and integrates with Home Assistant through MQTT.
Image processing happens elsewhere. The reference hardware is a Pi Zero W with
PiJuice; battery, solar and off/wake commissioning are still in progress.

Home Assistant discovers metrics, a cached camera image and six camera settings:
scheduled capture, interval, resolution, JPEG quality, rotation and settling time.
The Pi exchanges short MQTT bursts while awake. The archive publishes the latest
verified photograph so viewing it does not wake the camera.

## Start with local configuration

Python 3.11 or newer is required. From a checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[homeassistant]'
.venv/bin/python -m timelapse.setup init --directory local --device-id garden-camera --broker mqtt.example.com
.venv/bin/python -m timelapse --config local/camera.json capture --dry-run
.venv/bin/python -m timelapse.setup doctor --config local/camera.json --ha-config local/home-assistant.json
```

Replace the example device ID and broker with your own. `init` creates complete,
validated configuration files with private permissions and refuses to overwrite
existing files. `local/` is Git-ignored. Keep real configuration, keys, certificates,
database files and photographs outside tracked files. See [SECURITY.md](SECURITY.md).

The dry run and doctor work without contacting hardware or the network. On a
laptop, missing Linux/camera/hardware checks are expected. A successful doctor
checks prerequisites only; each real capture still requires a fresh power record.
The base application has no Python dependencies; omit `[homeassistant]` if MQTT is
not needed. For reproducible development, use `uv sync --locked --extra homeassistant`.

Fresh configuration keeps scheduled capture, remote changes and transfers disabled.
The camera defaults to its native resolution, no rotation and JPEG quality 90.
No user account, network address, battery qualification or archive destination is
assumed. Unknown configuration keys and invalid values fail validation.

## Deploy and commission

1. Install Raspberry Pi OS Lite, enable the camera/I2C as required and verify
   `rpicam-still`. Set up the [hardware recorder](hardware/README.md) for the
   reference PiJuice hardware. Its installer deliberately checks the original
   Zero W/Bookworm target; other hardware needs its own reviewed setup.
2. Follow the [camera workflow](docs/camera-workflow.md) to preview installation,
   install private configuration and qualify a real photograph. The application
   installer accepts ARM Raspberry Pi systems on Debian/Raspberry Pi OS Bookworm
   or Trixie; physical validation so far covers the reference Zero W only.
3. Configure [Home Assistant](docs/home-assistant.md), including per-device broker
   credentials, TLS, discovery and optional camera controls. Remote controls need
   explicit local opt-in and cannot change charging, power limits or executables.
4. Provision the [restricted SSH receiver](docs/ssh-receiver.md) and measured
   [solar transfer policy](docs/solar-transfer-design.md) before enabling uploads.

Scheduling runs only while Linux is awake. A weak solar input alone does not block
capture when a qualified battery has sufficient reserve. Missing/stale telemetry
or an unqualified battery still blocks capture. This package does not adjust the
charger, program wake alarms, shut down Linux or delete photographs after upload.

## Development

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -q
python3 -m unittest discover -s hardware -p 'test_*.py' -q
python3 ops/check_public_repo.py --working-tree
```

- [Home Assistant acceptance and limits](docs/home-assistant.md)
- [Power instrumentation and battery requirements](hardware/power-measurement-plan.md)

The top-level `camera.py`, `pijuice_config.py` and `logger.py` are historical
prototypes, excluded from the installed package. Use `python -m timelapse` for the
maintained application. `main.py --test` remains a compatibility dry run.
