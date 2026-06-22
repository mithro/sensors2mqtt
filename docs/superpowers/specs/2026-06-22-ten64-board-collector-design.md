# Ten64 Board-Sensor Collector Design (#56)

**Goal:** Give the Traverse Ten64 a proper device identity and meaningful names
for its onboard power-monitor and board-temperature channels, layered on the
generic hwmon engine from #57.

**Architecture:** A thin `Ten64Collector` (identity only) plus per-driver naming
overrides in #57's `PERIPHERAL_HWMON` registry. The generic engine already reads
and publishes every ten64 chip after #57 — #56 only renames the channels and
sets `Traverse Technologies / Ten64`. The two PAC1934 chips share a driver name
but monitor different rails, so the engine gains a small `instance_channels`
(per-i2c-address) override mechanism.

**Tech Stack:** Builds on #57's `collector/local/hwmon.py` engine; `SensorDef`
from `discovery`; pytest with a fake `sysfs_root` tree. No new runtime deps.

## Global Constraints

- Run all Python via `uv`. No new runtime dependencies.
- **Depends on #57** (the generic hwmon engine + registry). #56 executes after
  #57 merges; its worktree branches from the updated `main`.
- Preserve #57 behavior for all non-ten64 hosts: the new registry entries are
  keyed to ten64-only chips (`pac1934`/`emc1704`/`emc1813`/`emc2301`) and the
  two PAC1934 i2c addresses (`0_0011`, `0_001a`), so nothing else changes.
- Use the fleet's **common suffixes** where a channel is the logical owner:
  `supply_voltage` (board input), `board_temp` (board temperature), `fan_rpm`.
- `cpu_temp`/`soc_temp` remain the #57 thermal-zone sensors; do not remap them.
- Voltage scale for `pac1934`/`emc1704` is µV (÷1e6) — already set in #57.

---

## Background

ten64 (device-tree model `Traverse Ten64`, NXP LS1088A) matches no
`auto_detect()` branch, so it runs the generic `LocalCollector`. After #57 it
publishes every onboard channel, but with auto-generated suffixes
(`0_0018_temp0`, `0_0011_in0`, `0_002f_fan1`, …) and `manufacturer/model =
Unknown/Unknown`. The chips are exposed by the out-of-tree `traverse-sensors`
drivers (Microchip `pac1934`/`emc1704`/`emc1813`/`emc2301`).

### Confirmed channel map (sources: [Ten64 Sensors](https://ten64doc.traverse.com.au/hardware/sensors/), [Ten64 I2C](https://ten64doc.traverse.com.au/hardware/i2c/), `traverse-sensors` `emc181x.c`)

Cross-checked against the live values captured on ten64 2026-06-22.

| chip @i2c | channel | measured | suffix | name | unit |
|---|---|---|---|---|---|
| pac1934 @0x11 (U41) | in0 | 3.29 V | `minipcie_p4_3v3` | miniPCIe P4 3.3V | V |
| | in1 | 3.28 V | `minipcie_p5_3v3` | miniPCIe P5 3.3V | V |
| | in2 | 3.29 V | `lte_m2b_3v3` | LTE/M.2B 3.3V | V |
| | in3 | 4.88 V | `rail_5v` | 5V Rail | V |
| pac1934 @0x1a (U20) | in0 | 1.20 V | `ddr_vdd_1v2` | DDR VDD (1.2V) | V |
| | in1 | 2.51 V | `ddr_vpp_2v5` | DDR VPP (2.5V) | V |
| | in2 | 0.60 V | `ddr_vtt_0v6` | DDR VTT (0.6V) | V |
| | in3 | 1.83 V | `ovdd_1v8` | 1.8V (OVDD) | V |
| emc1704 @0x18 (U19) | in0 | 12.05 V | `supply_voltage` | Supply Voltage (12V) | V |
| | in1 | 0 V | *skip* (unused 2nd input) | — | — |
| | temp0 | 42 °C | `emc1704_internal_temp` | EMC1704 Internal Temp | °C |
| | temp1 | 65 °C | `ls1088_die_temp` | LS1088 Die Temperature | °C |
| | temp2 | 34 °C | `board_temp` | Board Temperature | °C |
| | temp3 | −128 °C | *skip* (unused 4th diode) | — | — |
| emc1813 @0x4c (U27) | temp1 | 40 °C | `emc1813_internal_temp` | PHY Monitor Internal Temp | °C |
| | temp2 | 53 °C | `phy_eth0_3_temp` | PHY Temp (eth0-eth3) | °C |
| | temp3 | 44 °C | `phy_eth4_7_temp` | PHY Temp (eth4-eth7) | °C |
| emc2301 @0x2f (U59) | fan1 | 5201 RPM | `fan_rpm` | Fan Speed | RPM |

EMC1704 temp index → meaning is from the driver: channel 0 = internal (reg
0x60), 1 = external1 (LS1088 die), 2 = external2 (board diode near NVMe), 3 =
unused. `supply_voltage`, `board_temp`, `fan_rpm` use the fleet's common
suffixes; `cpu_temp`/`soc_temp` come from #57's thermal-zone probe.

---

## Design

### 1. Engine extension: per-instance channel overrides

`DriverSpec` gains one optional field:

```python
instance_channels: dict[str, dict[str, ChannelSpec]] = field(default_factory=dict)
```

keyed by instance id (the slugged device basename, e.g. `"0_0011"`). In
`discover_hwmon_sensors`, the channel-spec lookup becomes:

```python
cspec = (spec.instance_channels.get(instance, {}).get(chan)
         or spec.channels.get(chan)
         or ChannelSpec())
```

This lets the two PAC1934 chips (same driver `name`, different i2c addresses)
get different rail names. i2c addresses are fixed, so `0_0011`/`0_001a` are
stable instance keys.

