# Generic hwmon Collector Implementation Plan (#57)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every local collector automatic host-agnostic hwmon harvesting (temp/voltage/fan/current/power) from a generic engine, and refactor RPi + Mellanox to lean on it.

**Architecture:** New `collector/local/hwmon.py` engine discovers every `/sys/class/hwmon` chip, maps each `*_input` channel to a `SensorDef` + `SysfsSource` via channel-type-default scales + a per-driver override registry, and returns `LocalSensor`s. `LocalCollector._probe_common_sensors()` calls it (all subclasses inherit). The per-driver registry IS the specialization layer: RPi/Mellanox naming lives there as channel overrides, shrinking the subclasses to device-identity + non-hwmon bits.

**Tech Stack:** Python 3.11+, stdlib only in the engine (`pathlib`/`re`/`os`), `SensorDef` from `sensors2mqtt.discovery`, `SysfsSource`/`LocalSensor` from `collector/local/base.py`, pytest with fake `sysfs_root` trees.

## Global Constraints

- Run all Python via `uv` (e.g. `uv run pytest ...`). No new runtime deps.
- Reads must work unprivileged (hwmon `*_input` are world-readable).
- HA suffixes are stable IDs: the RPi/Mellanox refactor MUST preserve every suffix those collectors publish today (`asic_temp`, `board_temp`, `fan1_rpm`..`fan8_rpm`, `rp1_v1`..`rp1_v4`, `rp1_temp`, `supply_voltage`, `supply_undervoltage`, `fan_rpm`).
- Generic (un-overridden) channels get `entity_category="diagnostic"`; override channels reproduce existing metadata (incl. non-diagnostic category).
- Scale by channel kind: `temp`÷1000 (°C, prec 1), `in`÷1000 (V, prec 3), `fan`×1 (RPM, prec 0), `curr`÷1000 (A, prec 3), `power`÷1e6 (W, prec 2). Per-driver scale overrides: `pac1934`/`emc1704` → `in`÷1e6.
- Publish everything (incl. −128 °C dead channels, 0 module slots). No curation filtering.
- `drivetemp` instance id = `disk_<slug(<hwmon>/device/wwid)>` (stable WWN), falling back to device basename.
- Thermal-backed hwmon nodes (device resolves under `…/thermal/thermal_zoneN`, OR `name` slug matches a thermal-zone `type` slug): skip their primary `temp1` (already a thermal-zone sensor), publish any other channels.
- `sysfs_root` injection honored everywhere (no hard-coded `/`).

---

## File Structure

- **Create** `src/sensors2mqtt/collector/local/hwmon.py` — engine: `KIND_META`, `CHAN_RE`, `ChannelSpec`, `DriverSpec`, `PERIPHERAL_HWMON`, `iter_hwmon`, `find_hwmon_by_name`, `discover_hwmon_sensors`.
- **Modify** `src/sensors2mqtt/collector/local/base.py` — `_find_hwmon_by_name` delegate, `_probe_peripheral_hwmon`, call in `_probe_common_sensors`, honest docstring.
- **Modify** `src/sensors2mqtt/collector/local/__init__.py` — `auto_detect` uses shared `find_hwmon_by_name`.
- **Modify** `src/sensors2mqtt/collector/local/rpi.py` — drop `_probe_rp1_adc`/`_probe_rpi_volt`(input part)/`_find_hwmon_by_name`; keep undervoltage alarm + cooling fan + vcgencmd.
- **Modify** `src/sensors2mqtt/collector/local/mellanox.py` — drop `sensors -j`, `SensorsJsonSource`, `poll()` override.
- **Create** `tests/test_local_hwmon.py`; **Modify** `tests/test_local_base.py`, `tests/test_local_rpi.py`, `tests/test_local_mellanox.py`.
- **Rebuild** `tests/fixtures/mellanox_sysfs/` to the real sw-bb-25g hwmon tree.

---

### Task 1: hwmon engine module

**Files:**
- Create: `src/sensors2mqtt/collector/local/hwmon.py`
- Test: `tests/test_local_hwmon.py`

