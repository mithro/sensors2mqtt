# sensors2mqtt — Storage Device + Enclosure Dashboard (big-storage)

**Status:** PLAN (not started). Tracks task #35.
**Sibling plan:** [`storage-lvm-dashboard-plan.md`](storage-lvm-dashboard-plan.md) (task #36) — the *logical* layer (LVM/RAID/integrity/filesystem). This document is the *physical* layer (drives + SAS enclosures + NVMe). They are two separate collectors that share infrastructure and a Home Assistant dashboard view.

## Context

big-storage (Supermicro X11DSC+, Debian 13, kernel 6.12.73) carries ~62 drives across two 30-bay SAS enclosures plus 8 NVMe. During the 2026-04-29/30 missing-drive incident, identifying which *physical slot* held a failed/missing drive required manual cross-referencing of `/sys/class/enclosure/`, `/sys/class/sas_device/`, and `/sys/class/sas_phy/`. A dashboard would have shown the failure at a glance and would catch SMART degradation before it causes a production outage.

This collector publishes the physical storage layer to Home Assistant via the existing sensors2mqtt MQTT auto-discovery framework. It is the device-layer complement to the LVM-layer collector (#36).

### Why now / what changed

The space-1 missing-PV incident is **resolved** — as of 2026-06-16 all three VGs report **0 missing PVs**. But the survey that confirmed this also surfaced exactly the conditions this dashboard exists to make visible, all present *right now*:

- **3 phantom slots** — SES status `OK` but **no enumerated block device**: enclosure `0:0:29:0` Slot10 & Slot12, enclosure `0:0:57:0` Slot24. (This is the precise failure mode that hid the missing drive in 2026-04.)
- **`/dev/sda`** (enclosure `0:0:29:0` Slot01) reports **0 B / "device is NOT READY (spun down)"** and belongs to **no VG** — an orphan/suspect drive that is invisible in any logical-layer view.
- **16 SAS phys "Unknown"** + **4 "Phy disabled"** out of 110 (90 negotiate 12.0 Gbit).
- **4 empty bays** reported `not installed`: `0:0:29:0` Slot28; `0:0:57:0` Slot07, Slot15, Slot26.

These become the dashboard's acceptance test cases: a correct implementation must visually distinguish *occupied+healthy*, *empty (not installed)*, *phantom (SES-OK, no device)*, and *present-but-not-ready (spun down)*.

## Current big-storage hardware reality (survey 2026-06-16)

Captured read-only via `tmp/bs_survey.py`. This is the ground truth the collector must parse.

### HBA & topology
- **1× LSI/Broadcom SAS3008** (`mpt3sas`) at PCI `3b:00.0`. Single SAS domain, `host0`.
- **2× SES enclosures** (30 bays each, 60 total):
  | sysfs enclosure id | topology | bays | role |
  |--------------------|----------|------|------|
  | `0:0:29:0` | `expander-0:0` (direct) | 30 | primary |
  | `0:0:57:0` | `expander-0:0 → expander-0:1` (cascaded) | 30 | secondary |
- **110 SAS phys**: 90 @ `12.0 Gbit`, 16 `Unknown`, 4 `Phy disabled`.
- `lsscsi` is **NOT installed** — do **not** depend on it; sysfs has everything.

### Drives
- **~53 SAS/SATA** behind the expanders (`/dev/sda`…`/dev/sdba`): a heterogeneous mix — Seagate ST10000NM0226 (10 TB SAS), HGST HUH721010 (10 TB), Seagate OOS12000G / ST12000NM0008 (12 TB), Toshiba MG07ACA12TE (12 TB), plus legacy 2/3 TB SATA and several SATA SSDs (Samsung 850 PRO, Crucial M500, Intel, Micron) bridged through the SAS expander (STP).
- **8× NVMe** (`nvme0`…`nvme7`) on PCIe directly (not behind the HBA): 4× Intel SSDPE2KX040T7 (4 TB), Intel 670p (2 TB), Phison, Kingston SNVS2000G, Crucial P2.

### Enclosure slot sysfs (the #35 primary data source)
Per slot: `/sys/class/enclosure/<encl>/Slot<NN>/`
- `status` — `OK` / `not installed` / (fault states)
- `slot` — backplane slot index (0-based; the dir name `SlotNN` is 1-based)
- `fault`, `locate` — LED state ints
- `type` — `array device`
- `device/block/<dev>` — the linked block device, **absent for phantom/empty slots**

## Work Process

- **Repository:** `mithro/sensors2mqtt` (this repo, checked out at `~/github/mithro/sensors2mqtt`).
- Work on a **dedicated branch in a git worktree**, not `main`.
- **Frequent small commits**; push after each. Keep the plan + a task list in-branch.
- **Code-review checkpoint** after the collector's first end-to-end publish, and again after any change to `base.py`/`discovery.py` (shared code).
- **Test on big-storage** — the only host with the SAS enclosures. `--once` mode (below) is the iteration loop; never leave a half-built service enabled.
- Deploy mirrors the existing `sensors2mqtt-ipmi-sensors` collector: shipped as a `sensors2mqtt-<name>` Debian package (unit runs `/usr/bin/python3 -m sensors2mqtt.collector.<name>`), unit reads `/etc/sensors2mqtt/env`, **runs as root** (smartctl/SES sysfs need it).

## Architecture

New collector module: **`sensors2mqtt.collector.storage_devices`** → systemd unit `sensors2mqtt-storage-devices.service`.

- Model on `collector/ipmi_sensors.py` (standalone `main()` + `discovery.py` helpers), **not** `LocalCollector` — the per-drive/per-slot data is richer than the scalar sysfs sensors `LocalCollector` is built for, and the **per-PSU multi-component discovery pattern** in `ipmi_sensors.publish_psu_discovery()` is the exact template: one HA child-device + per-component state topic per unit.
- `node_id = "big_storage"` (same as the IPMI collector) so all big-storage entities share one parent HA device; per-drive and per-enclosure entities attach as **child devices via `via_device`**.
- Runs as root. Poll interval ~60 s for SES/topology (cheap), with SMART on a slower cadence (see "Polling cadence").

### Data sources (all read-only, all root)
| Layer | Source | Notes |
|-------|--------|-------|
| Slot presence / status / LEDs | `/sys/class/enclosure/<encl>/Slot*/` | primary; no tool needed |
| Slot → block device | `…/Slot*/device/block/` | absent ⇒ phantom or empty |
| Drive id (model/serial/rev) | `/sys/block/<d>/device/{model,vendor,rev}` + `smartctl -i` | sysfs first, smartctl for serial/firmware |
| Capacity | `/sys/block/<d>/size` (×512) | |
| Transport (SAS/SATA) | `/sys/block/<d>/device/.../` + `lsblk -o TRAN` | SATA behind SAS = STP |
| Link rate | `/sys/class/sas_phy/<phy>/negotiated_linkrate` | per-phy |
| SAS address / phy id | `/sys/class/sas_device/<dev>/sas_address`, `phy_identifier` | |
| Spin state | `smartctl -n standby -i` exit code / "NOT READY" text | avoids spinning up idle drives |
| I/O load (IOPS, MB/s) | `/proc/diskstats` (delta between polls) | compute rate in-collector |
| SMART (SAS) | `smartctl -A -l error -l background <d>` | SCSI log pages; grown-defect list |
| SMART (SATA) | `smartctl -A <d>` | reallocated/pending/UDMA-CRC attrs |
| SMART (NVMe) | `smartctl -a /dev/nvmeN` / `nvme smart-log` | temp, % used, avail spare, media errors, unsafe shutdowns |

## Per-slot / per-drive data model

Published as one retained JSON blob per slot (`sensors2mqtt/big_storage/dev/<encl>_<slot>/state`) **plus** a small set of first-class HA sensor entities per occupied drive (for history + alerting). The JSON drives the visual grid card; the entities drive graphs/automations.

### Per-slot JSON fields
`enclosure`, `slot`, `presence` (`occupied`/`empty`/`phantom`), `status` (SES), `fault`, `locate`, `block_dev`, `model`, `serial`, `firmware`, `capacity_bytes`, `transport`, `link_rate`, `sas_address`, `spin_state`, `temperature_c`, `power_on_hours`, `health` (`ok`/`warn`/`fail`), plus per-class error counters.

### First-class HA entities per occupied drive (recommended minimum)
- `…_temp` (temperature, °C)
- `…_health` (ok/warn/fail — drives the dashboard colour)
- `…_power_on_hours` (diagnostic)
- `…_reallocated` / `…_grown_defects` (SAS) or `…_reallocated` + `…_pending` + `…_udma_crc` (SATA)
- `…_spin_state`
- NVMe: `…_percentage_used`, `…_available_spare`, `…_media_errors`, `…_unsafe_shutdowns`

> **Open decision (entity granularity):** ~53 drives × ~6 entities ≈ 320 entities + NVMe. That is fine for HA but noisy. Alternative: publish *only* the per-slot JSON + a tiny set of roll-up entities (counts of warn/fail drives, hottest drive, phantom-slot count) and render everything else from the retained JSON in a custom card. **Recommendation:** do both — JSON for the grid, but promote only `temp`, `health`, and the key error counter to real entities (so history/alerting exist) rather than the full set. Decide before implementing discovery.

### Enclosure-level entities (one HA device per enclosure)
- `phantom_slot_count`, `empty_slot_count`, `occupied_slot_count`
- `phys_12g`, `phys_unknown`, `phys_disabled`
- backplane/expander temp (already exposed by the IPMI collector as `BPN-*`/`Expander*` — link, don't duplicate)

## Detection logic (the point of the dashboard)

- **Phantom slot:** SES `status == OK` **and** no `device/block/` child. Surface as a distinct `presence=phantom` with a loud colour. *(Live examples: `0:0:29:0` Slot10/Slot12, `0:0:57:0` Slot24.)*
- **Spun-down / not-ready:** drive present but `smartctl -n standby` reports standby, or `-i` says "NOT READY". Distinct from healthy-active. *(Live example: `/dev/sda`.)*
- **Phy health:** count phys at 12 G vs `Unknown`/`disabled`; flag any drive whose backing phy negotiated below 12 G (a marginal cable/backplane).
- **Orphan drive:** occupied + healthy + **no LVM PV signature** and not a known system disk → "present but unused" (cross-checked against #36's PV list). *(Live example: `/dev/sda`.)*
- **SMART degradation:** non-zero grown-defect list (SAS) / reallocated/pending (SATA) / media errors (NVMe), or temp over threshold.

## Visual layout

Two physical-correct 6×5 grids (one per enclosure), each cell = one bay, rendered from the retained per-slot JSON via a custom Lovelace card (`custom:button-card` template grid, or `auto-entities` + a templated `markdown` card). Cell colour encodes `presence`/`health`; tap shows the drive's model/serial/temp/errors. A separate compact panel lists the 8 NVMe with `% used` / spare / temp. A roll-up header shows: total/occupied/phantom/empty bays, warn/fail counts, hottest drive.

## Implementation steps

1. **Scaffold** `collector/storage_devices.py` with `--once` and `--log-level`, mirroring `ipmi_sensors.main()`. Add `deploy/sensors2mqtt-storage-devices.service`.
2. **Enclosure walker** — enumerate `/sys/class/enclosure/*`, build the slot table (presence classification incl. phantom). Unit-test against a captured sysfs fixture (use `packaging/capture-fixture.py`).
3. **Drive identity + topology** — resolve slot→block-dev→sas_device→phy; pull model/serial/fw/capacity/transport/link-rate from sysfs.
4. **SMART layer** — SAS (`-A -l error -l background`), SATA (`-A`), NVMe (`-a`) parsers → temp/POH/error counters/health. Respect standby (don't spin up).
5. **diskstats rate** — delta-based IOPS/MB-s per device across polls.
6. **Discovery + publish** — per-enclosure device + per-drive child device via `via_device`; per-slot JSON state topics; roll-up entities. Follow the discovery conventions below.
7. **Polling cadence** — fast loop (60 s) for SES/presence/link/diskstats; slow loop (e.g. 15 min) for SMART; never spin up standby drives on the fast loop.
8. **Lovelace card** — build the two-grid view; add to the HA storage dashboard (shared with #36).
9. **Deploy** to big-storage via the Debian package (`apt install sensors2mqtt-<name>`); enable the unit; verify in HA.

## HA MQTT discovery conventions (per repo standard)

- Entity `name` = data point only ("Slot 12 Temperature"); HA prepends the device name (`has_entity_name`).
- `unique_id` = `{node_id}_{suffix}` (e.g. `big_storage_enc0_slot12_temp`); topic `object_id` equals it.
- **Discovery configs retained; state topics NOT retained; availability retained.** (Per the port-monitoring plan — current code over-retains; do not copy that bug.)
- Per-component (per-drive/per-enclosure) discovery loops emit a fixed `SensorDef` set per unit — the `publish_psu_discovery()` template.
- `via_device` chains drive → enclosure → big-storage so HA groups them.

## File changes summary

| File | Change |
|------|--------|
| `src/sensors2mqtt/collector/storage_devices.py` | new collector |
| `deploy/sensors2mqtt-storage-devices.service` | new unit (root, `python3 -m`, `EnvironmentFile=-/etc/sensors2mqtt/env`) |
| `tests/test_storage_devices.py` + `tests/fixtures/` | sysfs/smartctl fixtures + parser tests |
| `docs/collectors.md` | document the new collector |
| (HA) storage dashboard Lovelace YAML | enclosure grids + NVMe panel |

## Verification

- `uv run python -m sensors2mqtt.collector.storage_devices --once --log-level DEBUG` on big-storage: must classify all 60 bays, flag the 3 known phantom slots, the 4 empty bays, and `/dev/sda` as spun-down/orphan.
- `make test` + `make lint` green; existing collectors unaffected (shared-code regression).
- In HA: both enclosure grids render; tapping a known drive shows correct model/serial/temp; phantom slots visibly distinct.
- Confirm the SMART slow-loop does **not** spin up idle drives (watch `smartctl -n standby` exit codes / drive `power/runtime_status`).

## Open decisions (resolve before coding discovery)

1. **Entity granularity** — full per-field entities vs JSON-blob-+-roll-ups (recommendation above: promote temp/health/key-error only).
2. **One HA device per drive vs per enclosure-with-attributes** — per-drive gives clean history but 50+ devices; acceptable, recommended.
3. **Identity key for a drive that moves slots** — key first-class entities by **serial** (stable) not slot (positional), so history follows the disk; the slot is an attribute.
4. **Shared module with #36** — both collectors need `node_id`, MQTT env, and a "resolve dm-minor → /dev name" helper; factor a small `collector/storage_common.py` rather than duplicating.
