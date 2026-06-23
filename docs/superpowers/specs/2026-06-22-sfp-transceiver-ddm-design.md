# SFP/Transceiver DDM Collector Design (#41)

**Goal:** Publish the full per-cage SFP/SFP+ DDM/DOM set — module temperature,
Vcc, TX optical power, RX optical power, laser bias — to Home Assistant on both
local-collector hosts with SFP cages (ten64 + sw-bb-25g), with graceful
hot-plug.

**Architecture:** A new `collector/local/sfp.py` transceiver-DDM probe with two
backends (ten64 `sfp`-driver hwmon; Mellanox `mlxsw` hwmon temp + privileged
`ethtool -m` for the rest), re-run **every poll** so cages that populate at
runtime appear without a restart. A small dynamic-sensor hook in `BasePublisher`
publishes HA discovery for SFP entities as they appear. Sibling of #40 (SNMP).

**Tech Stack:** Builds on #57's engine helpers (`find_hwmon_by_name`); stdlib +
`subprocess` (`ethtool -m`); `SensorDef`/discovery; pytest with fake-sysfs +
canned `ethtool` fixtures. No packaging change (the local collector already runs as root).

## Global Constraints

- Run all Python via `uv`. No new Python runtime deps (uses `ethtool`, already
  present; `mlxlink`/MFT explicitly NOT required).
- **Depends on #57** (the hwmon engine + the `mlxsw` registry entry, which #41
  edits to skip the generic per-port temps + reuses `find_hwmon_by_name`).
  Independent of #56. Executes after #57 merges; rebase onto it first.
- Privilege: the `sensors2mqtt-local` unit has no `User=`, so the collector runs
  as **root** today — `ethtool -m` (which needs `CAP_NET_ADMIN`) works as-is, so
  #41 needs no packaging/privilege change. The probe still degrades to temp-only
  if `ethtool -m` is ever denied (future non-root hardening). ten64's hwmon path
  is unprivileged regardless.
- SFP sensors are **dynamic**: re-probed every poll; populated cages only (skip
  empty cages and passive DACs, which carry no DDM).
- **No live validation possible yet** (no DDM-capable optical module is seated on
  either host) — build + unit-test against fixtures; the bias/power **scaling**
  is a documented live-validation item.

---

## Background

