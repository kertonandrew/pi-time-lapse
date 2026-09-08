"""Publish acknowledged MQTT messages within one bounded worker process."""

import base64
import contextlib
import importlib
import json
import math
import os
from pathlib import Path
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time


MAX_REQUEST_BYTES = 67108864
MAX_PAYLOAD_BYTES = 37748736
SETTINGS = {
    "host",
    "port",
    "username",
    "password_file",
    "tls",
    "ca_file",
    "cert_file",
    "key_file",
    "timeout_seconds",
}


class MQTTError(RuntimeError):
    pass


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MQTTError("MQTT operation deadline expired")
    return remaining


def _topic(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 65535
        or any(character in value for character in ("\x00", "+", "#"))
    ):
        raise MQTTError("Invalid MQTT publish topic")


def _settings(settings):
    if not isinstance(settings, dict) or set(settings) - SETTINGS:
        raise MQTTError("Invalid MQTT settings")
    result = dict(settings)
    if (
        not isinstance(result.get("host"), str)
        or not result["host"]
        or "\x00" in result["host"]
    ):
        raise MQTTError("MQTT host is required")
    tls = result.setdefault("tls", True)
    if type(tls) is not bool:
        raise MQTTError("MQTT TLS must be explicitly true or false")
    port = result.setdefault("port", 8883 if tls else 1883)
    if type(port) is not int or not 1 <= port <= 65535:
        raise MQTTError("MQTT port is invalid")
    timeout = result.setdefault("timeout_seconds", 20)
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 120
    ):
        raise MQTTError("MQTT timeout must be positive and at most 120 seconds")
    username = result.get("username")
    if username is not None and (
        not isinstance(username, str) or not username or "\x00" in username
    ):
        raise MQTTError("MQTT username is invalid")
    if result.get("password_file") and username is None:
        raise MQTTError("A username is required with a password file")
    for name in ("password_file", "ca_file", "cert_file", "key_file"):
        value = result.get(name)
        if value is not None:
            if (
                not isinstance(value, (str, Path))
                or not Path(value).is_absolute()
                or "\x00" in str(value)
            ):
                raise MQTTError(
                    "MQTT credential and certificate paths must be absolute"
                )
            result[name] = str(value)
    if not tls and any(
        result.get(name) for name in ("ca_file", "cert_file", "key_file")
    ):
        raise MQTTError("TLS certificate settings require TLS")
    if result.get("key_file") and not result.get("cert_file"):
        raise MQTTError("A client certificate is required with a key file")
    return result


