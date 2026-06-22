"""SFP/transceiver DDM probe — dynamic, re-run each poll.

probe_sfp_hwmon: the mainline `sfp` driver's hwmon node (full DDM, unprivileged;
present only while a DDM optical module is seated). probe_sfp_mlxsw (Task 3): the
Mellanox path. Bias/optical-power scaling is calibrated against a live module.
"""
from __future__ import annotations

import logging
import math
import os
import re
from pathlib import Path

from sensors2mqtt.collector.local.hwmon import iter_hwmon
from sensors2mqtt.discovery import SensorDef

log = logging.getLogger(__name__)

DBM_FLOOR = -40.0


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def _dbm(microwatts: float) -> float:
    """Optical power µW -> dBm, floored for dark/zero."""
    mw = microwatts / 1000.0
    if mw <= 0:
        return DBM_FLOOR
    return round(10.0 * math.log10(mw), 2)


def _sfp_sensor(prefix: str, field: str, name: str, unit: str,
                device_class: str | None = None) -> SensorDef:
    return SensorDef(
        suffix=f"{prefix}_{field}", name=name, unit=unit,
        device_class=device_class, state_class="measurement",
        entity_category="diagnostic",
    )


def _cage_label(hw: Path) -> str:
    """ten64 cage from the device link: dpmac1_sfp -> cage1, else the basename."""
    try:
        base = os.path.basename(os.path.realpath(hw / "device"))
    except OSError:
        base = ""
    m = re.search(r"dpmac(\d+)", base)
    if m:
        return f"cage{m.group(1)}"
    return re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "sfp"


def probe_sfp_hwmon(sysfs_root: str) -> list[tuple[SensorDef, float]]:
    """Full DDM from every `sfp`-driver hwmon node (one per populated cage)."""
    out: list[tuple[SensorDef, float]] = []
    for hw in iter_hwmon(Path(sysfs_root) / "sys/class/hwmon"):
        if _read(hw / "name") != "sfp":
            continue
        cage = _cage_label(hw)
        prefix = f"sfp_{cage}"
        # scaled scalar channels: (field, file, scale, unit, device_class, precision)
        for field, fname, scale, unit, dclass, prec in (
            ("temp", "temp1_input", 0.001, "°C", "temperature", 1),
            ("vcc", "in1_input", 0.001, "V", "voltage", 3),
            ("bias", "curr1_input", 0.001, "mA", "current", 3),  # calibrate vs live module
        ):
            raw = _read(hw / fname)
            if raw is None:
                continue
            try:
                out.append((_sfp_sensor(prefix, field, f"SFP {cage} {field}".title(), unit, dclass),
                            round(float(raw) * scale, prec)))
            except ValueError:
                continue
        # optical power -> dBm
        for field, fname in (("tx_power", "power1_input"), ("rx_power", "power2_input")):
            raw = _read(hw / fname)
            if raw is None:
                continue
            try:
                name = f"SFP {cage} {field.replace('_', ' ')}".title()
                out.append((_sfp_sensor(prefix, field, name, "dBm"), _dbm(float(raw))))
            except ValueError:
                continue
    return out
