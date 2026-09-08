"""Run an opt-in acceptance test against isolated official HA and MQTT containers."""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import secrets
import shutil
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
        "source_root": str(source_root),
        "temporary_directory": str(root),
        "network_is_internal": True,
        "host_port_bindings": ["127.0.0.1:18123", "127.0.0.1:18884"],
        "mqtt_transport": "plaintext anonymous on isolated test network",
        "real_camera_or_battery_access": False,
        "tls_or_production_credentials_tested": False,
        "source_sha256": {
            f"timelapse/{name}": hashlib.sha256(
                (project / "timelapse" / name).read_bytes()
            ).hexdigest()
            for name in ("ha_discovery.py", "ha_mqtt.py", "ha_photos.py", "receiver.py")
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
        report["completed"] = True
    except Exception as error:
        report["completed"] = False
        report["failure_type"] = type(error).__name__
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            report["failure_last_line"] = error.stderr.splitlines()[-1][:300]
        raise
    finally:
        cleanup = []
        for kind, name in reversed(created):
            try:
                if kind == "container":
                    docker("rm", "-f", name)
                else:
                    docker("network", "rm", name)
                cleanup.append({"kind": kind, "name": name, "removed": True})
            except subprocess.CalledProcessError:
                cleanup.append({"kind": kind, "name": name, "removed": False})
        report["cleanup"] = cleanup
        output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(output), "completed": report["completed"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inside", choices=("bootstrap", "initial", "reconnect"))
    args = parser.parse_args()
    if args.inside:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        action = {
            "bootstrap": bootstrap,
            "initial": verify_initial,
            "reconnect": verify_reconnect,
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