def _password(path):
    if path is None:
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_mode & 0o077
                or before.st_size > 4096
            ):
                raise ValueError
            data = stream.read(4097)
            after = os.fstat(stream.fileno())
            if (
                len(data) > 4096
                or after.st_mode & 0o077
                or (
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                )
                != (after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise ValueError
            value = data.decode("utf-8").rstrip("\r\n")
            if not value or "\x00" in value:
                raise ValueError
            return value
    except (OSError, ValueError, UnicodeError):
        raise MQTTError(
            "MQTT password file must be a private regular UTF-8 file"
        ) from None


def _messages(messages, connection_topic):
    _topic(connection_topic)
    if not isinstance(messages, list) or len(messages) > 1024:
        raise MQTTError("MQTT publication batch exceeds its message limit")
    total = 0
    for message in messages:
        if not isinstance(message, (list, tuple)) or len(message) != 3:
            raise MQTTError("Invalid MQTT message")
        topic, payload, retain = message
        _topic(topic)
        if topic == connection_topic:
            raise MQTTError("Application messages must not overwrite connection status")
        if not isinstance(payload, bytes) or type(retain) is not bool:
            raise MQTTError("MQTT payloads must be bytes with a boolean retain flag")
        total += len(payload)
    if total > MAX_PAYLOAD_BYTES:
        raise MQTTError("MQTT publication batch exceeds its payload byte limit")


def _publish_session(settings, messages, client_id, connection_topic):
    settings = _settings(settings)
    _messages(messages, connection_topic)
    deadline = time.monotonic() + settings["timeout_seconds"]
    client = None
    loop_started = False
    graceful = False
    try:
        mqtt = importlib.import_module("paho.mqtt.client")
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
            reconnect_on_failure=False,
        )
        client.connect_timeout = min(10, _remaining(deadline))
        client.will_set(connection_topic, b"offline", qos=1, retain=True)
        if settings.get("username") is not None:
            client.username_pw_set(
                settings["username"], _password(settings.get("password_file"))
            )
        if settings["tls"]:
            context = ssl.create_default_context(cafile=settings.get("ca_file"))
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            if settings.get("cert_file"):
                context.load_cert_chain(settings["cert_file"], settings.get("key_file"))
            client.tls_set_context(context)
        connected = threading.Event()
        state = {"accepted": False, "disconnected": False}

        def on_connect(_client, _userdata, _flags, reason_code, _properties):
            state["accepted"] = reason_code == 0
            connected.set()

        def on_disconnect(_client, _userdata, _flags, _reason_code, _properties):
            state["disconnected"] = True
            connected.set()

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        if (
            client.connect(settings["host"], settings["port"], keepalive=20)
            != mqtt.MQTT_ERR_SUCCESS
        ):
            raise MQTTError("MQTT connection failed")
        if client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
            raise MQTTError("MQTT network loop failed")
        loop_started = True
        if (
            not connected.wait(_remaining(deadline))
            or not state["accepted"]
            or state["disconnected"]
        ):
            raise MQTTError("MQTT connection was refused or unacknowledged")
        for topic, payload, retain in (
            (connection_topic, b"online", True),
            *messages,
            (connection_topic, b"offline", True),
        ):
            _remaining(deadline)
            if state["disconnected"]:
                raise MQTTError("MQTT connection ended before publication completed")
            info = client.publish(topic, payload, qos=1, retain=retain)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise MQTTError("MQTT publish was rejected")
            info.wait_for_publish(timeout=_remaining(deadline))
            if not info.is_published() or state["disconnected"]:
                raise MQTTError("MQTT PUBACK was not received")
            _remaining(deadline)
        if client.disconnect() != mqtt.MQTT_ERR_SUCCESS:
            raise MQTTError("MQTT graceful disconnect failed")
        graceful = True
    except ModuleNotFoundError:
        raise MQTTError(
            "MQTT publishing requires the optional paho-mqtt dependency"
        ) from None
    except Exception:
        raise MQTTError("MQTT connection or acknowledged publication failed") from None
    finally:
        if client is not None:
            if not graceful:
                with contextlib.suppress(Exception):
                    connection = client.socket()
                    if connection is not None:
                        with contextlib.suppress(OSError):
                            connection.shutdown(socket.SHUT_RDWR)
                        connection.close()
            if loop_started:
                with contextlib.suppress(Exception):
                    client.loop_stop()


def publish_messages(
    settings: dict,
    messages: list[tuple[str, bytes, bool]],
    client_id: str,
    connection_topic: str,
) -> None:
    """Return only after QoS1 acknowledgements, including retained graceful offline."""
    started = time.monotonic()
    settings = _settings(settings)
    _messages(messages, connection_topic)
    if (
        not isinstance(client_id, str)
        or not 1 <= len(client_id) <= 128
        or "\x00" in client_id
    ):
        raise MQTTError("MQTT client identifier is invalid")
    deadline = started + settings["timeout_seconds"]
    request = json.dumps(
        {
            "settings": settings,
            "messages": [
                (topic, base64.b64encode(payload).decode("ascii"), retain)
                for topic, payload, retain in messages
            ],
            "client_id": client_id,
            "connection_topic": connection_topic,
        },
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    if len(request) > MAX_REQUEST_BYTES:
        raise MQTTError("MQTT worker request exceeds its byte limit")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "timelapse.ha_mqtt", "--worker"],
            input=request,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_remaining(deadline),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise MQTTError(
            "MQTT worker failed or exceeded the operation deadline"
        ) from None
    if result.returncode != 0:
        raise MQTTError("MQTT worker could not acknowledge every publication")
    _remaining(deadline)


def main():
    if sys.argv[1:] != ["--worker"]:
        return 2
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            return 2
        request = json.loads(raw)
        messages = [
            (topic, base64.b64decode(payload, validate=True), retain)
            for topic, payload, retain in request["messages"]
        ]
        _publish_session(
            request["settings"],
            messages,
            request["client_id"],
            request["connection_topic"],
        )
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
