"""Run an opt-in acceptance test against isolated official HA and MQTT containers."""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


HA_IMAGE = "ghcr.io/home-assistant/home-assistant:stable"
MQTT_IMAGE = "eclipse-mosquitto:2"
BASE = "http://127.0.0.1:8123"
DEVICE = "acceptance_camera"
ENTITY = "sensor.acceptance_camera_battery_voltage"
CAMERA = "camera.acceptance_camera_latest_uploaded_photograph"


def request(path, data=None, authenticated=True, form=False, binary=False):
    headers = {}
    if authenticated:
        credentials = json.loads(Path("/config/acceptance-secrets.json").read_text())
        headers["Authorization"] = "Bearer " + credentials["token"]
    if data is not None:
        headers["Content-Type"] = (
            "application/x-www-form-urlencoded" if form else "application/json"
        )
        data = (
            urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        )
    call = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(call, timeout=20) as response:
        payload = response.read()
        return payload if binary else json.loads(payload)


def wait_for(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise TimeoutError("Acceptance condition did not become true within its budget")


def states():
    return {
        state["entity_id"]: state
        for state in request("/api/states")
        if ".acceptance_camera_" in state["entity_id"]
    }


def bootstrap():
    wait_for(lambda: request("/api/onboarding", authenticated=False), 120)
    credentials = {"username": "acceptance", "password": secrets.token_urlsafe(32)}
    result = request(
        "/api/onboarding/users",
        {
            "client_id": BASE + "/",
            "name": "Acceptance Test",
            **credentials,
            "language": "en",
        },
        authenticated=False,
    )
    tokens = request(
        "/auth/token",
        {
            "grant_type": "authorization_code",
            "code": result["auth_code"],
            "client_id": BASE + "/",
        },
        authenticated=False,
        form=True,
    )
    credentials["token"] = tokens["access_token"]
    secret = Path("/config/acceptance-secrets.json")
    secret.write_text(json.dumps(credentials))
    secret.chmod(0o600)
    for step, data in (
        ("core_config", {}),
        ("analytics", {}),
        ("integration", {"client_id": BASE + "/", "redirect_uri": BASE + "/"}),
    ):
        request("/api/onboarding/" + step, data)
    flow = request(
        "/api/config/config_entries/flow",
        {"handler": "mqtt", "show_advanced_options": False},
    )
    result = request(
        "/api/config/config_entries/flow/" + flow["flow_id"],
        {
            "broker": "mqtt",
            "port": 1883,
            "protocol": "5",
            "other_settings": {
                "set_client_cert": False,
                "set_ca_cert": "off",
                "transport": "tcp",
                "tls_insecure": False,
            },
        },
    )
    if result.get("type") != "create_entry":
        raise RuntimeError("Native Home Assistant MQTT configuration did not complete")
    return {"onboarding": "REST API", "mqtt_setup": "native configuration flow"}


def fixture():
    from PIL import Image

    from timelapse.ha_photos import latest_photo
    from timelapse.receiver import handle_request

    root = Path("/config/synthetic-archive")
    existing = latest_photo(root, DEVICE)
    if existing is not None:
        return existing

    image = io.BytesIO()
    Image.new("RGB", (16, 16), (44, 103, 177)).save(image, format="JPEG")
    payload = image.getvalue()
    timestamp = datetime.now(timezone.utc).isoformat()
    metadata = {
        "filename": "acceptance.jpg",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "captured_at_utc": timestamp,
        "time_source": "RTC",
        "boot_id": "acceptance-synthetic-boot",
        "capture_duration_seconds": 1.0,
    }
    initialized = handle_request(
        "init", root, DEVICE, {"protocol": 1, "files": [metadata]}
    )
    (Path(initialized["incoming_path"]) / metadata["filename"]).write_bytes(payload)
    committed = handle_request(
        "commit", root, DEVICE, {"protocol": 1, "metadata": metadata}
    )
    if committed["receipt"]["sha256"] != metadata["sha256"]:
        raise AssertionError(
            "Production receiver did not acknowledge the synthetic JPEG"
        )
    if not (root / DEVICE / "latest.json").is_file():
        raise AssertionError(
            "Production receiver did not publish the latest-photo index"
        )
    return latest_photo(root, DEVICE)


def publication(include_discovery=True):
    from timelapse.ha_discovery import build_discovery, topics

    topic, discovery = build_discovery(DEVICE, "Acceptance Camera", expire_after=60)
    destination = topics(DEVICE)
    photo = fixture()
    state = {
        "battery_voltage": 3.97,
        "io_voltage": 5.01,
        "io_current": 0.13,
        "io_power": 0.651,
        "cpu_temperature": 32.5,
        "memory_available": 96.0,
        "load_1m": 0.2,
        "uptime": 123.0,
        "battery_status": "NORMAL",
        "usb_input_status": "PRESENT",
        "gpio_input_status": "NOT_PRESENT",
        "time_source": "RTC",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "telemetry_errors": 0,
        "pending_photos": 1,
        "stored_photos": 2,
        "spool_bytes": 1024,
        "free_bytes": 1048576,
    }
    messages = []
    if include_discovery:
        messages.append((topic, json.dumps(discovery).encode(), True))
    messages.extend(
        [
            (destination["state"], json.dumps(state).encode(), False),
            (destination["image"], photo["payload"], True),
            (
                destination["image_metadata"],
                json.dumps(photo["metadata"]).encode(),
                True,
            ),
        ]
    )
    return messages, destination, photo


def publish(messages, destination):
    from timelapse.ha_mqtt import publish_messages

    publish_messages(
        {"host": "mqtt", "port": 1883, "tls": False, "timeout_seconds": 20},
        messages,
        "acceptance_publisher",
        destination["connection"] + "/telemetry",
    )


def verify_initial():
    messages, destination, photo = publication()
    publish(messages, destination)
    wait_for(lambda: ENTITY in states())
    time.sleep(2)
    first_state = states()[ENTITY]["state"]
    messages, destination, _ = publication(include_discovery=False)
    publish(messages, destination)
    wait_for(lambda: states().get(ENTITY, {}).get("state") == "3.97")
    before = states()
    delivered = request("/api/camera_proxy/" + CAMERA, binary=True)
    expected_hash = hashlib.sha256(photo["payload"]).hexdigest()
    delivered_hash = hashlib.sha256(delivered).hexdigest()
    if expected_hash != delivered_hash:
        raise AssertionError("Home Assistant photo bytes differ from verified archive")
    messages, destination, _ = publication()
    publish(messages, destination)
    time.sleep(2)
    after = states()
    if set(before) != set(after):
        raise AssertionError("Repeated discovery changed registered entity identifiers")
    wait_for(lambda: states().get(ENTITY, {}).get("state") == "unavailable", 70)
    expired_image = request("/api/camera_proxy/" + CAMERA, binary=True)
    if hashlib.sha256(expired_image).hexdigest() != expected_hash:
        raise AssertionError("Cached photograph disappeared when telemetry expired")
    return {
        "home_assistant_version": request("/api/config")["version"],
        "enabled_entity_count": len(before),
        "registered_entity_ids": sorted(before),
        "first_discovery_state": first_state,
        "first_discovery_delivered_state": first_state == "3.97",
        "subsequent_publication_state": before[ENTITY]["state"],
        "idempotent_discovery": True,
        "photo_sha256": expected_hash,
        "photo_committed_through_production_receiver": True,
        "latest_photo_index_created": True,
        "photo_bytes": len(delivered),
        "photo_read_after_publisher_disconnect": True,
        "telemetry_expires_after_seconds": 60,
        "expired_state": "unavailable",
        "photo_survives_telemetry_expiry": True,
    }


def verify_reconnect():
    import paho.mqtt.client as mqtt

    subscribed = threading.Event()
    received = threading.Event()
    nonce = secrets.token_hex(12)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="acceptance_probe")

    def on_subscribe(_client, _userdata, _mid, _reason_codes, _properties):
        subscribed.set()

    def on_message(_client, _userdata, message):
        if message.payload == nonce.encode():
            received.set()

    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.connect("mqtt", 1883, keepalive=20)
    client.loop_start()
    try:
        client.subscribe("acceptance/server_probe", qos=1)
        if not subscribed.wait(10):
            raise TimeoutError("Probe subscription was not acknowledged")

        def ha_connected():
            request(
                "/api/services/mqtt/publish",
                {"topic": "acceptance/server_probe", "payload": nonce},
            )
            return received.wait(0.5)

        wait_for(ha_connected, 60)
    finally:
        client.disconnect()
        client.loop_stop()
    messages, destination, _ = publication()
    publish(messages, destination)
    time.sleep(2)
    messages, destination, _ = publication(include_discovery=False)
    publish(messages, destination)
    wait_for(lambda: states().get(ENTITY, {}).get("state") == "3.97", 30)
    return {"broker_restart_recovery": True, "recovered_state": "3.97"}


