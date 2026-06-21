# Generic hwmon Collector Design (#57)

**Goal:** Give every local-collector host automatic, host-agnostic harvesting
of all standard hwmon sensors (temperature, fan, voltage, current, power) from
a strong common base, and refactor the RPi and Mellanox specializations to lean
on that base.

**Architecture:** A new `collector/local/hwmon.py` engine discovers every chip
under `/sys/class/hwmon`, maps each `*_input` channel to a `SensorDef` +
`SysfsSource` using channel-type-default scaling with a per-driver override
table, and returns a list of `LocalSensor`. The base `LocalCollector` calls it
from `_probe_common_sensors()` so all subclasses inherit it. The hwmon-by-name
primitive moves into the shared layer. RPi/Mellanox specializations become
thin override sets layered on the engine.

**Tech stack:** Python 3.11+, stdlib only for the engine (pathlib/re/os),
`SensorDef`/`DeviceInfo` from `sensors2mqtt.discovery`, existing
`SysfsSource`/`LocalSensor` dataclasses, paho-mqtt v2 via `BasePublisher`.
Tests use the existing injectable `sysfs_root` fake-tree pattern.

## Global Constraints

- All Python run via `uv`. No new runtime dependencies (engine is stdlib).
- Sensor reads must work as the **unprivileged** service user (all observed
  hwmon `*_input` files are world-readable `r--r--r--`).
- HA discovery suffixes are **stable identifiers**; the refactor MUST preserve
  the suffixes the RPi and Mellanox collectors already publish, or it orphans
  HA entity history. New generic sensors get new stable suffixes.
- The generic probe reads only at construction time (probe), producing
  `SysfsSource` entries that the existing `base.poll()` reads — **no `poll()`
  override is needed** for generic sensors.
- Curation policy: **publish everything** (every discovered channel, including
  the −128 °C dead channel and the 56 empty module slots), all as
  `entity_category="diagnostic"`.
- `sysfs_root` injection must be honored throughout (no hard-coded `/`).

---

## Background

`LocalCollector._probe_common_sensors()` (`base.py:207`) calls only
`_probe_thermal_zones()` + `_probe_system_diagnostics()`; `_probe_hardware_sensors()`
(`base.py:212`) is an empty `pass`. The class docstring claims it probes "hwmon
drivers" — it does not. Direct hwmon reads exist only in `RpiCollector`
(`_find_hwmon_by_name`, `_probe_rp1_adc`, `_probe_rpi_volt`, `_probe_cooling_fan`)
and `MellanoxCollector` reads its chips indirectly via `sensors -j`.
`_find_hwmon_by_name` is duplicated between `rpi.py:254` and an inline copy in
`auto_detect()` (`__init__.py:42-54`).

### Empirical inventory (captured 2026-06-22)

**ten64** (Traverse Ten64, ARM64), 9 nodes:

| hwmon | name | device (bus) | channels |
|---|---|---|---|
| 0 | nvme | nvme0 (nvme) | temp1 (Composite) 39.85 °C |
| 1 | pac1934 | 0-0011 (i2c) | in0–in3 ≈ 3.29/3.28/3.29/4.88 V (raw µV) |
| 2 | core_cluster | thermal_zone0 (thermal) | temp1 67 °C |
| 3 | soc | thermal_zone1 (thermal) | temp1 67 °C |
| 4 | ath11k_hwmon | phy1 (ieee80211) | temp1 58 °C (WiFi) |
| 5 | pac1934 | 0-001a (i2c) | in0–in3 ≈ 1.20/2.51/0.60/1.83 V (raw µV) |
| 6 | emc2301 | 0-002f (i2c) | fan1 4902 RPM |
| 7 | emc1704 | 0-0018 (i2c) | in0 ≈ 12.05 V (raw µV), in1 0, temp0 42.4, temp1 65.1, temp2 34.0, temp3 −128 (dead) |
| 8 | emc1813 | 0-004c (i2c) | temp1 40.3, temp2 53.4, temp3 43.6 |

