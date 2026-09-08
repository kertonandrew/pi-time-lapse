import json
import unittest

from timelapse.ha_discovery import SENSORS, build_discovery, topics


class DiscoveryTests(unittest.TestCase):
    def test_one_device_groups_stable_sensor_camera_and_connection_identities(self):
        topic, payload = build_discovery("garden_01", "Garden camera")
        self.assertEqual(topic, "homeassistant/device/garden_01/config")
        self.assertEqual(payload["device"]["identifiers"], ["pi_timelapse_garden_01"])
        self.assertEqual(payload["origin"]["name"], "pi-time-lapse")
        components = payload["components"]
        self.assertEqual(
            set(components),
            set(SENSORS)
            | {"latest_photo", "last_uploaded_capture", "publisher_connection"},
        )
        self.assertEqual(
            len({item["unique_id"] for item in components.values()}), len(components)
        )
        renamed = build_discovery("garden_01", "Renamed camera")[1]
        self.assertEqual(components, renamed["components"])
        json.dumps(payload, allow_nan=False)

    def test_each_live_metric_expires_without_publisher_availability(self):
        payload = build_discovery("camera", "Camera", expire_after=600)[1]
        self.assertNotIn("availability", payload)
        self.assertNotIn("availability_topic", payload)
        self.assertNotIn("state_topic", payload)
        for key in SENSORS:
            with self.subTest(key=key):
                sensor = payload["components"][key]
                self.assertEqual(sensor["platform"], "sensor")
                self.assertEqual(sensor["state_topic"], "pi_timelapse/camera/state")
                self.assertEqual(sensor["expire_after"], 600)
                self.assertEqual(
                    sensor["value_template"], "{{ value_json.get('" + key + "') }}"
                )
                self.assertNotIn("availability", sensor)
                self.assertNotIn("availability_topic", sensor)
                self.assertNotIn("force_update", sensor)

    def test_camera_is_binary_and_historical_image_remains_available(self):
        components = build_discovery("camera", "Camera")[1]["components"]
        camera = components["latest_photo"]
        self.assertEqual(camera["platform"], "camera")
        self.assertEqual(camera["topic"], "pi_timelapse/camera/image")
        self.assertEqual(camera["encoding"], "")
        self.assertEqual(
            camera["json_attributes_topic"], "pi_timelapse/camera/image_metadata"
        )
        self.assertNotIn("image_encoding", camera)
        self.assertNotIn("expire_after", camera)
        self.assertNotIn("availability", camera)
        captured = components["last_uploaded_capture"]
        self.assertEqual(captured["device_class"], "timestamp")
        self.assertEqual(captured["state_topic"], camera["json_attributes_topic"])
        self.assertIn("time_source", captured["value_template"])
        self.assertIn("else none", captured["value_template"])
        self.assertNotIn("expire_after", captured)

    def test_unvalidated_estimates_are_explicit_and_disabled_by_default(self):
        components = build_discovery("camera", "Camera")[1]["components"]
        for key in (
            "battery_percent_estimate",
            "battery_current_estimate",
            "battery_power_estimate",
            "reported_battery_temperature",
        ):
            with self.subTest(key=key):
                self.assertEqual(components[key]["entity_category"], "diagnostic")
                self.assertFalse(components[key]["enabled_by_default"])
        self.assertIn("unverified", components["reported_battery_temperature"]["name"])
        self.assertEqual(components["battery_voltage"]["unit_of_measurement"], "V")
        self.assertEqual(components["memory_available"]["unit_of_measurement"], "MiB")
        self.assertEqual(components["uptime"]["device_class"], "duration")
        for key in (
            "battery_status",
            "usb_input_status",
            "gpio_input_status",
            "time_source",
            "observed_at",
            "last_uploaded_capture",
        ):
            self.assertNotIn("state_class", components[key])
        self.assertFalse(any("solar" in key or "energy" in key for key in components))

    def test_topic_overrides_preserve_identity(self):
        expected = topics("camera", "home/cameras", "custom/discovery")
        self.assertEqual(expected["state"], "home/cameras/camera/state")
        self.assertEqual(expected["discovery"], "custom/discovery/device/camera/config")
        custom = build_discovery(
            "camera", "Camera", "home/cameras", "custom/discovery"
        )[1]
        normal = build_discovery("camera", "Camera")[1]
        self.assertEqual(custom["device"], normal["device"])
        self.assertEqual(
            custom["components"]["battery_voltage"]["unique_id"],
            normal["components"]["battery_voltage"]["unique_id"],
        )

    def test_topic_identifiers_reject_wildcards_and_ambiguous_paths(self):
        for value in ("", "one/two", "+", "#", "camera\x00", "a" * 65, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                topics(value)
        for value in (
            "",
            "/prefix",
            "prefix/",
            "a//b",
            "$SYS",
            "+",
            "#",
            "x" * 129,
            None,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                topics("camera", value)

    def test_names_and_expiration_require_valid_values(self):
        for value in ("", " ", None, "a" * 101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_discovery("camera", value)
        for value in (True, 0, 59, 86401, 900.0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_discovery("camera", "Camera", expire_after=value)


if __name__ == "__main__":
    unittest.main()