**Interfaces:**
- Consumes: `LocalSensor`, `SysfsSource` from `base.py`; `SensorDef` from `discovery`.
- Produces: `find_hwmon_by_name(hwmon_root: Path, name: str) -> Path | None`; `discover_hwmon_sensors(sysfs_root: str, taken_suffixes: Iterable[str]) -> list[LocalSensor]`; `PERIPHERAL_HWMON: dict[str, DriverSpec]`; `ChannelSpec`, `DriverSpec` dataclasses.

- [ ] **Step 1: Write the failing tests** in `tests/test_local_hwmon.py`

```python
"""Tests for the generic hwmon discovery engine."""
import os
from pathlib import Path

from sensors2mqtt.collector.local.hwmon import (
    discover_hwmon_sensors,
    find_hwmon_by_name,
)


def mk_hwmon(root: Path, idx: int, name: str, channels: dict, *, device: str | None = None,
             labels: dict | None = None, wwid: str | None = None, thermal_zone: str | None = None):
    """Create a fake /sys/class/hwmon/hwmonN node. `channels` maps filename->value
    (e.g. {"temp1_input": "39850"}). `device` creates a device/ symlink basename."""
    hw = root / "sys/class/hwmon" / f"hwmon{idx}"
    hw.mkdir(parents=True)
    (hw / "name").write_text(name + "\n")
    for fname, val in channels.items():
        (hw / fname).write_text(f"{val}\n")
    for fname, val in (labels or {}).items():
        (hw / fname).write_text(f"{val}\n")
    if device or thermal_zone:
        dev_target = root / "sys/devices" / (thermal_zone or device)
        if thermal_zone:
            dev_target = root / "sys/devices/virtual/thermal" / thermal_zone
        dev_target.mkdir(parents=True, exist_ok=True)
        if wwid:
            (dev_target / "wwid").write_text(wwid + "\n")
        os.symlink(dev_target, hw / "device")


def mk_thermal_zone(root: Path, idx: int, ztype: str, temp: str = "50000"):
    z = root / "sys/class/thermal" / f"thermal_zone{idx}"
    z.mkdir(parents=True)
    (z / "type").write_text(ztype + "\n")
    (z / "temp").write_text(temp + "\n")


def suffixes(sensors):
    return {s.sensor.suffix for s in sensors}


def by_suffix(sensors, suffix):
    return next(s for s in sensors if s.sensor.suffix == suffix)


class TestGenericNaming:
    def test_nvme_composite_uses_label_and_device_instance(self, tmp_path):
        mk_hwmon(tmp_path, 0, "nvme", {"temp1_input": "39850"},
                 device="nvme0", labels={"temp1_label": "Composite"})
        out = discover_hwmon_sensors(str(tmp_path), taken_suffixes=set())
        s = by_suffix(out, "nvme0_composite")
        assert s.sensor.unit == "°C"
        assert s.sensor.device_class == "temperature"
        assert s.sensor.entity_category == "diagnostic"
        assert s.source.scale == 0.001 and s.source.precision == 1

    def test_unlabeled_temps_use_kind_index(self, tmp_path):
        mk_hwmon(tmp_path, 0, "emc1813", {"temp1_input": "40250", "temp2_input": "53375"},
                 device="0-004c")
        out = discover_hwmon_sensors(str(tmp_path), taken_suffixes=set())
        assert {"0_004c_temp1", "0_004c_temp2"} <= suffixes(out)

    def test_fan_kind_metadata(self, tmp_path):
        mk_hwmon(tmp_path, 0, "emc2301", {"fan1_input": "4902"}, device="0-002f")
        s = by_suffix(discover_hwmon_sensors(str(tmp_path), set()), "0_002f_fan1")
        assert s.sensor.unit == "RPM"
        assert s.sensor.icon == "mdi:fan"
        assert s.source.scale == 1.0 and s.source.precision == 0

    def test_multi_instance_disambiguates(self, tmp_path):
        mk_hwmon(tmp_path, 0, "nvme", {"temp1_input": "23850"}, device="nvme0",
                 labels={"temp1_label": "Composite"})
        mk_hwmon(tmp_path, 1, "nvme", {"temp1_input": "26850"}, device="nvme1",
                 labels={"temp1_label": "Composite"})
        assert {"nvme0_composite", "nvme1_composite"} <= suffixes(
            discover_hwmon_sensors(str(tmp_path), set()))


class TestScaleOverrides:
    def test_pac1934_microvolt_scale(self, tmp_path):
        mk_hwmon(tmp_path, 0, "pac1934", {"in0_input": "3286680"}, device="0-0011")
        s = by_suffix(discover_hwmon_sensors(str(tmp_path), set()), "0_0011_in0")
        assert s.sensor.unit == "V"
        assert s.source.scale == 1e-6  # microvolts, not the ABI millivolts


class TestChannelOverrides:
    def test_ath11k_renamed_to_wifi(self, tmp_path):
        mk_hwmon(tmp_path, 0, "ath11k_hwmon", {"temp1_input": "58000"}, device="phy1")
        out = discover_hwmon_sensors(str(tmp_path), set())
        assert "wifi_temp" in suffixes(out)
        assert by_suffix(out, "wifi_temp").sensor.name == "WiFi Temperature"

    def test_drivetemp_uses_wwid(self, tmp_path):
        mk_hwmon(tmp_path, 0, "drivetemp", {"temp1_input": "17000"},
                 device="0:0:1:0", wwid="naa.5000cca273c8468f")
        assert "disk_naa_5000cca273c8468f_temp1" in suffixes(
            discover_hwmon_sensors(str(tmp_path), set()))


class TestThermalBacked:
    def test_skips_symlinked_thermal_zone_primary(self, tmp_path):
        mk_hwmon(tmp_path, 0, "core_cluster", {"temp1_input": "67000"},
                 thermal_zone="thermal_zone0")
        assert discover_hwmon_sensors(str(tmp_path), set()) == []

    def test_skips_name_matched_thermal_zone(self, tmp_path):
        # No device symlink (fixture-style); name matches a registered thermal type.
        mk_thermal_zone(tmp_path, 0, "mlxsw", "42000")
        mk_hwmon(tmp_path, 0, "mlxsw", {"temp1_input": "42000"})
        assert discover_hwmon_sensors(str(tmp_path), set()) == []

    def test_thermal_backed_publishes_secondary_channel(self, tmp_path):
        mk_thermal_zone(tmp_path, 0, "acpitz", "27800")
        mk_hwmon(tmp_path, 0, "acpitz", {"temp1_input": "27800", "temp2_input": "29800"})
        out = suffixes(discover_hwmon_sensors(str(tmp_path), set()))
        assert "acpitz_temp2" in out
        assert "acpitz_temp1" not in out


class TestDedup:
    def test_taken_suffix_skipped(self, tmp_path):
        mk_hwmon(tmp_path, 0, "nvme", {"temp1_input": "39850"}, device="nvme0",
                 labels={"temp1_label": "Composite"})
        assert discover_hwmon_sensors(str(tmp_path), {"nvme0_composite"}) == []


class TestFindHwmon:
    def test_find_by_name(self, tmp_path):
        mk_hwmon(tmp_path, 0, "nvme", {"temp1_input": "1"}, device="nvme0")
        mk_hwmon(tmp_path, 1, "emc2301", {"fan1_input": "1"}, device="0-002f")
        hw = find_hwmon_by_name(tmp_path / "sys/class/hwmon", "emc2301")
        assert hw is not None and (hw / "name").read_text().strip() == "emc2301"

    def test_find_missing_returns_none(self, tmp_path):
        (tmp_path / "sys/class/hwmon").mkdir(parents=True)
        assert find_hwmon_by_name(tmp_path / "sys/class/hwmon", "nope") is None
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_local_hwmon.py -q`
Expected: FAIL/ERROR — `ModuleNotFoundError: sensors2mqtt.collector.local.hwmon`.

