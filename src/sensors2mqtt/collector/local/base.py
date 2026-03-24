"""LocalCollector: shared base for all local sensor collectors.

Provides common infrastructure for reading sysfs, /proc, and hwmon sensors.
Subclasses add hardware-specific probing (RPi, Mellanox, etc.).
"""

from __future__ import annotations

import logging
import re
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path

from sensors2mqtt.base import BasePublisher, MqttConfig
from sensors2mqtt.discovery import DeviceInfo, SensorDef

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sensor source types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SysfsSource:
    """A sysfs file to read for a sensor value.

    Attributes:
        path: Relative to sysfs_root (e.g. "sys/class/thermal/thermal_zone0/temp").
        scale: Multiply raw int by this (e.g. 0.001 for millidegrees → degrees).
        precision: Decimal places to round to.
    """

    path: str
    scale: float = 1.0
    precision: int = 1


@dataclass(frozen=True)
class ProcSource:
    """A /proc file with a specific key to extract.

    Attributes:
        path: Relative to sysfs_root (e.g. "proc/meminfo").
        key: Line prefix to match (e.g. "MemAvailable").
        scale: Multiply extracted value by this (e.g. 1/1024 for kB → MB).
        precision: Decimal places to round to.
    """

    path: str
    key: str
    scale: float = 1.0
    precision: int = 0


@dataclass(frozen=True)
class LocalSensor:
    """A probed local sensor: definition + data source."""

    sensor: SensorDef
    source: SysfsSource | ProcSource


# ---------------------------------------------------------------------------
# Default config search paths
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATHS = [
    Path("local.toml"),
    Path("/etc/sensors2mqtt/local.toml"),
]


# ---------------------------------------------------------------------------
# LocalCollector base class
# ---------------------------------------------------------------------------


