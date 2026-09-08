"""Exercise production MQTT TLS, authentication and ACL behavior in isolated Docker."""

from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time


BROKER_IMAGE = "eclipse-mosquitto:2"
CLIENT_IMAGE = "ghcr.io/home-assistant/home-assistant:stable"
PREFIX = "pi_timelapse/security_camera"


def run(arguments, input_data=None):
    return subprocess.run(
        arguments, input=input_data, check=True, capture_output=True, text=True
    ).stdout.strip()


def docker(*arguments, input_data=None):
    return run(["docker", *arguments], input_data)


def write_private(path, payload):
    path.write_text(payload)
    path.chmod(0o600)


def certificate_authority(root, name):
    run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-noenc",
            "-keyout",
            str(root / f"{name}.key"),
            "-out",
            str(root / f"{name}.crt"),
            "-days",
            "1",
            "-sha256",
            "-subj",
            f"/CN=Acceptance {name}",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ]
    )
    (root / f"{name}.key").chmod(0o600)


def certificates(root, broker_root, client_root):
    certificate_authority(root, "ca")
    certificate_authority(root, "wrong-ca")
    run(
        [
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-noenc",
            "-keyout",
            str(broker_root / "server.key"),
            "-out",
            str(root / "server.csr"),
            "-subj",
            "/CN=mqtt",
        ]
    )
    (broker_root / "server.key").chmod(0o600)
    (root / "server.ext").write_text(
        "subjectAltName=DNS:mqtt\nextendedKeyUsage=serverAuth\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nbasicConstraints=critical,CA:FALSE\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
    )
    run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(root / "server.csr"),
            "-CA",
            str(root / "ca.crt"),
            "-CAkey",
            str(root / "ca.key"),
            "-CAcreateserial",
            "-out",
            str(broker_root / "server.crt"),
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(root / "server.ext"),
        ]
    )
    for name in ("ca.crt", "wrong-ca.crt"):
        shutil.copyfile(root / name, client_root / name)


