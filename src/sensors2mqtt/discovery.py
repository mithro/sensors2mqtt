"""Home Assistant MQTT auto-discovery helpers.

Provides SensorDef (typed sensor definition) and functions to build
HA-compatible discovery and state messages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from sensors2mqtt import __version__

DISCOVERY_PREFIX = "homeassistant"

# Seconds after which HA marks a push entity unavailable if no fresh state arrives.
# Connection-agnostic freshness: any host publishing keeps a shared entity alive;
# all silent -> it expires. Generous enough that a slow sequential SNMP poll cycle
# never flaps an entity.
EXPIRE_AFTER = 300

ORIGIN = {
    "name": "sensors2mqtt",
    "sw": __version__,
    "url": "https://github.com/mithro/sensors2mqtt",
}


@dataclass(frozen=True)
class SensorDef:
    """Definition of a single sensor for HA auto-discovery.

    Attributes:
        suffix: Entity suffix used in MQTT topics and JSON keys (e.g. "asic_temp").
        name: Human-readable name shown in HA (e.g. "ASIC Temperature").
        unit: Unit of measurement (e.g. "°C", "RPM", "W").
        device_class: HA device class (e.g. "temperature", "power"). None if N/A.
        state_class: HA state class. None for non-numeric sensors. "measurement" for
            point-in-time readings. Only set on numeric sensors — HA logs errors if
            state_class is set on string/enum sensors.
        icon: MDI icon override (e.g. "mdi:fan"). None uses HA default.
        entity_category: HA entity category (e.g. "diagnostic"). None for normal.
    """

    suffix: str
    name: str
    unit: str
    device_class: str | None = None
    state_class: str | None = None
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
        connections: HA device connections for cross-integration linking.
            Typically MAC addresses: (("mac", "aa:bb:cc:dd:ee:ff"),).
        via_device: Identifier of a parent device (e.g. switch that a port belongs to).
    """

    node_id: str
    name: str
    manufacturer: str
    model: str
    configuration_url: str | None = None
    connections: tuple[tuple[str, str], ...] | None = None
    via_device: str | None = None


def availability_config(*topics: str | None, mode: str = "all") -> dict:
    """Build the HA availability portion of a discovery payload.

    One topic -> a single ``availability_topic``. Two or more (e.g. a device's
    own status plus a per-collector bridge status) -> an ``availability`` list
    with ``availability_mode`` (default ``all``: available only if every listed
    topic is online). ``None`` topics are ignored, so callers can pass an
    optional bridge topic unconditionally.
    """
    live = [t for t in topics if t]
    if len(live) <= 1:
        return {
            "availability_topic": live[0] if live else "",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
    return {
        "availability": [
            {"topic": t, "payload_available": "online", "payload_not_available": "offline"}
            for t in live
        ],
        "availability_mode": mode,
    }


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
        "device": device_dict(device),
        "expire_after": EXPIRE_AFTER,
        **availability_config(avail_topic),
        "origin": ORIGIN,
    }
    if sensor.state_class:
        config["state_class"] = sensor.state_class
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
    """Publish sensor state as JSON. Retained so new clients get current values."""
    client.publish(state_topic, json.dumps(values), retain=True)


def device_dict(device: DeviceInfo) -> dict:
    """Build HA device registry dict."""
    d: dict = {
        "identifiers": [f"sensors2mqtt_{device.node_id}"],
        "name": device.name,
    }
    if device.manufacturer and device.manufacturer != "Unknown":
        d["manufacturer"] = device.manufacturer
    if device.model and device.model != "Unknown":
        d["model"] = device.model
    if device.configuration_url:
        d["configuration_url"] = device.configuration_url
    if device.connections:
        d["connections"] = [list(c) for c in device.connections]
    if device.via_device:
        d["via_device"] = device.via_device
    return d


def publish_connection_diagnostic(
    client: mqtt.Client, host: str, module: str, hostname: str
) -> None:
    """Publish a per-host, per-daemon connectivity binary_sensor.

    Attaches to the host's device (identifiers + name only, so it never clobbers
    the manufacturer/model that a hardware-aware collector sets). Its state is the
    daemon's connection status topic, which is the daemon's Last-Will + per-cycle
    heartbeat. Surfaces "which daemon on which host is alive" without gating any
    shared device.
    """
    status_topic = f"sensors2mqtt/{host}/{module}/status"
    config = {
        "name": module,
        "unique_id": f"{host}_{module}_connection",
        "state_topic": status_topic,
        "payload_on": "online",
        "payload_off": "offline",
        "device_class": "connectivity",
        "entity_category": "diagnostic",
        "expire_after": EXPIRE_AFTER,
        "device": {"identifiers": [f"sensors2mqtt_{host}"], "name": hostname},
        "origin": ORIGIN,
    }
    config_topic = f"{DISCOVERY_PREFIX}/binary_sensor/{host}/{module}_connection/config"
    client.publish(config_topic, json.dumps(config), retain=True)
