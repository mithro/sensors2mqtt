"""RPi sensor collector specialization.

Adds Raspberry Pi-specific sensors: RP1 ADC voltages/temperature,
rpi_volt supply voltage, active cooler fan, vcgencmd throttle state.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

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
        self._probe_undervoltage_alarm()
        self._probe_cooling_fan()
        self._probe_vcgencmd()

    def _probe_undervoltage_alarm(self) -> None:
        """RPi 5 / some RPi 4 kernels expose in0_lcrit_alarm (not an *_input channel)."""
        hwmon_dir = self._find_hwmon_by_name("rpi_volt")
        if hwmon_dir is None:
            return
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
        parts.append(f"Undervolt={uv}")
        log.info("Published: %s", "  ".join(parts))