**big-storage** (x86_64), 52 nodes: `nvme`×8 (temp1 Composite, nvme7 also temp2
"Sensor 1"); `drivetemp`×44 (one temp1 each, SCSI `H:C:T:L` devices); `coretemp`×2
(temp1 "Package id N" + ~22 "Core N" each, sparse indices); `pch_lewisburg`
(thermal_zone0, temp1); one unnamed ACPI node (no channels).

**sw-bb-25g** (Mellanox SN2410), 4 nodes: `acpitz` (thermal_zone0, temp1+temp2);
`mlxsw` (PCI: fan1–fan8, temp1 ASIC, temp2–temp57 "front panel 001-056" all 0 =
empty/DAC); `jc42` (i2c, temp1 board); `coretemp` (temp1 "Package id 0" + 2 cores).

### Scaling truth (data-driven)

`tempN_input` is milli °C (÷1000) and `fanN_input` is RPM (×1) **universally**
across all three hosts. Voltage is **not** universal: `pac1934` and `emc1704`
report **microvolts** (÷1e6: `in0_input=12046900` → 12.05 V), while RPi's
`rpi_volt`/`rp1_adc` use the ABI's millivolts (÷1000). Therefore voltage (and by
extension current/power) require per-driver scale overrides; temp/fan do not.

---

## Design

### 1. Channel-type model and default scales

The engine recognises five hwmon channel kinds. Each kind has a default scale,
unit, and HA metadata, applied to every matching `<kind><idx>_input` file:

| kind | default scale | unit | device_class | state_class |
|---|---|---|---|---|
| `temp` | 0.001 | °C | temperature | measurement |
| `in` | 0.001 | V | voltage | measurement |
| `fan` | 1 | RPM | (none, `icon=mdi:fan`) | measurement |
| `curr` | 0.001 | A | current | measurement |
| `power` | 1e-6 | W | power | measurement |

Precision: temp 1, voltage 3, fan 0, current 3, power 2.

### 2. Per-driver override table

`PERIPHERAL_HWMON: dict[str, DriverSpec]` keyed by hwmon `name`. A `DriverSpec`
(frozen dataclass) may override, per driver and optionally per channel:

- `instance_id(hwmon_dir) -> str` — how to derive the stable instance token
  (default: `device` symlink basename).
- `scale` overrides per kind (e.g. `{"in": 1e-6}` for `pac1934`, `emc1704`).
- `channel_overrides: dict[str, ChannelSpec]` keyed by raw channel file name
  (e.g. `"temp1"`), supplying explicit `suffix`, `name`, `device_class`,
  `entity_category`, `icon`, or `skip=True`.
- `include: bool` (default True) — set False to let a specialization own the
  driver entirely.

**Metadata policy:** generic (un-overridden) channels default to
`entity_category="diagnostic"`. Override entries reproduce the **existing
`SensorDef` metadata exactly** (name, units, and a *non*-diagnostic category
where that is what the device publishes today) so that channels routed through
the engine during the RPi/Mellanox refactor are unchanged in HA.

A driver with **no** entry is probed with pure channel-type defaults and generic
naming (below). The override table is the single extension point shared by the
generic layer, the Mellanox/RPi refactors, and (later) #56's Ten64 rails.

### 3. Naming scheme (generic, un-overridden channels)

- `instance` = per-driver `instance_id` (default `device` basename), slugified.
- `channel` = slug(`<kind><idx>_label` value) if the label file exists, else
  `f"{kind}{idx}"`.
- `suffix` = `slug(f"{instance}_{channel}")`; `name` = title-cased human form.

Concrete generic results from the inventory:

| driver | instance source | example suffix(es) |
|---|---|---|
| nvme | device basename | `nvme0_composite`, `nvme7_sensor_1` |
| coretemp | `coretemp.N` → `coretempN` | `coretemp0_package_id_0`, `coretemp0_core_0` |
| drivetemp | block **serial** (`device/block/sdX/device/vpd_pg80`), fallback kernel name | `disk_<serial>_temp1` |
| ath11k_hwmon | override → `wifi` | `wifi_temp` |
| acpitz / pch_lewisburg | thermal-backed (see §5) | only secondary channels (e.g. `acpitz_temp2`) |

