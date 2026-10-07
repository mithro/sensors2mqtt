"""Home Assistant MQTT auto-discovery helpers.

Provides SensorDef (typed sensor definition) and functions to build
HA-compatible discovery and state messages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.metadata import version

import paho.mqtt.client as mqtt

DISCOVERY_PREFIX = "homeassistant"

# Seconds after which HA marks a push entity unavailable if no fresh state arrives.
# Connection-agnostic freshness: any host publishing keeps a shared entity alive;
# all silent -> it expires. Generous enough that a slow sequential SNMP poll cycle
# never flaps an entity.
EXPIRE_AFTER = 300

ORIGIN = {
    "name": "sensors2mqtt",
    "sw": version("sensors2mqtt"),
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
        enabled_by_default: False creates the entity disabled in HA (the user can
            enable it); for rarely wanted detail such as normalised SMART values.
        attributes: the entity also has JSON attributes, published (retained,
            when they change) on attributes_topic() rather than in the state
            message, which every entity of the device parses on every poll.
            Its value is then a WithAttributes.
        force_update: HA records every received state, not only changes. Set it
            on power readings HA integrates into energy: without it a constant
            reading (e.g. an idle 0 mW PoE port) never refreshes, leaving the
            integration no samples and no way to tell "constant" from "stale".
    """

    suffix: str
    name: str
    unit: str
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None
    enabled_by_default: bool = True
    attributes: bool = False
    force_update: bool = False


@dataclass(frozen=True)
class WithAttributes:
    """The value of a sensor with ``attributes``: its state and its attributes."""

    state: object
    attributes: dict


def attributes_topic(state_topic: str, suffix: str) -> str:
    """``sensors2mqtt/<node>/<module>/state`` -> ``.../<module>/attributes/<suffix>``."""
    return f"{state_topic.rsplit('/', 1)[0]}/attributes/{suffix}"


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
        sw_version: Firmware/software version shown in the HA device registry.
        serial_number: Serial number shown in the HA device registry.
    """

    node_id: str
    name: str
    manufacturer: str
    model: str
    configuration_url: str | None = None
    connections: tuple[tuple[str, str], ...] | None = None
    via_device: str | None = None
    sw_version: str | None = None
    serial_number: str | None = None


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
    extra_avail_topic: str | None = None,
    default_entity_id: bool = False,
) -> dict:
    """Build HA auto-discovery config payload for a sensor.

    ``extra_avail_topic``, if given, is a second availability topic (e.g. the
    collector's connection status, when ``avail_topic`` is per-device): the
    entity is available only while both are ``online``.

    ``default_entity_id`` asks HA to create the entity as
    ``sensor.<node_id>_<suffix>`` rather than deriving its id from the device
    and entity names, so dashboards can find it (applies when HA first creates
    the entity).
    """
    config = {
        "name": sensor.name,
        "unique_id": f"{device.node_id}_{sensor.suffix}",
        "state_topic": state_topic,
        "value_template": f"{{{{ value_json.{sensor.suffix} }}}}",
        "device": device_dict(device),
        "expire_after": EXPIRE_AFTER,
        **availability_config(avail_topic, extra_avail_topic),
        "origin": ORIGIN,
    }
    if default_entity_id:
        config["default_entity_id"] = f"sensor.{device.node_id}_{sensor.suffix}"
    if sensor.unit:
        config["unit_of_measurement"] = sensor.unit
    if not sensor.enabled_by_default:
        config["enabled_by_default"] = False
    if sensor.state_class:
        config["state_class"] = sensor.state_class
    if sensor.device_class:
        config["device_class"] = sensor.device_class
    if sensor.icon:
        config["icon"] = sensor.icon
    if sensor.entity_category:
        config["entity_category"] = sensor.entity_category
    if sensor.force_update:
        config["force_update"] = True
    if sensor.attributes:
        config["json_attributes_topic"] = attributes_topic(state_topic, sensor.suffix)
    return config


def publish_discovery(
    client: mqtt.Client,
    sensors: list[SensorDef],
    device: DeviceInfo,
    state_topic: str,
    avail_topic: str,
    extra_avail_topic: str | None = None,
    default_entity_id: bool = False,
) -> int:
    """Publish HA auto-discovery configs for all sensors. Returns count published."""
    for sensor in sensors:
        config_topic = f"{DISCOVERY_PREFIX}/sensor/{device.node_id}/{sensor.suffix}/config"
        payload = discovery_payload(
            sensor, device, state_topic, avail_topic, extra_avail_topic, default_entity_id
        )
        client.publish(config_topic, json.dumps(payload), retain=True)
    return len(sensors)


def remove_discovery(client: mqtt.Client, node_id: str, suffixes: list[str]) -> None:
    """Delete HA entities by publishing empty retained discovery configs."""
    for suffix in suffixes:
        client.publish(f"{DISCOVERY_PREFIX}/sensor/{node_id}/{suffix}/config", "", retain=True)


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
    if device.sw_version:
        d["sw_version"] = device.sw_version
    if device.serial_number:
        d["serial_number"] = device.serial_number
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
