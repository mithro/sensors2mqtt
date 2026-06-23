# SFP/Transceiver DDM Collector Implementation Plan (#41)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the full per-cage SFP/SFP+ DDM (temp/Vcc/bias/TX/RX power) to HA on ten64 + sw-bb-25g, re-probed each poll for hot-plug.

**Architecture:** A new `collector/local/sfp.py` with two probe backends; a `dynamic_sensors()` hook in `BasePublisher` that publishes HA discovery for SFP entities as cages populate; the base `LocalCollector` runs the hwmon backend (host-agnostic), `MellanoxCollector` overrides with the mlxsw+ethtool backend.

**Tech Stack:** Python 3.11+, stdlib + `subprocess` (`ethtool -m`); `SensorDef`/discovery; `find_hwmon_by_name`/`iter_hwmon` from #57; pytest with fake-sysfs + injected-ethtool fixtures.

## Global Constraints

- Run all Python via `uv`. No new runtime deps (`ethtool` already present; `mlxlink`/MFT not required).
- **Depends on #57** (engine helpers + the `mlxsw` registry entry it edits). Independent of #56. Execute after #57 merges; rebase first.
- SFP sensors are **dynamic** (re-probed every poll); populated cages only (`sfp` node present / mlxsw `temp{N}_crit != 0`).
- Collector runs as **root** today, so `ethtool -m` works with **no packaging change**; the probe degrades to temp-only if ethtool ever fails.
- Optical power in **dBm** (floored at -40 for dark); temp °C, Vcc V, bias mA. All SFP sensors `entity_category="diagnostic"`, `state_class="measurement"`.
- **No live module seated** → build + unit-test against fixtures; bias/power scaling is a flagged live-validation item.

---

## File Structure

- **Modify** `src/sensors2mqtt/base.py` — `dynamic_sensors()` hook + `_poll_once` integration + `_dynamic_discovered` set.
- **Create** `src/sensors2mqtt/collector/local/sfp.py` — `probe_sfp_hwmon`, `probe_sfp_mlxsw`, `run_ethtool`, `parse_ethtool_ddm`, `_dbm`.
- **Modify** `src/sensors2mqtt/collector/local/base.py` — `LocalCollector.dynamic_sensors()` → `probe_sfp_hwmon`.
- **Modify** `src/sensors2mqtt/collector/local/mellanox.py` — `MellanoxCollector.dynamic_sensors()` → `probe_sfp_mlxsw`.
- **Modify** `src/sensors2mqtt/collector/local/hwmon.py` — `mlxsw` entry: `temp2`..`temp57` → `skip=True`.
- **Test:** `tests/test_base.py` (hook), `tests/test_local_sfp.py` (both backends).

---

### Task 1: `dynamic_sensors()` hook in BasePublisher

**Files:**
- Modify: `src/sensors2mqtt/base.py` (`__init__` line 169-172; `_poll_once` line 237-257)
- Test: `tests/test_base.py`

**Interfaces:**
- Produces: `BasePublisher.dynamic_sensors(self) -> list[tuple[SensorDef, object]]` (default `[]`); publish loop publishes discovery for each dynamic suffix once, merges values into state.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_base.py`)

```python
from sensors2mqtt.base import BasePublisher, MqttConfig
from sensors2mqtt.discovery import DeviceInfo, SensorDef


class _FakePub(BasePublisher):
    def __init__(self, dyn):
        super().__init__(MqttConfig(host="t", port=1883, user="u", password="p"))
        self._dyn = dyn

    @property
    def sensors(self): return []
    @property
    def device(self): return DeviceInfo(node_id="n", name="n", manufacturer="m", model="x")
    @property
    def module(self): return "local"
    def poll(self): return {"static": 1}
    def dynamic_sensors(self): return list(self._dyn)


def _states(client):
    return [m for m in client.published if m["topic"].endswith("/state")]