def inside():
    import paho.mqtt.client as mqtt

    from timelapse.ha_mqtt import MQTTError, publish_messages

    root = Path("/test")
    subscribed = threading.Event()
    received = []
    subscriber = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id="security_observer"
    )
    subscriber.username_pw_set("observer", (root / "observer-password").read_text())
    subscriber.tls_set(ca_certs=str(root / "ca.crt"))

    def on_subscribe(_client, _userdata, _mid, reasons, _properties):
        if all(not reason.is_failure for reason in reasons):
            subscribed.set()

    def on_message(_client, _userdata, message):
        received.append((message.topic, bytes(message.payload)))

    subscriber.on_subscribe = on_subscribe
    subscriber.on_message = on_message
    deadline = time.monotonic() + 20
    while True:
        try:
            subscriber.connect("mqtt", 8883, keepalive=20)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
    tls_version = subscriber.socket().version()
    subscriber.loop_start()
    try:
        subscriber.subscribe(PREFIX + "/#", qos=1)
        if not subscribed.wait(10):
            raise TimeoutError(
                "Authenticated observer subscription was not acknowledged"
            )
        settings = {
            "host": "mqtt",
            "port": 8883,
            "ca_file": str(root / "ca.crt"),
            "username": "telemetry",
            "password_file": str(root / "telemetry-password"),
            "timeout_seconds": 8,
        }
        connection = PREFIX + "/connection/telemetry"
        allowed_topic = PREFIX + "/state"
        allowed_payload = secrets.token_hex(12).encode()
        publish_messages(
            settings,
            [(allowed_topic, allowed_payload, False)],
            "security_valid",
            connection,
        )
        deadline = time.monotonic() + 5
        while (allowed_topic, allowed_payload) not in received:
            if time.monotonic() >= deadline:
                raise AssertionError("Authorized TLS publication was not observed")
            time.sleep(0.05)
        report = {
            "tls_version": tls_version,
            "tls_enabled_by_default": True,
            "valid_ca_hostname_password_and_qos1": True,
            "authorized_publication_observed": True,
            "private_password_file_mode": oct(
                (root / "telemetry-password").stat().st_mode & 0o777
            ),
            "rejected": {},
        }
        variants = {
            "wrong_ca": {"ca_file": str(root / "wrong-ca.crt")},
            "hostname_mismatch": {"host": "mismatch"},
            "wrong_password": {"password_file": str(root / "wrong-password")},
        }
        for name, changes in variants.items():
            rejected_payload = secrets.token_hex(12).encode()
            started = time.monotonic()
            try:
                publish_messages(
                    dict(settings, **changes),
                    [(allowed_topic, rejected_payload, False)],
                    "security_" + name,
                    connection,
                )
            except MQTTError:
                report["rejected"][name] = {
                    "rejected": True,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            else:
                raise AssertionError(f"{name} unexpectedly accepted publication")
            if (allowed_topic, rejected_payload) in received:
                raise AssertionError(f"{name} delivered an unauthorized message")
        denied_topic = PREFIX + "/image"
        denied_payload = secrets.token_hex(12).encode()
        try:
            publish_messages(
                settings,
                [(denied_topic, denied_payload, False)],
                "security_acl",
                connection,
            )
            returned_success = True
        except MQTTError:
            returned_success = False
        after_payload = secrets.token_hex(12).encode()
        publish_messages(
            settings,
            [(allowed_topic, after_payload, False)],
            "security_after_acl",
            connection,
        )
        deadline = time.monotonic() + 5
        while (allowed_topic, after_payload) not in received:
            if time.monotonic() >= deadline:
                raise AssertionError("Observer did not receive the post-denial control")
            time.sleep(0.05)
        time.sleep(2)
        if (denied_topic, denied_payload) in received:
            raise AssertionError("Broker delivered a publication forbidden by its ACL")
        report["unauthorized_topic"] = {
            "publisher_returned_success": returned_success,
            "observed_by_authorized_subscriber": False,
            "allowed_control_received_after_denial": True,
            "observation_window_after_control_seconds": 2,
            "interpretation": "MQTT 3.1.1 QoS1 acknowledgement alone does not prove ACL-authorized delivery",
        }
        return report
    finally:
        subscriber.disconnect()
        subscriber.loop_stop()


def acceptance(output):
    root = Path(
        tempfile.mkdtemp(prefix="pi-ha-acceptance-security-", dir="/private/tmp")
    )
    broker_root, client_root = root / "broker", root / "client"
    broker_root.mkdir(mode=0o700)
    client_root.mkdir(mode=0o700)
    certificates(root, broker_root, client_root)
    source_root = Path(__file__).resolve().parents[1]
    project = client_root / "project"
    (project / "timelapse").mkdir(parents=True)
    (project / "ops").mkdir()
    for name in ("__init__.py", "ha_mqtt.py"):
        shutil.copyfile(source_root / "timelapse" / name, project / "timelapse" / name)
    shutil.copyfile(Path(__file__), project / "ops/verify_mqtt_security.py")
    suffix = root.name.removeprefix("pi-ha-acceptance-security-")
    network = "pi-ha-acceptance-security-" + suffix
    broker = network + "-broker"
    client = network + "-client"
    password_container = network + "-password"
    for index, username in enumerate(("telemetry", "observer")):
        password = secrets.token_urlsafe(32)
        write_private(client_root / (username + "-password"), password)
        arguments = [
            "run",
            "--rm",
            "-i",
            "--name",
            password_container,
            "--network",
            "none",
            "--entrypoint",
            "mosquitto_passwd",
            "--mount",
            f"type=bind,src={broker_root},dst=/fixture",
            BROKER_IMAGE,
        ]
        if index == 0:
            arguments.append("-c")
        docker(
            *arguments,
            "/fixture/passwords",
            username,
            input_data=password + "\n" + password + "\n",
        )
    write_private(client_root / "wrong-password", secrets.token_urlsafe(32))
    acl_template = (
        source_root / "deploy/home-assistant/mosquitto.acl.example"
    ).read_text()
    (broker_root / "acl").write_text(
        acl_template.replace("timelapse-zero-01", "security_camera")
        .replace("user security_camera", "user telemetry")
        .replace("user homeassistant", "user observer")
    )
    (broker_root / "mosquitto.conf").write_text(
        "listener 8883\nallow_anonymous false\n"
        "certfile /tmp/security/server.crt\nkeyfile /tmp/security/server.key\n"
        "password_file /tmp/security/passwords\nacl_file /tmp/security/acl\n"
        "persistence false\nlog_dest stdout\nlog_type all\n"
    )
    (broker_root / "start.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -m 700 /tmp/security\n"
        "cp /fixture/server.crt /fixture/server.key /fixture/passwords /fixture/acl /tmp/security/\n"
        "chown -R mosquitto:mosquitto /tmp/security\n"
        "exec mosquitto -c /fixture/mosquitto.conf\n"
    )
    created = []
    report = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "real TLS/authentication/ACL broker test with synthetic data and ephemeral credentials",
        "temporary_directory": str(root),
        "network_is_internal": True,
        "published_host_ports": [],
        "real_hardware_or_production_credentials_used": False,
        "production_transport_sha256": hashlib.sha256(
            (project / "timelapse/ha_mqtt.py").read_bytes()
        ).hexdigest(),
        "acl_template": "deploy/home-assistant/mosquitto.acl.example",
        "acl_template_sha256": hashlib.sha256(acl_template.encode()).hexdigest(),
        "acl_substitutions": "test-only device identifier and telemetry/observer usernames; whitespace preserved",
        "images": {
            image: json.loads(docker("image", "inspect", image))[0]["Id"]
            for image in (BROKER_IMAGE, CLIENT_IMAGE)
        },
    }
    try:
        docker("network", "create", "--internal", network)
        created.append(("network", network))
        docker(
            "run",
            "-d",
            "--name",
            broker,
            "--network",
            network,
            "--network-alias",
            "mqtt",
            "--network-alias",
            "mismatch",
            "--entrypoint",
            "sh",
            "--mount",
            f"type=bind,src={broker_root},dst=/fixture,readonly",
            BROKER_IMAGE,
            "/fixture/start.sh",
        )
        created.append(("container", broker))
        result = docker(
            "run",
            "--rm",
            "--name",
            client,
            "--network",
            network,
            "--entrypoint",
            "python3",
            "--mount",
            f"type=bind,src={client_root},dst=/test,readonly",
            "-w",
            "/test/project",
            CLIENT_IMAGE,
            "/test/project/ops/verify_mqtt_security.py",
            "--inside",
        )
        report.update(json.loads(result))
        logs = docker("logs", broker)
        report["broker_denied_publish_logged"] = (
            "Denied PUBLISH" in logs and PREFIX + "/image" in logs
        )
        report["mosquitto_version"] = docker(
            "exec", broker, "mosquitto", "-h"
        ).splitlines()[0]
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
                docker("rm", "-f", name) if kind == "container" else docker(
                    "network", "rm", name
                )
                cleanup.append({"kind": kind, "name": name, "removed": True})
            except subprocess.CalledProcessError:
                cleanup.append({"kind": kind, "name": name, "removed": False})
        report["cleanup"] = cleanup
        output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(output), "completed": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inside", action="store_true")
    args = parser.parse_args()
    if args.inside:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        print(json.dumps(inside()))
    elif args.run and args.output:
        acceptance(args.output.resolve())
    else:
        parser.error(
            "Explicitly pass --run and --output to create isolated test resources"
        )


if __name__ == "__main__":
    main()