`drivetemp` instance MUST prefer a reboot-stable id (serial/WWN) over `sdX`,
because `sdX` reorders across reboots and would create duplicate HA entities on
the 44-disk host.

### 4. Shared primitive: `find_hwmon_by_name`

Move the implementation into `hwmon.py` as `find_hwmon_by_name(hwmon_root, name)`
and `iter_hwmon(hwmon_root)`. `base.py` exposes a thin
`self._find_hwmon_by_name(name)` delegating to it (so subclasses and #56
inherit the convenient method); `auto_detect()` and `RpiCollector` use the
shared function instead of their private copies.

### 5. Wiring, dedup, and the thermal-zone overlap

`_probe_common_sensors()` gains a third call,
`self._probe_peripheral_hwmon()`, which invokes
`hwmon.discover_hwmon_sensors(self._sysfs_root, taken_suffixes=...)` and extends
`self._sensors_list`. To avoid `base.py ↔ hwmon.py` circular import, `base.py`
imports `hwmon` with a **function-local import** inside the method; `hwmon.py`
imports `SysfsSource`/`LocalSensor` from `base` and `SensorDef` from `discovery`
at module top.

**Dedup-by-suffix:** the engine is given the set of already-registered suffixes
and skips any collision, so it never clobbers `cpu_temp` (thermal zone) or a
subclass/override sensor.

**Thermal-zone overlap:** several hwmon nodes are *backed by a thermal_zone*
(`device` bus = `thermal`): ten64 `core_cluster`/`soc`, big-storage
`pch_lewisburg`, sw-bb-25g `acpitz`. Their primary channel (`temp1`) is the same
reading the thermal-zone probe already registers (as `cpu_temp`, `soc_temp`,
`pch_lewisburg_temp`, `acpitz_temp`), but plain suffix-dedup won't catch it
(`core_cluster_temp1` ≠ `cpu_temp`). Rule: **for a thermal-backed hwmon node the
engine skips its primary `temp1` channel** (the zone temperature, already
registered) **and publishes any additional channels** (e.g. `acpitz` `temp2`)
generically. This avoids duplicating every thermal-zone temperature while still
honoring "everything" for the extra channels. (Heuristic: `temp1` of an
of_thermal/ACPI hwmon corresponds to its zone temp.)

Order in `_probe_common_sensors()`: thermal zones first (so their canonical
suffixes are taken), then peripheral hwmon (deduped against them), then
`_probe_system_diagnostics()`.

### 6. Mellanox refactor

`MellanoxCollector` drops `sensors -j` and its `SensorsJsonSource` entirely; its
sensors come from the generic engine via a `mlxsw`/`jc42` override entry that
**preserves the existing suffixes and names** (from the current
`MELLANOX_SENSORS` table):

| mlxsw channel | suffix (preserved) | name (preserved) |
|---|---|---|
| temp1 | `asic_temp` | ASIC Temperature |
| fan1 / fan2 … fan7 / fan8 | `fan1_rpm` … `fan8_rpm` | Fan 1 Front / Fan 1 Rear … Fan 4 Front / Fan 4 Rear |
| temp2 … temp57 | `sfp_port01_temp` … `sfp_port56_temp` | SFP Port NN Temperature |
| jc42 temp1 | `board_temp` | Board Temperature |

`cpu_temp` continues to come from the base thermal-zone probe. `temp2..temp57`
fold in #41's Mellanox SFP module temps (port N → `temp{N+1}`); they publish 0
until a DDM-capable optical module is inserted. `coretemp`/`acpitz` on the
switch publish via the generic path (acpitz `temp1` deduped per §5, `temp2`
generic; coretemp generic).

### 7. RPi refactor

`RpiCollector`'s hwmon reads move onto the engine via `rp1_adc`/`rpi_volt`/
`cooling_fan` overrides preserving suffixes and metadata (`rp1_v1..rp1_v4`,
`rp1_temp`, `supply_voltage`, `fan_rpm`). RPi **keeps** what is not a standard
`*_input` hwmon channel: the undervoltage alarm (`rpi_volt/in0_lcrit_alarm`, an
alarm flag, not `*_input`) and the entire `vcgencmd` throttle path (subprocess,
`VcgencmdSource`, `poll()` override). The `cooling_fan` lives at a non-standard
path (`/sys/devices/platform/cooling_fan/hwmon`); its override supplies that
explicit search location.

