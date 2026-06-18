"""SNMP collector: poll Netgear managed switches for sensor data.

Runs on ten64 (the router). Polls multiple switches sequentially via
SnmpClient (ezsnmp). Per-switch availability — one switch being
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
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import paho.mqtt.client as mqtt

from sensors2mqtt.base import (
    MqttConfig,
    client_id_for,
    connection_status_topic,
    host_id,
    host_name,
    make_client,
)
from sensors2mqtt.discovery import (
    DISCOVERY_PREFIX,
    EXPIRE_AFTER,
    ORIGIN,
    DeviceInfo,
    SensorDef,
    availability_config,
    device_dict,
    publish_connection_diagnostic,
    publish_discovery,
    publish_state,
)
from sensors2mqtt.security import ensure_secure_file
from sensors2mqtt.snmp_client import SnmpClient, SnmpError, SnmpRow

# Legacy fixed bridge topic (pre multi-host). Cleared on startup; no longer used.
_LEGACY_BRIDGE_TOPIC = "sensors2mqtt/snmp_bridge/status"

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
        suffix: Entity suffix for MQTT (e.g. "cpu_temp").
        name: Human-readable name (e.g. "CPU Temperature").
        oid: Full OID to snmpget (e.g. "1.3.6.1.4.1.4526.10.43.1.8.1.5.1.0").
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
class BoxWalkDef:
    """A Netgear boxServices value column to walk.

    Instances are discovered from the walk rather than hardcoded because
    indexing differs by model: the M4300/S3300 index fans as unit.fan
    ("1.0"), the GSM7252PS as a bare fan number ("0"), and the GSM7252PS
    exposes four PSU rails ("1.0"-"1.3") where the others have one.

    Attributes:
        kind: Sensor kind — "fan", "temp", or "psu_power" (see box_entity).
        base_oid: The value column subtree to snmpwalk.
        unit: Unit of measurement.
        device_class: HA device class. None for RPM.
        icon: MDI icon override. None uses default.
    """

    kind: str
    base_oid: str
    unit: str
    device_class: str | None = None
    icon: str | None = None


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
    box_walks: list[BoxWalkDef] = field(default_factory=list)


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
        box_walks: List of boxServices walk defs (from model).
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
    box_walks: list[BoxWalkDef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Model definitions — hardware-specific OID tables
# ---------------------------------------------------------------------------

# Netgear Fully Managed (4526.10): M4300 series
_FM_BOX = "1.3.6.1.4.1.4526.10.43.1"
_FM_POE = "1.3.6.1.4.1.4526.10.15.1.1.1"

# Netgear Smart Managed Pro (4526.11): S3300 series
_SMP_BOX = "1.3.6.1.4.1.4526.11.43.1"
_SMP_POE = "1.3.6.1.4.1.4526.11.15.1.1.1"


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


def _box_walks(base: str) -> list[BoxWalkDef]:
    """Build boxServices walk definitions for a given enterprise OID base."""
    return [
        BoxWalkDef(kind="fan", base_oid=f"{base}.6.1.4", unit="RPM",
                   icon="mdi:fan"),
        BoxWalkDef(kind="temp", base_oid=f"{base}.15.1.3", unit="°C",
                   device_class="temperature"),
        BoxWalkDef(kind="psu_power", base_oid=f"{base}.8.1.5", unit="W",
                   device_class="power"),
    ]


# Known switch models — keyed by the name used in config files
MODELS: dict[str, SwitchModel] = {
    "m4300": SwitchModel(
        manufacturer="Netgear",
        model="M4300-24X",
        port_count=24,
        poe_port_count=0,
        box_walks=_box_walks(_FM_BOX),
    ),
    "gsm7252ps": SwitchModel(
        manufacturer="Netgear",
        model="GSM7252PS",
        port_count=52,
        poe_port_count=48,
        box_walks=_box_walks(_FM_BOX),
        walk_sensors=_poe_walk(_FM_POE),
    ),
    "s3300": SwitchModel(
        manufacturer="Netgear",
        model="GSM7228PS",
        port_count=52,
        poe_port_count=48,
        box_walks=_box_walks(_SMP_BOX),
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
        host = "sw-netgear-m4300-24x.example.com"
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
            raise FileNotFoundError(
                "No SNMP config file found. Create one at "
                + " or ".join(str(p) for p in DEFAULT_CONFIG_PATHS)
                + " (see snmp.toml.example)"
            )

    log.info("Loading config from %s", path)
    ensure_secure_file(path)
    with open(path, "rb") as f:
        data = tomllib.load(f)

    switches = []
    for name, sw_data in data.get("switches", {}).items():
        model_name = sw_data.get("model")
        if model_name not in MODELS:
            raise ValueError(
                f"Unknown model {model_name!r} for switch {name!r}; "
                f"valid models: {', '.join(sorted(MODELS))}"
            )

        model = MODELS[model_name]
        node_id = name.replace("-", "_")
        host = sw_data.get("host", name)
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
            box_walks=list(model.box_walks),
        ))

    log.info("Loaded %d switches from config", len(switches))
    return switches


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


def parse_snmpwalk(rows: list[SnmpRow]) -> list[tuple[int, str]]:
    """[(last_oid_component, value), ...] for rows whose final arc is numeric."""
    results = []
    for row in rows:
        last = row.oid.rsplit(".", 1)[-1]
        if last.isdigit():
            results.append((int(last), row.value))
    return results


def parse_box_walk(rows: list[SnmpRow], base_oid: str) -> list[tuple[str, str]]:
    """[(instance, value), ...] where instance is the OID suffix under base_oid.

    Keeps the whole suffix (e.g. "1.0" vs "0") so stacked-unit instances stay
    distinct, matching the previous behaviour.
    """
    prefix = base_oid + "."
    results = []
    for row in rows:
        if not row.oid.startswith(prefix):
            continue
        instance = row.oid[len(prefix):]
        if not re.fullmatch(r"\d+(\.\d+)*", instance):
            continue
        results.append((instance, row.value))
    return results


# Labels for the kinds that reach the dict lookup — "fan" returns early
# in box_entity() with its own numbered suffix scheme.
_BOX_KIND_LABELS = {"temp": "Temperature", "psu_power": "PSU Power"}


def box_entity(kind: str, ordinal: int) -> tuple[str, str]:
    """Map a discovered box sensor (kind, ordinal) to its (suffix, name).

    ordinal is the 0-based position in instance-sorted order. Suffixes are
    HA unique_id components and must stay stable across releases — renaming
    one orphans the entity's recorded history. Fans have always been
    numbered (fan1_rpm); the first temperature/PSU sensor keeps its historic
    unnumbered suffix ("temp", "psu_power") and only extra instances (e.g.
    the GSM7252PS's additional PSU rails) get numbered ones.

    Ordinals are recomputed each poll from the instances present, so an instance
    disappearing mid-run shifts later sensors down a slot; instance sets are
    stable on real hardware ("Not Supported" marks permanently absent slots).
    """
    if kind == "fan":
        return f"fan{ordinal + 1}_rpm", f"Fan {ordinal + 1}"
    label = _BOX_KIND_LABELS[kind]
    if ordinal == 0:
        return kind, label
    return f"{kind}{ordinal + 1}", f"{label} {ordinal + 1}"


def parse_lldp_walk(rows: list[SnmpRow], field_oid: str) -> dict[int, str]:
    """{local_port: value} from an LLDP remote-table field walk.

    Index is {timeMark}.{localPortNum}.{remIndex}; localPortNum is the middle.
    """
    results: dict[int, str] = {}
    pattern = re.compile(rf"\.{field_oid}\.(\d+)\.(\d+)\.(\d+)$")
    for row in rows:
        m = pattern.search("." + row.oid)
        if not m:
            continue
        port = int(m.group(2))
        if row.value and port not in results:
            results[port] = row.value
    return results


def parse_lldp_chassis_ids(rows: list[SnmpRow]) -> dict[int, str]:
    """{local_port: mac} from lldpRemChassisId rows that are 6-byte MACs."""
    results: dict[int, str] = {}
    pattern = re.compile(r"\.5\.(\d+)\.(\d+)\.(\d+)$")
    for row in rows:
        m = pattern.search("." + row.oid)
        if not m:
            continue
        port = int(m.group(2))
        mac = format_mac(row)
        if mac and port not in results:
            results[port] = mac
    return results


LLDP_CHASSIS_OID = "1.0.8802.1.1.2.1.4.1.1.5"  # lldpRemChassisId


def format_mac(row: SnmpRow) -> str | None:
    """Format an ezsnmp MAC OCTET STRING row as 'aa:bb:cc:dd:ee:ff', or None.

    Handles four known representations from ezsnmp:
    1. Separated hex (6 groups): "aa:bb:cc:dd:ee:ff", "aa bb cc dd ee ff",
       "aa-bb-cc-dd-ee-ff" — normalised to colon-lowercase.
    2. Contiguous 12-char hex (no separators): "0011223344aa" — split into pairs.
    3. Raw 6-byte string: chr(0)..chr(255) — ord() each byte.
    4. Space-separated hex fallback via parse_hex_mac.
    Returns None for anything that is not a 6-byte MAC.
    """
    v = row.value.strip()
    # Branch 1: separated hex — exactly 6 groups separated by ":", " ", or "-"
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}[:\- ]){5}[0-9A-Fa-f]{2}", v):
        return v.replace("-", ":").replace(" ", ":").lower()
    # Branch 2: contiguous hex — exactly 12 hex chars, no separators
    if re.fullmatch(r"[0-9A-Fa-f]{12}", v):
        return ":".join(v[i:i+2].lower() for i in range(0, 12, 2))
    # Branch 3: raw 6-byte string
    if len(v) == 6:
        return ":".join(f"{ord(c):02x}" for c in v)
    # Branch 4: space-separated hex fallback (parse_hex_mac)
    return parse_hex_mac(v)


def fetch_bridge_mac(client: SnmpClient, name: str) -> str | None:
    """Fetch switch base MAC via dot1dBaseBridgeAddress, or None."""
    try:
        row = client.get(BRIDGE_MAC_OID)
    except SnmpError as e:
        log.warning("%s: bridge MAC fetch failed: %s", name, e)
        return None
    if row is None:
        return None
    mac = format_mac(row)
    if mac is None:
        log.debug("%s: unrecognised bridge MAC value %r", name, row.value)
    return mac


def fetch_lldp_chassis_macs(client: SnmpClient, name: str) -> dict[int, str]:
    """Fetch LLDP neighbour chassis MACs per local port."""
    try:
        rows = client.walk(LLDP_CHASSIS_OID)
    except SnmpError as e:
        log.warning("%s: LLDP chassis MAC walk failed: %s", name, e)
        return {}
    macs = parse_lldp_chassis_ids(rows)
    if macs:
        log.info("%s: fetched %d LLDP chassis MACs", name, len(macs))
    return macs


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


def _default_client_factory(switch: SwitchConfig) -> SnmpClient:
    return SnmpClient(switch.host, switch.community,
                      write_community=switch.write_community)


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

    _CACHE_TTL = 300  # Re-fetch ifAlias, VLAN names, LLDP every 5 min
    _ERROR_RETRY = 60  # Retry failed fetches after 1 min

    def __init__(
        self,
        config: MqttConfig | None = None,
        switches: list[SwitchConfig] | None = None,
        client_factory=None,
    ):
        self.config = config or MqttConfig.from_env()
        self.switches = switches if switches is not None else load_config()
        self._client_factory = client_factory or _default_client_factory
        self._clients: dict[str, SnmpClient] = {}
        # Cache of port descriptions fetched via SNMP ifAlias: {node_id: {port: description}}
        self._port_descriptions: dict[str, dict[int, str]] = {}
        # Cache of VLAN names: {node_id: {vlan_id: name}}
        self._vlan_names: dict[str, dict[int, str]] = {}
        # Cache of LLDP neighbors: {node_id: {port: "sysname / portdesc"}}
        self._lldp_neighbors: dict[str, dict[int, str]] = {}
        # Cache of switch management MACs: {node_id: "aa:bb:cc:dd:ee:ff"}
        self._switch_macs: dict[str, str] = {}
        # Suffixes already announced via HA discovery: {node_id: {suffix}}
        self._published_suffixes: dict[str, set[str]] = {}
        # Cache expiry times: {"node_id:type": monotonic_expires_at}
        self._cache_times: dict[str, float] = {}

    def _client(self, switch: SwitchConfig) -> SnmpClient:
        c = self._clients.get(switch.node_id)
        if c is None:
            c = self._client_factory(switch)
            self._clients[switch.node_id] = c
        return c

    def poll_switch(self, switch: SwitchConfig) -> dict | None:
        """Poll all sensors on a single switch. Returns {suffix: value} or None."""
        values = {}
        client = self._client(switch)

        # snmpget-based sensors
        for sensor in switch.sensors:
            try:
                row = client.get(sensor.oid)
                raw = row.value if row else None
                val = snmpget_value(raw, sensor.value_type, sensor.scale)
                if val is not None:
                    values[sensor.suffix] = val
            except SnmpError as e:
                log.warning("%s: snmpget %s failed: %s", switch.name, sensor.suffix, e)

        # Walk-discovered boxServices sensors (fans, temperature, PSU).
        # Instances vary by model (see BoxWalkDef), so each value column is
        # walked and whatever rows exist become sensors, in instance order.
        for box in switch.box_walks:
            try:
                rows = client.walk(box.base_oid)
                readings = []
                for instance, raw in parse_box_walk(rows, box.base_oid):
                    if raw == "Not Supported":
                        # Netgear's literal placeholder for an absent sensor
                        # slot (e.g. the GSM7252PS middle fan) — not an error.
                        continue
                    try:
                        value = int(raw)
                    except ValueError:
                        log.warning(
                            "%s: non-integer %s reading %r at instance %s",
                            switch.name, box.kind, raw, instance,
                        )
                        continue
                    readings.append(
                        (tuple(int(c) for c in instance.split(".")), value)
                    )
                readings.sort()
                for ordinal, (_instance, value) in enumerate(readings):
                    suffix, _name = box_entity(box.kind, ordinal)
                    values[suffix] = value
            except SnmpError as e:
                log.warning("%s: snmpwalk %s (%s) failed: %s",
                            switch.name, box.base_oid, box.kind, e)

        # snmpwalk-based sensors
        for walk_def in switch.walk_sensors:
            try:
                rows = client.walk(walk_def.base_oid)
                for index, raw in parse_snmpwalk(rows):
                    if index < walk_def.min_index or index > walk_def.max_index:
                        continue
                    m = re.match(r"([\d.]+)", raw)
                    if not m:
                        continue
                    val = float(m.group(1)) * walk_def.scale
                    formatted = walk_def.format_index(index)
                    suffix = walk_def.suffix_template.format(index=formatted)
                    values[suffix] = val
            except SnmpError as e:
                log.warning("%s: snmpwalk %s failed: %s", switch.name, walk_def.base_oid, e)

        return values if values else None

    def _walk_int_table(self, switch: SwitchConfig, oid: str) -> dict[int, int]:
        """Walk an SNMP integer table, returning {index: value} for physical ports."""
        try:
            rows = self._client(switch).walk(oid)
            out = {}
            for index, val in parse_snmpwalk(rows):
                if 1 <= index <= switch.port_count:
                    try:
                        out[index] = int(val)
                    except ValueError:
                        pass
            return out
        except SnmpError:
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

        Returns {port_number: alias}. The ifAlias convention is
        "{interface}.{hostname}" (e.g. "eth0.rpi5-pmod"); the full alias
        is kept, matching the combined LLDP neighbor format.

        Results are cached per switch with a TTL to pick up changes.
        """
        cache_key = f"{switch.node_id}:descriptions"
        if switch.node_id in self._port_descriptions and \
                time.monotonic() < self._cache_times.get(cache_key, 0):
            return self._port_descriptions[switch.node_id]

        # ifAlias OID: 1.3.6.1.2.1.31.1.1.1.18
        IF_ALIAS_OID = "1.3.6.1.2.1.31.1.1.1.18"
        descriptions: dict[int, str] = {}
        success = False

        try:
            rows = self._client(switch).walk(IF_ALIAS_OID)
            success = True
            for row in rows:
                last = row.oid.rsplit(".", 1)[-1]
                if not last.isdigit():
                    continue
                alias = row.value.strip()
                if not alias:
                    continue
                descriptions[int(last)] = alias
        except SnmpError as e:
            log.warning("%s: ifAlias walk error: %s", switch.name, e)

        if success:
            self._port_descriptions[switch.node_id] = descriptions
            self._cache_times[cache_key] = time.monotonic() + self._CACHE_TTL
            if descriptions:
                log.info("%s: fetched %d port descriptions", switch.name, len(descriptions))
        elif switch.node_id in self._port_descriptions:
            self._cache_times[cache_key] = time.monotonic() + self._ERROR_RETRY
        return self._port_descriptions.get(switch.node_id, {})

    def fetch_vlan_names(self, switch: SwitchConfig) -> dict[int, str]:
        """Fetch VLAN ID to name mapping from switch via SNMP dot1qVlanStaticName.

        Returns {vlan_id: name}. Results are cached per switch with a TTL.
        """
        cache_key = f"{switch.node_id}:vlan_names"
        if switch.node_id in self._vlan_names and \
                time.monotonic() < self._cache_times.get(cache_key, 0):
            return self._vlan_names[switch.node_id]

        # dot1qVlanStaticName: 1.3.6.1.2.1.17.7.1.4.3.1.1.{vlan_id}
        VLAN_NAME_OID = "1.3.6.1.2.1.17.7.1.4.3.1.1"
        names: dict[int, str] = {}
        success = False

        try:
            rows = self._client(switch).walk(VLAN_NAME_OID)
            success = True
            for index, val in parse_snmpwalk(rows):
                if val:
                    names[index] = val
        except SnmpError as e:
            log.warning("%s: VLAN name walk error: %s", switch.name, e)

        if success:
            self._vlan_names[switch.node_id] = names
            self._cache_times[cache_key] = time.monotonic() + self._CACHE_TTL
            if names:
                log.info("%s: fetched %d VLAN names", switch.name, len(names))
        elif switch.node_id in self._vlan_names:
            self._cache_times[cache_key] = time.monotonic() + self._ERROR_RETRY
        return self._vlan_names.get(switch.node_id, {})

    def fetch_lldp_neighbors(self, switch: SwitchConfig) -> dict[int, str]:
        """Fetch LLDP neighbor info from switch.

        Returns {port: "port_desc.sys_name"} (e.g. "eth0.rpi5-pmod") for
        ports with LLDP neighbors. Results are cached per switch with a TTL.

        LLDP remote table base OID: 1.0.8802.1.1.2.1.4.1.1
        Field .9 = lldpRemSysName, field .8 = lldpRemPortDesc.
        Index: {timeMark}.{localPortNum}.{remIndex} (three-part).
        """
        cache_key = f"{switch.node_id}:lldp"
        if switch.node_id in self._lldp_neighbors and \
                time.monotonic() < self._cache_times.get(cache_key, 0):
            return self._lldp_neighbors[switch.node_id]

        LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"
        sys_names: dict[int, str] = {}
        port_descs: dict[int, str] = {}
        success = True

        for field_oid, target in [("9", sys_names), ("8", port_descs)]:
            try:
                rows = self._client(switch).walk(f"{LLDP_REM}.{field_oid}")
                target.update(parse_lldp_walk(rows, field_oid))
            except SnmpError as e:
                log.warning("%s: LLDP .%s walk error: %s", switch.name, field_oid, e)
                success = False

        # Strip FQDN to short hostname in sysName values
        for port in sys_names:
            sn = sys_names[port]
            if "." in sn:
                # e.g. "sw-bb-25g.net.example.com" → "sw-bb-25g"
                # e.g. "ten64.example.com" → "ten64"
                sys_names[port] = sn.split(".")[0]

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

        if success:
            self._lldp_neighbors[switch.node_id] = neighbors
            self._cache_times[cache_key] = time.monotonic() + self._CACHE_TTL
            if neighbors:
                log.info("%s: fetched %d LLDP neighbors", switch.name, len(neighbors))
        elif switch.node_id in self._lldp_neighbors:
            self._cache_times[cache_key] = time.monotonic() + self._ERROR_RETRY
        return self._lldp_neighbors.get(switch.node_id, {})

    def get_sensors_for_switch(self, switch: SwitchConfig, values: dict) -> list[SensorDef]:
        """Build SensorDef list for switch-level hardware sensors only.

        Box sensors (fans, temperature, PSU) are included based on which
        suffixes the poll discovered. Walk sensors (PoE per-port power) are
        NOT included here — they are
        published as per-port sub-device sensors in _publish_port_discovery().
        Including them here would create duplicate discovery on the parent
        device that conflicts with the per-port sub-device version.
        """
        sensors = []

        # Static snmpget sensors (extension point; currently unused by any model)
        for s in switch.sensors:
            sensors.append(SensorDef(
                suffix=s.suffix,
                name=s.name,
                unit=s.unit,
                device_class=s.device_class,
                state_class="measurement",
                icon=s.icon,
            ))

        # Walk-discovered box sensors: poll_switch() assigns contiguous
        # ordinals per kind, so probe values until the first missing suffix.
        for box in switch.box_walks:
            ordinal = 0
            while True:
                suffix, name = box_entity(box.kind, ordinal)
                if suffix not in values:
                    break
                sensors.append(SensorDef(
                    suffix=suffix,
                    name=name,
                    unit=box.unit,
                    device_class=box.device_class,
                    state_class="measurement",
                    icon=box.icon,
                ))
                ordinal += 1

        return sensors

    def new_sensor_defs(self, switch: SwitchConfig, values: dict) -> list[SensorDef]:
        """Return defs for hardware sensors not yet announced for this switch.

        Discovery is published incrementally: a walk-discovered sensor can
        first appear on a later poll (e.g. after a transient walk failure
        on startup), and HA needs its discovery published exactly once.
        """
        done = self._published_suffixes.setdefault(switch.node_id, set())
        new = [
            s for s in self.get_sensors_for_switch(switch, values)
            if s.suffix not in done
        ]
        done.update(s.suffix for s in new)
        return new

    def get_device_info(self, switch: SwitchConfig) -> DeviceInfo:
        # Lazy-fetch and cache switch management MAC
        if switch.node_id not in self._switch_macs:
            mac = fetch_bridge_mac(self._client(switch), switch.name)
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
    chassis_macs: dict[int, str] | None = None,
) -> DeviceInfo:
    """Build a per-port sub-device linked to the parent switch via via_device.

    The device name includes the switch name for globally unique HA entity IDs
    (e.g. "sw-netgear-gsm7252ps-s1 Port 01"). It does NOT include the connected
    hostname — that changes when cables move and would make entity IDs unstable.
    """
    nn = str(port).zfill(2)
    mac = chassis_macs.get(port) if chassis_macs else None
    return DeviceInfo(
        node_id=f"{switch.node_id}_port{nn}",
        name=f"{switch.name} Port {nn}",
        manufacturer=switch.manufacturer,
        model=switch.model,
        connections=(("mac", mac),) if mac else None,
        via_device=f"sensors2mqtt_{switch.node_id}",
    )


