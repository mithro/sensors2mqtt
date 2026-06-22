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
import subprocess
from pathlib import Path

from sensors2mqtt.collector.local.hwmon import find_hwmon_by_name, iter_hwmon
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


# ---------------------------------------------------------------------------
# Mellanox mlxsw backend (Task 3)
# ---------------------------------------------------------------------------

_DDM_PATTERNS = [
    ("temp", "module temperature", r"(-?\d+\.?\d*)\s*degrees c"),
    ("vcc", "module voltage", r"(-?\d+\.?\d*)\s*v\b"),
    ("bias", "laser bias current", r"(-?\d+\.?\d*)\s*ma\b"),
    ("tx_power", "laser output power", r"(-?\d+\.?\d*)\s*dbm"),
    ("tx_power", "transmit avg optical power", r"(-?\d+\.?\d*)\s*dbm"),
    ("rx_power", "receiver signal average optical power", r"(-?\d+\.?\d*)\s*dbm"),
    ("rx_power", "rcvr signal avg optical power", r"(-?\d+\.?\d*)\s*dbm"),
]


def parse_ethtool_ddm(text: str) -> dict[str, float]:
    """Pull DDM fields from `ethtool -m` decoded text. Tolerant of missing lines."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        label, sep, val = line.partition(":")
        if not sep:
            continue
        label, val = label.strip().lower(), val.strip().lower()
        for field, key, pat in _DDM_PATTERNS:
            if field in out or key not in label:
                continue
            m = re.search(pat, val)
            if m:
                out[field] = float(m.group(1))
    return out


def run_ethtool(iface: str) -> str:
    try:
        r = subprocess.run(
            ["ethtool", "-m", iface], capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            log.warning(
                "ethtool -m %s failed (rc=%d): %s", iface, r.returncode, r.stderr.strip()
            )
            return ""
        return r.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("ethtool -m %s error: %s", iface, e)
        return ""


def probe_sfp_mlxsw(sysfs_root: str, ethtool=run_ethtool) -> list[tuple[SensorDef, float]]:
    """Mellanox per-port DDM: temp from mlxsw hwmon, rest from `ethtool -m`."""
    hw = find_hwmon_by_name(Path(sysfs_root) / "sys/class/hwmon", "mlxsw")
    if hw is None:
        return []
    out: list[tuple[SensorDef, float]] = []
    for n in range(2, 58):              # temp2..temp57 = front-panel ports 1..56
        port = n - 1
        crit = _read(hw / f"temp{n}_crit")
        if not crit or crit == "0":     # crit==0 -> no DDM module present
            continue
        prefix = f"sfp_port{port:02d}"
        traw = _read(hw / f"temp{n}_input")
        if traw is not None:
            try:
                out.append((
                    _sfp_sensor(
                        prefix, "temp", f"SFP Port {port:02d} Temperature", "°C", "temperature"
                    ),
                    round(float(traw) * 0.001, 1),
                ))
            except ValueError:
                pass
        ddm = parse_ethtool_ddm(ethtool(f"swp{port:02d}"))
        for field, unit, dclass in (
            ("vcc", "V", "voltage"),
            ("bias", "mA", "current"),
            ("tx_power", "dBm", None),
            ("rx_power", "dBm", None),
        ):
            if field in ddm:
                name = f"SFP Port {port:02d} {field.replace('_', ' ').upper()}"
                out.append((_sfp_sensor(prefix, field, name, unit, dclass), ddm[field]))
    return out