---

## Components / files

- **Create** `src/sensors2mqtt/collector/local/hwmon.py` — channel-kind table,
  `DriverSpec`/`ChannelSpec`, `PERIPHERAL_HWMON` registry, `find_hwmon_by_name`,
  `iter_hwmon`, `discover_hwmon_sensors(sysfs_root, taken_suffixes)`.
- **Modify** `base.py` — thin `_find_hwmon_by_name` delegate;
  `_probe_peripheral_hwmon()`; call it from `_probe_common_sensors()`; honest
  docstring.
- **Modify** `__init__.py` — `auto_detect` uses shared `find_hwmon_by_name`.
- **Modify** `rpi.py` — replace direct hwmon probes with override entries; keep
  vcgencmd + undervoltage alarm; drop the private `_find_hwmon_by_name`.
- **Modify** `mellanox.py` — delete `sensors -j` path; add `mlxsw`/`jc42`
  overrides; remove `poll()` override and `SensorsJsonSource`.

## Data flow

Construction → `_probe_common_sensors()` → thermal zones → peripheral hwmon
(engine builds `LocalSensor(SensorDef, SysfsSource)` per channel) → diagnostics.
`poll()` (unchanged base loop) reads every `SysfsSource`; computed values
(mem%) unchanged. Generic sensors need no poll-time special-casing.

## Error handling

- Unreadable/missing channel file → `_read_sysfs` returns `None` → omitted that
  cycle (existing behavior).
- A hwmon node that disappears between probe and poll → reads `None`; HA
  `expire_after` handles staleness (dynamic re-probe is out of scope; see #41
  residue).
- Sentinels (−128 °C dead channel; 0 module slots) are **published as-is** per
  the "everything" policy.

## Testing

TDD against fake `sysfs_root` trees built from the captured inventory:

- `hwmon.py` unit tests: a temp-only chip, a labeled multi-temp chip (nvme7),
  microvolt voltage with override (pac1934/emc1704), a fan chip (emc2301),
  multi-instance disambiguation (8× nvme, 44× drivetemp with serials), label vs
  no-label naming, thermal-backed primary-channel dedup (publishes `temp2+`),
  general dedup-by-suffix.
- Mellanox fixture from the real sw-bb-25g tree: asserts `asic_temp`,
  `fan1_rpm`..`fan8_rpm`, `board_temp`, `sfp_port01_temp`..`sfp_port56_temp`
  exist with the preserved suffixes/names and `sensors -j` is gone.
- RPi fixture: asserts `rp1_v1..rp1_v4`, `rp1_temp`, `supply_voltage`,
  `fan_rpm` preserved (suffix + metadata); vcgencmd + undervoltage still work.
- A ten64 fixture: asserts emc1704/emc1813 temps + emc2301 fan now appear
  generically; pac1934/emc1704 voltages publish generically with the µV-scale
  registry override (correct volts, generic names) — #56 later renames them to
  meaningful rails.

## Out of scope / follow-ups

- **#56** (sequenced next): Ten64 specialization — pac1934 ×2 + emc1704 12 V
  rails with meaningful names, and `Traverse Technologies / Ten64` device
  identity. Builds on this engine + `_find_hwmon_by_name`.
- **#41 residue:** ten64 dynamic hot-plug re-probe (sfp node appears on module
  insert), the richer SFP DDM fields (Vcc/bias/optical power = curr/power
  channels), and cage-name mapping on ten64.
- curr/power channels are supported by the engine but light up only when such
  hardware is present (SFP optics, future power monitors).

## Risks

- **HA continuity:** if a preserved suffix is mistyped in an override, the live
  RPi/Mellanox devices orphan entities. Tests pin every preserved suffix.
- **Disk id stability:** `drivetemp` must use a stable serial-based id; `sdX`
  fallback risks duplicate entities across reboots on the 44-disk host.
- **Blast radius:** one PR changes ten64, Mellanox, RPi, and big-storage live
  devices. Mitigated by `sysfs_root` fixtures replaying each host's real tree.
