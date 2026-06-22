# Ten64 Board-Sensor Collector Implementation Plan (#56)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Traverse Ten64 a proper device identity and meaningful names for its onboard power-monitor + board-temperature channels, layered on #57's generic hwmon engine.

**Architecture:** A small `instance_channels` (per-i2c-address) override added to the engine, ten64 naming entries in `PERIPHERAL_HWMON`, and a thin `Ten64Collector` (identity only) reached via an `auto_detect` model match. The engine already reads/publishes every ten64 chip after #57 — this only renames channels and sets identity.

**Tech Stack:** Builds on #57's `collector/local/hwmon.py`; `SensorDef` from `discovery`; pytest with `tmp_path` fake-sysfs trees. No new runtime deps.

## Global Constraints

- Run all Python via `uv` (`uv run pytest …`). No new runtime deps.
- **Depends on #57** (the engine + registry + the `ChannelSpec.diagnostic` field added in #57's revision). #56 executes after #57 merges; this worktree rebases onto the updated `main` before execution.
- New registry entries are keyed to ten64-only chips (`pac1934`/`emc1704`/`emc1813`/`emc2301`) and the two PAC1934 instances (`0_0011`/`0_001a`); nothing else changes.
- Common suffixes for the logical owners: `supply_voltage`, `board_temp`, `fan_rpm` (all **non-diagnostic**, matching RPi/Mellanox); the 8 pac rails + detailed board/PHY temps stay `diagnostic`.
- Do not remap `cpu_temp`/`soc_temp` (they remain #57 thermal-zone sensors).
- pac1934/emc1704 voltage scale is µV (÷1e6) — already set in #57.
- SFP/transceiver channels are **out of scope** (owned by #41).

---

## File Structure

- **Modify** `src/sensors2mqtt/collector/local/hwmon.py` — add `DriverSpec.instance_channels` + lookup; add ten64 registry entries.
- **Create** `src/sensors2mqtt/collector/local/ten64.py` — `Ten64Collector` (identity only).
- **Modify** `src/sensors2mqtt/collector/local/__init__.py` — `auto_detect` `"Traverse Ten64"` branch.
- **Modify** `tests/test_local_hwmon.py` — re-point the 3 generic-naming tests off the now-overridden ten64 chips; add `instance_channels` + ten64-naming unit tests.
- **Create** `tests/test_local_ten64.py` — collector integration (auto_detect, identity, suffix set, poll, skips), building the ten64 tree in `tmp_path`.

---

### Task 1: `instance_channels` engine extension

**Files:**
- Modify: `src/sensors2mqtt/collector/local/hwmon.py` (`DriverSpec`; the channel-spec lookup in `discover_hwmon_sensors`)
- Test: `tests/test_local_hwmon.py`

**Interfaces:**
- Consumes: #57's `DriverSpec`, `ChannelSpec`, `discover_hwmon_sensors`, and the `mk_hwmon`/`suffixes` test helpers.
- Produces: `DriverSpec.instance_channels: dict[str, dict[str, ChannelSpec]]` — per-instance (slugged device-basename) channel overrides, consulted before `channels`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_local_hwmon.py`)

```python
class TestInstanceChannels:
    def test_same_driver_two_instances_distinct_names(self, tmp_path, monkeypatch):
        from sensors2mqtt.collector.local import hwmon

        monkeypatch.setitem(hwmon.PERIPHERAL_HWMON, "fakemon", hwmon.DriverSpec(
            instance_channels={
                "0_0011": {"in0": hwmon.ChannelSpec(suffix="rail_a", name="Rail A")},
                "0_0022": {"in0": hwmon.ChannelSpec(suffix="rail_b", name="Rail B")},
            },
        ))
        mk_hwmon(tmp_path, 0, "fakemon", {"in0_input": "1000"}, device="0-0011")
        mk_hwmon(tmp_path, 1, "fakemon", {"in0_input": "2000"}, device="0-0022")
        out = suffixes(hwmon.discover_hwmon_sensors(str(tmp_path), set()))
        assert {"rail_a", "rail_b"} <= out

    def test_instance_channels_fall_through_to_channels(self, tmp_path, monkeypatch):
        from sensors2mqtt.collector.local import hwmon

        monkeypatch.setitem(hwmon.PERIPHERAL_HWMON, "fakemon2", hwmon.DriverSpec(
            channels={"temp1": hwmon.ChannelSpec(suffix="generic_temp")},
            instance_channels={"0_0099": {"temp1": hwmon.ChannelSpec(suffix="special_temp")}},
        ))
        mk_hwmon(tmp_path, 0, "fakemon2", {"temp1_input": "40000"}, device="0-0050")
        out = suffixes(hwmon.discover_hwmon_sensors(str(tmp_path), set()))
        assert "generic_temp" in out  # no instance match -> falls through to channels
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_local_hwmon.py::TestInstanceChannels -q`
Expected: FAIL — `DriverSpec.__init__() got an unexpected keyword argument 'instance_channels'`.

- [ ] **Step 3: Add the field + lookup** in `hwmon.py`

Add to `DriverSpec` (after `channels`):

```python
    instance_channels: dict[str, dict[str, ChannelSpec]] = field(default_factory=dict)
```

In `discover_hwmon_sensors`, replace the channel-spec lookup line
`cspec = spec.channels.get(chan, ChannelSpec())` with:

```python
            cspec = (
                spec.instance_channels.get(instance, {}).get(chan)
                or spec.channels.get(chan)
                or ChannelSpec()
            )
```

(`instance` is already computed earlier in the loop.)

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_local_hwmon.py::TestInstanceChannels -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/sensors2mqtt/collector/local/hwmon.py tests/test_local_hwmon.py
git add src/sensors2mqtt/collector/local/hwmon.py tests/test_local_hwmon.py
git commit -m "feat(local): per-instance hwmon channel overrides"
```

---

### Task 2: Ten64 registry entries + #57 test fix-ups

**Files:**
- Modify: `src/sensors2mqtt/collector/local/hwmon.py` (`PERIPHERAL_HWMON`)
- Test: `tests/test_local_hwmon.py`

**Interfaces:**
- Consumes: `DriverSpec`, `ChannelSpec` (with `diagnostic` from #57 and `instance_channels` from Task 1).
- Produces: ten64 channel names in the registry — `minipcie_p4_3v3`/`minipcie_p5_3v3`/`lte_m2b_3v3`/`rail_5v` (pac @0x11), `ddr_vdd_1v2`/`ddr_vpp_2v5`/`ddr_vtt_0v6`/`ovdd_1v8` (pac @0x1a), `supply_voltage`/`ls1088_die_temp`/`board_temp`/`emc1704_internal_temp` (emc1704), `emc1813_internal_temp`/`phy_eth0_3_temp`/`phy_eth4_7_temp` (emc1813), `fan_rpm` (emc2301).

- [ ] **Step 1: Re-point the 3 generic-naming tests** in `tests/test_local_hwmon.py`

These used ten64 chips as *generic* examples; #56 now overrides those chips. Change them to a neutral, un-overridden driver name, and assert the scale override via the new pac1934 suffix:

```python
    def test_unlabeled_temps_use_kind_index(self, tmp_path):
        # 'lm75' is unregistered -> pure generic naming from device basename.
        mk_hwmon(tmp_path, 0, "lm75", {"temp1_input": "40250", "temp2_input": "53375"},
                 device="0-0048")
        assert {"0_0048_temp1", "0_0048_temp2"} <= suffixes(
            discover_hwmon_sensors(str(tmp_path), set()))

    def test_fan_kind_metadata(self, tmp_path):
        mk_hwmon(tmp_path, 0, "genericfan", {"fan1_input": "4902"}, device="fan0")
        s = by_suffix(discover_hwmon_sensors(str(tmp_path), set()), "fan0_fan1")
        assert s.sensor.unit == "RPM" and s.sensor.icon == "mdi:fan"
        assert s.source.scale == 1.0 and s.source.precision == 0
```

And replace `TestScaleOverrides.test_pac1934_microvolt_scale` to assert scale on the now-named rail:

```python
class TestScaleOverrides:
    def test_pac1934_microvolt_scale(self, tmp_path):
        mk_hwmon(tmp_path, 0, "pac1934", {"in0_input": "3286680"}, device="0-0011")
        s = by_suffix(discover_hwmon_sensors(str(tmp_path), set()), "minipcie_p4_3v3")
        assert s.sensor.unit == "V"
        assert s.source.scale == 1e-6  # microvolts -> 3.29 V
```

- [ ] **Step 2: Add ten64-naming tests** in `tests/test_local_hwmon.py`

```python
class TestTen64Naming:
    def test_pac1934_two_chips_distinct_rails(self, tmp_path):
        mk_hwmon(tmp_path, 0, "pac1934",
                 {"in0_input": "3286680", "in1_input": "3281800",
                  "in2_input": "3294000", "in3_input": "4881952"}, device="0-0011")
        mk_hwmon(tmp_path, 1, "pac1934",
                 {"in0_input": "1199504", "in1_input": "2510272",
                  "in2_input": "603656", "in3_input": "1832440"}, device="0-001a")
        s = suffixes(discover_hwmon_sensors(str(tmp_path), set()))
        assert {"minipcie_p4_3v3", "minipcie_p5_3v3", "lte_m2b_3v3", "rail_5v"} <= s
        assert {"ddr_vdd_1v2", "ddr_vpp_2v5", "ddr_vtt_0v6", "ovdd_1v8"} <= s

    def test_emc1704_names_and_skips(self, tmp_path):
        mk_hwmon(tmp_path, 0, "emc1704",
                 {"in0_input": "12046900", "in1_input": "0",
                  "temp0_input": "42375", "temp1_input": "65125",
                  "temp2_input": "34000", "temp3_input": "-128000"}, device="0-0018")
        out = discover_hwmon_sensors(str(tmp_path), set())
        s = suffixes(out)
        assert {"supply_voltage", "ls1088_die_temp", "board_temp",
                "emc1704_internal_temp"} <= s
        assert "0_0018_in1" not in s and "0_0018_temp3" not in s  # skipped
        sv = by_suffix(out, "supply_voltage")
        assert sv.sensor.entity_category is None  # primary, not diagnostic
        assert sv.source.scale == 1e-6

    def test_emc1813_phy_temps(self, tmp_path):
        mk_hwmon(tmp_path, 0, "emc1813",
                 {"temp1_input": "40250", "temp2_input": "53375", "temp3_input": "43625"},
                 device="0-004c")
        s = suffixes(discover_hwmon_sensors(str(tmp_path), set()))
        assert {"emc1813_internal_temp", "phy_eth0_3_temp", "phy_eth4_7_temp"} <= s

    def test_emc2301_fan(self, tmp_path):
        mk_hwmon(tmp_path, 0, "emc2301", {"fan1_input": "4902"}, device="0-002f")
        out = discover_hwmon_sensors(str(tmp_path), set())
        f = by_suffix(out, "fan_rpm")
        assert f.sensor.unit == "RPM" and f.sensor.entity_category is None
```

- [ ] **Step 3: Run, verify the new tests fail**

Run: `uv run pytest tests/test_local_hwmon.py::TestTen64Naming -q`
Expected: FAIL — ten64 chips still produce generic names (no registry entries yet).

- [ ] **Step 4: Add/extend the registry entries** in `hwmon.py` `PERIPHERAL_HWMON`

```python
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
            "in0": ChannelSpec(suffix="supply_voltage", name="Supply Voltage (12V)", diagnostic=False),
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
```

(If #57 already added bare `pac1934`/`emc1704` scale-only entries, replace them with these expanded ones.)

- [ ] **Step 5: Run the full hwmon suite, verify pass**

Run: `uv run pytest tests/test_local_hwmon.py -q`
Expected: PASS (re-pointed generic tests + new ten64 tests all green).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/sensors2mqtt/collector/local/hwmon.py tests/test_local_hwmon.py
git add src/sensors2mqtt/collector/local/hwmon.py tests/test_local_hwmon.py
git commit -m "feat(local): name the Traverse Ten64 board sensors"
```

---

### Task 3: `Ten64Collector` + auto_detect + integration tests

**Files:**
- Create: `src/sensors2mqtt/collector/local/ten64.py`
- Modify: `src/sensors2mqtt/collector/local/__init__.py` (`auto_detect`)
- Test: `tests/test_local_ten64.py`

**Interfaces:**
- Consumes: `LocalCollector`; the ten64 registry names (Task 2).
- Produces: `Ten64Collector` (manufacturer `Traverse Technologies`, model `Ten64`); `auto_detect` returns it for the `Traverse Ten64` device-tree model.

- [ ] **Step 1: Write the failing tests** `tests/test_local_ten64.py`

```python
"""Tests for the Traverse Ten64 collector."""
import os
from pathlib import Path
from unittest.mock import patch

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.local import auto_detect
from sensors2mqtt.collector.local.ten64 import Ten64Collector


def build_ten64(root: Path):
    """Minimal ten64 fake sysfs: identity + the board chips (with device symlinks
    so the two pac1934 instances resolve)."""
    (root / "proc").mkdir(parents=True)
    (root / "proc/uptime").write_text("100.0 200.0\n")
    (root / "proc/meminfo").write_text("MemTotal: 1024 kB\nMemAvailable: 512 kB\n")
    (root / "proc/loadavg").write_text("0.1 0.2 0.3 1/10 100\n")
    dt = root / "proc/device-tree"
    dt.mkdir(parents=True)
    (dt / "model").write_text("Traverse Ten64\x00")
    eth0 = root / "sys/class/net/eth0"
    eth0.mkdir(parents=True)
    (eth0 / "address").write_text("70:b3:d5:1e:aa:bb\n")

    def chip(idx, name, channels, device):
        hw = root / "sys/class/hwmon" / f"hwmon{idx}"
        hw.mkdir(parents=True)
        (hw / "name").write_text(name + "\n")
        for f, v in channels.items():
            (hw / f).write_text(f"{v}\n")
        target = root / "sys/devices" / device
        target.mkdir(parents=True, exist_ok=True)
        os.symlink(target, hw / "device")

    chip(0, "pac1934", {"in0_input": "3286680", "in1_input": "3281800",
                        "in2_input": "3294000", "in3_input": "4881952"}, "0-0011")
    chip(1, "pac1934", {"in0_input": "1199504", "in1_input": "2510272",
                        "in2_input": "603656", "in3_input": "1832440"}, "0-001a")
    chip(2, "emc1704", {"in0_input": "12046900", "in1_input": "0",
                        "temp0_input": "42375", "temp1_input": "65125",
                        "temp2_input": "34000", "temp3_input": "-128000"}, "0-0018")
    chip(3, "emc1813", {"temp1_input": "40250", "temp2_input": "53375",
                        "temp3_input": "43625"}, "0-004c")
    chip(4, "emc2301", {"fan1_input": "5201"}, "0-002f")


def make(root):
    return Ten64Collector(
        config=MqttConfig(host="t", port=1883, user="u", password="p"),
        sysfs_root=str(root))


def test_auto_detect_ten64(tmp_path):
    build_ten64(tmp_path)
    assert auto_detect(sysfs_root=str(tmp_path)) is Ten64Collector


@patch("sensors2mqtt.base.socket.gethostname", return_value="ten64")
def test_identity(_m, tmp_path):
    build_ten64(tmp_path)
    c = make(tmp_path)
    assert c.device.manufacturer == "Traverse Technologies"
    assert c.device.model == "Ten64"


def test_full_suffix_set(tmp_path):
    build_ten64(tmp_path)
    s = {ls.sensor.suffix for ls in make(tmp_path)._sensors_list}
    assert {"minipcie_p4_3v3", "minipcie_p5_3v3", "lte_m2b_3v3", "rail_5v",
            "ddr_vdd_1v2", "ddr_vpp_2v5", "ddr_vtt_0v6", "ovdd_1v8",
            "supply_voltage", "ls1088_die_temp", "board_temp",
            "emc1813_internal_temp", "phy_eth0_3_temp", "phy_eth4_7_temp",
            "fan_rpm"} <= s
    assert "0_0018_temp3" not in s and "0_0018_in1" not in s  # dead channels skipped


def test_poll_values(tmp_path):
    build_ten64(tmp_path)
    v = make(tmp_path).poll()
    assert v["supply_voltage"] == 12.047        # 12046900 µV
    assert v["ddr_vtt_0v6"] == 0.604            # 603656 µV
    assert v["rail_5v"] == 4.882
    assert v["fan_rpm"] == 5201
    assert v["board_temp"] == 34.0
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_local_ten64.py -q`
Expected: FAIL — `ModuleNotFoundError: sensors2mqtt.collector.local.ten64`.

- [ ] **Step 3: Create `ten64.py`**

```python
"""Traverse Ten64 collector: device identity only.

All board sensors come from the generic hwmon engine in the base collector via
the ten64 registry overrides (pac1934/emc1704/emc1813/emc2301). This subclass
only supplies the manufacturer/model the engine cannot infer.
"""
from __future__ import annotations

import logging

from sensors2mqtt.collector.local.base import LocalCollector

log = logging.getLogger(__name__)


class Ten64Collector(LocalCollector):
    """Traverse Ten64 (NXP LS1088A): identity only; sensors via the base engine."""

    def _manufacturer(self) -> str:
        return "Traverse Technologies"

    def _model(self) -> str:
        return "Ten64"

    def _log_summary(self, values: dict) -> None:
        log.info(
            "Published: CPU=%s°C Board=%s°C 12V=%sV Fan=%sRPM",
            values.get("cpu_temp", "?"), values.get("board_temp", "?"),
            values.get("supply_voltage", "?"), values.get("fan_rpm", "?"),
        )
```

- [ ] **Step 4: Add the auto_detect branch** in `__init__.py`

In `auto_detect()`'s device-tree-model block (where `"Raspberry Pi"` is matched), add before/after that check, inside the same `model` scope:

```python
            if "Traverse Ten64" in model:
                from sensors2mqtt.collector.local.ten64 import Ten64Collector

                log.info("Auto-detected Traverse Ten64")
                return Ten64Collector
```

- [ ] **Step 5: Run ten64 tests + full suite, verify pass**

Run: `uv run pytest tests/test_local_ten64.py -q && uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/sensors2mqtt/collector/local/ten64.py src/sensors2mqtt/collector/local/__init__.py tests/test_local_ten64.py
git add src/sensors2mqtt/collector/local/ten64.py src/sensors2mqtt/collector/local/__init__.py tests/test_local_ten64.py
git commit -m "feat(local): Traverse Ten64 collector (identity + auto_detect)"
```

---

## Self-Review

**Spec coverage:** instance_channels extension (Task 1) ✓; all 8 pac rails + emc1704 (supply_voltage/ls1088_die/board_temp/internal) + emc1813 (3 PHY temps) + emc2301 fan named (Task 2) ✓; dead-channel skips emc1704 temp3/in1 (Task 2) ✓; `Ten64Collector` identity + auto_detect (Task 3) ✓; common suffixes non-diagnostic via `diagnostic=False` (Task 2) ✓; #57 generic-test fix-ups (Task 2) ✓.

**Dependency note:** relies on #57's `ChannelSpec.diagnostic` field (added in the #57 revision) and #57's `mk_hwmon`/`suffixes`/`by_suffix` test helpers + `discover_hwmon_sensors`. Execute after #57 merges; rebase this branch onto the merged engine first.

**Placeholder scan:** none — every step has concrete code, values, and commands.

**Type consistency:** `instance_channels: dict[str, dict[str, ChannelSpec]]` keyed by slugged device basenames (`"0_0011"`, `"0_001a"`) matches the engine's `instance` computation; `ChannelSpec(skip=…, diagnostic=…)` fields match #57's dataclass; poll values use the µV scale (÷1e6) consistently (`supply_voltage`/`ddr_vtt_0v6`/`rail_5v`).

**Out of scope:** SFP/transceiver channels (#41); pac1934 current/power (driver exposes only voltage today; #57's engine would handle them generically if they appear).
