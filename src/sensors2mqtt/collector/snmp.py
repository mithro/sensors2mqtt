"""SNMP collector: poll Netgear managed switches for sensor data.

Runs on ten64 (the router). Polls multiple switches sequentially via
subprocess snmpget/snmpwalk. Per-switch availability — one switch being
down doesn't block others.

Usage:
    python -m sensors2mqtt.collector.snmp
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.discovery import DeviceInfo, SensorDef, publish_discovery, publish_state

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SnmpSensor:
    """An individual SNMP sensor to poll.

    Attributes:
        suffix: Entity suffix for MQTT (e.g. "fan1_rpm").
        name: Human-readable name (e.g. "Fan 1").
        oid: Full OID to snmpget (e.g. "1.3.6.1.4.1.4526.10.43.1.6.1.4.1.0").
        unit: Unit of measurement.
        device_class: HA device class (temperature, power, etc.). None for RPM.
        icon: MDI icon override. None uses default.
        scale: Multiply raw value by this factor (e.g. 0.001 for mW -> W).
        value_type: How to parse the SNMP value ("int", "float", "string_int").
    """

    suffix: str
    name: str
    oid: str
    unit: str
    device_class: str | None = None
    icon: str | None = None
    scale: float = 1.0
    value_type: str = "int"


@dataclass(frozen=True)
class SwitchConfig:
    """Configuration for a single managed switch.

    Attributes:
        node_id: Python-safe ID (e.g. "m4300_24x").
        name: Display name (e.g. "M4300-24X").
        host: IP or hostname.
        community: SNMP community string.
        manufacturer: For HA device registry.
        model: For HA device registry.
        sensors: List of sensors to poll.
        walk_sensors: List of (base_oid, sensor_factory) for walk-based polling.
    """

    node_id: str
    name: str
    host: str
    community: str
    manufacturer: str
    model: str
    sensors: list[SnmpSensor] = field(default_factory=list)
    walk_sensors: list[WalkSensorDef] = field(default_factory=list)


@dataclass(frozen=True)
class WalkSensorDef:
    """Defines a set of sensors discovered by snmpwalk.

    Used for variable-length tables like per-port PoE power.

    Attributes:
        base_oid: OID to walk.
        suffix_template: Python format string for suffix, e.g. "port{index}_poe_watts".
        name_template: Format string for name, e.g. "Port {index} PoE Power".
        unit: Unit of measurement.
        device_class: HA device class.
        icon: MDI icon.
        scale: Multiply raw value by this factor.
        min_index: Minimum index to include.
        max_index: Maximum index to include.
    """

    base_oid: str
    suffix_template: str
    name_template: str
    unit: str
    device_class: str | None = None
    icon: str | None = None
    scale: float = 1.0
    min_index: int = 1
    max_index: int = 48


# ---------------------------------------------------------------------------
# sw-netgear-m4300-24x switch definition
# ---------------------------------------------------------------------------

# boxServices MIB base: 1.3.6.1.4.1.4526.10.43.1
_M4300_BOX = "1.3.6.1.4.1.4526.10.43.1"

M4300_24X = SwitchConfig(
    node_id="sw_netgear_m4300_24x",
    name="sw-netgear-m4300-24x",
    host="sw-netgear-m4300-24x.welland.mithis.com",
    community="public",
    manufacturer="Netgear",
    model="M4300-24X",
    sensors=[
        # Fans: .6.1.4.1.{index} = speed as STRING (RPM)
        SnmpSensor(
            suffix="fan1_rpm", name="Fan 1", unit="RPM", icon="mdi:fan",
            oid=f"{_M4300_BOX}.6.1.4.1.0", value_type="string_int",
        ),
        SnmpSensor(
            suffix="fan2_rpm", name="Fan 2", unit="RPM", icon="mdi:fan",
            oid=f"{_M4300_BOX}.6.1.4.1.1", value_type="string_int",
        ),
        # Temperature: .15.1.3.{index} = temp in Celsius
        SnmpSensor(
            suffix="temp", name="Temperature", unit="°C",
            device_class="temperature",
            oid=f"{_M4300_BOX}.15.1.3.1",
        ),
        # PSU power: .8.1.5.1.{index} = watts
        SnmpSensor(
            suffix="psu_power", name="PSU Power", unit="W",
            device_class="power",
            oid=f"{_M4300_BOX}.8.1.5.1.0",
        ),
    ],
)


# ---------------------------------------------------------------------------
# sw-netgear-gsm7252ps-s2 switch definition
# ---------------------------------------------------------------------------

# FASTPATH PoE MIB: 1.3.6.1.4.1.4526.10.15.1.1.1
_GSM7252PS_POE = "1.3.6.1.4.1.4526.10.15.1.1.1"

GSM7252PS_S2 = SwitchConfig(
    node_id="sw_netgear_gsm7252ps_s2",
    name="sw-netgear-gsm7252ps-s2",
    host="sw-netgear-gsm7252ps-s2.welland.mithis.com",
    community="public",
    manufacturer="Netgear",
    model="GSM7252PS",
    walk_sensors=[
        # Per-port actual PoE power delivery: .2.1.{port} = milliwatts
        WalkSensorDef(
            base_oid=f"{_GSM7252PS_POE}.2.1",
            suffix_template="port{index}_poe_mw",
            name_template="Port {index} PoE Power",
            unit="mW",
            device_class="power",
            min_index=1,
            max_index=48,
        ),
    ],
)

# ---------------------------------------------------------------------------
# sw-netgear-s3300-1 switch definition
# ---------------------------------------------------------------------------

# S3300 uses 4526.11 (Smart Managed Pro) instead of 4526.10 (Fully Managed)
_S3300_BOX = "1.3.6.1.4.1.4526.11.43.1"
_S3300_POE = "1.3.6.1.4.1.4526.11.15.1.1.1"

S3300_1 = SwitchConfig(
    node_id="sw_netgear_s3300_1",
    name="sw-netgear-s3300-1",
    host="sw-netgear-s3300-1.welland.mithis.com",
    community="public",
    manufacturer="Netgear",
    model="GSM7228PS",
    sensors=[
        # Fans: .6.1.4.1.{index} = speed as STRING (RPM) — 3 fans
        SnmpSensor(
            suffix="fan1_rpm", name="Fan 1", unit="RPM", icon="mdi:fan",
            oid=f"{_S3300_BOX}.6.1.4.1.0", value_type="string_int",
        ),
        SnmpSensor(
            suffix="fan2_rpm", name="Fan 2", unit="RPM", icon="mdi:fan",
            oid=f"{_S3300_BOX}.6.1.4.1.1", value_type="string_int",
        ),
        SnmpSensor(
            suffix="fan3_rpm", name="Fan 3", unit="RPM", icon="mdi:fan",
            oid=f"{_S3300_BOX}.6.1.4.1.2", value_type="string_int",
        ),
        # Temperature: .15.1.3.{index} = temp in Celsius
        SnmpSensor(
            suffix="temp", name="Temperature", unit="°C",
            device_class="temperature",
            oid=f"{_S3300_BOX}.15.1.3.1",
        ),
        # PSU power: .8.1.5.1.{index} = watts
        SnmpSensor(
            suffix="psu_power", name="PSU Power", unit="W",
            device_class="power",
            oid=f"{_S3300_BOX}.8.1.5.1.0",
        ),
    ],
    walk_sensors=[
        # Per-port PoE power delivery: .2.1.{port} = milliwatts
        WalkSensorDef(
            base_oid=f"{_S3300_POE}.2.1",
            suffix_template="port{index}_poe_mw",
            name_template="Port {index} PoE Power",
            unit="mW",
            device_class="power",
            min_index=1,
            max_index=48,
        ),
    ],
)

# All switches to poll
SWITCHES: list[SwitchConfig] = [M4300_24X, GSM7252PS_S2, S3300_1]


# ---------------------------------------------------------------------------
# SNMP parsing helpers
# ---------------------------------------------------------------------------

def parse_snmpget_value(output: str) -> str | None:
    """Extract the value from a single snmpget output line.

    Handles formats like:
        iso.3.6.1... = INTEGER: 42
        iso.3.6.1... = STRING: "5280"
        iso.3.6.1... = Gauge32: 1234

    Returns the raw value string, or None if not parseable.
    """
    m = re.search(r"=\s*\S+:\s*(.*)", output.strip())
    if not m:
        return None
    val = m.group(1).strip().strip('"')
    return val if val else None


def parse_snmpwalk(output: str) -> list[tuple[int, str]]:
    """Parse snmpwalk output into [(last_oid_index, value), ...].

    Each line like:
        iso.3.6.1...2.1.5 = Gauge32: 3300
    yields (5, "3300").
    """
    results = []
    for line in output.strip().splitlines():
        m = re.match(r".*\.(\d+)\s*=\s*\S+:\s*(.*)", line)
        if m:
            index = int(m.group(1))
            val = m.group(2).strip().strip('"')
            results.append((index, val))
    return results


def snmpget_value(raw: str, value_type: str, scale: float) -> float | None:
    """Convert a raw SNMP value string to a numeric value."""
    if raw is None:
        return None
    if value_type == "string_int":
        m = re.match(r"(\d+)", raw)
        if not m:
            return None
        return int(m.group(1)) * scale
    else:
        m = re.match(r"([\d.]+)", raw)
        if not m:
            return None
        return float(m.group(1)) * scale


# ---------------------------------------------------------------------------
# SNMP collector
# ---------------------------------------------------------------------------

class SnmpCollector:
    """Polls multiple switches and publishes to MQTT.

    Unlike the single-device BasePublisher, this collector manages multiple
    devices (switches), each with their own HA discovery, state topic, and
    availability. It uses BasePublisher-style MQTT setup but with a custom
    run loop.
    """

    def __init__(
        self, config: MqttConfig | None = None, switches: list[SwitchConfig] | None = None,
    ):
        self.config = config or MqttConfig.from_env()
        self.switches = switches or SWITCHES
        self._timeout = 10

    def poll_switch(self, switch: SwitchConfig) -> dict | None:
        """Poll all sensors on a single switch. Returns {suffix: value} or None."""
        values = {}

        # snmpget-based sensors
        for sensor in switch.sensors:
            try:
                result = subprocess.run(
                    ["snmpget", "-v2c", "-c", switch.community, switch.host, sensor.oid],
                    capture_output=True, text=True, timeout=self._timeout,
                )
                if result.returncode != 0:
                    log.warning(
                        "%s: snmpget %s failed: %s",
                        switch.name, sensor.suffix, result.stderr.strip(),
                    )
                    continue
                raw = parse_snmpget_value(result.stdout)
                val = snmpget_value(raw, sensor.value_type, sensor.scale)
                if val is not None:
                    values[sensor.suffix] = val
            except subprocess.TimeoutExpired:
                log.warning("%s: snmpget %s timed out", switch.name, sensor.suffix)
            except Exception as e:
                log.warning("%s: snmpget %s error: %s", switch.name, sensor.suffix, e)

        # snmpwalk-based sensors
        for walk_def in switch.walk_sensors:
            try:
                result = subprocess.run(
                    ["snmpwalk", "-v2c", "-c", switch.community, switch.host, walk_def.base_oid],
                    capture_output=True, text=True, timeout=self._timeout * 3,
                )
                if result.returncode != 0:
                    log.warning(
                        "%s: snmpwalk %s failed: %s",
                        switch.name, walk_def.base_oid, result.stderr.strip(),
                    )
                    continue
                for index, raw in parse_snmpwalk(result.stdout):
                    if index < walk_def.min_index or index > walk_def.max_index:
                        continue
                    m = re.match(r"([\d.]+)", raw)
                    if not m:
                        continue
                    val = float(m.group(1)) * walk_def.scale
                    suffix = walk_def.suffix_template.format(index=index)
                    values[suffix] = val
            except subprocess.TimeoutExpired:
                log.warning("%s: snmpwalk %s timed out", switch.name, walk_def.base_oid)
            except Exception as e:
                log.warning("%s: snmpwalk %s error: %s", switch.name, walk_def.base_oid, e)

        return values if values else None

    def get_sensors_for_switch(self, switch: SwitchConfig, values: dict) -> list[SensorDef]:
        """Build SensorDef list for a switch, including dynamic walk sensors."""
        sensors = []

        # Static snmpget sensors
        for s in switch.sensors:
            sensors.append(SensorDef(
                suffix=s.suffix,
                name=s.name,
                unit=s.unit,
                device_class=s.device_class,
                icon=s.icon,
            ))

        # Dynamic walk sensors (only for indices that returned data)
        for walk_def in switch.walk_sensors:
            for key in sorted(values.keys()):
                # Match keys generated by this walk_def's template
                m = re.match(
                    walk_def.suffix_template.replace("{index}", r"(\d+)"),
                    key,
                )
                if m:
                    index = int(m.group(1))
                    sensors.append(SensorDef(
                        suffix=key,
                        name=walk_def.name_template.format(index=index),
                        unit=walk_def.unit,
                        device_class=walk_def.device_class,
                        icon=walk_def.icon,
                    ))

        return sensors

    def get_device_info(self, switch: SwitchConfig) -> DeviceInfo:
        return DeviceInfo(
            node_id=switch.node_id,
            name=switch.name,
            manufacturer=switch.manufacturer,
            model=switch.model,
        )

    def state_topic(self, switch: SwitchConfig) -> str:
        return f"sensors2mqtt/{switch.node_id}/state"

    def avail_topic(self, switch: SwitchConfig) -> str:
        return f"sensors2mqtt/{switch.node_id}/status"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    import signal
    import threading

    import paho.mqtt.client as mqtt

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = MqttConfig.from_env()
    collector = SnmpCollector(config=config)
    stop_event = threading.Event()
    discovery_published: set[str] = set()

    def shutdown(signum, frame):
        log.info("Shutting down (signal %d)", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sensors2mqtt-snmp")
    client.username_pw_set(config.user, config.password)

    log.info("Connecting to MQTT %s:%d", config.host, config.port)
    client.connect(config.host, config.port, keepalive=120)
    client.loop_start()

    try:
        while not stop_event.is_set():
            for switch in collector.switches:
                if stop_event.is_set():
                    break

                log.info("Polling %s (%s)", switch.name, switch.host)
                values = collector.poll_switch(switch)
                avail = collector.avail_topic(switch)
                state = collector.state_topic(switch)

                if values is None:
                    client.publish(avail, "offline", retain=True)
                    log.warning("%s: no data", switch.name)
                    continue

                if switch.node_id not in discovery_published:
                    sensors = collector.get_sensors_for_switch(switch, values)
                    device = collector.get_device_info(switch)
                    count = publish_discovery(client, sensors, device, state, avail)
                    discovery_published.add(switch.node_id)
                    log.info("%s: published discovery for %d sensors", switch.name, count)

                publish_state(client, state, values)
                client.publish(avail, "online", retain=True)
                log.info("%s: published %d values", switch.name, len(values))

            stop_event.wait(timeout=config.poll_interval)

    finally:
        for switch in collector.switches:
            client.publish(collector.avail_topic(switch), "offline", retain=True)
        client.disconnect()
        client.loop_stop()
        log.info("Disconnected from MQTT")


if __name__ == "__main__":
    main()
