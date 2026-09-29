# sensors2mqtt — LVM / RAID / Integrity / Filesystem Dashboard (big-storage)

**Status:** PLAN (not started). Tracks task #36.
**Sibling plan:** [`storage-device-dashboard-plan.md`](storage-device-dashboard-plan.md) (task #35) — the *physical* layer (drives + enclosures). This document is the *logical* layer (LVM hierarchy, dm-raid, dm-integrity, filesystems, allocation). Two collectors, shared infrastructure, one HA dashboard.

## Context

big-storage's logical storage is a deep device-mapper stack: LVM thin/cache on top of dm-raid6 on top of dm-integrity on top of linear PV mappings. During the 2026-04-29/30 incident, understanding the state of one volume meant manually cross-referencing `lvs -a -P`, `pvs --segments`, `vgs -P`, and `dmsetup ls --tree`. A correct mental model required knowing that one LV being *partial* (missing PV) silently blocked a **different** healthy LV in the same VG from auto-activating — the root cause of the `/space-new`/`/space-ml` mount failures.

This collector surfaces the whole logical stack — VG/LV/PV hierarchy, RAID/integrity health, filesystem state, and **allocation including unallocated capacity** — to Home Assistant so those conditions are visible instantly instead of reconstructed by hand.

### Why now / what changed

As of 2026-06-16 the logical layer is **healthy** (all VGs `0 missing PV`, all RAID/integrity `100%`), but the survey shows several states this dashboard is built to track, live right now:

- **`/space-ml` is 100 % full (0 bytes free)** — an ext4 on a 62.5 TiB RAID6+integrity LV (`space-2`). Invisible in any LVM-only view; only a filesystem-aware dashboard catches it.
- **`space-1` is now cache-accelerated** (an `lvmcache` `cdata`/`cmeta` pair, `space1cache_cvol`, a RAID1+integrity volume on two NVMe) and its **cache is still warming (92.28 % sync)** — a transient state worth showing.
- **`space-3` → `/backups`** is **new** since the last documented state (a third 62.5 TiB RAID6+integrity LV).
- **15 `tail-reserve-*` keepalive LVs** (`-wi------k`, 65.30 GiB each) occupy the tail extents of specific PVs — a deliberate allocation pattern a naive "free space" view would misread.

## Current logical reality (survey 2026-06-16)

Captured read-only via `tmp/bs_survey.py`. Ground truth the collector must parse.

### Volume groups
| VG | size | PVs | LVs | missing | notes |
|----|------|-----|-----|---------|-------|
| `space` | 3.7 TiB | 1 (`nvme2n1p1`) | 1 | 0 | `vm-docker-root` linear 400 G |
| `storage-big` | 263 TiB | 33 | 20 | 0 | the main pool |
| `storage-more` | 65 TiB | 6 | 1 | 0 | `space` RAID6+integrity → `/space` |

### storage-big logical volumes
| LV | type (lvs attr) | size | filesystem |
|----|------|------|-----------|
| `boot-debian` | RAID1 (`rwi-aor`) | 4 G | `/boot` |
| `root-debian` | RAID1 + integrity (`rwi-aor`, images `gwi`) | 3.65 T | `/` |
| `space-1` | **cache** (`Cwi-aoC`) → `_corig` RAID6+integrity (9 images) | 62.7 T | `/space-new` (cache 92.28 %) |
| `space-2` | RAID6 + integrity (9 images) | 62.7 T | `/space-ml` (**100 % full**) |
| `space-3` | RAID6 + integrity (9 images) | 62.7 T | `/backups` |
| `space1cache_cvol` | RAID1 + integrity (cache origin vol, on NVMe) | 3.2 T | (cache for space-1) |
| `tail-reserve-*` ×15 | linear, keepalive (`-wi------k`) | 65.3 G ea | (reserved tail extents) |

### Filesystems (findmnt)
| mount | LV | fs | used | notes |
|-------|----|----|------|-------|
| `/` | `storage-big/root-debian` | ext4 | 9 % | `errors=remount-ro,discard` |
| `/space-new` | `storage-big/space-1` | ext4 | 73 % | `noatime,stripe=112` |
| `/space-ml` | `storage-big/space-2` | ext4 | **100 %** | 0 avail |
| `/backups` | `storage-big/space-3` | ext4 | 31 % | |
| `/space` | `storage-more/space` | ext4 | 41 % | `stripe=512` |
| `/boot`, `/boot/efi` | `boot-debian`, `nvme1n1p1` | ext4/vfat | | |

`fstab` mounts the four big ext4 by `UUID=…` with `nofail,noatime` (the `nofail` is what lets boot proceed when a VG is partial — relevant to #31).

### The dm stack depth (why this is the hard one)
`dmsetup ls --tree` shows one user-facing volume expands to a 5-level tree. For `/space-new`:

```
space-1 (cache)
└─ space-1_corig (raid6)
   └─ rimage_0..8 (integrity)           ×9
      ├─ rimage_N_iorig (data)  ─┐
      └─ rimage_N_imeta (CRC)   ─┴→ linear → (major:minor) → physical SAS disk
```

~40 dm nodes back one filesystem. The **only** place the physical disk is named is the `(8:80)`/`(65:128)` major:minor leaf — so the "filesystem → physical disk" chain must resolve those minors via `/sys/dev/block/<maj>:<min>`. **This resolver is shared with #35** (factor into `collector/storage_common.py`).

## Work Process

Identical to the device plan: `mithro/sensors2mqtt` repo, dedicated worktree branch, frequent commits, code-review checkpoints on shared-code changes, test on big-storage with `--once`, deploy as a root systemd unit reading `/etc/sensors2mqtt/env`.

## Architecture

New collector module: **`sensors2mqtt.collector.storage_lvm`** → unit `sensors2mqtt-storage-lvm.service`.

- Standalone `main()` like `ipmi_sensors.py`; `node_id = "big_storage"`.
- **Generalizable to other storage hosts** (tweed/nvmeof/gpu) — unlike #35's SAS-enclosure code, the LVM layer is host-agnostic. Keep host-specific assumptions out; discover VGs/LVs/PVs dynamically.
- Runs as root (LVM reporting + `dmsetup status` need it).

### Data sources (all read-only, all root)
| Data | Source | Notes |
|------|--------|-------|
| VG summary | `vgs --reportformat json -o vg_name,vg_size,vg_free,pv_count,lv_count,vg_missing_pv_count,vg_attr` | **LVM emits JSON** — parse that, not columns |
| LV detail | `lvs -a --reportformat json -o lv_name,vg_name,lv_attr,lv_size,segtype,sync_percent,raid_mismatch_count,lv_health_status,raid_sync_action,cache_*,data_percent,metadata_percent` | |
| PV + allocation | `pvs --segments --reportformat json -o pv_name,vg_name,pv_size,pv_free,pv_used,pv_missing,seg_start_pe,seg_size_pe,lv_name,segtype` | per-segment allocation map |
| dm tree | `dmsetup ls --tree` + `dmsetup table` | structural graph |
| Integrity mismatches | `dmsetup status <…_imeta target>` / `lvs -o integrity_mismatches` | per-image CRC mismatch counts |
| Filesystems | `findmnt --json -t ext4,xfs,btrfs,vfat` + `statvfs()` per mount | size/used/avail/options |
| FS errors | `journalctl -k -b -g 'EXT4-fs error|I/O error|remount-ro' -o json` | health, since-boot |
| Map dm→disk | `/sys/dev/block/<maj>:<min>` | shared resolver (#35) |

## Data model & entities

Three logical object classes, each an HA child-device (`via_device` → big-storage):

### Per-VG
`size`, `free`, `used_percent`, `pv_count`, `lv_count`, `missing_pv_count`, `partial` (bool), `attr`. **Health rolls up:** any missing PV / partial LV / sync<100 / mismatch>0 in the VG ⇒ VG `warn`/`fail`.

### Per-LV (top-level only; internal `_rimage`/`_rmeta`/`_iorig`/`_imeta` rolled into their parent, not separate entities)
`type` (cache/raid6/raid1/linear), `has_integrity` (bool), `size`, `sync_percent`, `raid_mismatch_count`, `health_status`, `sync_action`, `cache_dirty_percent` (cache LVs), `image_count`, `images_healthy`/`images_total`, and (if mounted) the filesystem roll-up.

### Per-PV
`size`, `free`, `used`, `allocated_percent`, `missing` (bool), `is_fully_unallocated` (the "spare in the pool" flag), backing `/dev/…` and (via the resolver) physical disk + enclosure slot.

### Filesystem entities (per mount)
`fs_type`, `size`, `used_percent`, `avail`, `mount_options`, `read_only` (bool), `errors_since_boot` (count). **`/space-ml` at 100 % must alarm.**

### Roll-up header entities (VG-agnostic)
`total_capacity`, `total_unallocated` (system-wide free PE across all VGs — the #36 "unallocated capacity" requirement, not just per-VG), `partial_vg_count`, `degraded_lv_count`, `resyncing_lv_count`, `full_filesystem_count`.

> **Open decision (entity vs JSON):** as with #35, recommend promoting health/sync/used_percent to real entities (history + alerting) and publishing a per-VG JSON blob for the dm-tree/allocation visualization that a custom card renders. The full per-segment allocation map is JSON-only (too granular for entities).

## Problem / health detection (the point)

A single "Storage Problems" panel surfaces, in priority order:

1. **Partial VG** — `vg_missing_pv_count > 0` (and which LVs/images are impacted). *The 2026-04 failure.*
2. **Partial LV** — `lv_attr` position-9 `p` flag.
3. **Degraded / resyncing** — `sync_percent < 100` or `raid_sync_action != idle` (distinguish *rebuild* from *cache-warming* — `space-1`'s 92 % is benign cache fill, not a RAID6 rebuild).
4. **Integrity mismatch** — `raid_mismatch_count > 0` or non-zero `dmsetup status` mismatch on any `_imeta`.
5. **Filesystem full** — `used_percent ≥ 95` (`/space-ml` today).
6. **Wrong FS state** — mounted `ro` when `fstab` expects `rw`, or `errors_since_boot > 0`.
7. **Missing PV** — UUID, last-known `/dev`, impacted LVs/images (cross-linked to #35's slot view).

## Visual layout

- **Storage Problems** banner (the health roll-up; empty/green when all clear).
- **Filesystem capacity** — horizontal bars per mount (`/`, `/space-new`, `/space-ml`, `/backups`, `/space`), coloured by used %.
- **VG → LV hierarchy** — collapsible tree (custom card from per-VG JSON): VG → LVs with type/size/sync/health badges; expand an LV to its dm chain down to physical disks (the shared resolver makes this clickable through to #35's slot grid).
- **Allocation** — per-VG used vs free PE, with fully-unallocated PVs highlighted (the drop-in-spare pattern) and system-wide total unallocated.

## Implementation steps

1. **Scaffold** `collector/storage_lvm.py` (`--once`, `--log-level`) + `deploy/sensors2mqtt-storage-lvm.service`. Factor `collector/storage_common.py` (node_id, MQTT env, dm-minor→disk resolver) shared with #35.
2. **LVM JSON parsers** — `vgs`/`lvs`/`pvs --reportformat json` → typed structs. Unit-test against captured JSON fixtures (`packaging/capture-fixture.py`).
3. **lv_attr decoder** — map the 10-char attr to `{type, integrity, partial, health}` (cache `C`, raid `r`, integrity image `g`, etc.). Roll internal sub-LVs into parents.
4. **dm-tree resolver** — parse `dmsetup ls --tree`; resolve each top LV to its physical disk set via `/sys/dev/block`.
5. **Filesystem layer** — `findmnt --json` + `statvfs` + `fstab` expectation diff + journal error scan.
6. **Health roll-ups** — VG/LV/system roll-up logic per the detection rules above.
7. **Discovery + publish** — per-VG/LV/PV/FS child devices + roll-up entities + per-VG JSON; retained-config/non-retained-state conventions.
8. **Lovelace** — Problems banner, capacity bars, hierarchy tree, allocation view (shared HA dashboard with #35).
9. **Deploy** to big-storage; enable; verify. Then prove host-agnosticism with `--once` on a second storage host.

## HA MQTT discovery conventions

Same repo standard as #35: `name` = data point only; `unique_id` = `{node_id}_{suffix}` (e.g. `big_storage_lv_space_2_used_percent`); **discovery retained, state not retained, availability retained**; per-component discovery loops; `via_device` grouping.

## File changes summary

| File | Change |
|------|--------|
| `src/sensors2mqtt/collector/storage_lvm.py` | new collector |
| `src/sensors2mqtt/collector/storage_common.py` | shared helper (with #35): node_id, MQTT env, dm-minor→disk resolver |
| `deploy/sensors2mqtt-storage-lvm.service` | new root unit |
| `tests/test_storage_lvm.py` + `tests/fixtures/*.json` | LVM JSON + lv_attr + dm-tree parser tests |
| `docs/collectors.md` | document the collector |
| (HA) storage dashboard Lovelace YAML | problems/capacity/hierarchy/allocation views |

## Verification

- `uv run python -m sensors2mqtt.collector.storage_lvm --once --log-level DEBUG` on big-storage must report: 3 VGs / 0 missing; `space-1` as cache @ ~92 %; `space-2`→`/space-ml` at 100 % full (alarm); `space-3`→`/backups`; the 15 `tail-reserve` LVs not miscounted as free; correct dm→disk chains for each mount.
- Inject a fault safely (e.g. parse a **captured** partial-VG JSON fixture from the 2026-04 incident) to confirm the Problems banner fires for missing-PV/partial-LV — do **not** create real faults on the live array.
- `make test` + `make lint` green; shared `storage_common.py` change reviewed (affects #35).
- In HA: capacity bars correct vs `df`; hierarchy tree expands to physical disks; Problems banner green when healthy.

## Open decisions (resolve before coding discovery)

1. **Entity vs JSON granularity** — recommendation: promote health/sync/used_percent to entities; allocation map + dm-tree as JSON for a custom card.
2. **Internal sub-LV handling** — confirmed: roll `_rimage`/`_rmeta`/`_iorig`/`_imeta` into the parent LV; expose `images_healthy/total` not 40 entities per volume.
3. **Multi-host generalization** — design `node_id`/device identity now so the same collector runs on tweed/nvmeof later without per-host code.
4. **Cache-warming vs rebuild** — ensure the health logic treats `lvmcache` `data_percent`/cache-sync separately from RAID `sync_percent` so a warming cache never shows as "degraded array."