def test_dynamic_sensor_value_in_state(mock_mqtt_client):
    sd = SensorDef("sfp_cage1_temp", "SFP Cage 1 Temp", "°C", device_class="temperature")
    _FakePub([(sd, 35.0)])._poll_once(mock_mqtt_client)
    assert '"sfp_cage1_temp": 35.0' in _states(mock_mqtt_client)[-1]["payload"]


def test_dynamic_discovery_published_once(mock_mqtt_client):
    sd = SensorDef("sfp_cage1_temp", "SFP Cage 1 Temp", "°C", device_class="temperature")
    p = _FakePub([(sd, 35.0)])
    p._poll_once(mock_mqtt_client)
    p._poll_once(mock_mqtt_client)
    cfgs = [m for m in mock_mqtt_client.published if m["topic"].endswith("sfp_cage1_temp/config")]
    assert len(cfgs) == 1  # published on first sight only


def test_no_dynamic_sensors_unchanged(mock_mqtt_client):
    _FakePub([])._poll_once(mock_mqtt_client)
    assert '"static": 1' in _states(mock_mqtt_client)[-1]["payload"]
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_base.py -q -k dynamic`
Expected: FAIL — `_FakePub` can't be instantiated (`dynamic_sensors` not abstract but `_poll_once` doesn't call it) / no discovery for the dynamic suffix.

- [ ] **Step 3: Add the hook + integration** in `base.py`

In `__init__`, after `self._discovery_published = False`:

```python
        self._dynamic_discovered: set[str] = set()
```

Add the method (near the abstract methods):

```python
    def dynamic_sensors(self) -> list[tuple[SensorDef, object]]:
        """Sensors discovered at runtime, re-probed each poll. Override to add.

        Returns (SensorDef, value) pairs; discovery is published the first time a
        suffix appears, values are merged into the published state each cycle.
        """
        return []
```

Replace `_poll_once` body with:

```python
    def _poll_once(self, client: mqtt.Client) -> None:
        """Execute one poll cycle."""
        log.info("Polling sensors")
        values = self.poll()
        dynamic = self.dynamic_sensors()

        if values is None and not dynamic:
            client.publish(self.avail_topic, "offline", retain=True)
            log.warning("No sensor data")
            return

        values = dict(values or {})
        for sensor_def, value in dynamic:
            values[sensor_def.suffix] = value

        if not self._discovery_published:
            count = publish_discovery(
                client, self.sensors, self.device, self.state_topic, self.avail_topic,
            )
            self._discovery_published = True
            log.info("Published MQTT discovery for %d sensors", count)

        new_dynamic = [sd for sd, _ in dynamic if sd.suffix not in self._dynamic_discovered]
        if new_dynamic:
            publish_discovery(
                client, new_dynamic, self.device, self.state_topic, self.avail_topic,
            )
            self._dynamic_discovered.update(sd.suffix for sd in new_dynamic)
            log.info("Published discovery for %d new dynamic sensor(s)", len(new_dynamic))

        publish_state(client, self.state_topic, values)
        client.publish(self.avail_topic, "online", retain=True)
        self._log_summary(values)
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_base.py -q`
Expected: PASS (new + existing base tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/sensors2mqtt/base.py tests/test_base.py
git add src/sensors2mqtt/base.py tests/test_base.py
git commit -m "feat(base): dynamic_sensors() hook with discovery-on-first-sight"
```

---

### Task 2: SFP hwmon backend (`probe_sfp_hwmon`) + base wire-in

**Files:**
- Create: `src/sensors2mqtt/collector/local/sfp.py`
- Modify: `src/sensors2mqtt/collector/local/base.py` (`LocalCollector`)
- Test: `tests/test_local_sfp.py`

**Interfaces:**
- Consumes: `iter_hwmon` from #57's `hwmon.py`; `SensorDef`.
- Produces: `probe_sfp_hwmon(sysfs_root: str) -> list[tuple[SensorDef, float]]`; `_dbm(microwatts: float) -> float`.

- [ ] **Step 1: Write the failing tests** `tests/test_local_sfp.py`