class LocalCollector(BasePublisher):
    """Base for all local sensor collectors.

    Probes sysfs thermal zones, hwmon drivers, and /proc diagnostics at startup.
    Subclasses override ``_probe_hardware_sensors()`` to add device-specific sensors.
    """

    def __init__(
        self,
        config: MqttConfig | None = None,
        config_path: Path | None = None,
        sysfs_root: str = "/",
    ):
        super().__init__(config)
        self._sysfs_root = Path(sysfs_root)
        self._local_config: dict = self._load_config(config_path)
        self._sensors_list: list[LocalSensor] = []
        self._device_info: DeviceInfo = self._probe_device()
        self._probe_common_sensors()
        self._probe_hardware_sensors()
        log.info(
            "Detected %s, probed %d sensors",
            type(self).__name__,
            len(self._sensors_list),
        )

    # ------------------------------------------------------------------
    # Satisfy BasePublisher abstract interface
    # ------------------------------------------------------------------

    @property
    def sensors(self) -> list[SensorDef]:
        return [ls.sensor for ls in self._sensors_list]

    @property
    def device(self) -> DeviceInfo:
        return self._device_info

    @property
    def client_id(self) -> str:
        return f"sensors2mqtt-local-{self._device_info.node_id}"

    def poll(self) -> dict | None:
        """Read all probed sensors, return {suffix: value} dict."""
        values: dict = {}
        for ls in self._sensors_list:
            val = self._read_source(ls.source)
            if val is not None:
                values[ls.sensor.suffix] = val
        # Computed values
        total = values.get("mem_total_mb")
        avail = values.get("mem_available_mb")
        if total is not None and avail is not None and total > 0:
            values["mem_used_percent"] = round(100.0 * (total - avail) / total, 1)
        return values if values else None

    # ------------------------------------------------------------------
    # Source reader dispatch
    # ------------------------------------------------------------------

    def _read_source(self, source: SysfsSource | ProcSource) -> float | int | None:
        if isinstance(source, SysfsSource):
            return self._read_sysfs(source)
        if isinstance(source, ProcSource):
            return self._read_proc_key(source)
        return None

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self, config_path: Path | None) -> dict:
        """Load TOML config file. Returns empty dict if not found."""
        paths = [config_path] if config_path else DEFAULT_CONFIG_PATHS
        for p in paths:
            if p and p.exists():
                log.info("Loading config from %s", p)
                with open(p, "rb") as f:
                    return tomllib.load(f)
        return {}

    # ------------------------------------------------------------------
    # Device identification
    # ------------------------------------------------------------------

    def _probe_device(self) -> DeviceInfo:
        hostname = socket.gethostname()
        node_id = self._local_config.get("node_id", hostname.replace("-", "_"))
        mac = self._read_mac()
        via = self._local_config.get("via_device")
        return DeviceInfo(
            node_id=node_id,
            name=hostname,
            manufacturer=self._manufacturer(),
            model=self._model(),
            connections=(("mac", mac),) if mac else None,
            via_device=via,
        )

    def _manufacturer(self) -> str:
        """Override in subclass to return device manufacturer."""
        return "Unknown"

    def _model(self) -> str:
        """Override in subclass to return device model."""
        return "Unknown"

    def _mac_interfaces(self) -> tuple[str, ...]:
        """Network interfaces to try for MAC address, in priority order."""
        return ("eth0",)

    def _read_mac(self) -> str | None:
        """Read MAC from first available network interface."""
        for iface in self._mac_interfaces():
            path = self._sysfs_root / f"sys/class/net/{iface}/address"
            try:
                mac = path.read_text().strip().lower()
                if mac and mac != "00:00:00:00:00:00":
                    return mac
            except OSError:
                continue
        return None

    # ------------------------------------------------------------------
    # Common sensor probing
    # ------------------------------------------------------------------

    def _probe_common_sensors(self) -> None:
        """Probe sensors available on any Linux box."""
        self._probe_thermal_zones()
        self._probe_system_diagnostics()

    def _probe_hardware_sensors(self) -> None:
        """Override in subclass to add hardware-specific sensors."""

    def _probe_thermal_zones(self) -> None:
        """Walk /sys/class/thermal/thermal_zone*/ and register temperature sensors."""
        thermal_dir = self._sysfs_root / "sys/class/thermal"
        if not thermal_dir.is_dir():
            return
        for zone in sorted(thermal_dir.glob("thermal_zone*")):
            type_file = zone / "type"
            temp_file = zone / "temp"
            if not type_file.exists() or not temp_file.exists():
                continue
            try:
                zone_type = type_file.read_text().strip()
            except OSError:
                continue
            # Derive a suffix from the zone type
            suffix = re.sub(r"[^a-z0-9]+", "_", zone_type.lower().strip("-_"))
            # Common name mapping
            name_map = {
                "cpu_thermal": ("cpu_temp", "CPU Temperature"),
            }
            if suffix in name_map:
                suffix, name = name_map[suffix]
            else:
                name = f"{zone_type} Temperature"
                suffix = f"{suffix}_temp"

            # Skip if we already registered a sensor with this suffix
            if any(ls.sensor.suffix == suffix for ls in self._sensors_list):
                continue

            rel_path = str(temp_file.relative_to(self._sysfs_root))
            self._sensors_list.append(
                LocalSensor(
                    sensor=SensorDef(
                        suffix=suffix,
                        name=name,
                        unit="°C",
                        device_class="temperature",
                        state_class="measurement",
                    ),
                    source=SysfsSource(path=rel_path, scale=0.001, precision=1),
                )
            )
            log.debug("Probed thermal zone: %s (%s)", suffix, rel_path)

    def _probe_system_diagnostics(self) -> None:
        """Register /proc-based system diagnostics (uptime, memory, load)."""
        # Uptime
        uptime_path = self._sysfs_root / "proc/uptime"
        if uptime_path.exists():
            self._sensors_list.append(
                LocalSensor(
                    sensor=SensorDef(
                        suffix="uptime",
                        name="Uptime",
                        unit="s",
                        device_class="duration",
                        state_class="total_increasing",
                        entity_category="diagnostic",
                    ),
                    source=ProcSource(path="proc/uptime", key="_uptime_", precision=0),
                )
            )

        # Memory
        meminfo_path = self._sysfs_root / "proc/meminfo"
        if meminfo_path.exists():
            self._sensors_list.append(
                LocalSensor(
                    sensor=SensorDef(
                        suffix="mem_total_mb",
                        name="Memory Total",
                        unit="MB",
                        entity_category="diagnostic",
                    ),
                    source=ProcSource(
                        path="proc/meminfo", key="MemTotal", scale=1 / 1024, precision=0
                    ),
                )
            )
            self._sensors_list.append(
                LocalSensor(
                    sensor=SensorDef(
                        suffix="mem_available_mb",
                        name="Memory Available",
                        unit="MB",
                        device_class="data_size",
                        state_class="measurement",
                        entity_category="diagnostic",
                    ),
                    source=ProcSource(
                        path="proc/meminfo",
                        key="MemAvailable",
                        scale=1 / 1024,
                        precision=0,
                    ),
                )
            )
            # mem_used_percent is computed in poll(), not probed
            self._sensors_list.append(
                LocalSensor(
                    sensor=SensorDef(
                        suffix="mem_used_percent",
                        name="Memory Used",
                        unit="%",
                        state_class="measurement",
                        entity_category="diagnostic",
                    ),
                    # Dummy source — computed in poll() from total and available
                    source=ProcSource(path="proc/meminfo", key="_computed_"),
                )
            )

        # Load averages
        loadavg_path = self._sysfs_root / "proc/loadavg"
        if loadavg_path.exists():
            for idx, (suffix, name) in enumerate(
                [
                    ("load_1m", "Load (1m)"),
                    ("load_5m", "Load (5m)"),
                    ("load_15m", "Load (15m)"),
                ]
            ):
                self._sensors_list.append(
                    LocalSensor(
                        sensor=SensorDef(
                            suffix=suffix,
                            name=name,
                            unit="",
                            state_class="measurement",
                            entity_category="diagnostic",
                        ),
                        source=ProcSource(
                            path="proc/loadavg",
                            key=f"_loadavg_{idx}_",
                            precision=2,
                        ),
                    )
                )

    # ------------------------------------------------------------------
    # Sysfs / proc readers
    # ------------------------------------------------------------------

    def _read_sysfs(self, source: SysfsSource) -> float | None:
        """Read a sysfs file, parse as int/float, apply scale and round."""
        path = self._sysfs_root / source.path
        try:
            raw = path.read_text().strip()
            value = float(raw) * source.scale
            return round(value, source.precision)
        except (OSError, ValueError):
            return None

    def _read_proc_key(self, source: ProcSource) -> float | int | None:
        """Read a value from a /proc file by key prefix."""
        path = self._sysfs_root / source.path

        # Special case: uptime (first field of /proc/uptime)
        if source.key == "_uptime_":
            try:
                raw = path.read_text().strip()
                return int(float(raw.split()[0]))
            except (OSError, ValueError, IndexError):
                return None

        # Special case: load average (positional field)
        if source.key.startswith("_loadavg_"):
            try:
                idx = int(source.key.split("_")[2])
                raw = path.read_text().strip()
                return round(float(raw.split()[idx]), source.precision)
            except (OSError, ValueError, IndexError):
                return None

        # Special case: computed values (handled in poll())
        if source.key == "_computed_":
            return None

        # Standard key: value parsing (e.g. "MemTotal:   1234 kB")
        try:
            for line in path.read_text().splitlines():
                if line.startswith(source.key):
                    # Extract numeric value after the colon
                    parts = line.split()
                    if len(parts) >= 2:
                        value = float(parts[1]) * source.scale
                        return round(value, source.precision)
        except (OSError, ValueError):
            pass
        return None

    def _log_summary(self, values: dict) -> None:
        cpu_temp = values.get("cpu_temp", "?")
        mem_pct = values.get("mem_used_percent", "?")
        load = values.get("load_1m", "?")
        log.info("Published: CPU=%s°C  Mem=%s%%  Load=%s", cpu_temp, mem_pct, load)