- [ ] **Step 3: Implement `src/sensors2mqtt/collector/local/hwmon.py`**

```python
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


@dataclass(frozen=True)
class DriverSpec:
    """Per-driver override (keyed by hwmon `name`)."""
    instance_id: Callable[[Path], str] | None = None
    scale: dict[str, float] = field(default_factory=dict)
    channels: dict[str, ChannelSpec] = field(default_factory=dict)
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
    chans = {"temp1": ChannelSpec(suffix="asic_temp", name="ASIC Temperature")}
    fan_names = ["Fan 1 Front", "Fan 1 Rear", "Fan 2 Front", "Fan 2 Rear",
                 "Fan 3 Front", "Fan 3 Rear", "Fan 4 Front", "Fan 4 Rear"]
    for i, fname in enumerate(fan_names, start=1):
        chans[f"fan{i}"] = ChannelSpec(suffix=f"fan{i}_rpm", name=fname)
    for n in range(2, 58):  # temp2..temp57 -> SFP front-panel module temps
        port = n - 1
        chans[f"temp{n}"] = ChannelSpec(
            suffix=f"sfp_port{port:02d}_temp", name=f"SFP Port {port:02d} Temperature")
    return chans


PERIPHERAL_HWMON: dict[str, DriverSpec] = {
    # Voltage scale quirks (microvolts, not the ABI millivolts).
    "pac1934": DriverSpec(scale={"in": 1e-6}),
    "emc1704": DriverSpec(scale={"in": 1e-6}),
    # Friendlier names.
    "ath11k_hwmon": DriverSpec(
        channels={"temp1": ChannelSpec(suffix="wifi_temp", name="WiFi Temperature")}),
    "drivetemp": DriverSpec(instance_id=_drivetemp_instance),
    # RPi specialization naming (Task 3).
    "rp1_adc": DriverSpec(channels={
        "in1": ChannelSpec(suffix="rp1_v1", name="RP1 Voltage 1"),
        "in2": ChannelSpec(suffix="rp1_v2", name="RP1 Voltage 2"),
        "in3": ChannelSpec(suffix="rp1_v3", name="RP1 Voltage 3"),
        "in4": ChannelSpec(suffix="rp1_v4", name="RP1 Voltage 4"),
        "temp1": ChannelSpec(suffix="rp1_temp", name="RP1 Temperature"),
    }),
    "rpi_volt": DriverSpec(channels={
        "in0": ChannelSpec(suffix="supply_voltage", name="Supply Voltage")}),
    # Mellanox specialization naming (Task 4).
    "mlxsw": DriverSpec(channels=_mlxsw_channels()),
    "jc42": DriverSpec(channels={"temp1": ChannelSpec(suffix="board_temp", name="Board Temperature")}),
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
            cspec = spec.channels.get(chan, ChannelSpec())
            if cspec.skip:
                continue
            meta = KIND_META[kind]
            if cspec.suffix:
                suffix, disp = cspec.suffix, (cspec.name or cspec.suffix)
            else:
                label = _read(hw / f"{chan}_label")
                chan_part = _slug(label) if label else chan
                suffix = _slug(f"{instance}_{chan_part}")
                disp = _title(suffix)
            if suffix in taken:
                continue
            taken.add(suffix)
            out.append(LocalSensor(
                sensor=SensorDef(
                    suffix=suffix,
                    name=disp,
                    unit=meta.unit,
                    device_class=cspec.device_class or meta.device_class,
                    state_class="measurement",
                    icon=cspec.icon or meta.icon,
                    entity_category=cspec.entity_category or "diagnostic",
                ),
                source=SysfsSource(
                    path=str((hw / f.name).relative_to(root)),
                    scale=spec.scale.get(kind, meta.scale),
                    precision=meta.precision,
                ),
            ))
    return out
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/test_local_hwmon.py -q`
Expected: PASS (all tests green).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/sensors2mqtt/collector/local/hwmon.py tests/test_local_hwmon.py
git add src/sensors2mqtt/collector/local/hwmon.py tests/test_local_hwmon.py
git commit -m "feat(local): generic hwmon discovery engine"
```

---

### Task 2: Promote `find_hwmon_by_name` into the shared layer

**Files:**
- Modify: `src/sensors2mqtt/collector/local/__init__.py` (auto_detect's inline hwmon scan, lines 41-54)
- Test: `tests/test_local_autodetect.py` (existing — must stay green)

**Interfaces:**
- Consumes: `find_hwmon_by_name` from `hwmon.py` (Task 1).
- Produces: no new public API; behavior-preserving DRY refactor.

- [ ] **Step 1: Replace the inline hwmon scan in `auto_detect()`**

In `__init__.py`, replace the Mellanox-detection block (currently iterating `hwmon_dir.glob("hwmon*")` and reading each `name`) with a loop over the shared helper. Keep the "mlxsw substring" match (driver names are like `mlxsw`):

```python
    # Check for Mellanox ASIC hwmon driver (name contains "mlxsw")
    from sensors2mqtt.collector.local.hwmon import iter_hwmon

    for hwmon in iter_hwmon(root / "sys/class/hwmon"):
        name_file = hwmon / "name"
        try:
            if name_file.exists() and "mlxsw" in name_file.read_text():
                from sensors2mqtt.collector.local.mellanox import MellanoxCollector
                log.info("Auto-detected Mellanox switch (driver: %s)",
                         name_file.read_text().strip())
                return MellanoxCollector
        except OSError:
            pass