```python
"""Tests for the SFP/transceiver DDM probe."""
import os
from pathlib import Path

from sensors2mqtt.collector.local.sfp import probe_sfp_hwmon, _dbm


def mk_sfp(root: Path, idx: int, cage_dev: str, channels: dict):
    hw = root / "sys/class/hwmon" / f"hwmon{idx}"
    hw.mkdir(parents=True)
    (hw / "name").write_text("sfp\n")
    for f, v in channels.items():
        (hw / f).write_text(f"{v}\n")
    target = root / "sys/devices/platform" / cage_dev
    target.mkdir(parents=True, exist_ok=True)
    os.symlink(target, hw / "device")


def suffixes(pairs):
    return {sd.suffix: val for sd, val in pairs}


def test_dbm_math():
    assert _dbm(1000) == 0.0       # 1 mW -> 0 dBm
    assert _dbm(500) == -3.01      # 0.5 mW
    assert _dbm(0) == -40.0        # dark -> floor


def test_full_ddm_one_cage(tmp_path):
    mk_sfp(tmp_path, 0, "dpmac1_sfp", {
        "temp1_input": "35000", "in1_input": "3300",
        "curr1_input": "6000", "power1_input": "501", "power2_input": "398",
    })
    s = suffixes(probe_sfp_hwmon(str(tmp_path)))
    assert s["sfp_cage1_temp"] == 35.0
    assert s["sfp_cage1_vcc"] == 3.3
    assert "sfp_cage1_bias" in s
    assert "sfp_cage1_tx_power" in s and "sfp_cage1_rx_power" in s


def test_two_cages_distinct(tmp_path):
    mk_sfp(tmp_path, 0, "dpmac1_sfp", {"temp1_input": "35000"})
    mk_sfp(tmp_path, 1, "dpmac2_sfp", {"temp1_input": "40000"})
    s = suffixes(probe_sfp_hwmon(str(tmp_path)))
    assert s["sfp_cage1_temp"] == 35.0 and s["sfp_cage2_temp"] == 40.0


def test_no_sfp_node(tmp_path):
    (tmp_path / "sys/class/hwmon").mkdir(parents=True)
    assert probe_sfp_hwmon(str(tmp_path)) == []
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_local_sfp.py -q`
Expected: FAIL — `ModuleNotFoundError: sensors2mqtt.collector.local.sfp`.

- [ ] **Step 3: Create `sfp.py`** (hwmon backend + helpers)

```python
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
    return f"cage{m.group(1)}" if m else (re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "sfp")


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
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_local_sfp.py -q`
Expected: PASS.

- [ ] **Step 5: Wire into the base collector** — in `collector/local/base.py`, add to `LocalCollector`:

```python
    def dynamic_sensors(self) -> list:
        """Per-poll SFP/transceiver DDM (host-agnostic sfp-driver hwmon)."""
        from sensors2mqtt.collector.local.sfp import probe_sfp_hwmon

        return probe_sfp_hwmon(str(self._sysfs_root))
```

- [ ] **Step 6: Full suite + lint + commit**

```bash
uv run pytest -q
uv run ruff check src/sensors2mqtt/collector/local/sfp.py src/sensors2mqtt/collector/local/base.py tests/test_local_sfp.py
git add src/sensors2mqtt/collector/local/sfp.py src/sensors2mqtt/collector/local/base.py tests/test_local_sfp.py
git commit -m "feat(local): SFP DDM hwmon backend (full DDM, host-agnostic)"
```

---

### Task 3: Mellanox SFP backend (`probe_sfp_mlxsw`) + wire-in + supersede #57 temps

**Files:**
- Modify: `src/sensors2mqtt/collector/local/sfp.py` (add mlxsw backend + ethtool)
- Modify: `src/sensors2mqtt/collector/local/mellanox.py`
- Modify: `src/sensors2mqtt/collector/local/hwmon.py` (`mlxsw` `temp2..temp57` → skip)
- Test: `tests/test_local_sfp.py`, `tests/test_local_mellanox.py`, `tests/test_local_hwmon.py`

