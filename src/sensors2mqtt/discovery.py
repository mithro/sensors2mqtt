"""Home Assistant MQTT auto-discovery helpers.

Provides SensorDef (typed sensor definition) and functions to build
HA-compatible discovery and state messages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt

DISCOVERY_PREFIX = "homeassistant"


@dataclass(frozen=True)
class SensorDef:
    """Definition of a single sensor for HA auto-discovery.

    Attributes:
        suffix: Entity suffix used in MQTT topics and JSON keys (e.g. "asic_temp").
        name: Human-readable name shown in HA (e.g. "ASIC Temperature").
        unit: Unit of measurement (e.g. "°C", "RPM", "W").
        device_class: HA device class (e.g. "temperature", "power"). None if N/A.
        state_class: HA state class. Defaults to "measurement".
        icon: MDI icon override (e.g. "mdi:fan"). None uses HA default.
        entity_category: HA entity category (e.g. "diagnostic"). None for normal.
    """

    suffix: str
    name: str
    unit: str
    device_class: str | None = None
    state_class: str = "measurement"
    icon: str | None = None
    entity_category: str | None = None


@dataclass(frozen=True)
class DeviceInfo:
    """HA device registry info.

    Attributes:
        node_id: Python-safe identifier (e.g. "sw_bb_25g"). Used in MQTT topics.
        name: Display name (e.g. "sw-bb-25g").
        manufacturer: Device manufacturer.
        model: Device model.
        configuration_url: Optional URL to device management interface.
    """

    node_id: str
    name: str
    manufacturer: str
    model: str
    configuration_url: str | None = None


def discovery_payload(
    sensor: SensorDef,
    device: DeviceInfo,
    state_topic: str,
    avail_topic: str,
) -> dict:
    """Build HA auto-discovery config payload for a sensor."""
    config = {
        "name": sensor.name,
        "unique_id": f"{device.node_id}_{sensor.suffix}",
        "state_topic": state_topic,
        "value_template": f"{{{{ value_json.{sensor.suffix} }}}}",
        "unit_of_measurement": sensor.unit,
        "state_class": sensor.state_class,
        "device": _device_dict(device),
        "availability_topic": avail_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    if sensor.device_class:
        config["device_class"] = sensor.device_class
    if sensor.icon:
        config["icon"] = sensor.icon
    if sensor.entity_category:
        config["entity_category"] = sensor.entity_category
    return config


def publish_discovery(
    client: mqtt.Client,
    sensors: list[SensorDef],
    device: DeviceInfo,
    state_topic: str,
    avail_topic: str,
) -> int:
    """Publish HA auto-discovery configs for all sensors. Returns count published."""
    for sensor in sensors:
        config_topic = f"{DISCOVERY_PREFIX}/sensor/{device.node_id}/{sensor.suffix}/config"
        payload = discovery_payload(sensor, device, state_topic, avail_topic)
        client.publish(config_topic, json.dumps(payload), retain=True)
    return len(sensors)


def publish_state(client: mqtt.Client, state_topic: str, values: dict) -> None:
    """Publish sensor state as JSON."""
    client.publish(state_topic, json.dumps(values), retain=True)


def _device_dict(device: DeviceInfo) -> dict:
    """Build HA device registry dict."""
    d = {
        "identifiers": [f"sensors2mqtt_{device.node_id}"],
        "name": device.name,
        "manufacturer": device.manufacturer,
        "model": device.model,
    }
    if device.configuration_url:
        d["configuration_url"] = device.configuration_url
    return d