```

- [ ] **Step 2: Run the auto_detect tests**

Run: `uv run pytest tests/test_local_autodetect.py -q`
Expected: PASS (detection behavior unchanged).

- [ ] **Step 3: Commit**

```bash
git add src/sensors2mqtt/collector/local/__init__.py
git commit -m "refactor(local): auto_detect uses shared hwmon iterator"
```

---

### Task 3: Wire engine into the base + refactor RPi onto it

**Files:**
- Modify: `src/sensors2mqtt/collector/local/base.py` (`_probe_common_sensors` line 207-210; add `_find_hwmon_by_name` + `_probe_peripheral_hwmon`; docstring line 81-85)
- Modify: `src/sensors2mqtt/collector/local/rpi.py` (drop `_probe_rp1_adc`, `_probe_rpi_volt` input handling, `_find_hwmon_by_name`)
- Test: `tests/test_local_base.py`, `tests/test_local_rpi.py`

**Interfaces:**
- Consumes: `discover_hwmon_sensors`, `find_hwmon_by_name` from `hwmon.py`.
- Produces: `LocalCollector._find_hwmon_by_name(name) -> Path | None`; base now registers generic hwmon sensors for all subclasses.

- [ ] **Step 1: Update base-collector count tests** in `tests/test_local_base.py`

The base now runs the engine, so `rpi5_sysfs` gains 5 sensors (`rp1_v1`..`rp1_v4`, `rp1_temp` via the rp1_adc override; its `cpu_thermal` hwmon is thermal-backed → skipped) and `rpi4_sysfs` gains 1 (`supply_voltage`). Replace `TestSensorCounts`:

```python
class TestSensorCounts:
    def test_rpi5_count(self):
        """8 common + rp1_adc(4 V + 1 temp) = 13. cpu_thermal hwmon is thermal-backed."""
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        assert len(c._sensors_list) == 13
        suffixes = {ls.sensor.suffix for ls in c._sensors_list}
        assert {"rp1_v1", "rp1_v2", "rp1_v3", "rp1_v4", "rp1_temp"} <= suffixes

    def test_rpi4_count(self):
        """8 common + rpi_volt in0 -> supply_voltage = 9."""
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi4_sysfs"))
        assert len(c._sensors_list) == 9
        assert "supply_voltage" in {ls.sensor.suffix for ls in c._sensors_list}

    def test_rpizero_count(self):
        """No hwmon -> 8 common only."""
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpizero_sysfs"))
        assert len(c._sensors_list) == 8

    def test_mellanox_count_old_fixture(self):
        """Old mellanox fixture: only a thermal-backed mlxsw hwmon -> 8 common."""
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "mellanox_sysfs"))
        assert len(c._sensors_list) == 8

    def test_no_double_cpu_temp(self):
        """core_cluster/cpu_thermal hwmon must NOT duplicate the thermal-zone cpu_temp."""
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert suffixes.count("cpu_temp") == 1
        assert "cpu_thermal_temp1" not in suffixes
