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

import paho.mqtt.client as mqtt

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.discovery import (
    DISCOVERY_PREFIX,
    ORIGIN,
    DeviceInfo,
    SensorDef,
    device_dict,
    publish_discovery,
    publish_state,
)

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
        index_width: Zero-pad index to this width (e.g. 2 → "01", "48").
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
    index_width: int = 0

    def format_index(self, index: int) -> str:
        """Format an index value with zero-padding if configured."""
        if self.index_width > 0:
            return str(index).zfill(self.index_width)
        return str(index)


@dataclass(frozen=True)
class SwitchModel:
    """Hardware model definition — OID tables and sensor mappings.

    This is the code-defined part: which OIDs to poll and how to interpret
    them. Shared by all switches of the same model.
    """

    manufacturer: str
    model: str
    port_count: int = 0
    poe_port_count: int = 0
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
    port_count: int = 0
    poe_port_count: int = 0
    write_community: str | None = None
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
        index_width=2,
    )]


# Known switch models — keyed by the name used in config files
MODELS: dict[str, SwitchModel] = {
    "m4300": SwitchModel(
        manufacturer="Netgear",
        model="M4300-24X",
        port_count=24,
        poe_port_count=0,
        sensors=_box_sensors(_FM_BOX, num_fans=2),
    ),
    "gsm7252ps": SwitchModel(
        manufacturer="Netgear",
        model="GSM7252PS",
        port_count=52,
        poe_port_count=48,
        walk_sensors=_poe_walk(_FM_POE),
    ),
    "s3300": SwitchModel(
        manufacturer="Netgear",
        model="GSM7228PS",
        port_count=52,
        poe_port_count=48,
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
        write_community = sw_data.get("write_community")

        switches.append(SwitchConfig(
            node_id=node_id,
            name=name,
            host=host,
            community=community,
            manufacturer=model.manufacturer,
            model=model.model,
            port_count=model.port_count,
            poe_port_count=model.poe_port_count,
            write_community=write_community,
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
        port_count=model.port_count,
        poe_port_count=model.poe_port_count,
        sensors=list(model.sensors),
        walk_sensors=list(model.walk_sensors),
    )


# ---------------------------------------------------------------------------
# SNMP parsing helpers
# ---------------------------------------------------------------------------


def parse_hex_mac(hex_str: str) -> str | None:
    """Parse a Hex-STRING MAC address to colon-separated lowercase format.

    Input:  "E0 91 F5 0C D5 C7"  (space-separated hex bytes)
    Output: "e0:91:f5:0c:d5:c7"
    Returns None if not exactly 6 bytes (filters non-MAC LLDP chassis IDs).
    """
    parts = hex_str.strip().split()
    if len(parts) != 6:
        return None
    return ":".join(p.lower() for p in parts)


BRIDGE_MAC_OID = "1.3.6.1.2.1.17.1.1.0"  # dot1dBaseBridgeAddress


def fetch_bridge_mac(switch: SwitchConfig, timeout: int = 10) -> str | None:
    """Fetch switch base MAC address via SNMP dot1dBaseBridgeAddress.

    Returns lowercase colon-separated MAC (e.g. "e0:91:f5:0c:d5:c7"),
    or None on failure.
    """
    try:
        result = subprocess.run(
            ["snmpget", "-v2c", "-c", switch.community, switch.host, BRIDGE_MAC_OID],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            log.warning("%s: bridge MAC fetch failed: %s", switch.name, result.stderr.strip())
            return None
        # Output: iso.3.6.1.2.1.17.1.1.0 = Hex-STRING: E0 91 F5 0C D5 C7
        m = re.search(r"Hex-STRING:\s*(.+)", result.stdout)
        if not m:
            return None
        return parse_hex_mac(m.group(1))
    except subprocess.TimeoutExpired:
        log.warning("%s: bridge MAC fetch timed out", switch.name)
        return None
    except Exception as e:
        log.warning("%s: bridge MAC fetch error: %s", switch.name, e)
        return None


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


def parse_lldp_walk(output: str, field_oid: str) -> dict[int, str]:
    """Parse LLDP remote table walk output into {local_port: value}.

    LLDP uses a three-part index: {timeMark}.{localPortNum}.{remIndex}.
    We extract localPortNum (the middle component) as the port key.

    Args:
        output: Raw snmpwalk output.
        field_oid: The field number in the OID (e.g. "9" for sysName, "8" for portDesc).
    """
    results: dict[int, str] = {}
    for line in output.strip().splitlines():
        # Skip Hex-STRING lines (MAC addresses, not useful as text)
        if "Hex-STRING:" in line:
            continue
        # OID suffix: ...{field_oid}.{timeMark}.{localPortNum}.{remIndex}
        m = re.match(
            rf".*\.{field_oid}\.(\d+)\.(\d+)\.(\d+)\s*=\s*\S+:\s*(.*)",
            line,
        )
        if not m:
            continue
        port = int(m.group(2))  # localPortNum is the middle component
        val = m.group(4).strip().strip('"')
        if val and port not in results:
            results[port] = val
    return results


def parse_lldp_chassis_ids(output: str) -> dict[int, str]:
    """Parse LLDP chassis ID walk into {local_port: mac_address}.

    LLDP chassis IDs are Hex-STRING MACs with three-part OID index:
        iso.0.8802.1.1.2.1.4.1.1.5.0.1.1 = Hex-STRING: E0 91 F5 0C D6 DB

    Only returns entries that parse as exactly 6-byte MACs (filters out
    non-MAC chassis ID subtypes like networkAddress or interfaceName).
    """
    results: dict[int, str] = {}
    for line in output.strip().splitlines():
        if "Hex-STRING:" not in line:
            continue
        # OID suffix: ...5.{timeMark}.{localPortNum}.{remIndex} = Hex-STRING: ...
        m = re.match(
            r".*\.5\.(\d+)\.(\d+)\.(\d+)\s*=\s*Hex-STRING:\s*(.*)",
            line,
        )
        if not m:
            continue
        port = int(m.group(2))  # localPortNum is the middle component
        mac = parse_hex_mac(m.group(4))
        if mac and port not in results:
            results[port] = mac
    return results


LLDP_CHASSIS_OID = "1.0.8802.1.1.2.1.4.1.1.5"  # lldpRemChassisId


def fetch_lldp_chassis_macs(switch: SwitchConfig, timeout: int = 30) -> dict[int, str]:
    """Fetch LLDP neighbor chassis MACs per port.

    Returns {port: "aa:bb:cc:dd:ee:ff"} for ports with LLDP neighbors
    that report a MAC-type chassis ID.
    """
    try:
        result = subprocess.run(
            ["snmpwalk", "-v2c", "-c", switch.community, switch.host,
             LLDP_CHASSIS_OID],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            log.warning("%s: LLDP chassis MAC walk failed: %s",
                        switch.name, result.stderr.strip())
            return {}
        macs = parse_lldp_chassis_ids(result.stdout)
        if macs:
            log.info("%s: fetched %d LLDP chassis MACs", switch.name, len(macs))
        return macs
    except subprocess.TimeoutExpired:
        log.warning("%s: LLDP chassis MAC walk timed out", switch.name)
    except Exception as e:
        log.warning("%s: LLDP chassis MAC walk error: %s", switch.name, e)
    return {}


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
        # Cache of port descriptions fetched via SNMP ifAlias: {node_id: {port: description}}
        self._port_descriptions: dict[str, dict[int, str]] = {}
        # Cache of VLAN names: {node_id: {vlan_id: name}}
        self._vlan_names: dict[str, dict[int, str]] = {}
        # Cache of LLDP neighbors: {node_id: {port: "sysname / portdesc"}}
        self._lldp_neighbors: dict[str, dict[int, str]] = {}
        # Cache of switch management MACs: {node_id: "aa:bb:cc:dd:ee:ff"}
        self._switch_macs: dict[str, str] = {}

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
                    formatted = walk_def.format_index(index)
                    suffix = walk_def.suffix_template.format(index=formatted)
                    values[suffix] = val
            except subprocess.TimeoutExpired:
                log.warning("%s: snmpwalk %s timed out", switch.name, walk_def.base_oid)
            except Exception as e:
                log.warning("%s: snmpwalk %s error: %s", switch.name, walk_def.base_oid, e)

        return values if values else None

    def _walk_int_table(self, switch: SwitchConfig, oid: str) -> dict[int, int]:
        """Walk an SNMP integer table, returning {index: value} for physical ports."""
        try:
            result = subprocess.run(
                ["snmpwalk", "-v2c", "-c", switch.community, switch.host, oid],
                capture_output=True, text=True, timeout=self._timeout * 3,
            )
            if result.returncode != 0:
                log.warning("%s: walk %s failed: %s", switch.name, oid, result.stderr.strip())
                return {}
            out = {}
            for index, val in parse_snmpwalk(result.stdout):
                if 1 <= index <= switch.port_count:
                    try:
                        out[index] = int(val)
                    except ValueError:
                        pass
            return out
        except subprocess.TimeoutExpired:
            log.warning("%s: walk %s timed out", switch.name, oid)
            return {}

    def poll_port_status(self, switch: SwitchConfig) -> dict[int, dict]:
        """Poll per-port status from a switch.

        Returns {port: {field: value}} for ports 1..port_count.
        Fields: link (str), speed_mbps (int), vlan_pvid (int), vlan_name (str).
        PoE switches also get: poe_admin (str), poe_status (str).
        """
        if switch.port_count == 0:
            return {}

        # Standard OIDs (all switches)
        IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
        IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"
        DOT1Q_PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"

        oper = self._walk_int_table(switch, IF_OPER_STATUS)
        speed = self._walk_int_table(switch, IF_HIGH_SPEED)
        pvid = self._walk_int_table(switch, DOT1Q_PVID)

        # PoE OIDs (PoE switches only)
        poe_admin: dict[int, int] = {}
        poe_detect: dict[int, int] = {}
        if switch.poe_port_count > 0:
            POE_ADMIN = "1.3.6.1.2.1.105.1.1.1.3.1"
            POE_DETECT = "1.3.6.1.2.1.105.1.1.1.6.1"
            poe_admin = self._walk_int_table(switch, POE_ADMIN)
            poe_detect = self._walk_int_table(switch, POE_DETECT)

        # Cached lookups
        vlan_names = self.fetch_vlan_names(switch)
        port_descs = self.fetch_port_descriptions(switch)
        lldp = self.fetch_lldp_neighbors(switch)

        # Build per-port dict
        OPER_MAP = {1: "up", 2: "down"}
        POE_ADMIN_MAP = {1: "enabled", 2: "disabled"}
        POE_DETECT_MAP = {1: "unused", 2: "searching", 3: "delivering", 4: "fault"}

        ports: dict[int, dict] = {}
        for port in range(1, switch.port_count + 1):
            data: dict = {
                "link": OPER_MAP.get(oper.get(port, 2), "down"),
                "speed_mbps": speed.get(port, 0),
                "vlan_pvid": pvid.get(port, 0),
                "vlan_name": vlan_names.get(pvid.get(port, 0), ""),
                "description": port_descs.get(port, ""),
                "lldp_neighbor": lldp.get(port, ""),
            }
            if port <= switch.poe_port_count:
                data["poe_admin"] = POE_ADMIN_MAP.get(poe_admin.get(port, 0), "")
                data["poe_status"] = POE_DETECT_MAP.get(poe_detect.get(port, 0), "")
            ports[port] = data

        return ports

    def fetch_port_descriptions(self, switch: SwitchConfig) -> dict[int, str]:
        """Fetch port descriptions from switch via SNMP ifAlias.

        Returns {port_number: device_name}. The ifAlias convention is
        "{interface}.{hostname}" (e.g. "eth0.rpi5-pmod") — we strip the
        interface prefix to get just the device name.

        Results are cached per switch (port descriptions don't change between polls).
        """
        if switch.node_id in self._port_descriptions:
            return self._port_descriptions[switch.node_id]

        # ifAlias OID: 1.3.6.1.2.1.31.1.1.1.18
        IF_ALIAS_OID = "1.3.6.1.2.1.31.1.1.1.18"
        descriptions: dict[int, str] = {}

        try:
            result = subprocess.run(
                ["snmpwalk", "-v2c", "-c", switch.community, switch.host, IF_ALIAS_OID],
                capture_output=True, text=True, timeout=self._timeout * 3,
            )
            if result.returncode != 0:
                log.warning("%s: ifAlias walk failed: %s", switch.name, result.stderr.strip())
            else:
                for line in result.stdout.strip().splitlines():
                    m = re.match(r'.*\.(\d+)\s*=\s*STRING:\s*"(.+)"', line)
                    if not m:
                        continue
                    port = int(m.group(1))
                    alias = m.group(2).strip()
                    if not alias:
                        continue
                    descriptions[port] = alias
        except subprocess.TimeoutExpired:
            log.warning("%s: ifAlias walk timed out", switch.name)
        except Exception as e:
            log.warning("%s: ifAlias walk error: %s", switch.name, e)

        self._port_descriptions[switch.node_id] = descriptions
        if descriptions:
            log.info("%s: fetched %d port descriptions", switch.name, len(descriptions))
        return descriptions

    def fetch_vlan_names(self, switch: SwitchConfig) -> dict[int, str]:
        """Fetch VLAN ID to name mapping from switch via SNMP dot1qVlanStaticName.

        Returns {vlan_id: name}. Results are cached per switch.
        """
        if switch.node_id in self._vlan_names:
            return self._vlan_names[switch.node_id]

        # dot1qVlanStaticName: 1.3.6.1.2.1.17.7.1.4.3.1.1.{vlan_id}
        VLAN_NAME_OID = "1.3.6.1.2.1.17.7.1.4.3.1.1"
        names: dict[int, str] = {}

        try:
            result = subprocess.run(
                ["snmpwalk", "-v2c", "-c", switch.community, switch.host, VLAN_NAME_OID],
                capture_output=True, text=True, timeout=self._timeout * 3,
            )
            if result.returncode != 0:
                log.warning(
                    "%s: VLAN name walk failed: %s", switch.name, result.stderr.strip(),
                )
            else:
                for index, val in parse_snmpwalk(result.stdout):
                    if val:
                        names[index] = val
        except subprocess.TimeoutExpired:
            log.warning("%s: VLAN name walk timed out", switch.name)
        except Exception as e:
            log.warning("%s: VLAN name walk error: %s", switch.name, e)

        self._vlan_names[switch.node_id] = names
        if names:
            log.info("%s: fetched %d VLAN names", switch.name, len(names))
        return names

    def fetch_lldp_neighbors(self, switch: SwitchConfig) -> dict[int, str]:
        """Fetch LLDP neighbor info from switch.

        Returns {port: "sys_name / port_desc"} for ports with LLDP neighbors.
        Results are cached per switch.

        LLDP remote table base OID: 1.0.8802.1.1.2.1.4.1.1
        Field .9 = lldpRemSysName, field .8 = lldpRemPortDesc.
        Index: {timeMark}.{localPortNum}.{remIndex} (three-part).
        """
        if switch.node_id in self._lldp_neighbors:
            return self._lldp_neighbors[switch.node_id]

        LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"
        sys_names: dict[int, str] = {}
        port_descs: dict[int, str] = {}

        for field_oid, target in [("9", sys_names), ("8", port_descs)]:
            try:
                result = subprocess.run(
                    [
                        "snmpwalk", "-v2c", "-c", switch.community,
                        switch.host, f"{LLDP_REM}.{field_oid}",
                    ],
                    capture_output=True, text=True, timeout=self._timeout * 3,
                )
                if result.returncode != 0:
                    log.warning(
                        "%s: LLDP .%s walk failed: %s",
                        switch.name, field_oid, result.stderr.strip(),
                    )
                else:
                    target.update(parse_lldp_walk(result.stdout, field_oid))
            except subprocess.TimeoutExpired:
                log.warning("%s: LLDP .%s walk timed out", switch.name, field_oid)
            except Exception as e:
                log.warning("%s: LLDP .%s walk error: %s", switch.name, field_oid, e)

        # Strip "<site>.mithis.com" domain suffix from sysName values
        for port in sys_names:
            sn = sys_names[port]
            parts = sn.split(".")
            # e.g. "sw-bb-25g.net.welland.mithis.com" → "sw-bb-25g"
            # e.g. "ten64.welland.mithis.com" → "ten64"
            if len(parts) >= 3 and parts[-1] == "com" and parts[-2] == "mithis":
                sys_names[port] = parts[0]

        # Combine as "port_desc.sys_name" (e.g. "eth0.rpi5-pmod") to match ifAlias convention
        neighbors: dict[int, str] = {}
        all_ports = set(sys_names.keys()) | set(port_descs.keys())
        for port in all_ports:
            pd = port_descs.get(port, "")
            sn = sys_names.get(port, "")
            if pd and sn:
                neighbors[port] = f"{pd}.{sn}"
            elif pd:
                neighbors[port] = pd
            elif sn:
                neighbors[port] = sn

        self._lldp_neighbors[switch.node_id] = neighbors
        if neighbors:
            log.info("%s: fetched %d LLDP neighbors", switch.name, len(neighbors))
        return neighbors

    def get_sensors_for_switch(self, switch: SwitchConfig, values: dict) -> list[SensorDef]:
        """Build SensorDef list for a switch, including dynamic walk sensors."""
        sensors = []

        # Static snmpget sensors (all numeric — fans, temp, PSU power)
        for s in switch.sensors:
            sensors.append(SensorDef(
                suffix=s.suffix,
                name=s.name,
                unit=s.unit,
                device_class=s.device_class,
                state_class="measurement",
                icon=s.icon,
            ))

        # Dynamic walk sensors (only for indices that returned data)
        port_descs = self.fetch_port_descriptions(switch)
        for walk_def in switch.walk_sensors:
            for key in sorted(values.keys()):
                # Match keys generated by this walk_def's template
                m = re.match(
                    walk_def.suffix_template.replace("{index}", r"(\d+)"),
                    key,
                )
                if m:
                    index = int(m.group(1))
                    formatted = walk_def.format_index(index)
                    port_device = port_descs.get(index)
                    if port_device:
                        friendly = f"(Port {formatted} PoE) {port_device}"
                    else:
                        friendly = walk_def.name_template.format(index=formatted)
                    sensors.append(SensorDef(
                        suffix=key,
                        name=friendly,
                        unit=walk_def.unit,
                        device_class=walk_def.device_class,
                        state_class="measurement",
                        icon=walk_def.icon,
                    ))

        return sensors

    def get_device_info(self, switch: SwitchConfig) -> DeviceInfo:
        # Lazy-fetch and cache switch management MAC
        if switch.node_id not in self._switch_macs:
            mac = fetch_bridge_mac(switch, timeout=self._timeout)
            if mac:
                self._switch_macs[switch.node_id] = mac
                log.info("%s: bridge MAC %s", switch.name, mac)
        mac = self._switch_macs.get(switch.node_id)
        return DeviceInfo(
            node_id=switch.node_id,
            name=switch.name,
            manufacturer=switch.manufacturer,
            model=switch.model,
            connections=(("mac", mac),) if mac else None,
        )

    def state_topic(self, switch: SwitchConfig) -> str:
        return f"sensors2mqtt/{switch.node_id}/state"

    def avail_topic(self, switch: SwitchConfig) -> str:
        return f"sensors2mqtt/{switch.node_id}/status"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _build_port_device(
    switch: SwitchConfig,
    port: int,
    hostnames: dict[int, str] | None = None,
    chassis_macs: dict[int, str] | None = None,
) -> DeviceInfo:
    """Build a per-port sub-device linked to the parent switch via via_device."""
    nn = str(port).zfill(2)
    hostname = hostnames.get(port) if hostnames else None
    name = f"Port {nn} ({hostname})" if hostname else f"Port {nn}"
    mac = chassis_macs.get(port) if chassis_macs else None
    return DeviceInfo(
        node_id=f"{switch.node_id}_port{nn}",
        name=name,
        manufacturer=switch.manufacturer,
        model=switch.model,
        connections=(("mac", mac),) if mac else None,
        via_device=f"sensors2mqtt_{switch.node_id}",
    )


def _publish_port_discovery(
    client: mqtt.Client,
    switch: SwitchConfig,
    device: DeviceInfo,
    avail_topic: str,
    hostnames: dict[int, str] | None = None,
    chassis_macs: dict[int, str] | None = None,
) -> int:
    """Publish per-port sensor discovery for all ports on a switch.

    Each port gets its own sub-device (via_device → parent switch).
    """
    import json as _json

    count = 0
    for port in range(1, switch.port_count + 1):
        nn = str(port).zfill(2)
        port_state_topic = f"sensors2mqtt/{switch.node_id}/port/{nn}/state"
        port_prefix = f"port{nn}"

        # Build per-port sub-device
        port_device = _build_port_device(
            switch, port, hostnames, chassis_macs,
        )
        port_dev_dict = device_dict(port_device)

        # Sensors for ALL ports
        port_sensors = [
            ("link", "sensor", f"Port {nn} Link", None, None, None, "mdi:ethernet"),
            ("speed_mbps", "sensor", f"Port {nn} Speed", "data_rate", "Mbit/s",
             "measurement", None),
            ("vlan_pvid", "sensor", f"Port {nn} VLAN", None, None, "measurement", None),
            ("vlan_name", "sensor", f"Port {nn} VLAN Name", None, None, None, None),
            ("description", "sensor", f"Port {nn} Description", None, None, None, None),
            ("lldp_neighbor", "sensor", f"Port {nn} LLDP", None, None, None, None),
        ]

        # PoE sensors (only for PoE-capable ports)
        if port <= switch.poe_port_count:
            port_sensors.extend([
                ("poe_mw", "sensor", f"Port {nn} PoE Power", "power", "mW",
                 "measurement", None),
                ("poe_admin", "sensor", f"Port {nn} PoE Admin", None, None, None, None),
                ("poe_status", "sensor", f"Port {nn} PoE Status", None, None, None, None),
            ])

        for value_key, platform, name, dev_class, unit, state_class, icon in port_sensors:
            suffix = f"{port_prefix}_{value_key}"
            unique_id = f"{switch.node_id}_{suffix}"
            config_topic = (
                f"{DISCOVERY_PREFIX}/sensor/{switch.node_id}/{suffix}/config"
            )

            # Determine entity_category: link and PoE power are primary, rest diagnostic
            entity_category = "diagnostic"
            if value_key == "link":
                entity_category = None  # primary

            config = {
                "name": name,
                "unique_id": unique_id,
                "state_topic": port_state_topic,
                "value_template": f"{{{{ value_json.{value_key} }}}}",
                "device": port_dev_dict,
                "availability_topic": avail_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "origin": ORIGIN,
            }
            if unit:
                config["unit_of_measurement"] = unit
            if state_class:
                config["state_class"] = state_class
            if dev_class:
                config["device_class"] = dev_class
            if icon:
                config["icon"] = icon
            if entity_category:
                config["entity_category"] = entity_category

            client.publish(config_topic, _json.dumps(config), retain=True)
            count += 1

    return count


def main():
    import argparse
    import json as _json
    import signal
    import threading

    import paho.mqtt.client as mqtt

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="SNMP switch sensor collector")
    parser.add_argument("--config", type=Path, help="Path to TOML config file")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    args = parser.parse_args()

    config = MqttConfig.from_env()
    switches = load_config(args.config)
    collector = SnmpCollector(config=config, switches=switches)
    stop_event = threading.Event()
    discovery_published: set[str] = set()

    def shutdown(signum, frame):
        log.info("Shutting down (signal %d)", signum)
        stop_event.set()

    if not args.once:
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
                avail = collector.avail_topic(switch)

                # Poll hardware sensors (fans, temp, PSU)
                hw_values = collector.poll_switch(switch)

                # Poll per-port status
                port_status = collector.poll_port_status(switch)

                if hw_values is None and not port_status:
                    client.publish(avail, "offline", retain=True)
                    log.warning("%s: no data", switch.name)
                    continue

                # Publish discovery (once per startup)
                if switch.node_id not in discovery_published:
                    device = collector.get_device_info(switch)

                    # Hardware sensor discovery (fans, temp, PSU)
                    if hw_values:
                        hw_sensors = collector.get_sensors_for_switch(switch, hw_values)
                        hw_state = collector.state_topic(switch)
                        hw_count = publish_discovery(
                            client, hw_sensors, device, hw_state, avail,
                        )
                        log.info(
                            "%s: published discovery for %d hardware sensors",
                            switch.name, hw_count,
                        )

                    # Per-port discovery (each port is a sub-device)
                    if switch.port_count > 0:
                        # Fetch LLDP chassis MACs and hostnames for
                        # per-port device naming and connections
                        chassis_macs = fetch_lldp_chassis_macs(switch)
                        lldp_neighbors = collector.fetch_lldp_neighbors(switch)
                        port_descs = collector.fetch_port_descriptions(switch)
                        # Build hostnames: ifAlias > LLDP sysName
                        hostnames: dict[int, str] = {}
                        for p in set(port_descs.keys()) | set(lldp_neighbors.keys()):
                            if p in port_descs:
                                desc = port_descs[p]
                                dot = desc.find(".")
                                hostnames[p] = desc[dot + 1:] if dot >= 0 else desc
                            elif p in lldp_neighbors:
                                nb = lldp_neighbors[p]
                                dot = nb.find(".")
                                hostnames[p] = nb[dot + 1:] if dot >= 0 else nb

                        port_count = _publish_port_discovery(
                            client, switch, device, avail,
                            hostnames=hostnames,
                            chassis_macs=chassis_macs,
                        )
                        log.info(
                            "%s: published discovery for %d port sensors",
                            switch.name, port_count,
                        )

                    discovery_published.add(switch.node_id)

                # Publish hardware sensor state (single blob, not retained)
                if hw_values:
                    publish_state(client, collector.state_topic(switch), hw_values)

                # Publish per-port state (per-port topics, not retained)
                for port_num, port_data in sorted(port_status.items()):
                    nn = str(port_num).zfill(2)
                    port_topic = f"sensors2mqtt/{switch.node_id}/port/{nn}/state"
                    # Merge PoE power from hw_values if present
                    poe_key = f"port{nn}_poe_mw"
                    if hw_values and poe_key in hw_values:
                        port_data["poe_mw"] = hw_values[poe_key]
                    client.publish(port_topic, _json.dumps(port_data), retain=False)

                client.publish(avail, "online", retain=True)
                log.info(
                    "%s: published %d hw values + %d port states",
                    switch.name,
                    len(hw_values) if hw_values else 0,
                    len(port_status),
                )

            if args.once:
                break
            stop_event.wait(timeout=config.poll_interval)

    finally:
        for switch in collector.switches:
            client.publish(collector.avail_topic(switch), "offline", retain=True)
        client.disconnect()
        client.loop_stop()
        log.info("Disconnected from MQTT")


if __name__ == "__main__":
    main()
