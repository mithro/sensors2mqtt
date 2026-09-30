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
(`encl_<id>`), with per-slot status (`OK`, `not installed`, faults), the
slot's own label (the SES element name, "Slot07") and the serial of the drive
in the slot, so empty and phantom slots show up.

**NVMe enclosure and slot names** (added 2026-09-30): all of a host's NVMe
drives are in one virtual enclosure (`encl_<host>_nvme`, id `nvme`). Its bays
are every PCIe position with an NVMe controller plus every hot-plug port
(`/sys/bus/pci/slots/*/adapter`) with no other kind of card in it (a card that
is present but didn't come up is a phantom slot). A bay's slot number is its
PCI position, `(domain * 256 + bus) * 32 + device`, so it doesn't change when
another drive disappears. Bays and controllers
are named from the SMBIOS type 9 (System Slot) records, which give the
board's silkscreen name and the PCI address of the device in each slot: a port
of a switch card is `<slot> port <n>` (downstream ports in PCI order), a CPU
port with no slot record is `CPU<n> root port <bus:dev.fn>`. SAS/SATA drives
get `controller_slot`, the slot of their HBA. The SMBIOS table is root-only,
so the unit passes it in as a systemd credential (`LoadCredential=smbios:`,
with `SetCredential=smbios:-` as the fallback where there is none).

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
- **What is on what** (added 2026-09-30): per filesystem, its LV, VG, PVs and
  drives (serials); per VG, its PVs; per PV, its current device and drive;
  per LV, `lv_<vg>_<lv>_layout`, whose attributes are the LV's tree from the
  metadata segments (`raids`, `stripes`, `origin`, `cache_pool`, `meta_dev`,
  ...) down to PVs and their drives, so a missing PV still shows where it
  was. PVs resolve to devices through the `lvm-pv-uuid-*` links and to drives
  through sysfs `slaves` (through md and dm). Lists go in entity attributes,
  published on `sensors2mqtt/<node>/<module>/attributes/<suffix>` (retained,
  only when changed) rather than in the state message every entity parses.

## Dashboard

A self-contained HTML page (like the soundproof-rack and energy dashboards in
`~/local/zigbee` on ten64), served from HA's `/config/www/storage-dashboard/`
in a Lovelace iframe view, reading `/api/states` with the read-only dashboard
token: a problems banner, one bay grid per enclosure (the NVMe enclosure
included), a sortable drive table, the filesystem / LVM / md views with what
each filesystem and VG is on, a drawing of each LV's layout, and history
pages (filesystem usage and per-drive I/O) from HA's long-term statistics.

## Also changed

- The local collector no longer reads `drivetemp` (each read is an ATA SMART
  command); drive temperatures now come from smartd via the storage collector.
- `DeviceInfo` gains `sw_version` / `serial_number`, and `SensorDef` gains
  `enabled_by_default`, for the per-drive devices.