```

- [ ] **Step 2: Run them, verify they fail**

Run: `uv run pytest tests/test_local_base.py::TestSensorCounts -q`
Expected: FAIL (counts still 8/8/8; no rp1_v* yet — base doesn't probe hwmon).

- [ ] **Step 3: Wire the engine into `base.py`**

Replace `_probe_common_sensors` and the empty `_probe_hardware_sensors`-area helpers, and fix the docstring. Add the delegate + probe (function-local import avoids the `base ↔ hwmon` import cycle):

```python
    def _probe_common_sensors(self) -> None:
        """Probe sensors available on any Linux box."""
        self._probe_thermal_zones()
        self._probe_peripheral_hwmon()
        self._probe_system_diagnostics()

    def _probe_peripheral_hwmon(self) -> None:
        """Register generic hwmon sensors (deduped against already-probed suffixes)."""
        from sensors2mqtt.collector.local.hwmon import discover_hwmon_sensors

        taken = {ls.sensor.suffix for ls in self._sensors_list}
        self._sensors_list.extend(
            discover_hwmon_sensors(str(self._sysfs_root), taken_suffixes=taken)
        )

    def _find_hwmon_by_name(self, driver_name: str):
        """Find hwmon directory by driver name (shared primitive)."""
        from sensors2mqtt.collector.local.hwmon import find_hwmon_by_name

        return find_hwmon_by_name(self._sysfs_root / "sys/class/hwmon", driver_name)
