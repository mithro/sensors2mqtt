"""RPi sensor collector specialization.

Adds Raspberry Pi-specific sensors: RP1 ADC voltages/temperature,
rpi_volt supply voltage, active cooler fan, vcgencmd throttle state.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from sensors2mqtt.collector.local.base import (
    LocalCollector,
    LocalSensor,
    SysfsSource,
)
from sensors2mqtt.discovery import SensorDef

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# vcgencmd throttle bit definitions
# ---------------------------------------------------------------------------

THROTTLE_BITS = [
    (0, "throttle_under_voltage", "Under-voltage"),
    (1, "throttle_freq_capped", "Frequency Capped"),
    (2, "throttle_throttled", "Throttled"),
    (3, "throttle_soft_temp", "Soft Temp Limit"),
]


# ---------------------------------------------------------------------------
# VcgencmdSource — not in base.py because it's RPi-specific
# ---------------------------------------------------------------------------

class VcgencmdSource:
    """Marker source for vcgencmd-based sensors. Actual reading is batched."""

    def __init__(self, bit: int | None = None):
        self.bit = bit  # None means raw hex value


# ---------------------------------------------------------------------------
# RpiCollector
# ---------------------------------------------------------------------------


class RpiCollector(LocalCollector):
    """RPi-specific sensor collector.

    Adds RP1 ADC (RPi 5), rpi_volt supply voltage (RPi 3/4),
    active cooler fan (RPi 5), and vcgencmd throttle state.
    """

    def __init__(self, *args, **kwargs):
        self._has_vcgencmd = False
        self._vcgencmd_sensors: list[tuple[LocalSensor, VcgencmdSource]] = []
        super().__init__(*args, **kwargs)

    def _manufacturer(self) -> str:
        return "Raspberry Pi"

    def _model(self) -> str:
        model_path = self._sysfs_root / "proc/device-tree/model"
        try:
            return model_path.read_text().rstrip("\x00").strip()
        except OSError:
            return "Raspberry Pi"

    def _mac_interfaces(self) -> tuple[str, ...]:
        return ("eth0", "wlan0")  # RPi Zero W has no eth0

    # ------------------------------------------------------------------
    # Hardware-specific probing
    # ------------------------------------------------------------------

    def _probe_hardware_sensors(self) -> None:
        self._probe_rp1_adc()
        self._probe_rpi_volt()
        self._probe_cooling_fan()
        self._probe_vcgencmd()

    def _probe_rp1_adc(self) -> None:
        """Probe RP1 ADC (RPi 5 only): voltage channels + RP1 temperature.

        The RP1 ADC exposes up to 4 voltage input channels (in1-in4) measuring
        various PMIC power rails. The exact rail names are undocumented by the
        RPi Foundation, so we use generic labels.
        """
        hwmon_dir = self._find_hwmon_by_name("rp1_adc")
        if hwmon_dir is None:
            return

        # Dynamically discover inN_input files
        for voltage_file in sorted(hwmon_dir.glob("in*_input")):
            # Extract channel number from filename (in1_input → 1)
            m = re.match(r"in(\d+)_input", voltage_file.name)
            if not m:
                continue
            channel = m.group(1)
            suffix = f"rp1_v{channel}"
            display_name = f"RP1 Voltage {channel}"

            rel_path = str(voltage_file.relative_to(self._sysfs_root))
            self._sensors_list.append(
                LocalSensor(
                    sensor=SensorDef(
                        suffix=suffix,
                        name=display_name,
                        unit="V",
                        device_class="voltage",
                        state_class="measurement",
                    ),
                    source=SysfsSource(path=rel_path, scale=0.001, precision=3),
                )
            )
            log.debug("Probed RP1 ADC: %s (%s)", suffix, rel_path)

        # RP1 temperature
        temp_file = hwmon_dir / "temp1_input"
        if temp_file.exists():
            rel_path = str(temp_file.relative_to(self._sysfs_root))
            self._sensors_list.append(
                LocalSensor(
                    sensor=SensorDef(
                        suffix="rp1_temp",
                        name="RP1 Temperature",
                        unit="°C",
                        device_class="temperature",
                        state_class="measurement",
                    ),
                    source=SysfsSource(path=rel_path, scale=0.001, precision=1),
                )
            )
            log.debug("Probed RP1 ADC: rp1_temp (%s)", rel_path)

    def _probe_rpi_volt(self) -> None:
        """Probe rpi_volt supply voltage.

        On RPi 3/4: ``in0_input`` provides the supply voltage in millivolts.
        On RPi 5: only ``in0_lcrit_alarm`` exists (undervoltage alarm flag, 0=OK 1=alarm).
        """
        hwmon_dir = self._find_hwmon_by_name("rpi_volt")
        if hwmon_dir is None:
            return

        # Try in0_input first (RPi 3/4 — actual voltage reading)
        input_file = hwmon_dir / "in0_input"
        if input_file.exists():
            rel_path = str(input_file.relative_to(self._sysfs_root))
            self._sensors_list.append(
                LocalSensor(
                    sensor=SensorDef(
                        suffix="supply_voltage",
                        name="Supply Voltage",
                        unit="V",
                        device_class="voltage",
                        state_class="measurement",
                    ),
                    source=SysfsSource(path=rel_path, scale=0.001, precision=3),
                )
            )
            log.debug("Probed rpi_volt: supply_voltage")

        # Check for undervoltage alarm (RPi 5 and some RPi 4 kernels)
        alarm_file = hwmon_dir / "in0_lcrit_alarm"
        if alarm_file.exists():
            rel_path = str(alarm_file.relative_to(self._sysfs_root))
            self._sensors_list.append(
                LocalSensor(
                    sensor=SensorDef(
                        suffix="supply_undervoltage",
                        name="Supply Undervoltage",
                        unit="",
                        entity_category="diagnostic",
                    ),
                    source=SysfsSource(path=rel_path, precision=0),
                )
            )
            log.debug("Probed rpi_volt: supply_undervoltage alarm")

    def _probe_cooling_fan(self) -> None:
        """Probe RPi 5 active cooler fan speed."""
        fan_base = self._sysfs_root / "sys/devices/platform/cooling_fan/hwmon"
        if not fan_base.is_dir():
            return
        for hwmon in sorted(fan_base.glob("hwmon*")):
            fan_file = hwmon / "fan1_input"
            if fan_file.exists():
                rel_path = str(fan_file.relative_to(self._sysfs_root))
                self._sensors_list.append(
                    LocalSensor(
                        sensor=SensorDef(
                            suffix="fan_rpm",
                            name="Fan Speed",
                            unit="RPM",
                            state_class="measurement",
                            icon="mdi:fan",
                        ),
                        source=SysfsSource(path=rel_path, precision=0),
                    )
                )
                log.debug("Probed cooling fan: %s", rel_path)
                return

    def _probe_vcgencmd(self) -> None:
        """Register vcgencmd throttle state sensors if vcgencmd is available."""
        if not shutil.which("vcgencmd"):
            log.debug("vcgencmd not found, skipping throttle sensors")
            return

        self._has_vcgencmd = True

        # Individual throttle bit sensors
        for bit, suffix, name in THROTTLE_BITS:
            source = VcgencmdSource(bit=bit)
            ls = LocalSensor(
                sensor=SensorDef(
                    suffix=suffix,
                    name=name,
                    unit="",
                    entity_category="diagnostic",
                ),
                source=source,  # type: ignore[arg-type]
            )
            self._sensors_list.append(ls)
            self._vcgencmd_sensors.append((ls, source))

        # Raw hex value
        raw_source = VcgencmdSource(bit=None)
        raw_ls = LocalSensor(
            sensor=SensorDef(
                suffix="throttle_raw",
                name="Throttle State",
                unit="",
                entity_category="diagnostic",
            ),
            source=raw_source,  # type: ignore[arg-type]
        )
        self._sensors_list.append(raw_ls)
        self._vcgencmd_sensors.append((raw_ls, raw_source))

        log.debug("Probed vcgencmd: %d throttle sensors", len(self._vcgencmd_sensors))

    # ------------------------------------------------------------------
    # Hwmon helper
    # ------------------------------------------------------------------

    def _find_hwmon_by_name(self, driver_name: str) -> Path | None:
        """Find hwmon directory by driver name (stable across reboots)."""
        hwmon_dir = self._sysfs_root / "sys/class/hwmon"
        if not hwmon_dir.is_dir():
            return None
        for hwmon in sorted(hwmon_dir.glob("hwmon*")):
            name_file = hwmon / "name"
            if name_file.exists():
                try:
                    if name_file.read_text().strip() == driver_name:
                        return hwmon
                except OSError:
                    continue
        return None

    # ------------------------------------------------------------------
    # Poll override — add vcgencmd reading
    # ------------------------------------------------------------------

    def poll(self) -> dict | None:
        values = super().poll()
        if values is None:
            values = {}

        if self._has_vcgencmd:
            throttle_val = self._read_throttle()
            if throttle_val is not None:
                for _ls, source in self._vcgencmd_sensors:
                    if source.bit is not None:
                        active = bool(throttle_val & (1 << source.bit))
                        values[_ls.sensor.suffix] = "ON" if active else "OFF"
                    else:
                        values[_ls.sensor.suffix] = hex(throttle_val)

        return values if values else None

    def _read_throttle(self) -> int | None:
        """Run vcgencmd get_throttled and parse the hex value."""
        try:
            result = subprocess.run(
                ["vcgencmd", "get_throttled"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            m = re.search(r"throttled=(0x[0-9a-fA-F]+)", result.stdout)
            if m:
                return int(m.group(1), 16)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None

    def _log_summary(self, values: dict) -> None:
        cpu = values.get("cpu_temp", "?")
        mem = values.get("mem_used_percent", "?")
        fan = values.get("fan_rpm")
        uv = values.get("throttle_under_voltage", "?")
        parts = [f"CPU={cpu}°C", f"Mem={mem}%"]
        if fan is not None:
            parts.append(f"Fan={fan}RPM")
        parts.append(f"UV={uv}")
        log.info("Published: %s", "  ".join(parts))