**Interfaces:**
- Consumes: `find_hwmon_by_name` from #57; the dynamic hook (Task 1).
- Produces: `probe_sfp_mlxsw(sysfs_root, ethtool=run_ethtool)`, `run_ethtool(iface)`, `parse_ethtool_ddm(text) -> dict[str, float]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_local_sfp.py`)

```python
from sensors2mqtt.collector.local.sfp import probe_sfp_mlxsw, parse_ethtool_ddm

ETHTOOL_SAMPLE = """\
\tModule temperature                        : 35.00 degrees C / 95.00 degrees F
\tModule voltage                            : 3.3000 V
\tLaser bias current                        : 6.000 mA
\tLaser output power                        : 0.5012 mW / -2.99 dBm
\tReceiver signal average optical power      : 0.4000 mW / -3.98 dBm
"""


def mk_mlxsw(root: Path, ports_with_module: dict[int, str]):
    """ports_with_module: {port -> temp_milli_c}; those get crit!=0 (DDM present)."""
    hw = root / "sys/class/hwmon/hwmon1"
    hw.mkdir(parents=True)
    (hw / "name").write_text("mlxsw\n")
    for n in range(2, 58):
        port = n - 1
        present = port in ports_with_module
        (hw / f"temp{n}_input").write_text(f"{ports_with_module.get(port, 0)}\n")
        (hw / f"temp{n}_crit").write_text(("90000" if present else "0") + "\n")


def test_parse_ethtool_ddm():
    d = parse_ethtool_ddm(ETHTOOL_SAMPLE)
    assert d["temp"] == 35.0 and d["vcc"] == 3.3
    assert d["bias"] == 6.0
    assert d["tx_power"] == -2.99 and d["rx_power"] == -3.98


def test_mlxsw_populated_port_full_ddm(tmp_path):
    mk_mlxsw(tmp_path, {1: 36000})
    s = suffixes(probe_sfp_mlxsw(str(tmp_path), ethtool=lambda iface: ETHTOOL_SAMPLE))
    assert s["sfp_port01_temp"] == 36.0
    assert s["sfp_port01_vcc"] == 3.3 and s["sfp_port01_tx_power"] == -2.99


def test_mlxsw_empty_port_skipped(tmp_path):
    mk_mlxsw(tmp_path, {})  # no DDM modules (all crit=0)
    assert probe_sfp_mlxsw(str(tmp_path), ethtool=lambda iface: "") == []


def test_mlxsw_ethtool_failure_temp_only(tmp_path):
    mk_mlxsw(tmp_path, {1: 36000})
    s = suffixes(probe_sfp_mlxsw(str(tmp_path), ethtool=lambda iface: ""))
    assert s["sfp_port01_temp"] == 36.0
    assert "sfp_port01_vcc" not in s  # ethtool gave nothing -> temp only
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_local_sfp.py -q -k mlxsw`
Expected: FAIL — `probe_sfp_mlxsw`/`parse_ethtool_ddm` not defined.

- [ ] **Step 3: Add the mlxsw backend** to `sfp.py`

```python
import subprocess

from sensors2mqtt.collector.local.hwmon import find_hwmon_by_name

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
        r = subprocess.run(["ethtool", "-m", iface], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            log.warning("ethtool -m %s failed (rc=%d): %s", iface, r.returncode, r.stderr.strip())
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
                out.append((_sfp_sensor(prefix, "temp", f"SFP Port {port:02d} Temperature",
                                        "°C", "temperature"), round(float(traw) * 0.001, 1)))
            except ValueError:
                pass
        ddm = parse_ethtool_ddm(ethtool(f"swp{port:02d}"))
        for field, unit, dclass in (("vcc", "V", "voltage"), ("bias", "mA", "current"),
                                    ("tx_power", "dBm", None), ("rx_power", "dBm", None)):
            if field in ddm:
                name = f"SFP Port {port:02d} {field.replace('_', ' ').upper()}"
                out.append((_sfp_sensor(prefix, field, name, unit, dclass), ddm[field]))
    return out
```

