# Storage collectors: drives, enclosures, LVM/RAID/filesystems

**Status:** implemented on branch `storage-collectors` (2026-09-30).
Supersedes the open decisions in [`storage-device-dashboard-plan.md`](../../storage-device-dashboard-plan.md)
(#35) and [`storage-lvm-dashboard-plan.md`](../../storage-lvm-dashboard-plan.md) (#36).

## Decisions (agreed with Tim, 2026-09-29)

1. **Every lifetime counter is its own Home Assistant entity**, with a
   `state_class`, so HA's long-term statistics keep its history. No
   JSON-only blobs.
2. **A drive is identified by its serial number**, never by host, slot or
   `/dev` name. Each drive is one HA device, `node_id = disk_<serial>`, so a
   disk that moves to another slot or host keeps its device and history. Where
   it is now (host, `/dev` name, enclosure slot) is published as entities of
   the drive, and the drive's `via_device` is the host that has it.
3. **smartd is the only process that sends SMART / log commands to drives.**
   The collectors never run `smartctl`, `hdparm`, `nvme` or read
   `drivetemp`: SMART data comes from smartd's `--jsonstate` files
   (`/var/lib/smartmontools/smartd-json.*.json`, from the
   [mithro/smartmontools](https://github.com/mithro/smartmontools) build, which
   adds ATA Device Statistics, SAS lifetime and phy counters). Drives that must
   get no commands at all are `-d ignore`d in `smartd.conf`.
   *Why:* a SAMSUNG HD204UI (fw 1AQ10001) silently discards a pending write
   when an IDENTIFY DEVICE arrives; that bug made the 64 KiB zero holes in
   md125 (2026-03). Monitoring traffic to drives must be minimal and in one
   controlled place.
4. **Everything else is passive**: sysfs, `/proc`, `statvfs`, kernel
   device-mapper status, and LVM's own metadata backups. No collector reads
   from a disk, so none can wake a spun-down drive.
5. Built and deployed on big-storage first, with nothing host-specific.

## Collector 1: `sensors2mqtt.collector.storage` (module `storage`)

Unprivileged (`DynamicUser=yes`): everything it reads is world-readable.

**Drives** (`/sys/block/*` with a serial: SCSI/SATA from the kernel's cached
VPD page 0x80, NVMe from `/sys/class/nvme/*/serial`). Per drive, one HA device
`disk_<serial>` named `<model> <serial>` (`sw_version` = firmware), `via_device`
= the host:

| source | entities |
|---|---|
| sysfs | host, `/dev` name, enclosure + slot, capacity, firmware, what uses it (partitions -> md / dm names) |
| expander phy (`/sys/class/sas_phy`, SMP to the expander, not the drive) | negotiated link rate, invalid dwords, disparity errors, loss of dword sync, phy reset problems |
| `/proc/diskstats` | bytes and operations read/written, busy time (lifetime counters); read/write MB/s, IOPS, utilisation, in-flight (rates between polls) |
| smartd JSON (refreshed each smartd check, 30 min) | SMART status, last update time, temperature and lifetime min/max; common `power_on_hours`, `power_cycles`; every ATA attribute (raw value; normalised value disabled by default); every ATA Device Statistics entry; SCSI error counters, start-stop/load-unload cycles, grown/pending defects, background scans, drive-side phy counters; every NVMe health log field |

Availability of a drive's entities is `all` of the drive's own status topic
(`offline` when it leaves this host) and the collector's connection topic
(its Last-Will). A drive that moves host is re-discovered by the new host
with that host's topics.

**Enclosures** (`/sys/class/enclosure/*`): one HA device per enclosure
(`encl_<id>`), with per-slot status (`OK`, `not installed`, faults) and the
serial of the drive in the slot, so empty and phantom slots show up.

## Collector 2: `sensors2mqtt.collector.storage_lvm` (module `storage_lvm`)

Runs as root (device-mapper status needs it). Publishes on the host device:

- **Filesystems** (`/proc/self/mountinfo`, block-backed types): size, used,
  available, used %; fstab entries that aren't mounted.
- **md arrays** (`/sys/block/md*/md/`): level, state, degraded disks, sync
  action and progress, mismatch count.
- **LVM**: VG/LV/PV sizes and allocation from `/etc/lvm/backup/*` (LVM
  rewrites it on every metadata change; reading it touches no disk), PV
  presence from udev's `/dev/disk/by-id/lvm-pv-uuid-*` links, and live health
  from `dmsetup status`: RAID health, sync %, mismatches; dm-integrity
  mismatches; cache usage.
- **Roll-ups**: unallocated space, missing PVs, degraded / resyncing LVs,
  degraded md arrays, integrity mismatches, filesystems over 95 %.

## Dashboard

A self-contained HTML page (like the soundproof-rack and energy dashboards in
`~/local/zigbee` on ten64), served from HA's `/config/www/storage-dashboard/`
in a Lovelace iframe view, reading `/api/states` with the read-only dashboard
token: a problems banner, one bay grid per enclosure, NVMe and other drives,
a sortable drive table, and the filesystem / LVM / md views.

## Also changed

- The local collector no longer reads `drivetemp` (each read is an ATA SMART
  command); drive temperatures now come from smartd via the storage collector.
- `DeviceInfo` gains `sw_version` / `serial_number`, and `SensorDef` gains
  `enabled_by_default`, for the per-drive devices.