def _publish_port_discovery(
    client: mqtt.Client,
    switch: SwitchConfig,
    avail_topic: str,
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
        port_device = _build_port_device(switch, port, chassis_macs)
        port_dev_dict = device_dict(port_device)

        # Sensors for ALL ports
        # Names are short — the device name already identifies the port.
        # HA entity_id = {device_name}_{sensor_name}, so "Link" on device
        # "sw-netgear-m4300-24x Port 20" → sensor.sw_netgear_m4300_24x_port_20_link
        port_sensors = [
            ("link", "sensor", "Link", None, None, None, "mdi:ethernet"),
            ("speed_mbps", "sensor", "Speed", "data_rate", "Mbit/s",
             "measurement", None),
            ("vlan_pvid", "sensor", "VLAN", None, None, "measurement", None),
            ("vlan_name", "sensor", "VLAN Name", None, None, None, None),
            ("description", "sensor", "Description", None, None, None, None),
            ("lldp_neighbor", "sensor", "LLDP", None, None, None, None),
        ]

        # PoE sensors (only for PoE-capable ports)
        if port <= switch.poe_port_count:
            port_sensors.extend([
                ("poe_watts", "sensor", "PoE Power", "power", "mW",
                 "measurement", None),
                ("poe_admin", "sensor", "PoE Admin", None, None, None, None),
                ("poe_status", "sensor", "PoE Status", None, None, None, None),
            ])

        for value_key, platform, name, dev_class, unit, state_class, icon in port_sensors:
            # Topic suffix uses "port01_link" format for MQTT paths
            topic_suffix = f"{port_prefix}_{value_key}"
            unique_id = f"{switch.node_id}_{nn}_{value_key}"
            config_topic = (
                f"{DISCOVERY_PREFIX}/sensor/{switch.node_id}/{topic_suffix}/config"
            )

            # Determine entity_category: link and PoE power are primary, rest diagnostic
            entity_category = "diagnostic"
            if value_key in ("link", "poe_watts"):
                entity_category = None  # primary

            config = {
                "name": name,
                "unique_id": unique_id,
                "state_topic": port_state_topic,
                "value_template": f"{{{{ value_json.{value_key} }}}}",
                "device": port_dev_dict,
                "expire_after": EXPIRE_AFTER,
                **availability_config(avail_topic),
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

    conn_topic = connection_status_topic("snmp")

    def _on_connected(c: mqtt.Client) -> None:
        c.publish(conn_topic, "online", retain=True)
        publish_connection_diagnostic(c, host_id(), "snmp", host_name())

    client = make_client(
        config, client_id_for("snmp"),
        on_connected=_on_connected,
        will_topic=conn_topic,
    )

    log.info("Connecting to MQTT %s:%d", config.host, config.port)
    client.connect(config.host, config.port, keepalive=120)
    client.loop_start()
    client.publish(_LEGACY_BRIDGE_TOPIC, "", retain=True)  # one-time cleanup

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

                # Hardware sensor discovery (fans, temp, PSU) — incremental:
                # a walk-discovered sensor can first appear on a later poll
                # (e.g. after a transient walk failure), so publish discovery
                # for any suffix not yet announced rather than only once.
                if hw_values:
                    hw_sensors = collector.new_sensor_defs(switch, hw_values)
                    if hw_sensors:
                        device = collector.get_device_info(switch)
                        hw_state = collector.state_topic(switch)
                        hw_count = publish_discovery(
                            client, hw_sensors, device, hw_state, avail,
                        )
                        log.info(
                            "%s: published discovery for %d hardware sensors",
                            switch.name, hw_count,
                        )

                # Per-port discovery (once per startup; each port is a sub-device)
                if switch.node_id not in discovery_published:
                    if switch.port_count > 0:
                        chassis_macs = fetch_lldp_chassis_macs(
                            collector._client(switch), switch.name
                        )
                        port_count = _publish_port_discovery(
                            client, switch, avail,
                            chassis_macs=chassis_macs,
                        )
                        log.info(
                            "%s: published discovery for %d port sensors",
                            switch.name, port_count,
                        )
                    discovery_published.add(switch.node_id)

                # Publish hardware sensor state (single blob, retained)
                if hw_values:
                    publish_state(client, collector.state_topic(switch), hw_values)

                # Publish per-port state (per-port topics, retained)
                for port_num, port_data in sorted(port_status.items()):
                    nn = str(port_num).zfill(2)
                    port_topic = f"sensors2mqtt/{switch.node_id}/port/{nn}/state"
                    # Merge PoE power from hw_values if present
                    poe_key = f"port{nn}_poe_mw"
                    if hw_values and poe_key in hw_values:
                        port_data["poe_watts"] = hw_values[poe_key]
                    client.publish(port_topic, _json.dumps(port_data), retain=True)

                client.publish(avail, "online", retain=True)
                log.info(
                    "%s: published %d hw values + %d port states",
                    switch.name,
                    len(hw_values) if hw_values else 0,
                    len(port_status),
                )

            # heartbeat the collector's own connection liveness
            client.publish(conn_topic, "online", retain=True)
            if args.once:
                break
            stop_event.wait(timeout=config.poll_interval)

    finally:
        for switch in collector.switches:
            client.publish(collector.avail_topic(switch), "offline", retain=True)
        client.publish(conn_topic, "offline", retain=True)
        client.disconnect()
        client.loop_stop()
        log.info("Disconnected from MQTT")


if __name__ == "__main__":
    main()