def controls_fixture(root):
    from timelapse.ha_config import validate_config
    from timelapse.ha_metrics import NUMERIC_FIELDS
    from timelapse.spool import Spool

    camera_path = root / "camera.json"
    camera_path.write_text(
        json.dumps(
            {
                "remote_controls": {
                    "enabled": True,
                    "device_id": DEVICE,
                    "state_path": str(root / "controls/settings.json"),
                }
            }
        )
    )
    row = {value[0]: 0 for value in NUMERIC_FIELDS.values()}
    row.update(
        session_id="synthetic-controls-session",
        sample_index=1,
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        uptime_seconds=float(Path("/proc/uptime").read_text().split()[0]),
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        time_source="NTP",
        battery_present=1,
        battery_status="NORMAL",
        power_input_status="PRESENT",
        power_5v_io_status="NOT_PRESENT",
        battery_voltage_mv=3970,
        errors_json="{}",
    )
    database = root / "metrics.sqlite3"
    with sqlite3.connect(database) as connection:
        columns = ",".join(
            f"{key} {'TEXT' if isinstance(value, str) else 'REAL' if isinstance(value, float) else 'INTEGER'}"
            for key, value in row.items()
        )
        connection.execute(f"CREATE TABLE samples ({columns})")
        connection.execute(
            f"INSERT INTO samples ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
    Spool(root / "spool")
    return validate_config(
        {
            "device_id": DEVICE,
            "device_name": "Acceptance Camera",
            "mqtt": {"host": "mqtt", "port": 1883, "tls": False},
            "telemetry": {"database": str(database), "spool": str(root / "spool")},
            "controls": {"timelapse_config": str(camera_path), "poll_seconds": 1},
        }
    )


def verify_controls():
    from timelapse.config import effective_config, load_config
    from timelapse.ha import run_controls

    entities = {
        "capture_enabled": "switch.acceptance_camera_capture_enabled",
        "interval_seconds": "number.acceptance_camera_capture_interval",
        "resolution": "select.acceptance_camera_capture_resolution",
        "jpeg_quality": "number.acceptance_camera_jpeg_quality",
        "rotation": "select.acceptance_camera_capture_rotation",
        "settle_ms": "number.acceptance_camera_camera_settling_time",
    }

    def reported():
        observed = states()
        result = {
            field: observed[entity]["state"] for field, entity in entities.items()
        }
        for field in ("interval_seconds", "jpeg_quality", "settle_ms"):
            result[field] = int(float(result[field]))
        result["capture_enabled"] = result["capture_enabled"] == "on"
        result["rotation"] = int(result["rotation"])
        return result

    with tempfile.TemporaryDirectory(prefix="camera-controls-") as directory:
        root = Path(directory)
        config = controls_fixture(root)
        local = load_config(config["controls"]["timelapse_config"])
        path = Path(local["remote_controls"]["state_path"])
        preview = run_controls(config, dry_run=True)
        if preview["action"] != "preview" or path.exists():
            raise AssertionError("Controls preview wrote configuration")
        initial = run_controls(config)["settings"]
        wait_for(lambda: reported() == initial)
        changes = {
            "capture_enabled": True,
            "interval_seconds": 600,
            "resolution": "1920x1080",
            "jpeg_quality": 75,
            "rotation": 180,
            "settle_ms": 1500,
        }
        before = path.read_bytes()
        for field, value in changes.items():
            entity = entities[field]
            domain = entity.split(".")[0]
            service = (
                "turn_on"
                if domain == "switch"
                else "select_option"
                if domain == "select"
                else "set_value"
            )
            data = {"entity_id": entity}
            if domain != "switch":
                data["option" if domain == "select" else "value"] = (
                    str(value) if domain == "select" else value
                )
            request(f"/api/services/{domain}/{service}", data)
        time.sleep(1)
        if path.read_bytes() != before or reported() != initial:
            raise AssertionError(
                "Offline desired configuration was optimistically applied"
            )
        applied = run_controls(config)
        if applied["settings"] != changes:
            raise AssertionError(
                "Bounded controls poll did not apply all retained desired settings"
            )
        wait_for(lambda: reported() == changes)
        snapshot = effective_config(local)
        if (
            snapshot["camera"]["width"] != 1920
            or snapshot["camera"]["height"] != 1080
            or snapshot["camera"]["quality"] != 75
            or snapshot["camera"]["rotation"] != 180
            or snapshot["camera"]["settle_ms"] != 1500
            or snapshot["schedule"] != {"enabled": True, "interval_seconds": 600}
            or snapshot["power"] != local["power"]
            or snapshot["server"] != local["server"]
            or snapshot["camera"]["camera_command"] != local["camera"]["camera_command"]
        ):
            raise AssertionError(
                "Effective capture configuration differs from durable reported settings"
            )
        before = path.read_bytes()
        inode = path.stat().st_ino
        request(
            "/api/services/mqtt/publish",
            {
                "topic": f"pi_timelapse/{DEVICE}/desired/interval_seconds",
                "payload": "NaN",
                "retain": True,
                "qos": 1,
            },
        )
        try:
            run_controls(config)
        except ValueError:
            pass
        else:
            raise AssertionError("Malformed retained control was accepted")
        if path.read_bytes() != before or reported() != changes:
            raise AssertionError("Malformed control changed settings or reported state")
        request(
            "/api/services/mqtt/publish",
            {
                "topic": f"pi_timelapse/{DEVICE}/desired/interval_seconds",
                "payload": "600",
                "retain": True,
                "qos": 1,
            },
        )
        replay = run_controls(config)
        if replay["changed_fields"] or path.stat().st_ino != inode:
            raise AssertionError(
                "Retained configuration replay rewrote durable settings"
            )
        request(
            "/api/services/switch/turn_off", {"entity_id": entities["capture_enabled"]}
        )
        request(
            "/api/services/select/select_option",
            {"entity_id": entities["resolution"], "option": "configured"},
        )
        final = run_controls(config)["settings"]
        wait_for(lambda: reported() == final)
        snapshot = effective_config(local)
        if snapshot["schedule"]["enabled"] or (
            snapshot["camera"]["width"],
            snapshot["camera"]["height"],
        ) != (local["camera"]["width"], local["camera"]["height"]):
            raise AssertionError(
                "Disable or configured resolution did not reach effective capture settings"
            )
        return {
            "camera_controls": {
                "native_entity_ids": entities,
                "native_service_calls": [
                    "switch.turn_on",
                    "switch.turn_off",
                    "number.set_value",
                    "select.select_option",
                ],
                "retained_desires_survive_device_offline": True,
                "nonoptimistic_before_durable_readback": True,
                "reported_settings_match_effective_capture_config": True,
                "malformed_retained_control_rejected_without_change": True,
                "retained_replay_avoids_state_rewrite": True,
                "configured_resolution_and_disable_applied": True,
                "reported_final_settings": final,
                "telemetry_boundary": "synthetic SQLite row with current container boot and uptime; production reader and guard; no hardware calls or mocks",
                "physical_capture_or_scheduler_execution": False,
            }
        }


def docker(*arguments):
    return subprocess.run(
        ["docker", *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def container_phase(name, phase):
    output = docker(
        "exec",
        "-w",
        "/config/project",
        name,
        "python3",
        "/config/project/ops/verify_home_assistant.py",
        "--inside",
        phase,
    )
    return json.loads(output)


def acceptance(output):
    root = Path(tempfile.mkdtemp(prefix="pi-ha-acceptance-", dir="/private/tmp"))
    suffix = root.name.removeprefix("pi-ha-acceptance-")
    network = f"pi-ha-acceptance-{suffix}"
    ha = f"pi-ha-acceptance-ha-{suffix}"
    broker = f"pi-ha-acceptance-broker-{suffix}"
    ha_root = root / "ha"
    broker_root = root / "mosquitto"
    ha_root.mkdir()
    broker_root.mkdir()
    (ha_root / "configuration.yaml").write_text(
        "homeassistant:\n  name: Pi camera acceptance\n  latitude: 0\n"
        "  longitude: 0\n  elevation: 0\n  unit_system: metric\n"
        "  time_zone: UTC\ndefault_config:\n"
    )
    (broker_root / "mosquitto.conf").write_text(
        "listener 1883\nallow_anonymous true\npersistence false\nlog_dest stdout\n"
    )
    project = ha_root / "project"
    for package in ("timelapse", "ops"):
        (project / package).mkdir(parents=True)
    source_root = Path(__file__).resolve().parents[1]
    for source in (source_root / "timelapse").glob("*.py"):
        shutil.copyfile(source, project / "timelapse" / source.name)
    shutil.copyfile(Path(__file__), project / "ops/verify_home_assistant.py")
    (project / "ops/__init__.py").write_text("")
    created = []
    report = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "isolated official containers and synthetic data only",
        "source_root": ".",
        "temporary_directory": "ephemeral test workspace",
        "network_is_internal": True,
        "host_port_bindings": ["127.0.0.1:18123", "127.0.0.1:18884"],
        "mqtt_transport": "plaintext anonymous on isolated test network",
        "real_camera_or_battery_access": False,
        "tls_or_production_credentials_tested": False,
        "source_sha256": {
            f"timelapse/{name}": hashlib.sha256(
                (project / "timelapse" / name).read_bytes()
            ).hexdigest()
            for name in (
                "ha.py",
                "ha_config.py",
                "ha_controls.py",
                "ha_discovery.py",
                "ha_metrics.py",
                "ha_mqtt.py",
                "ha_photos.py",
                "receiver.py",
                "config.py",
            )
        },
    }
    try:
        report["images"] = {
            image: json.loads(docker("image", "inspect", image))[0]["Id"]
            for image in (HA_IMAGE, MQTT_IMAGE)
        }
        docker("network", "create", "--internal", network)
        created.append(("network", network))
        for name, image, port, mount in (
            (
                broker,
                MQTT_IMAGE,
                "127.0.0.1:18884:1883",
                f"type=bind,src={broker_root},dst=/mosquitto/config,readonly",
            ),
            (
                ha,
                HA_IMAGE,
                "127.0.0.1:18123:8123",
                f"type=bind,src={ha_root},dst=/config",
            ),
        ):
            arguments = ["run", "-d", "--name", name, "--network", network]
            if name == broker:
                arguments.extend(["--network-alias", "mqtt"])
            docker(*arguments, "-p", port, "--mount", mount, image)
            created.append(("container", name))
        report["mosquitto_version"] = docker(
            "exec", broker, "mosquitto", "-h"
        ).splitlines()[0]
        report.update(container_phase(ha, "bootstrap"))
        report.update(container_phase(ha, "initial"))
        docker("restart", broker)
        report.update(container_phase(ha, "reconnect"))
        report.update(container_phase(ha, "controls"))
        report["completed"] = True
    except Exception as error:
        report["completed"] = False
        report["failure_type"] = type(error).__name__
        raise
    finally:
        cleanup = []
        for kind, name in reversed(created):
            try:
                if kind == "container":
                    docker("rm", "-f", name)
                else:
                    docker("network", "rm", name)
                cleanup.append({"kind": kind, "removed": True})
            except subprocess.CalledProcessError:
                cleanup.append({"kind": kind, "removed": False})
        report["cleanup"] = cleanup
        output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(output), "completed": report["completed"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--inside", choices=("bootstrap", "initial", "reconnect", "controls")
    )
    args = parser.parse_args()
    if args.inside:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        action = {
            "bootstrap": bootstrap,
            "initial": verify_initial,
            "reconnect": verify_reconnect,
            "controls": verify_controls,
        }[args.inside]
        print(json.dumps(action()))
    elif args.run and args.output:
        acceptance(args.output.resolve())
    else:
        parser.error(
            "Explicitly pass --run and --output to create isolated test containers"
        )


if __name__ == "__main__":
    main()
