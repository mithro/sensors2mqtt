"""Generic hwmon discovery engine.

Discovers every chip under /sys/class/hwmon and maps each *_input channel to a
SensorDef + SysfsSource using channel-type-default scaling plus a per-driver
override registry. Shared by the base collector (all hosts) and the RPi/Mellanox
specializations (whose naming lives here as channel overrides).
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sensors2mqtt.collector.local.base import LocalSensor, SysfsSource
from sensors2mqtt.discovery import SensorDef

CHAN_RE = re.compile(r"^(temp|in|fan|curr|power)(\d+)_input$")


@dataclass(frozen=True)
class KindMeta:
    scale: float
    precision: int
    unit: str
    device_class: str | None
    icon: str | None = None


# hwmon ABI defaults per channel kind.
KIND_META: dict[str, KindMeta] = {
    "temp": KindMeta(0.001, 1, "°C", "temperature"),
    "in": KindMeta(0.001, 3, "V", "voltage"),
    "fan": KindMeta(1.0, 0, "RPM", None, "mdi:fan"),
    "curr": KindMeta(0.001, 3, "A", "current"),
    "power": KindMeta(1e-6, 2, "W", "power"),
}


@dataclass(frozen=True)
class ChannelSpec:
    """Per-channel override (keyed by raw channel name e.g. 'temp1')."""
    suffix: str | None = None
    name: str | None = None
    device_class: str | None = None
    entity_category: str | None = None
    icon: str | None = None
    skip: bool = False
    diagnostic: bool = True  # generic sensors are diagnostic; False = primary entity


@dataclass(frozen=True)
class DriverSpec:
    """Per-driver override (keyed by hwmon `name`)."""
    instance_id: Callable[[Path], str] | None = None
    scale: dict[str, float] = field(default_factory=dict)
    channels: dict[str, ChannelSpec] = field(default_factory=dict)
    instance_channels: dict[str, dict[str, ChannelSpec]] = field(default_factory=dict)
    include: bool = True


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _title(suffix: str) -> str:
    return suffix.replace("_", " ").title()


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def iter_hwmon(hwmon_root: Path):
    if not hwmon_root.is_dir():
        return
    for hw in sorted(hwmon_root.glob("hwmon*"),
                     key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        if hw.is_dir():  # skip any stray non-directory hwmon* entry
            yield hw


def find_hwmon_by_name(hwmon_root: Path, name: str) -> Path | None:
    for hw in iter_hwmon(hwmon_root):
        if _read(hw / "name") == name:
            return hw
    return None


def _device_basename(hw: Path) -> str | None:
    dev = hw / "device"
    if dev.exists():
        try:
            return os.path.basename(os.path.realpath(dev))
        except OSError:
            return None
    return None


def _drivetemp_instance(hw: Path) -> str:
    wwid = _read(hw / "device" / "wwid")
    if wwid:
        return f"disk_{_slug(wwid)}"
    base = _device_basename(hw)
    return _slug(base) if base else "disk"


def _mlxsw_channels() -> dict[str, ChannelSpec]:
    # The switch's own primary sensors. temp2..temp57 (per-port transceiver
    # module temps) are intentionally NOT named here: #57 lets them publish
    # generically as mlxsw_front_panel_0NN; #41 owns proper sfp_portNN naming + DDM.
    chans = {"temp1": ChannelSpec(suffix="asic_temp", name="ASIC Temperature", diagnostic=False)}
    fan_names = ["Fan 1 Front", "Fan 1 Rear", "Fan 2 Front", "Fan 2 Rear",
                 "Fan 3 Front", "Fan 3 Rear", "Fan 4 Front", "Fan 4 Rear"]
    for i, fname in enumerate(fan_names, start=1):
        chans[f"fan{i}"] = ChannelSpec(suffix=f"fan{i}_rpm", name=fname, diagnostic=False)
    return chans


PERIPHERAL_HWMON: dict[str, DriverSpec] = {
    # Traverse Ten64 board sensors (pac1934 × 2, emc1704, emc1813, emc2301).
    "pac1934": DriverSpec(
        scale={"in": 1e-6},
        instance_channels={
            "0_0011": {
                "in0": ChannelSpec(suffix="minipcie_p4_3v3", name="miniPCIe P4 3.3V"),
                "in1": ChannelSpec(suffix="minipcie_p5_3v3", name="miniPCIe P5 3.3V"),
                "in2": ChannelSpec(suffix="lte_m2b_3v3", name="LTE/M.2B 3.3V"),
                "in3": ChannelSpec(suffix="rail_5v", name="5V Rail"),
            },
            "0_001a": {
                "in0": ChannelSpec(suffix="ddr_vdd_1v2", name="DDR VDD (1.2V)"),
                "in1": ChannelSpec(suffix="ddr_vpp_2v5", name="DDR VPP (2.5V)"),
                "in2": ChannelSpec(suffix="ddr_vtt_0v6", name="DDR VTT (0.6V)"),
                "in3": ChannelSpec(suffix="ovdd_1v8", name="1.8V (OVDD)"),
            },
        },
    ),
    "emc1704": DriverSpec(
        scale={"in": 1e-6},
        channels={
            "in0": ChannelSpec(
                suffix="supply_voltage", name="Supply Voltage (12V)", diagnostic=False),
            "in1": ChannelSpec(skip=True),
            "temp0": ChannelSpec(suffix="emc1704_internal_temp", name="EMC1704 Internal Temp"),
            "temp1": ChannelSpec(suffix="ls1088_die_temp", name="LS1088 Die Temperature"),
            "temp2": ChannelSpec(suffix="board_temp", name="Board Temperature", diagnostic=False),
            "temp3": ChannelSpec(skip=True),
        },
    ),
    "emc1813": DriverSpec(channels={
        "temp1": ChannelSpec(suffix="emc1813_internal_temp", name="PHY Monitor Internal Temp"),
        "temp2": ChannelSpec(suffix="phy_eth0_3_temp", name="PHY Temp (eth0-eth3)"),
        "temp3": ChannelSpec(suffix="phy_eth4_7_temp", name="PHY Temp (eth4-eth7)"),
    }),
    "emc2301": DriverSpec(channels={
        "fan1": ChannelSpec(suffix="fan_rpm", name="Fan Speed", diagnostic=False),
    }),
    # Friendlier names.
    "ath11k_hwmon": DriverSpec(
        channels={"temp1": ChannelSpec(suffix="wifi_temp", name="WiFi Temperature")}),
    "drivetemp": DriverSpec(instance_id=_drivetemp_instance),
    # RPi specialization naming (Task 3) - primary (non-diagnostic) sensors.
    "rp1_adc": DriverSpec(channels={
        "in1": ChannelSpec(suffix="rp1_v1", name="RP1 Voltage 1", diagnostic=False),
        "in2": ChannelSpec(suffix="rp1_v2", name="RP1 Voltage 2", diagnostic=False),
        "in3": ChannelSpec(suffix="rp1_v3", name="RP1 Voltage 3", diagnostic=False),
        "in4": ChannelSpec(suffix="rp1_v4", name="RP1 Voltage 4", diagnostic=False),
        "temp1": ChannelSpec(suffix="rp1_temp", name="RP1 Temperature", diagnostic=False),
    }),
    "rpi_volt": DriverSpec(channels={
        "in0": ChannelSpec(suffix="supply_voltage", name="Supply Voltage", diagnostic=False)}),
    # Mellanox specialization naming (Task 4). instance_id keeps the un-named
    # per-port module temps generic as mlxsw_front_panel_0NN (for #41 to refine).
    "mlxsw": DriverSpec(instance_id=lambda hw: "mlxsw", channels=_mlxsw_channels()),
    "jc42": DriverSpec(channels={
        "temp1": ChannelSpec(
            suffix="board_temp",
            name="Board Temperature",
            diagnostic=False,
        )
    }),
}


def _thermal_zone_types(sysfs_root: Path) -> set[str]:
    out: set[str] = set()
    tdir = sysfs_root / "sys/class/thermal"
    if tdir.is_dir():
        for z in tdir.glob("thermal_zone*"):
            t = _read(z / "type")
            if t:
                out.add(_slug(t))
    return out


def _is_thermal_backed(hw: Path, name: str, thermal_types: set[str]) -> bool:
    dev = hw / "device"
    if dev.exists():
        # realpath is intentionally sysfs_root-agnostic: real sysfs `device`
        # symlinks are relative, so they resolve within an injected sysfs_root,
        # and we only test the resolved path's "thermal_zone" component/basename.
        real = os.path.realpath(dev)
        if "/thermal/thermal_zone" in real or os.path.basename(real).startswith("thermal_zone"):
            return True
    return _slug(name) in thermal_types


def discover_hwmon_sensors(sysfs_root: str, taken_suffixes: Iterable[str]) -> list[LocalSensor]:
    root = Path(sysfs_root)
    hwmon_root = root / "sys/class/hwmon"
    taken = set(taken_suffixes)
    thermal_types = _thermal_zone_types(root)
    out: list[LocalSensor] = []
    for hw in iter_hwmon(hwmon_root):
        name = _read(hw / "name")
        if not name:
            continue
        spec = PERIPHERAL_HWMON.get(name, DriverSpec())
        if not spec.include:
            continue
        thermal_backed = _is_thermal_backed(hw, name, thermal_types)
        instance = spec.instance_id(hw) if spec.instance_id else (
            _slug(_device_basename(hw) or name))
        for f in sorted(hw.iterdir()):
            m = CHAN_RE.match(f.name)
            if not m:
                continue
            kind, idx = m.group(1), m.group(2)
            chan = f"{kind}{idx}"
            if thermal_backed and chan == "temp1":
                continue
            cspec = (
                spec.instance_channels.get(instance, {}).get(chan)
                or spec.channels.get(chan)
                or ChannelSpec()
            )
            if cspec.skip:
                continue
            meta = KIND_META[kind]
            if cspec.suffix:
                suffix, disp = cspec.suffix, (cspec.name or cspec.suffix)
            else:
                # Deterministic fallback name (e.g. "0 004C Temp1"); human-friendly
                # names come from PERIPHERAL_HWMON overrides (RPi/Mellanox today,
                # ten64/SFP via #56/#41), not from prettifying the generic slug.
                label = _read(hw / f"{chan}_label")
                chan_part = _slug(label) if label else chan
                suffix = _slug(f"{instance}_{chan_part}")
                disp = _title(suffix)
            if suffix in taken:
                continue
            taken.add(suffix)
            entity_category = cspec.entity_category or (
                "diagnostic" if cspec.diagnostic else None
            )
            out.append(LocalSensor(
                sensor=SensorDef(
                    suffix=suffix,
                    name=disp,
                    unit=meta.unit,
                    device_class=cspec.device_class or meta.device_class,
                    state_class="measurement",
                    icon=cspec.icon or meta.icon,
                    entity_category=entity_category,
                ),
                source=SysfsSource(
                    path=str((hw / f.name).relative_to(root)),
                    scale=spec.scale.get(kind, meta.scale),
                    precision=meta.precision,
                ),
            ))
    return out