```

Also update the class docstring (line 81-85) to state it probes thermal zones, generic hwmon drivers, and /proc — now true.

- [ ] **Step 4: Run base tests, verify pass**

Run: `uv run pytest tests/test_local_base.py -q`
Expected: PASS.

- [ ] **Step 5: Refactor `rpi.py` onto the engine**

The `rp1_adc` and `rpi_volt` `in0_input` sensors now come from the base engine (via the registry overrides), with identical suffixes. Delete `_probe_rp1_adc`, delete the `in0_input` half of `_probe_rpi_volt` (keep the `in0_lcrit_alarm` half — it is an alarm flag, not an `*_input`, so the engine never touches it), and delete the now-unused private `_find_hwmon_by_name`. Rename the trimmed method and update `_probe_hardware_sensors`:

```python
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
```

Remove the now-unused `re` import if it is no longer referenced. Keep `_probe_cooling_fan`, `_probe_vcgencmd`, `poll()`, device-identity, and `THROTTLE_BITS`/`VcgencmdSource` exactly as-is.

- [ ] **Step 6: Run RPi tests, verify pass**

Run: `uv run pytest tests/test_local_rpi.py -q`
Expected: PASS — suffix assertions (`rp1_v1`..`rp1_v4`, `rp1_temp`, `supply_voltage`, `supply_undervoltage`) and counts (14/9/8/8, +5 with vcgencmd) are unchanged because the engine reproduces the same suffixes. If a test references the deleted `_probe_rp1_adc`/`_find_hwmon_by_name` by name, update it to assert behavior (suffix presence) instead.

- [ ] **Step 7: Full suite + lint + commit**

```bash
uv run pytest -q
uv run ruff check src/sensors2mqtt/collector/local/
git add src/sensors2mqtt/collector/local/base.py src/sensors2mqtt/collector/local/rpi.py tests/test_local_base.py tests/test_local_rpi.py
git commit -m "feat(local): probe generic hwmon in base; lean RPi on the engine"
```

---

### Task 4: Refactor Mellanox onto the engine

**Files:**
- Modify: `src/sensors2mqtt/collector/local/mellanox.py` (drop `sensors -j`, `SensorsJsonSource`, `MELLANOX_SENSORS`, `poll()`/`_run_sensors_json`)
- Rebuild: `tests/fixtures/mellanox_sysfs/` (real sw-bb-25g hwmon tree)
- Test: `tests/test_local_mellanox.py`

**Interfaces:**
- Consumes: the base engine (Task 3) + the `mlxsw`/`jc42` registry overrides (Task 1).
- Produces: `MellanoxCollector` reduced to device identity (`manufacturer`/`model`/`_mac_interfaces`).

- [ ] **Step 1: Rebuild the `mellanox_sysfs` fixture** to the real tree

Replace the single thermal-only hwmon node with the real sw-bb-25g layout (captured 2026-06-22). Write a fixture-builder helper (or commit files); the key nodes:
- `sys/class/thermal/thermal_zone0/type` = `acpitz`, `/temp` = `27800` (so the base thermal probe yields `acpitz_temp`).
- `sys/class/hwmon/hwmon0/name` = `acpitz` (no device symlink → name-matches the thermal zone → primary skipped; `temp2_input`=`29800` → `acpitz_temp2`).
- `sys/class/hwmon/hwmon1/name` = `mlxsw`, with `fan1_input`..`fan8_input` = `6239,5399,6355,5399,6385,5464,6297,5464`, `temp1_input`=`42000`, and `temp2_input`..`temp57_input` all `0` each with a `tempN_label` = `front panel 0NN`.
- `sys/class/hwmon/hwmon2/name` = `jc42`, `temp1_input` = `29375`.
- `sys/class/hwmon/hwmon3/name` = `coretemp`, `temp1_input`=`45000` (`temp1_label`=`Package id 0`), `temp2_input`=`45000` (`Core 0`), `temp3_input`=`41000` (`Core 1`).
- Keep the existing `sys/class/net/{eth0,bmc}/address` files so MAC tests still pass.

Generate the 56 module-temp files with a small loop in the fixture builder rather than by hand.

- [ ] **Step 2: Rewrite `tests/test_local_mellanox.py`**

Drop all `subprocess.run` mocking and the `MELLANOX_SENSORS`/`sensors_j` fixture tests. Assert the engine-sourced sensors:

```python
"""Tests for MellanoxCollector (refactored onto the generic hwmon engine)."""
from pathlib import Path
from unittest.mock import patch

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.local.mellanox import MellanoxCollector

