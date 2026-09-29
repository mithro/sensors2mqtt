"""Logical storage collector: filesystems, md arrays, LVM (module ``storage_lvm``).

Publishes on the host's device. Runs as root (`dmsetup status` and
/etc/lvm/backup need it). Reads nothing from any disk: see storage/lvm.py.

Usage:
    python -m sensors2mqtt.collector.storage_lvm
    python -m sensors2mqtt.collector.storage_lvm --once
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from sensors2mqtt.base import BasePublisher, host_id, host_name
from sensors2mqtt.collector.storage import lvm
from sensors2mqtt.collector.storage.smartd import slug
from sensors2mqtt.discovery import DeviceInfo, SensorDef

log = logging.getLogger(__name__)

MODULE = "storage_lvm"
FULL_PCT = 95.0


def _text(suffix, name, icon=None, diagnostic=True) -> SensorDef:
    return SensorDef(suffix, name, "", icon=icon,
                     entity_category="diagnostic" if diagnostic else None)


def _gauge(suffix, name, unit="", icon=None, device_class=None, diagnostic=False) -> SensorDef:
    return SensorDef(suffix, name, unit, device_class=device_class, state_class="measurement",
                     icon=icon, entity_category="diagnostic" if diagnostic else None)


def _size(suffix, name, diagnostic=False) -> SensorDef:
    return _gauge(suffix, name, "GB", "mdi:harddisk", "data_size", diagnostic)


def _pct(suffix, name, icon="mdi:gauge", diagnostic=False) -> SensorDef:
    return _gauge(suffix, name, "%", icon, diagnostic=diagnostic)


def collect_logical(filesystems, fstab_missing, md_arrays, vgs,
                    dm) -> list[tuple[SensorDef, object]]:
    """Sensors and values for one poll (pure: easy to test)."""
    out: list[tuple[SensorDef, object]] = []

    def add(sensor, value):
        if value is not None:
            out.append((sensor, value))

    # Filesystems
    full = 0
    for fs in filesystems:
        s, m = f"fs_{lvm.mount_slug(fs.mountpoint)}", fs.mountpoint
        add(_size(f"{s}_size", f"{m} Size", diagnostic=True), lvm.gb(fs.size_bytes))
        add(_size(f"{s}_used", f"{m} Used"), lvm.gb(fs.used_bytes))
        add(_size(f"{s}_available", f"{m} Available"), lvm.gb(fs.avail_bytes))
        add(_pct(f"{s}_used_pct", f"{m} Used %", "mdi:chart-donut"), fs.used_pct)
        add(_pct(f"{s}_inodes_used_pct", f"{m} Inodes Used %", diagnostic=True),
            fs.inodes_used_pct)
        add(_text(f"{s}_device", f"{m} Device", "mdi:harddisk"), fs.source)
        add(_text(f"{s}_type", f"{m} Filesystem", "mdi:file-tree"), fs.fstype)
        add(_text(f"{s}_mode", f"{m} Mode", "mdi:lock"), "ro" if fs.readonly else "rw")
        full += fs.used_pct >= FULL_PCT
    add(_gauge("fs_full_count", f"Filesystems Over {FULL_PCT:.0f}%", icon="mdi:alert"), full)
    add(_gauge("fs_not_mounted_count", "fstab Filesystems Not Mounted", icon="mdi:alert"),
        len(fstab_missing))
    add(_text("fs_not_mounted", "fstab Filesystems Not Mounted (list)", "mdi:alert",
              diagnostic=False), ", ".join(fstab_missing) or "none")

    # md arrays
    md_degraded = 0
    for md in md_arrays:
        s, m = f"md_{slug(md.name)}", md.name
        add(_text(f"{s}_level", f"{m} Level", "mdi:raid"), md.level)
        add(_text(f"{s}_state", f"{m} State", "mdi:raid", diagnostic=False), md.state)
        add(_gauge(f"{s}_degraded", f"{m} Degraded Disks", icon="mdi:alert"), md.degraded)
        add(_gauge(f"{s}_raid_disks", f"{m} Disks", icon="mdi:harddisk", diagnostic=True),
            md.raid_disks)
        add(_text(f"{s}_sync_action", f"{m} Sync Action", "mdi:sync"), md.sync_action)
        add(_pct(f"{s}_sync_pct", f"{m} Sync Progress", "mdi:sync"), md.sync_pct)
        add(_gauge(f"{s}_mismatches", f"{m} Mismatches", icon="mdi:alert-circle"),
            md.mismatches)
        add(_size(f"{s}_size", f"{m} Size", diagnostic=True), lvm.gb(md.size_bytes))
        md_degraded += bool(md.degraded)
    add(_gauge("md_degraded_count", "Degraded md Arrays", icon="mdi:alert"), md_degraded)

    # LVM layout
    unallocated = missing = 0
    for vg in vgs:
        s, m = f"vg_{slug(vg.name)}", f"VG {vg.name}"
        add(_size(f"{s}_size", f"{m} Size"), lvm.gb(vg.size_bytes))
        add(_size(f"{s}_free", f"{m} Unallocated"), lvm.gb(vg.free_bytes))
        add(_pct(f"{s}_allocated_pct", f"{m} Allocated %", "mdi:chart-donut"),
            round(100.0 * (vg.size_bytes - vg.free_bytes) / vg.size_bytes, 1)
            if vg.size_bytes else None)
        add(_gauge(f"{s}_pvs", f"{m} PVs", icon="mdi:harddisk", diagnostic=True), len(vg.pvs))
        add(_gauge(f"{s}_missing_pvs", f"{m} Missing PVs", icon="mdi:alert"), vg.missing_pvs)
        add(_gauge(f"{s}_lvs", f"{m} LVs", icon="mdi:layers", diagnostic=True), len(vg.lvs))
        for lv in vg.lvs:
            ls, lm = f"lv_{slug(vg.name)}_{slug(lv.name)}", f"LV {vg.name}/{lv.name}"
            add(_size(f"{ls}_size", f"{lm} Size", diagnostic=True), lvm.gb(lv.size_bytes))
            add(_text(f"{ls}_type", f"{lm} Type", "mdi:layers"), lv.type)
        for pv in vg.pvs:
            ps = f"pv_{slug(vg.name)}_{slug(pv.uuid)}"
            pm = f"PV {vg.name} {pv.device_hint or pv.uuid}"
            add(_size(f"{ps}_size", f"{pm} Size", diagnostic=True), lvm.gb(pv.size_bytes))
            add(_size(f"{ps}_used", f"{pm} Allocated", diagnostic=True),
                lvm.gb(pv.used_bytes))
            add(_text(f"{ps}_state", f"{pm} State", "mdi:harddisk", diagnostic=True),
                "present" if pv.present else "missing")
        unallocated += vg.free_bytes
        missing += vg.missing_pvs
    add(_size("lvm_unallocated", "LVM Unallocated"), lvm.gb(unallocated))
    add(_gauge("lvm_missing_pvs", "LVM Missing PVs", icon="mdi:alert"), missing)

    # LVM health (device-mapper)
    degraded = resyncing = 0
    for lv, r in sorted(dm.raid.items()):
        s, m = f"raid_{slug(lv)}", f"RAID {lv}"
        bad = sum(1 for c in r["health"] if c != "A")
        add(_text(f"{s}_health", f"{m} Health", "mdi:raid", diagnostic=False), r["health"])
        add(_gauge(f"{s}_failed_images", f"{m} Degraded Images", icon="mdi:alert"), bad)
        add(_text(f"{s}_type", f"{m} Type", "mdi:raid"), r["type"])
        add(_pct(f"{s}_sync_pct", f"{m} Sync", "mdi:sync"),
            round(r["sync_pct"], 2) if r["sync_pct"] is not None else None)
        add(_text(f"{s}_sync_action", f"{m} Sync Action", "mdi:sync"), r["action"])
        add(_gauge(f"{s}_mismatches", f"{m} Mismatches", icon="mdi:alert-circle"),
            r["mismatches"])
        degraded += bad > 0
        resyncing += r["action"] not in ("idle", "frozen") or (
            r["sync_pct"] is not None and r["sync_pct"] < 100)
    total_mismatch = 0
    for lv, n in sorted(dm.integrity.items()):
        add(_gauge(f"integrity_{slug(lv)}_mismatches", f"Integrity {lv} Mismatches",
                   icon="mdi:alert-circle"), n)
        total_mismatch += n
    for lv, c in sorted(dm.cache.items()):
        s, m = f"cache_{slug(lv)}", f"Cache {lv}"
        add(_pct(f"{s}_used_pct", f"{m} Used", "mdi:cached"),
            round(c["used_pct"], 1) if c["used_pct"] is not None else None)
        add(_gauge(f"{s}_dirty_blocks", f"{m} Dirty Blocks", icon="mdi:cached"), c["dirty"])
        add(_pct(f"{s}_read_hit_pct", f"{m} Read Hit Rate", "mdi:cached", diagnostic=True),
            round(c["read_hit_pct"], 1) if c["read_hit_pct"] is not None else None)
    for lv, t in sorted(dm.thin_pool.items()):
        s, m = f"thin_{slug(lv)}", f"Thin Pool {lv}"
        add(_pct(f"{s}_data_pct", f"{m} Data Used", "mdi:chart-donut"),
            round(t["data_pct"], 1) if t["data_pct"] is not None else None)
        add(_pct(f"{s}_metadata_pct", f"{m} Metadata Used", "mdi:chart-donut"),
            round(t["metadata_pct"], 1) if t["metadata_pct"] is not None else None)
    add(_gauge("lvm_degraded_raids", "Degraded LVM RAIDs", icon="mdi:alert"), degraded)
    add(_gauge("lvm_resyncing_raids", "Resyncing LVM RAIDs", icon="mdi:sync"), resyncing)
    add(_gauge("integrity_mismatches", "Integrity Mismatches", icon="mdi:alert-circle"),
        total_mismatch)
    return out


class StorageLvmCollector(BasePublisher):
    """Filesystems, md arrays and LVM on this host."""

    def __init__(self, config=None, sysfs_root: str = "/", proc_root: str = "/",
                 backup_dir: str = "/etc/lvm/backup", fstab: str = "/etc/fstab"):
        super().__init__(config)
        self.sysfs_root, self.proc_root = sysfs_root, proc_root
        self.backup_dir, self.fstab = backup_dir, fstab
        self._current: list[tuple[SensorDef, object]] = []
        # Only identifiers + name: never clobber the manufacturer/model a
        # hardware-aware collector (local, ipmi_sensors) sets on the host device.
        self._device = DeviceInfo(node_id=host_id(), name=host_name(),
                                  manufacturer="Unknown", model="Unknown")

    @property
    def sensors(self) -> list[SensorDef]:
        return []

    @property
    def device(self) -> DeviceInfo:
        return self._device

    @property
    def module(self) -> str:
        return MODULE

    def poll(self) -> dict | None:
        filesystems = lvm.read_filesystems(self.proc_root)
        try:
            fstab = Path(self.fstab).read_text()
        except OSError:
            fstab = ""
        missing = lvm.fstab_not_mounted(fstab, {f.mountpoint for f in filesystems})
        self._current = collect_logical(filesystems, missing,
                                        lvm.read_md_arrays(self.sysfs_root),
                                        lvm.read_vgs(self.backup_dir),
                                        lvm.read_dmsetup_status())
        return {}

    def dynamic_sensors(self) -> list[tuple[SensorDef, object]]:
        return self._current


def main() -> None:
    parser = argparse.ArgumentParser(description="Logical storage collector")
    parser.add_argument("--once", action="store_true", help="Print one poll and exit")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(message)s")
    collector = StorageLvmCollector()
    if args.once:
        collector.poll()
        for sensor, value in collector.dynamic_sensors():
            print(f"{sensor.suffix} = {value}")
        return
    collector.run()


if __name__ == "__main__":
    main()
