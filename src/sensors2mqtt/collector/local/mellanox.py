"""Mellanox SN2410 sensor collector specialization.

Migrated from collector/hwmon.py. Uses `sensors -j` to read ASIC temp,
CPU temp, board temp, and 8 fan speeds. Inherits system diagnostics
(uptime, memory, load) from LocalCollector base.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

from sensors2mqtt.collector.local.base import LocalCollector, LocalSensor
from sensors2mqtt.discovery import SensorDef

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SensorsJsonSource — for `sensors -j` based readings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SensorsJsonSource:
    """A sensor value extracted from `sensors -j` JSON output.

    Attributes:
        chip: Chip key (e.g. "mlxsw-pci-0300").
        sensor_key: Sensor key (e.g. "temp1").
        value_key: Value key (e.g. "temp1_input").
    """

    chip: str
    sensor_key: str
    value_key: str


# ---------------------------------------------------------------------------
# Mellanox-specific sensor definitions
# ---------------------------------------------------------------------------

MELLANOX_SENSORS: list[tuple[SensorDef, tuple[str, str, str]]] = [
    (
        SensorDef(
            "asic_temp", "ASIC Temperature", "°C",
            device_class="temperature", state_class="measurement",
        ),
        ("mlxsw-pci-0300", "temp1", "temp1_input"),
    ),
    (
        SensorDef(
            "board_temp", "Board Temperature", "°C",
            device_class="temperature", state_class="measurement",
        ),
        ("jc42-i2c-0-1b", "temp1", "temp1_input"),
    ),
    (
        SensorDef("fan1_rpm", "Fan 1 Front", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan1", "fan1_input"),
    ),
    (
        SensorDef("fan2_rpm", "Fan 1 Rear", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan2", "fan2_input"),
    ),
    (
        SensorDef("fan3_rpm", "Fan 2 Front", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan3", "fan3_input"),
    ),
    (
        SensorDef("fan4_rpm", "Fan 2 Rear", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan4", "fan4_input"),
    ),
    (
        SensorDef("fan5_rpm", "Fan 3 Front", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan5", "fan5_input"),
    ),
    (
        SensorDef("fan6_rpm", "Fan 3 Rear", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan6", "fan6_input"),
    ),
    (
        SensorDef("fan7_rpm", "Fan 4 Front", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan7", "fan7_input"),
    ),
    (
        SensorDef("fan8_rpm", "Fan 4 Rear", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan8", "fan8_input"),
    ),
]

# Note: cpu_temp (coretemp-isa-0000) is NOT listed here — the base class
# thermal zone probe registers it from /sys/class/thermal/thermal_zone0.


# ---------------------------------------------------------------------------
# MellanoxCollector
# ---------------------------------------------------------------------------


class MellanoxCollector(LocalCollector):
    """Mellanox SN2410 sensor collector.

    Adds ASIC temp, board temp, and 8 fans via `sensors -j`.
    CPU temp comes from the base class thermal zone probe.
    """

    def __init__(self, *args, **kwargs):
        self._json_sensors: list[tuple[LocalSensor, SensorsJsonSource]] = []
        super().__init__(*args, **kwargs)

    def _manufacturer(self) -> str:
        return "Mellanox"

    def _model(self) -> str:
        return "SN2410"

    def _mac_interfaces(self) -> tuple[str, ...]:
        return ("bmc", "eth0")

    # ------------------------------------------------------------------
    # Hardware-specific probing
    # ------------------------------------------------------------------

    def _probe_hardware_sensors(self) -> None:
        for sensor_def, (chip, sensor_key, value_key) in MELLANOX_SENSORS:
            source = SensorsJsonSource(chip=chip, sensor_key=sensor_key, value_key=value_key)
            ls = LocalSensor(
                sensor=sensor_def,
                source=source,  # type: ignore[arg-type]
            )
            self._sensors_list.append(ls)
            self._json_sensors.append((ls, source))

    # ------------------------------------------------------------------
    # Poll override — add sensors -j reading
    # ------------------------------------------------------------------

    def poll(self) -> dict | None:
        # Read base sensors (sysfs, /proc)
        values = super().poll()
        if values is None:
            values = {}

        # Read sensors -j and extract Mellanox-specific values
        json_data = self._run_sensors_json()
        if json_data is not None:
            for ls, source in self._json_sensors:
                val = (
                    json_data
                    .get(source.chip, {})
                    .get(source.sensor_key, {})
                    .get(source.value_key)
                )
                if val is not None:
                    values[ls.sensor.suffix] = round(val, 1)

        return values if values else None

    def _run_sensors_json(self) -> dict | None:
        """Run `sensors -j` and return parsed JSON, or None on failure."""
        try:
            result = subprocess.run(
                ["sensors", "-j"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                log.warning(
                    "sensors failed (rc=%d): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return None
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            log.warning("sensors command timed out")
            return None
        except json.JSONDecodeError as e:
            log.warning("Bad JSON from sensors: %s", e)
            return None

    def _log_summary(self, values: dict) -> None:
        log.info(
            "Published: ASIC=%.1f°C CPU=%s°C Board=%.1f°C",
            values.get("asic_temp", 0),
            values.get("cpu_temp", "?"),
            values.get("board_temp", 0),
        )
