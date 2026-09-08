# Pi timelapse

Remote timelapse camera using a Raspberry Pi Zero W and PiJuice, with eventual
solar charging and Wi-Fi photo retrieval.

Commission the battery profile and physical power-off/wake behavior before field use. The camera package now captures durable JPEGs and supports resumable,
verified SSH uploads during calibrated power opportunities. Image processing is
deferred.

- [Camera setup, operation and rollback](docs/camera-workflow.md)
- [Solar transfer policy and measurement limits](docs/solar-transfer-design.md)
- [Hardware setup](hardware/README.md)
- [Power instrumentation and battery requirements](hardware/power-measurement-plan.md)
- [Home Assistant metrics and camera integration](docs/home-assistant.md)

The application uses Python's standard library, the installed `rpicam-still`, and
rsync/OpenSSH. It does not change charging settings or shut down the Pi.
The optional Home Assistant publishers add `paho-mqtt`, with short telemetry
sessions on the Pi and cached camera images published from the archive server.

```sh
python3 -m timelapse capture --dry-run
python3 -m unittest discover -s tests -p 'test_*.py' -q
```

The local `main.py --test` compatibility command is now a capture dry run. The
older top-level `camera.py`, `pijuice_config.py` and `logger.py` are retained as
historical prototypes and are not imported by the new application.
