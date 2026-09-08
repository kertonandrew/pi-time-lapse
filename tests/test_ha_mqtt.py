import base64
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from timelapse import ha_mqtt


MESSAGES = [("camera/state", b'{"voltage":4.0}', True)]
CONNECTION_TOPIC = "camera/connection"


class FakeInfo:
    def __init__(self, rc=0, acknowledged=True, on_wait=None):
        self.rc = rc
        self.acknowledged = acknowledged
        self.on_wait = on_wait
        self.timeouts = []

    def wait_for_publish(self, timeout):
        self.timeouts.append(timeout)
        if self.on_wait:
            self.on_wait(timeout)

    def is_published(self):
        return self.acknowledged


class FakeClient:
    def __init__(self):
        self.reason_code = 0
        self.suppress_connack = False
        self.infos = []
        self.sent = []
        self.reply_factory = lambda _index: FakeInfo()
        self.will_set = Mock()
        self.username_pw_set = Mock()
        self.tls_set_context = Mock()
        self.tls_insecure_set = Mock()
        self.connection = Mock()
        self.socket = Mock(return_value=self.connection)
        self.loop_stop = Mock()
        self.disconnect_count = 0
        self.connect = Mock(return_value=0)
        self.incoming = []
        self.subscription_reasons = None
        self.suppress_suback = False
        self.disconnect_on_subscribe = False
        self.subscriptions = []

    def loop_start(self):
        if not self.suppress_connack:
            self.on_connect(self, None, None, self.reason_code, None)
        return 0

    def publish(self, topic, payload, qos, retain):
        self.sent.append((topic, payload, qos, retain))
        info = self.reply_factory(len(self.sent))
        self.infos.append(info)
        return info

    def disconnect(self):
        self.disconnect_count += 1
        self.on_disconnect(self, None, None, 0, None)
        return 0

    def subscribe(self, topics):
        self.subscriptions = topics
        if not self.suppress_suback:
            self.on_subscribe(
                self, None, 7, self.subscription_reasons or [1] * len(topics), None
            )
        for topic, payload in self.incoming:
            self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload))
        if self.disconnect_on_subscribe:
            self.on_disconnect(self, None, None, 1, None)
        return 0, 7


class MQTTSessionTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.mqtt = SimpleNamespace(
            Client=Mock(return_value=self.client),
            CallbackAPIVersion=SimpleNamespace(VERSION2="version-two"),
            MQTTv311=4,
            MQTT_ERR_SUCCESS=0,
        )
        self.context = Mock()
        self.importer = patch.object(
            ha_mqtt.importlib, "import_module", return_value=self.mqtt
        )
        self.tls = patch.object(
            ha_mqtt.ssl, "create_default_context", return_value=self.context
        )
        self.importer.start()
        self.create_context = self.tls.start()
        self.addCleanup(self.importer.stop)
        self.addCleanup(self.tls.stop)

    def publish(self, settings=None):
        ha_mqtt._publish_session(
            settings or {"host": "mqtt.example"},
            MESSAGES,
            "camera-telemetry",
            CONNECTION_TOPIC,
        )

    def receive(self, settings=None):
        return ha_mqtt._publish_session(
            settings or {"host": "mqtt.example"},
            [],
            "camera-controls-read",
            CONNECTION_TOPIC,
            (["camera/desired/interval_seconds"], 0.001),
        )

    def test_bounded_subscription_requires_suback_and_preserves_small_payloads(self):
        self.client.incoming = [("camera/desired/interval_seconds", b"300")]
        self.assertEqual(self.receive(), self.client.incoming)
        self.assertEqual(
            self.client.subscriptions, [("camera/desired/interval_seconds", 1)]
        )
        self.assertEqual(
            self.client.sent,
            [
                (CONNECTION_TOPIC, b"online", 1, True),
                (CONNECTION_TOPIC, b"offline", 1, True),
            ],
        )
        self.client.tls_set_context.assert_called_once_with(self.context)
        self.assertEqual(self.context.verify_mode, ssl.CERT_REQUIRED)

    def test_subscription_rejection_timeout_and_disconnect_never_return_commands(self):
        for failure in ("rejected", "timeout", "disconnected"):
            self.client.subscription_reasons = [128] if failure == "rejected" else None
            self.client.suppress_suback = failure == "timeout"
            self.client.disconnect_on_subscribe = failure == "disconnected"
            with self.subTest(failure=failure), self.assertRaises(ha_mqtt.MQTTError):
                self.receive(
                    {"host": "mqtt.lan", "tls": False, "timeout_seconds": 0.01}
                )
        self.assertEqual(self.client.disconnect_count, 0)

    def test_control_flood_oversize_and_unsubscribed_topic_abort_entire_batch(self):
        topic = "camera/desired/interval_seconds"
        for incoming in (
            [(topic, b"300")] * 65,
            [(topic, b"x" * 65)],
            [("other-camera/desired/interval_seconds", b"300")],
        ):
            self.client.incoming = incoming
            with (
                self.subTest(count=len(incoming)),
                self.assertRaises(ha_mqtt.MQTTError),
            ):
                self.receive()
        self.assertEqual(self.client.disconnect_count, 0)

    def test_qos1_acknowledged_online_messages_and_offline_use_clean_v311_session(self):
        self.publish()
        self.mqtt.Client.assert_called_once_with(
            callback_api_version="version-two",
            client_id="camera-telemetry",
            clean_session=True,
            protocol=4,
            reconnect_on_failure=False,
        )
        self.assertEqual(
            self.client.sent,
            [
                (CONNECTION_TOPIC, b"online", 1, True),
                ("camera/state", b'{"voltage":4.0}', 1, True),
                (CONNECTION_TOPIC, b"offline", 1, True),
            ],
        )
        self.client.will_set.assert_called_once_with(
            CONNECTION_TOPIC, b"offline", qos=1, retain=True
        )
        self.assertTrue(
            all(
                len(info.timeouts) == 1 and 0 < info.timeouts[0] <= 20
                for info in self.client.infos
            )
        )
        self.assertEqual(self.client.disconnect_count, 1)
        self.client.loop_stop.assert_called_once()
        self.client.connection.close.assert_not_called()

    def test_tls_defaults_to_verified_context_and_client_certificates_are_loaded(self):
        self.publish(
            {
                "host": "mqtt.example",
                "ca_file": "/tls/ca.pem",
                "cert_file": "/tls/cert.pem",
                "key_file": "/tls/key.pem",
            }
        )
        self.create_context.assert_called_once_with(cafile="/tls/ca.pem")
        self.assertTrue(self.context.check_hostname)
        self.assertEqual(self.context.verify_mode, ssl.CERT_REQUIRED)
        self.context.load_cert_chain.assert_called_once_with(
            "/tls/cert.pem", "/tls/key.pem"
        )
        self.client.tls_set_context.assert_called_once_with(self.context)
        self.client.tls_insecure_set.assert_not_called()

    def test_explicit_lan_plaintext_skips_tls(self):
        self.publish({"host": "mqtt.lan", "tls": False})
        self.create_context.assert_not_called()
        self.client.tls_set_context.assert_not_called()
        self.client.connect.assert_called_once_with("mqtt.lan", 1883, keepalive=20)

    def test_connack_refusal_never_publishes_or_gracefully_cancels_lwt(self):
        self.client.reason_code = 5
        with self.assertRaises(ha_mqtt.MQTTError):
            self.publish()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.client.disconnect_count, 0)
        self.client.connection.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        self.client.connection.close.assert_called_once()
        self.client.loop_stop.assert_called_once()

    def test_connack_timeout_is_bounded_and_closes_socket(self):
        self.client.suppress_connack = True
        with self.assertRaises(ha_mqtt.MQTTError):
            self.publish({"host": "mqtt.lan", "tls": False, "timeout_seconds": 0.01})
        self.assertEqual(self.client.sent, [])
        self.assertLessEqual(self.client.connect_timeout, 0.01)
        self.client.connection.close.assert_called_once()

    def test_unacknowledged_payload_fails_without_publishing_graceful_offline(self):
        self.client.reply_factory = lambda index: FakeInfo(acknowledged=index != 2)
        with self.assertRaises(ha_mqtt.MQTTError):
            self.publish()
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual(self.client.disconnect_count, 0)
        self.client.connection.close.assert_called_once()

    def test_rejected_publish_fails_before_waiting_for_ack(self):
        self.client.reply_factory = lambda index: FakeInfo(rc=4 if index == 2 else 0)
        with self.assertRaises(ha_mqtt.MQTTError):
            self.publish()
        self.assertEqual(self.client.infos[1].timeouts, [])

    def test_final_offline_puback_is_required(self):
        self.client.reply_factory = lambda index: FakeInfo(acknowledged=index != 3)
        with self.assertRaises(ha_mqtt.MQTTError):
            self.publish()
        self.assertEqual(self.client.sent[-1], (CONNECTION_TOPIC, b"offline", 1, True))
        self.assertEqual(self.client.disconnect_count, 0)

    def test_all_puback_waits_share_one_deadline(self):
        clock = [0.0]

        def consume(_timeout):
            clock[0] += 0.75

        self.client.reply_factory = lambda _index: FakeInfo(on_wait=consume)
        with patch.object(ha_mqtt.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(ha_mqtt.MQTTError):
                self.publish({"host": "mqtt.lan", "tls": False, "timeout_seconds": 2})
        self.assertEqual(
            [info.timeouts[0] for info in self.client.infos], [2, 1.25, 0.5]
        )

    def test_private_password_file_is_read_without_logging_or_exception_leakage(self):
        with tempfile.TemporaryDirectory() as temporary:
            password = Path(temporary) / "password"
            password.write_text("do-not-expose-this-password\n")
            password.chmod(0o600)
            settings = {
                "host": "mqtt.lan",
                "username": "camera",
                "password_file": str(password),
            }
            self.publish(settings)
            self.client.username_pw_set.assert_called_with(
                "camera", "do-not-expose-this-password"
            )
            self.client.username_pw_set.side_effect = RuntimeError(
                "do-not-expose-this-password"
            )
            try:
                self.publish(settings)
            except ha_mqtt.MQTTError:
                self.assertNotIn("do-not-expose-this-password", traceback.format_exc())
            else:
                self.fail("Credential setup failure was not rejected")

    def test_password_file_rejects_shared_permissions_symlinks_and_nonregular_files(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            password = Path(temporary) / "password"
            password.write_text("secret")
            password.chmod(0o644)
            with self.assertRaises(ha_mqtt.MQTTError):
                ha_mqtt._password(password)
            password.chmod(0o600)
            link = Path(temporary) / "link"
            link.symlink_to(password)
            with self.assertRaises(ha_mqtt.MQTTError):
                ha_mqtt._password(link)
            with self.assertRaises(ha_mqtt.MQTTError):
                ha_mqtt._password(Path(temporary))


class MQTTWorkerTests(unittest.TestCase):
    def test_publish_and_receive_workers_preserve_parent_import_isolation(self):
        def reply(*_args, **kwargs):
            if kwargs["stdout"] != subprocess.DEVNULL:
                kwargs["stdout"].write(b"[]")
            return SimpleNamespace(returncode=0)

        for isolated in (0, 1):
            with (
                self.subTest(isolated=isolated),
                patch.object(ha_mqtt.sys, "flags", SimpleNamespace(isolated=isolated)),
                patch.object(ha_mqtt.subprocess, "run", side_effect=reply) as run,
            ):
                ha_mqtt.publish_messages(
                    {"host": "mqtt.lan"}, MESSAGES, "camera", CONNECTION_TOPIC
                )
                ha_mqtt.receive_messages(
                    {"host": "mqtt.lan"},
                    ["camera/desired/interval_seconds"],
                    "camera-controls",
                    CONNECTION_TOPIC,
                )
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertEqual(
                    call.args[0],
                    [
                        ha_mqtt.sys.executable,
                        *(["-I"] if isolated else []),
                        "-m",
                        "timelapse.ha_mqtt",
                        "--worker",
                    ],
                )

    def test_control_worker_result_is_bounded_decoded_and_uses_no_secret_arguments(
        self,
    ):
        topic = "camera/desired/interval_seconds"

        def reply(*_args, **kwargs):
            kwargs["stdout"].write(json.dumps([(topic, "MzAw")]).encode())
            return SimpleNamespace(returncode=0)

        settings = {
            "host": "mqtt.lan",
            "username": "camera",
            "password_file": "/run/secrets/mqtt",
        }
        with patch.object(ha_mqtt.subprocess, "run", side_effect=reply) as run:
            result = ha_mqtt.receive_messages(
                settings, [topic], "camera-controls", CONNECTION_TOPIC
            )
        self.assertEqual(result, [(topic, b"300")])
        self.assertNotIn("/run/secrets/mqtt", repr(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["mode"], "receive")
        self.assertEqual(request["topics"], [topic])
        self.assertLessEqual(run.call_args.kwargs["timeout"], 20)

    def test_control_result_and_subscription_validation_are_bounded(self):
        topic = "camera/desired/interval_seconds"
        results = [
            b"x" * (ha_mqtt.MAX_CONTROL_RESULT_BYTES + 1),
            b"{}",
            b"not JSON",
            json.dumps([(topic, "MzAw")] * 65).encode(),
            json.dumps([("other-camera/desired/interval_seconds", "MzAw")]).encode(),
            json.dumps([(topic, base64.b64encode(b"x" * 65).decode())]).encode(),
        ]
        for payload in results:

            def reply(*_args, **kwargs):
                kwargs["stdout"].write(payload)
                return SimpleNamespace(returncode=0)

            with patch.object(ha_mqtt.subprocess, "run", side_effect=reply):
                with self.assertRaises(ha_mqtt.MQTTError):
                    ha_mqtt.receive_messages(
                        {"host": "mqtt.lan"}, [topic], "camera", CONNECTION_TOPIC
                    )
        with patch.object(ha_mqtt.subprocess, "run") as run:
            for topics, poll in (
                (["camera/#"], 3),
                ([topic] * 2, 3),
                ([], 3),
                ([topic], float("inf")),
                ([topic], 11),
            ):
                with self.assertRaises(ha_mqtt.MQTTError):
                    ha_mqtt.receive_messages(
                        {"host": "mqtt.lan"}, topics, "camera", CONNECTION_TOPIC, poll
                    )
        run.assert_not_called()

    def test_worker_ipc_encodes_bytes_but_exposes_no_credentials_in_argv_or_output(
        self,
    ):
        settings = {
            "host": "mqtt.lan",
            "username": "camera",
            "password_file": "/run/secrets/mqtt",
            "tls": False,
        }
        messages = [("camera/photo", b"\xff\xd8binary jpeg\xff\xd9", True)]
        with patch.object(
            ha_mqtt.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            ha_mqtt.publish_messages(
                settings, messages, "camera-photos", CONNECTION_TOPIC
            )
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(base64.b64decode(request["messages"][0][1]), messages[0][1])
        self.assertNotIn("/run/secrets/mqtt", repr(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 20)

    def test_worker_failure_or_timeout_never_reports_success_or_raw_error(self):
        for failure in (
            SimpleNamespace(returncode=1),
            subprocess.TimeoutExpired(["secret"], 20, output=b"private-data"),
        ):
            with self.subTest(failure=failure):
                arguments = (
                    {"side_effect": failure}
                    if isinstance(failure, Exception)
                    else {"return_value": failure}
                )
                with patch.object(ha_mqtt.subprocess, "run", **arguments):
                    try:
                        ha_mqtt.publish_messages(
                            {"host": "mqtt.lan"}, MESSAGES, "camera", CONNECTION_TOPIC
                        )
                    except ha_mqtt.MQTTError:
                        details = traceback.format_exc()
                        self.assertNotIn("private-data", details)
                        self.assertNotIn("['secret']", details)
                    else:
                        self.fail("Failed worker reported success")

    def test_invalid_publish_settings_fail_before_starting_worker(self):
        invalid_settings = [
            {"host": "mqtt.lan", "password": "example-only-password"},
            {"host": "mqtt.lan", "tls": "false"},
            {"host": "mqtt.lan", "tls": False, "ca_file": "/ca.pem"},
            {"host": "mqtt.lan", "password_file": "/secret"},
            {"host": "mqtt.lan", "timeout_seconds": float("nan")},
        ]
        with patch.object(ha_mqtt.subprocess, "run") as run:
            for settings in invalid_settings:
                with (
                    self.subTest(settings=settings),
                    self.assertRaises(ha_mqtt.MQTTError),
                ):
                    ha_mqtt.publish_messages(
                        settings, MESSAGES, "camera", CONNECTION_TOPIC
                    )
            with self.assertRaises(ha_mqtt.MQTTError):
                ha_mqtt.publish_messages(
                    {"host": "mqtt.lan"},
                    [(CONNECTION_TOPIC, b"online", True)],
                    "camera",
                    CONNECTION_TOPIC,
                )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