FIXTURES = Path(__file__).parent / "fixtures"


def make_mellanox():
    return MellanoxCollector(config=MqttConfig(host="t", port=1883, user="u", password="p"),
                             sysfs_root=str(FIXTURES / "mellanox_sysfs"))


class TestMellanoxDeviceInfo:
    @patch("sensors2mqtt.base.socket.gethostname", return_value="sw-bb-25g")
    def test_identity(self, _m):
        c = make_mellanox()
        assert c.device.node_id == "sw_bb_25g"
        assert c.device.manufacturer == "Mellanox"
        assert c.device.model == "SN2410"


class TestMellanoxSensors:
    def test_preserved_suffixes(self):
        s = {ls.sensor.suffix for ls in make_mellanox()._sensors_list}
        assert {"asic_temp", "board_temp"} <= s
        assert {f"fan{i}_rpm" for i in range(1, 9)} <= s

    def test_sfp_module_ports(self):
        s = {ls.sensor.suffix for ls in make_mellanox()._sensors_list}
        assert "sfp_port01_temp" in s
        assert "sfp_port56_temp" in s

    def test_poll_reads_from_sysfs(self):
        v = make_mellanox().poll()
        assert v["asic_temp"] == 42.0
        assert v["board_temp"] == round(29375 * 0.001, 1)
        assert v["fan1_rpm"] == 6239
        assert v["sfp_port01_temp"] == 0.0  # empty cage

    def test_cpu_temp_from_thermal_zone(self):
        # acpitz thermal zone -> acpitz_temp (not cpu_temp on this box)
        s = {ls.sensor.suffix for ls in make_mellanox()._sensors_list}
        assert "acpitz_temp" in s
