"""Hwmon collector: publish local `sensors -j` data to MQTT.

Runs locally on sw-bb-25g (Mellanox SN2410). Calls `sensors -j` and
publishes hwmon sensor data via MQTT auto-discovery.

Usage:
    python -m sensors2mqtt.collector.hwmon
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

from sensors2mqtt.base import BasePublisher
from sensors2mqtt.discovery import DeviceInfo, SensorDef

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sensor mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HwmonSensor:
    """Hwmon sensor with JSON path for extraction.

    Attributes:
        sensor: SensorDef for HA discovery.
        json_path: Tuple of (chip_key, sensor_key, value_key) into sensors -j output.
    """

    sensor: SensorDef
    json_path: tuple[str, str, str]


# sw-bb-25g sensor definitions (Mellanox SN2410)
HWMON_SENSORS: list[HwmonSensor] = [
    HwmonSensor(
        SensorDef(
            "asic_temp",
            "ASIC Temperature",
            "°C",
            device_class="temperature",
            state_class="measurement",
        ),
        ("mlxsw-pci-0300", "temp1", "temp1_input"),
    ),
    HwmonSensor(
        SensorDef(
            "cpu_temp",
            "CPU Temperature",
            "°C",
            device_class="temperature",
            state_class="measurement",
        ),
        ("coretemp-isa-0000", "Package id 0", "temp1_input"),
    ),
    HwmonSensor(
        SensorDef(
            "board_temp",
            "Board Temperature",
            "°C",
            device_class="temperature",
            state_class="measurement",
        ),
        ("jc42-i2c-0-1b", "temp1", "temp1_input"),
    ),
    HwmonSensor(
        SensorDef("fan1_rpm", "Fan 1 Front", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan1", "fan1_input"),
    ),
    HwmonSensor(
        SensorDef("fan2_rpm", "Fan 1 Rear", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan2", "fan2_input"),
    ),
    HwmonSensor(
        SensorDef("fan3_rpm", "Fan 2 Front", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan3", "fan3_input"),
    ),
    HwmonSensor(
        SensorDef("fan4_rpm", "Fan 2 Rear", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan4", "fan4_input"),
    ),
    HwmonSensor(
        SensorDef("fan5_rpm", "Fan 3 Front", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan5", "fan5_input"),
    ),
    HwmonSensor(
        SensorDef("fan6_rpm", "Fan 3 Rear", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan6", "fan6_input"),
    ),
    HwmonSensor(
        SensorDef("fan7_rpm", "Fan 4 Front", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan7", "fan7_input"),
    ),
    HwmonSensor(
        SensorDef("fan8_rpm", "Fan 4 Rear", "RPM", state_class="measurement", icon="mdi:fan"),
        ("mlxsw-pci-0300", "fan8", "fan8_input"),
    ),
]


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class HwmonCollector(BasePublisher):
    """Polls local hwmon via `sensors -j` and publishes to MQTT."""

    @property
    def sensors(self) -> list[SensorDef]:
        return [hs.sensor for hs in HWMON_SENSORS]

    @property
    def device(self) -> DeviceInfo:
        return DeviceInfo(
            node_id="sw_bb_25g",
            name="sw-bb-25g",
            manufacturer="Mellanox",
            model="SN2410",
        )

    @property
    def client_id(self) -> str:
        return "sensors2mqtt-hwmon"

    def poll(self) -> dict | None:
        data = self._run_sensors()
        if data is None:
            return None
        return self._extract_values(data)

    def _run_sensors(self) -> dict | None:
        """Run `sensors -j` and parse JSON output."""
        try:
            result = subprocess.run(
                ["sensors", "-j"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                log.warning("sensors failed (rc=%d): %s", result.returncode, result.stderr.strip())
                return None
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            log.warning("sensors command timed out")
            return None
        except json.JSONDecodeError as e:
            log.warning("Bad JSON from sensors: %s", e)
            return None

    def _extract_values(self, data: dict) -> dict:
        """Extract sensor values from parsed JSON into a flat dict."""
        values = {}
        for hs in HWMON_SENSORS:
            chip, sensor_key, value_key = hs.json_path
            val = data.get(chip, {}).get(sensor_key, {}).get(value_key)
            if val is not None:
                values[hs.sensor.suffix] = round(val, 1)
        return values

    def _log_summary(self, values: dict) -> None:
        log.info(
            "Published: ASIC=%.1f°C CPU=%.1f°C Board=%.1f°C",
            values.get("asic_temp", 0),
            values.get("cpu_temp", 0),
            values.get("board_temp", 0),
        )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    collector = HwmonCollector()
    collector.run()


if __name__ == "__main__":
    main()
