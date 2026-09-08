import re

from .ha_controls import FIELDS, INTEGER_LIMITS, RESOLUTIONS


IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
PREFIX = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")

SENSORS = {
    "battery_percent_estimate": ("Battery estimate", "%", "battery", True),
    "battery_voltage": ("Battery voltage", "V", "voltage", False),
    "battery_current_estimate": ("Battery current estimate", "A", "current", True),
    "battery_power_estimate": ("Battery power estimate", "W", "power", True),
    "reported_battery_temperature": (
        "Reported battery temperature (unverified)",
        "°C",
        "temperature",
        True,
    ),
    "io_voltage": ("Pi supply voltage", "V", "voltage", False),
    "io_current": ("Pi supply current", "A", "current", False),
    "io_power": ("Pi supply power", "W", "power", False),
    "cpu_temperature": ("CPU temperature", "°C", "temperature", False),
    "memory_available": ("Available memory", "MiB", "data_size", False),
    "load_1m": ("System load (1 minute)", None, None, False),
    "uptime": ("Uptime", "s", "duration", False),
    "battery_status": ("Battery status", None, None, False),
    "usb_input_status": ("USB input status", None, None, False),
    "gpio_input_status": ("GPIO input status", None, None, False),
    "time_source": ("Clock source", None, None, False),
    "observed_at": ("Telemetry observed at", None, "timestamp", False),
    "telemetry_errors": ("Telemetry errors", None, None, False),
    "pending_photos": ("Pending photographs", None, None, False),
    "stored_photos": ("Stored photographs", None, None, False),
    "spool_bytes": ("Photograph storage", "B", "data_size", False),
    "free_bytes": ("Free storage", "B", "data_size", False),
}
TEXT_SENSORS = {
    "battery_status",
    "usb_input_status",
    "gpio_input_status",
    "time_source",
    "observed_at",
}


def topics(device_id, topic_prefix="pi_timelapse", discovery_prefix="homeassistant"):
    if not isinstance(device_id, str) or not IDENTIFIER.fullmatch(device_id):
        raise ValueError(
            "device_id must contain 1 to 64 letters, digits, underscores or hyphens"
        )
    for prefix in (topic_prefix, discovery_prefix):
        if (
            not isinstance(prefix, str)
            or len(prefix) > 128
            or not PREFIX.fullmatch(prefix)
        ):
            raise ValueError("MQTT prefixes must contain nonempty safe topic segments")
    base = f"{topic_prefix}/{device_id}"
    return {
        "discovery": f"{discovery_prefix}/device/{device_id}/config",
        **{
            name: f"{base}/{name}"
            for name in (
                "state",
                "image",
                "image_metadata",
                "connection",
                "desired",
                "reported",
            )
        },
    }


def build_discovery(
    device_id,
    device_name,
    topic_prefix="pi_timelapse",
    discovery_prefix="homeassistant",
    expire_after=900,
):
    destination = topics(device_id, topic_prefix, discovery_prefix)
    if (
        not isinstance(device_name, str)
        or not device_name.strip()
        or len(device_name) > 100
    ):
        raise ValueError("device_name must contain 1 to 100 characters")
    if type(expire_after) is not int or not 60 <= expire_after <= 86400:
        raise ValueError("expire_after must be an integer between 60 and 86400 seconds")
    components = {}
    for key, (name, unit, device_class, estimated) in SENSORS.items():
        component = {
            "platform": "sensor",
            "name": name,
            "unique_id": f"pi_timelapse_{device_id}_{key}",
            "state_topic": destination["state"],
            "value_template": "{{ value_json.get('" + key + "') }}",
            "expire_after": expire_after,
            "qos": 1,
        }
        if unit is not None:
            component["unit_of_measurement"] = unit
        if device_class is not None:
            component["device_class"] = device_class
        if key not in TEXT_SENSORS:
            component["state_class"] = "measurement"
        if estimated:
            component.update(entity_category="diagnostic", enabled_by_default=False)
        if key in {
            "telemetry_errors",
            "time_source",
            "observed_at",
            "uptime",
            "load_1m",
        }:
            component["entity_category"] = "diagnostic"
        components[key] = component
    components["latest_photo"] = {
        "platform": "camera",
        "name": "Latest uploaded photograph",
        "unique_id": f"pi_timelapse_{device_id}_latest_photo",
        "topic": destination["image"],
        "encoding": "",
        "json_attributes_topic": destination["image_metadata"],
    }
    components["last_uploaded_capture"] = {
        "platform": "sensor",
        "name": "Last uploaded capture",
        "unique_id": f"pi_timelapse_{device_id}_last_uploaded_capture",
        "device_class": "timestamp",
        "state_topic": destination["image_metadata"],
        "value_template": "{{ value_json.get('captured_at_utc') if value_json.get('time_source') in ['NTP', 'RTC'] else none }}",
        "qos": 1,
    }
    components["publisher_connection"] = {
        "platform": "binary_sensor",
        "name": "Publisher connection",
        "unique_id": f"pi_timelapse_{device_id}_publisher_connection",
        "device_class": "connectivity",
        "state_topic": destination["connection"] + "/telemetry",
        "payload_on": "online",
        "payload_off": "offline",
        "entity_category": "diagnostic",
        "enabled_by_default": False,
        "qos": 1,
    }
    names = {
        "capture_enabled": "Capture enabled",
        "interval_seconds": "Capture interval",
        "resolution": "Capture resolution",
        "jpeg_quality": "JPEG quality",
        "rotation": "Capture rotation",
        "settle_ms": "Camera settling time",
    }
    for field in FIELDS:
        component = {
            "name": names[field],
            "unique_id": f"pi_timelapse_{device_id}_control_{field}",
            "command_topic": f"{destination['desired']}/{field}",
            "state_topic": f"{destination['reported']}/{field}",
            "optimistic": False,
            "retain": True,
            "qos": 1,
            "entity_category": "config",
        }
        if field == "capture_enabled":
            component.update(platform="switch", payload_on="true", payload_off="false")
        elif field in ("resolution", "rotation"):
            component.update(
                platform="select",
                options=list(RESOLUTIONS) if field == "resolution" else ["0", "180"],
            )
        else:
            minimum, maximum = INTEGER_LIMITS[field]
            component.update(
                platform="number",
                min=minimum,
                max=maximum,
                step=1,
                mode="box",
                command_template="{{ value | int }}",
            )
            if field in ("interval_seconds", "settle_ms"):
                component["unit_of_measurement"] = (
                    "s" if field == "interval_seconds" else "ms"
                )
        components[f"control_{field}"] = component
    return destination["discovery"], {
        "device": {
            "identifiers": [f"pi_timelapse_{device_id}"],
            "name": device_name,
            "model": "Raspberry Pi timelapse camera",
            "sw_version": "0.1.0",
        },
        "origin": {"name": "pi-time-lapse", "sw_version": "0.1.0"},
        "components": components,
    }