```

- [ ] **Step 3: Run, verify fail**

Run: `uv run pytest tests/test_local_mellanox.py -q`
Expected: FAIL — `MellanoxCollector` still imports `MELLANOX_SENSORS`/uses `sensors -j`; some asserted suffixes come only after the slimmed collector relies on the engine.

- [ ] **Step 4: Slim `mellanox.py`**

```python
"""Mellanox SN2410 sensor collector.

All sensors come from the generic hwmon engine in the base collector via the
`mlxsw`/`jc42` registry overrides; this subclass only supplies device identity.
"""
from __future__ import annotations

import logging

from sensors2mqtt.collector.local.base import LocalCollector

log = logging.getLogger(__name__)


class MellanoxCollector(LocalCollector):
    """Mellanox SN2410: identity only; sensors via the base hwmon engine."""

    def _manufacturer(self) -> str:
        return "Mellanox"

    def _model(self) -> str:
        return "SN2410"

    def _mac_interfaces(self) -> tuple[str, ...]:
        return ("bmc", "eth0")

    def _log_summary(self, values: dict) -> None:
        log.info(
            "Published: ASIC=%s°C Board=%s°C CPU=%s°C",
            values.get("asic_temp", "?"),
            values.get("board_temp", "?"),
            values.get("cpu_temp", values.get("acpitz_temp", "?")),
        )
```

- [ ] **Step 5: Run mellanox + full suite, verify pass**

Run: `uv run pytest tests/test_local_mellanox.py -q && uv run pytest -q`
Expected: PASS. (If `tests/fixtures/sensors_j_sw_bb_25g.json` is now unused, leave it; removing it is optional cleanup.)

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/sensors2mqtt/collector/local/mellanox.py tests/test_local_mellanox.py
git add src/sensors2mqtt/collector/local/mellanox.py tests/test_local_mellanox.py tests/fixtures/mellanox_sysfs
git commit -m "feat(local): refactor Mellanox onto the generic hwmon engine"
```

---

## Self-Review

**Spec coverage:** engine + 5 channel kinds (Task 1) ✓; channel-type scaling + per-driver overrides incl. µV (Task 1) ✓; promote `find_hwmon_by_name` (Tasks 1-2, base delegate Task 3) ✓; wire into `_probe_common_sensors` for all collectors + dedup (Task 3) ✓; thermal-zone overlap incl. name-match fallback (Task 1) ✓; everything/diagnostic (Task 1 defaults) ✓; preserve suffixes/metadata (Tasks 3-4) ✓; Mellanox drops `sensors -j` (Task 4) ✓; RPi keeps vcgencmd + undervoltage (Task 3) ✓; drivetemp wwid id (Task 1) ✓; `sfp_portNN` folds in #41 (Task 1 `_mlxsw_channels` + Task 4 fixture) ✓.

**Out-of-scope (per spec):** #56 ten64 voltage-rail naming + identity; #41 ten64 hot-plug re-probe + DDM curr/power fields; cooling-fan stays in `RpiCollector` (non-standard `/sys/devices/platform/cooling_fan/hwmon` path — a future override could move it, but it is not duplicated because no `cooling_fan` registry entry exists and the existing RPi fixtures carry no fan node).

**Type consistency:** `discover_hwmon_sensors(sysfs_root: str, taken_suffixes)` and `find_hwmon_by_name(hwmon_root: Path, name)` signatures are used identically in base.py (Task 3) and tests (Task 1). `ChannelSpec.suffix`/`DriverSpec.channels` keys (`"temp1"`, `"in0"`, `"fan1"`) match `CHAN_RE` channel names throughout.

**Known risk:** if `coretemp`/`acpitz` appear on a host with a CPU thermal zone whose `type` slug does NOT match the hwmon `name` and has no `device` symlink, a duplicate CPU temp could publish; on the real fleet (ten64 core_cluster, big-storage coretemp w/ symlinks, mellanox acpitz name-match) this does not occur.
