"""SNMP collector: poll Netgear managed switches for sensor data.

Runs on ten64 (the router). Polls multiple switches sequentially via
subprocess snmpget/snmpwalk. Per-switch availability — one switch being
down doesn't block others.

Switch models (OID tables) are defined in code. Which switches to poll
and their connection details are loaded from a TOML config file.

Usage:
    python -m sensors2mqtt.collector.snmp
    python -m sensors2mqtt.collector.snmp --config /etc/sensors2mqtt/snmp.toml
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.discovery import DeviceInfo, SensorDef, publish_discovery, publish_state

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

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
class WalkSensorDef:
    """Defines a set of sensors discovered by snmpwalk.

    Used for variable-length tables like per-port PoE power.

    Attributes:
        base_oid: OID to walk.
        suffix_template: Python format string for suffix, e.g. "port{index}_poe_mw".
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


@dataclass(frozen=True)
class SwitchModel:
    """Hardware model definition — OID tables and sensor mappings.

    This is the code-defined part: which OIDs to poll and how to interpret
    them. Shared by all switches of the same model.
    """

    manufacturer: str
    model: str
    sensors: list[SnmpSensor] = field(default_factory=list)
    walk_sensors: list[WalkSensorDef] = field(default_factory=list)


@dataclass(frozen=True)
class SwitchConfig:
    """Fully resolved switch configuration (model + deployment).

    Attributes:
        node_id: Python-safe ID for MQTT topics (e.g. "sw_netgear_m4300_24x").
        name: Display name matching DNS hostname (e.g. "sw-netgear-m4300-24x").
        host: DNS hostname for SNMP polling.
        community: SNMP community string.
        manufacturer: For HA device registry (from model).
        model: For HA device registry (from model).
        sensors: List of sensors to poll (from model).
        walk_sensors: List of walk sensor defs (from model).
    """

    node_id: str
    name: str
    host: str
    community: str
    manufacturer: str
    model: str
    sensors: list[SnmpSensor] = field(default_factory=list)
    walk_sensors: list[WalkSensorDef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Model definitions — hardware-specific OID tables
# ---------------------------------------------------------------------------

# Netgear Fully Managed (4526.10): M4300 series
_FM_BOX = "1.3.6.1.4.1.4526.10.43.1"
_FM_POE = "1.3.6.1.4.1.4526.10.15.1.1.1"

# Netgear Smart Managed Pro (4526.11): S3300 series
_SMP_BOX = "1.3.6.1.4.1.4526.11.43.1"
_SMP_POE = "1.3.6.1.4.1.4526.11.15.1.1.1"


def _box_sensors(base: str, num_fans: int) -> list[SnmpSensor]:
    """Build boxServices sensor list for a given OID base and fan count."""
    sensors = []
    for i in range(num_fans):
        sensors.append(SnmpSensor(
            suffix=f"fan{i + 1}_rpm", name=f"Fan {i + 1}", unit="RPM", icon="mdi:fan",
            oid=f"{base}.6.1.4.1.{i}", value_type="string_int",
        ))
    sensors.append(SnmpSensor(
        suffix="temp", name="Temperature", unit="°C",
        device_class="temperature",
        oid=f"{base}.15.1.3.1",
    ))
    sensors.append(SnmpSensor(
        suffix="psu_power", name="PSU Power", unit="W",
        device_class="power",
        oid=f"{base}.8.1.5.1.0",
    ))
    return sensors


def _poe_walk(base: str) -> list[WalkSensorDef]:
    """Build PoE per-port walk sensor for a given OID base."""
    return [WalkSensorDef(
        base_oid=f"{base}.2.1",
        suffix_template="port{index}_poe_mw",
        name_template="Port {index} PoE Power",
        unit="mW",
        device_class="power",
        min_index=1,
        max_index=48,
    )]


# Known switch models — keyed by the name used in config files
MODELS: dict[str, SwitchModel] = {
    "m4300": SwitchModel(
        manufacturer="Netgear",
        model="M4300-24X",
        sensors=_box_sensors(_FM_BOX, num_fans=2),
    ),
    "gsm7252ps": SwitchModel(
        manufacturer="Netgear",
        model="GSM7252PS",
        walk_sensors=_poe_walk(_FM_POE),
    ),
    "s3300": SwitchModel(
        manufacturer="Netgear",
        model="GSM7228PS",
        sensors=_box_sensors(_SMP_BOX, num_fans=3),
        walk_sensors=_poe_walk(_SMP_POE),
    ),
}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATHS = [
    Path("snmp.toml"),
    Path("/etc/sensors2mqtt/snmp.toml"),
]


def load_config(path: Path | None = None) -> list[SwitchConfig]:
    """Load switch deployment config from a TOML file.

    Config format:
        [switches.sw-netgear-m4300-24x]
        model = "m4300"
        host = "sw-netgear-m4300-24x.welland.mithis.com"
        community = "public"

    The switch name (TOML key) becomes both the display name and the
    node_id (with hyphens replaced by underscores).
    """
    if path is None:
        for candidate in DEFAULT_CONFIG_PATHS:
            if candidate.exists():
                path = candidate
                break
        if path is None:
            log.warning("No config file found, using built-in defaults")
            return _builtin_defaults()

    log.info("Loading config from %s", path)
    with open(path, "rb") as f:
        data = tomllib.load(f)

    switches = []
    for name, sw_data in data.get("switches", {}).items():
        model_name = sw_data.get("model")
        if model_name not in MODELS:
            log.error("Unknown model %r for switch %s (known: %s)",
                      model_name, name, ", ".join(MODELS.keys()))
            continue

        model = MODELS[model_name]
        node_id = name.replace("-", "_")
        host = sw_data.get("host", f"{name}.welland.mithis.com")
        community = sw_data.get("community", "public")

        switches.append(SwitchConfig(
            node_id=node_id,
            name=name,
            host=host,
            community=community,
            manufacturer=model.manufacturer,
            model=model.model,
            sensors=list(model.sensors),
            walk_sensors=list(model.walk_sensors),
        ))

    log.info("Loaded %d switches from config", len(switches))
    return switches


def _builtin_defaults() -> list[SwitchConfig]:
    """Fallback defaults when no config file is found."""
    return [
        _make_switch("sw-netgear-m4300-24x", "m4300"),
        _make_switch("sw-netgear-gsm7252ps-s2", "gsm7252ps"),
        _make_switch("sw-netgear-s3300-1", "s3300"),
    ]


def _make_switch(name: str, model_name: str) -> SwitchConfig:
    """Create a SwitchConfig from a name and model, using default host/community."""
    model = MODELS[model_name]
    return SwitchConfig(
        node_id=name.replace("-", "_"),
        name=name,
        host=f"{name}.welland.mithis.com",
        community="public",
        manufacturer=model.manufacturer,
        model=model.model,
        sensors=list(model.sensors),
        walk_sensors=list(model.walk_sensors),
    )


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
        self,
        config: MqttConfig | None = None,
        switches: list[SwitchConfig] | None = None,
    ):
        self.config = config or MqttConfig.from_env()
        self.switches = switches if switches is not None else load_config()
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
    import argparse
    import signal
    import threading

    import paho.mqtt.client as mqtt

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="SNMP switch sensor collector")
    parser.add_argument("--config", type=Path, help="Path to TOML config file")
    args = parser.parse_args()

    config = MqttConfig.from_env()
    switches = load_config(args.config)
    collector = SnmpCollector(config=config, switches=switches)
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