### 2. Registry entries (added/extended in `PERIPHERAL_HWMON`)

- **pac1934** — extend the existing `DriverSpec(scale={"in": 1e-6})` with
  `instance_channels` for `0_0011` (in0-3 → miniPCIe P4/P5 3.3V, LTE/M.2B 3.3V,
  5V Rail) and `0_001a` (in0-3 → DDR VDD/VPP/VTT, 1.8V OVDD).
- **emc1704** — extend the existing `DriverSpec(scale={"in": 1e-6})` with
  `channels`: `in0`→`supply_voltage`, `in1`→`skip`, `temp0`→`emc1704_internal_temp`,
  `temp1`→`ls1088_die_temp`, `temp2`→`board_temp`, `temp3`→`skip`.
- **emc1813** — new `DriverSpec(channels=...)`: `temp1`→`emc1813_internal_temp`,
  `temp2`→`phy_eth0_3_temp`, `temp3`→`phy_eth4_7_temp`.
- **emc2301** — new `DriverSpec(channels={"fan1": ChannelSpec(suffix="fan_rpm",
  name="Fan Speed")})`.

`supply_voltage`/`board_temp`/`fan_rpm` are not produced by ten64's thermal-zone
probe, so dedup-by-suffix leaves them free. Voltage/temp metadata (device_class,
state_class) comes from the engine's channel-kind defaults; these stay
`entity_category="diagnostic"` like all generic hwmon sensors.

### 3. Skipped channels

emc1704 `temp3` (−128 °C, the unused 4th diode) and `in1` (0 V, unused second
voltage input) are `ChannelSpec(skip=True)` — a deliberate, ten64-specific
refinement of #57's global "everything", because the `traverse-sensors` driver
documents these as physically unconnected. They publish briefly from #57, then
disappear when #56 lands.

### 4. `Ten64Collector` (`collector/local/ten64.py`)

A thin subclass, identical in spirit to the refactored `MellanoxCollector` —
identity only, all sensors via the base engine:

```python
class Ten64Collector(LocalCollector):
    def _manufacturer(self) -> str:
        return "Traverse Technologies"

    def _model(self) -> str:
        return "Ten64"
```

(`_mac_interfaces` defaults to `("eth0",)`, which ten64 has.)

### 5. auto_detect hook

In `auto_detect()`'s device-tree-model check (alongside the `Raspberry Pi`
branch), add: model contains `"Traverse Ten64"` → import and return
`Ten64Collector`.

---

## Components / files

- **Modify** `src/sensors2mqtt/collector/local/hwmon.py` — add `instance_channels`
  to `DriverSpec`; the channel-spec lookup; pac1934/emc1704 channel maps;
  emc1813/emc2301 entries.
- **Create** `src/sensors2mqtt/collector/local/ten64.py` — `Ten64Collector`.
- **Modify** `src/sensors2mqtt/collector/local/__init__.py` — `auto_detect`
  `"Traverse Ten64"` branch.
- **Create** `tests/test_local_ten64.py`; **create** `tests/fixtures/ten64_sysfs/`.
- **Modify** `tests/test_local_hwmon.py` — re-point the three generic-naming
  tests (currently using emc1813/emc2301/pac1934 as generic examples) at a
  fixture chip #56 does not override, and add the ten64 override assertions.

## Data flow

Unchanged from #57: `auto_detect` → `Ten64Collector` → base
`_probe_common_sensors` → engine reads ten64 chips, now with override names →
`poll()` reads the `SysfsSource`s. No `poll()` override needed.

## Error handling

Inherited from #57: missing/unreadable channel → omitted that cycle. Skipped
channels never register. A chip at an unexpected i2c address falls back to
generic naming (no `instance_channels` match) rather than failing.

## Testing

- `tests/fixtures/ten64_sysfs/` — fake tree with the two pac1934 (device
  symlinks `0-0011`/`0-001a`), emc1704, emc1813, emc2301 hwmon nodes (real
  captured values), the `core_cluster`/`soc` thermal zones, nvme + ath11k, and
  `proc` + `eth0/address` so identity/diagnostics work.
- `test_local_ten64.py`: `auto_detect` returns `Ten64Collector` for the
  `Traverse Ten64` model; identity is `Traverse Technologies / Ten64`; the
  full suffix set is present (all 8 pac rails, `supply_voltage`, `board_temp`,
  `ls1088_die_temp`, `emc1704_internal_temp`, the 3 emc1813 temps, `fan_rpm`);
  the two pac1934 chips get distinct rail names (instance_channels works);
  `0_0018_temp3` / `0_0018_in1` are absent (skipped); `poll()` values scale
  correctly (e.g. `supply_voltage ≈ 12.05`, `ddr_vtt_0v6 ≈ 0.60`, `fan_rpm
  == 5201`).
- `test_local_hwmon.py` updates: the generic-naming examples move to a neutral
  fixture chip; add a `instance_channels` unit test (two same-named chips at
  different device basenames → different suffixes).

## Out of scope / follow-ups

- #41 residue on ten64: SFP cage DDM (the `sfp` hwmon node appears on optical
  module insertion) + dynamic hot-plug re-probe — not a board chip.
- PAC1934 current/power: the driver currently exposes only bus voltage; if
  current/power channels appear, #57's engine handles them generically.

## Risks

- **Cross-task test churn:** #56 changes registry behavior for chips #57 tested
  as generic; the three affected `test_local_hwmon.py` tests are updated here.
  Mitigated by re-pointing them at a neutral fixture chip.
- **Instance-key stability:** relies on the pac1934 i2c addresses staying
  `0x11`/`0x1a` (fixed in hardware) — safe for this board.