- [ ] **Step 4: Run sfp tests, verify pass**

Run: `uv run pytest tests/test_local_sfp.py -q`
Expected: PASS.

- [ ] **Step 5: Wire into MellanoxCollector** — in `mellanox.py`:

```python
    def dynamic_sensors(self) -> list:
        """Per-poll SFP DDM: mlxsw temp + privileged ethtool -m for the rest."""
        from sensors2mqtt.collector.local.sfp import probe_sfp_mlxsw

        return probe_sfp_mlxsw(str(self._sysfs_root))
```

- [ ] **Step 6: Supersede #57's generic per-port temps** — in `hwmon.py`, change the `mlxsw` `_mlxsw_channels()` so `temp2`..`temp57` are skipped (they're now owned by the SFP probe). Replace the function body's channel set to keep `temp1`/`fan1-8` and add skips:

```python
    for n in range(2, 58):  # per-port module temps owned by the SFP probe (#41)
        chans[f"temp{n}"] = ChannelSpec(skip=True)
    return chans
```

(Add this loop before `return chans`. The mlxsw `instance_id` and asic/fan entries from #57 are unchanged.)

- [ ] **Step 7: Update the Mellanox front-panel test** — in `tests/test_local_mellanox.py`, replace `test_front_panel_module_temps_generic` (which asserted `mlxsw_front_panel_001`) with:

```python
    def test_front_panel_temps_not_generic(self):
        # Per-port module temps are now owned by the SFP probe (#41), not the engine.
        s = {ls.sensor.suffix for ls in make_mellanox()._sensors_list}
        assert not any(x.startswith("mlxsw_front_panel_") for x in s)
```

And update `test_poll_reads_from_sysfs` to drop the `mlxsw_front_panel_001` assertion.

- [ ] **Step 8: Full suite + lint + commit**

```bash
uv run pytest -q
uv run ruff check src/sensors2mqtt/collector/local/sfp.py src/sensors2mqtt/collector/local/mellanox.py src/sensors2mqtt/collector/local/hwmon.py tests/
git add src/sensors2mqtt/collector/local/sfp.py src/sensors2mqtt/collector/local/mellanox.py src/sensors2mqtt/collector/local/hwmon.py tests/test_local_sfp.py tests/test_local_mellanox.py
git commit -m "feat(local): Mellanox SFP DDM (mlxsw temp + ethtool); supersede generic front-panel temps"
```

---

## Self-Review

**Spec coverage:** dynamic hook + discovery-on-first-sight (Task 1) ✓; ten64 full-DDM hwmon backend, host-agnostic in the base (Task 2) ✓; Mellanox temp + ethtool DDM with graceful temp-only fallback (Task 3) ✓; supersede #57's `mlxsw_front_panel` temps with `sfp_portNN` (Task 3) ✓; dBm + zero-floor (Task 2) ✓; populated-cages-only (`sfp` node / `temp_crit != 0`) ✓; diagnostic + measurement metadata ✓.

**No packaging task:** collector runs as root → `ethtool -m` works; non-root hardening + `AmbientCapabilities` is a separate tracked task.

**Placeholder scan:** none — concrete code, values, commands. Bias/optical-power scaling is implemented with a flagged live-validation calibration (not a TBD).

**Type consistency:** `dynamic_sensors() -> list[tuple[SensorDef, value]]` used identically in `base.py`, `LocalCollector`, `MellanoxCollector`, and tests; `probe_sfp_hwmon`/`probe_sfp_mlxsw` signatures match their call sites; `_sfp_sensor` builds the `sfp_{cage|portNN}_{field}` suffixes consistently.

**Out of scope:** live calibration of bias/power (needs a seated optical module); #40 (SNMP-side SFP); non-root + `AmbientCapabilities` hardening (separate task).