From the 2026-06-22 spike (see task #41), the two hosts expose DDM differently:

- **ten64** — the mainline `sfp` driver creates an hwmon node (`name="sfp"`)
  **only while a DDM-capable optical module is seated**, exposing the full set:
  `temp1_input`, `in1_input` (Vcc), `curr1_input` (bias), `power1_input` (TX),
  `power2_input` (RX). World-readable, so #41 reads DDM straight from the hwmon
  node (no privilege, no parsing). Two cages: `eth8`/`dpmac1_sfp`,
  `eth9`/`dpmac2_sfp` (both passive-DAC today). Node is dynamic → re-probe.
  **Correction (2026-06-23, verified on hardware):** an earlier spike note here
  claimed `fsl_dpaa2_eth` "has no EEPROM access / `ethtool -m` unusable" — that
  is **wrong**. `sudo ethtool -m eth9` returns the full SFF-8472 page-A0h decode
  (the cages are driven by phylink + the kernel `sfp` bus, which provides
  `get_module_eeprom` independent of the dpaa2 MAC driver). DDM still comes from
  the hwmon node (simpler + unprivileged); the working `ethtool -m` only matters
  for the static-identity follow-up, which can therefore cover ten64 too.
  Passive DACs carry only static A0h identity (vendor/PN/SN/length/type) — no
  A2h DDM, so #41 correctly reports nothing live for them.
- **sw-bb-25g** (Mellanox SN2410) — `mlxsw` hwmon exposes per-port module
  **temperature only** at `temp2_input`..`temp57_input` (port N → `temp{N+1}`;
  `temp{N}_crit != 0` flags a DDM module). The richer DDM needs `ethtool -m
  swpN` (CAP_NET_ADMIN) — `mlxlink`/MFT is not installed and is not required.

#57 already publishes the Mellanox per-port temps **generically** as
`mlxsw_front_panel_0NN`. #41 supersedes that with proper `sfp_portNN` naming +
the full DDM (see §5).

---

## Design

### 1. `collector/local/sfp.py` — the DDM probe

A pure, side-effect-free function per backend returning the *current* SFP
sensors as `list[tuple[SensorDef, value]]` (value already scaled):

```python
def probe_sfp_hwmon(sysfs_root: str) -> list[tuple[SensorDef, float]]:   # ten64
def probe_sfp_mlxsw(sysfs_root: str, ethtool=run_ethtool) -> list[...]:  # Mellanox
```

- **ten64 (`probe_sfp_hwmon`)**: for each `/sys/class/hwmon` node with
  `name=="sfp"`, resolve its cage from the `device` link (`dpmac1_sfp`→`cage1`,
  `dpmac2_sfp`→`cage2`) and read `temp1`/`in1`/`curr1`/`power1`/`power2`.
- **Mellanox (`probe_sfp_mlxsw`)**: find the `mlxsw` hwmon; for each port whose
  `temp{N+1}_crit != 0` (DDM module present), read `temp{N+1}_input` for temp
  and parse `ethtool -m swp{N:02d}` for Vcc/TX/RX/bias. `ethtool` is injected
  (`ethtool=` param) so tests pass canned output.

### 2. Fields, suffixes, units

Per populated cage/port, up to five sensors. All `entity_category="diagnostic"`
(per-port optics telemetry), `state_class="measurement"`:

| field | suffix (ten64 / Mellanox) | unit | device_class |
|---|---|---|---|
| module temp | `sfp_cage{N}_temp` / `sfp_port{NN}_temp` | °C | temperature |
| Vcc | `sfp_cage{N}_vcc` / `sfp_port{NN}_vcc` | V | voltage |
| laser bias | `sfp_cage{N}_bias` / `sfp_port{NN}_bias` | mA | current |
| TX power | `sfp_cage{N}_tx_power` / `sfp_port{NN}_tx_power` | dBm | (none) |
| RX power | `sfp_cage{N}_rx_power` / `sfp_port{NN}_rx_power` | dBm | (none) |

Optical power is reported in **dBm** (optics convention): the hwmon backend
computes `dBm = 10*log10(power_µW / 1000)` with a floor (e.g. `≤ 0 µW → -40 dBm`);
the ethtool backend reads ethtool's `dBm` figure directly. **Scaling of bias
(curr1 µA vs mA) and power must be confirmed against a live module** — the
mainline `sfp` driver has reported bias in µA in some versions; the probe
normalises to mA/dBm and this is the key live-validation check.

### 3. Dynamic sensors + discovery hook

SFP entities appear/disappear at runtime, so they cannot use the static
`_sensors_list`. Add a `BasePublisher` hook:

```python
def dynamic_sensors(self) -> list[tuple[SensorDef, value]]:
    return []   # default: none
```

The publish cycle: call `dynamic_sensors()` each poll; for any suffix not seen
before, publish its HA discovery config (retained); then include all dynamic
values in the published state. A removed module simply stops being returned and
goes stale via `expire_after`. The base `LocalCollector.dynamic_sensors()` runs
the hwmon backend (host-agnostic — any host with a `sfp`-driver node, ten64
included); `MellanoxCollector` overrides it for the mlxsw + `ethtool -m` backend.
Placing the hwmon backend in the base keeps #41 dependent only on #57 (not on
#56's `Ten64Collector`), so #56 and #41 can run in parallel after #57.

### 4. Privilege

The Mellanox `ethtool -m` path needs `CAP_NET_ADMIN`. The `sensors2mqtt-local`
systemd unit has no `User=`, so the collector runs as **root** — it already has
`CAP_NET_ADMIN`, and full Mellanox DDM works with **no packaging change**. ten64
needs nothing (hwmon is world-readable). If `ethtool -m` ever fails (denied or
unsupported), the probe logs once and yields temp-only for that port — never
crashes the poll. Hardening the collector to a non-root user (which would then
need `AmbientCapabilities=CAP_NET_ADMIN` on the unit) is a separate, fleet-wide
task tracked outside #41.

### 5. Superseding #57's generic Mellanox temps

#41 edits the `mlxsw` registry entry (from #57) to mark `temp2`..`temp57` as
`ChannelSpec(skip=True)`, so the generic engine stops emitting
`mlxsw_front_panel_0NN`; the per-port temps are then published by the SFP probe
as `sfp_port{NN}_temp` (alongside the rest of the DDM). When #41 lands, the
`mlxsw_front_panel_0NN` entities are replaced by `sfp_port` ones.

---

## Components / files

- **Create** `src/sensors2mqtt/collector/local/sfp.py` — `probe_sfp_hwmon`,
  `probe_sfp_mlxsw`, `run_ethtool`, an `ethtool -m` DDM parser, dBm helper.
- **Modify** `src/sensors2mqtt/base.py` — `dynamic_sensors()` hook + publish-loop
  integration (discovery-on-first-sight + state).
- **Modify** `src/sensors2mqtt/collector/local/base.py` (`LocalCollector`, not the
  `BasePublisher` above) — override `dynamic_sensors()` → `probe_sfp_hwmon`
  (host-agnostic; runs on every local collector).
- **Modify** `src/sensors2mqtt/collector/local/mellanox.py` — override
  `dynamic_sensors()` → `probe_sfp_mlxsw`; `_mac_interfaces` unchanged.
- **Modify** `src/sensors2mqtt/collector/local/hwmon.py` — `mlxsw` entry:
  `temp2`..`temp57` → `skip=True`.
- *(No packaging change — the collector already runs as root; non-root hardening
  + `AmbientCapabilities` is a separate fleet-wide task.)*
- **Create** `tests/test_local_sfp.py`; SFP fixtures (synthetic `sfp` hwmon tree;
  canned `ethtool -m` text).

## Data flow

Each poll: base `poll()` (static sensors) → `dynamic_sensors()` (SFP probe,
fresh) → publish discovery for newly-seen SFP suffixes → publish combined state.
ten64 reads sysfs only; Mellanox reads mlxsw sysfs + shells `ethtool -m` per
populated port.

## Error handling

- No `sfp` node / no populated port → `dynamic_sensors()` returns `[]` (nothing
  published); never errors.
- `ethtool -m` failure (permission/unsupported) → log once, yield temp-only for
  that port.
- Optical power `≤ 0` (dark) → floored dBm, not `-inf`.
- A module removed mid-run → dropped from the next poll; HA `expire_after`.

## Testing

- `probe_sfp_hwmon`: synthetic `sfp` hwmon node(s) in `tmp_path` (with `device`
  symlink → `dpmac1_sfp`), assert `sfp_cage1_temp/vcc/bias/tx_power/rx_power`
  and the dBm/zero-floor math.
- `probe_sfp_mlxsw`: `mlxsw` fixture + an injected `ethtool` returning canned
  SFF-8472 text for one port; assert `sfp_port01_*`, and that a non-DDM port
  (`temp_crit == 0`) is skipped; assert ethtool-failure → temp-only.
- Dynamic discovery: a fake publisher records published configs; assert a config
  is published the first time a cage appears and not re-published while present;
  state includes the dynamic values.
- `hwmon.py`: `mlxsw` `temp2..temp57` now skipped (no `mlxsw_front_panel_*`).

## Out of scope / follow-ups

- Live calibration of bias/power scaling (needs a seated optical module).
- #40 (SNMP-side SFP on the Netgear switches) — sibling, separate.
- `mlxlink`/MFT richer vendor telemetry — explicitly not pursued.

## Risks

- **Scaling uncertainty:** bias (µA vs mA) and power decode are unverifiable
  without a live module; isolated in the probe + flagged for live validation.
- **Runs as root today:** #41 relies on the collector's existing root to call
  `ethtool -m`; the temp-only fallback bounds the blast radius if that ever
  changes. Long-term, hardening to non-root + `AmbientCapabilities=CAP_NET_ADMIN`
  is cleaner (separate task).
- **Dynamic-discovery churn:** mis-tracking "seen" suffixes could re-publish
  configs each poll — tests pin the publish-once behavior.
- **ethtool output format:** parser keys off ethtool's DDM line labels, which
  can vary by version — parser tolerates missing fields (yields what it finds).
