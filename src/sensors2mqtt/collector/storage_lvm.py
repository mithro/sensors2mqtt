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
import os
from collections.abc import Callable
from pathlib import Path

from sensors2mqtt.base import BasePublisher, host_id, host_name
from sensors2mqtt.collector.storage import lvm
from sensors2mqtt.collector.storage.drives import identity, whole_disks
from sensors2mqtt.collector.storage.smartd import slug
from sensors2mqtt.discovery import DeviceInfo, SensorDef, WithAttributes

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


def _listing(suffix, name, icon, diagnostic=False) -> SensorDef:
    """A text sensor whose attributes hold the whole list (a state is <= 255)."""
    return SensorDef(suffix, name, "", icon=icon, attributes=True,
                     entity_category="diagnostic" if diagnostic else None)


def short_list(items: list[str], limit: int = 255) -> str:
    """``a, b, c`` cut to fit an HA state: ``a, b, … (+1)``."""
    if not items:
        return "none"
    text = ", ".join(items)
    if len(text) <= limit:
        return text
    for n in range(len(items) - 1, 0, -1):
        text = ", ".join(items[:n]) + f", … (+{len(items) - n})"
        if len(text) <= limit:
            return text
    return f"{len(items)} items"


# block device name -> the physical drives under it: [{"dev", "serial", "model"}]
DisksOf = Callable[[str], list[dict]]


def _no_disks(_dev: str) -> list[dict]:
    return []


def _pv_entry(pv: dict, disks_of: DisksOf) -> dict:
    """A PV leaf of an LV tree with the drives it is on."""
    e = dict(pv)
    e["disks"] = disks_of(pv["device"]) if pv.get("device") else []
    return e


def _annotate(tree: dict, disks_of: DisksOf) -> dict:
    """A copy of an LV tree with each PV's drives."""
    if "pv" in tree:
        return _pv_entry(tree, disks_of)
    return {**tree, "children": [_annotate(c, disks_of) for c in tree.get("children", [])]}


def _pv_label(pv: dict) -> str:
    return pv.get("device") or f"missing {pv.get('pv')}"


def _drive_label(d: dict) -> str:
    return d.get("serial") or d.get("dev") or "?"


def collect_logical(filesystems, fstab_missing, md_arrays, vgs, dm,
                    disks_of: DisksOf = _no_disks,
                    fs_backing: Callable[[lvm.Filesystem], tuple[str | None, str | None]]
                    = lambda fs: (None, None)) -> list[tuple[SensorDef, object]]:
    """Sensors and values for one poll (pure: easy to test).

    ``disks_of(dev)`` lists the drives under a block device; ``fs_backing(fs)``
    is ``("vg/lv", None)`` for a filesystem on an LV, else ``(None, dev)``.
    """
    out: list[tuple[SensorDef, object]] = []

    def add(sensor, value):
        if value is not None:
            out.append((sensor, value))

    lv_by_name = {f"{vg.name}/{lv.name}": (vg, lv) for vg in vgs for lv in vg.lvs}

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
        # What it is on: LV, VG, PVs, drives
        lv_name, dev = fs_backing(fs)
        if lv_name in lv_by_name:
            vg, lv = lv_by_name[lv_name]
            pvs = [_pv_entry(p, disks_of) for p in lv.pvs()]
            disks = [d for p in pvs for d in p["disks"]]
        else:
            vg, lv, pvs = None, None, []
            disks = disks_of(dev) if dev else []
        add(_text(f"{s}_lv", f"{m} LV", "mdi:layers", diagnostic=False), lv_name or "none")
        add(_text(f"{s}_vg", f"{m} VG", "mdi:database", diagnostic=False),
            vg.name if vg else "none")
        add(_listing(f"{s}_pvs", f"{m} PVs", "mdi:harddisk"),
            WithAttributes(short_list([_pv_label(p) for p in pvs]), {"pvs": pvs}))
        uniq = list({_drive_label(d): d for d in disks}.values())
        add(_listing(f"{s}_drives", f"{m} Drives", "mdi:harddisk"),
            WithAttributes(short_list([_drive_label(d) for d in uniq]), {"drives": uniq}))
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
            pvs = lv.pvs()
            gone = sum(1 for p in pvs if not p["present"])
            add(_listing(f"{ls}_layout", f"{lm} Layout", "mdi:layers-triple"),
                WithAttributes(
                    f"{lv.type} on {len(pvs)} PV{'s' if len(pvs) != 1 else ''}"
                    + (f" ({gone} missing)" if gone else ""),
                    {"vg": vg.name, "lv": lv.name, "type": lv.type,
                     "size_gb": lvm.gb(lv.size_bytes),
                     "tree": _annotate(lv.tree, disks_of)}))
        vg_pvs = []
        for pv in vg.pvs:
            ps = f"pv_{slug(vg.name)}_{slug(pv.uuid)}"
            pm = f"PV {vg.name} {pv.device_hint or pv.uuid}"
            add(_size(f"{ps}_size", f"{pm} Size", diagnostic=True), lvm.gb(pv.size_bytes))
            add(_size(f"{ps}_used", f"{pm} Allocated", diagnostic=True),
                lvm.gb(pv.used_bytes))
            add(_text(f"{ps}_state", f"{pm} State", "mdi:harddisk", diagnostic=True),
                "present" if pv.present else "missing")
            disks = disks_of(pv.device) if pv.device else []
            add(_text(f"{ps}_device", f"{pm} Device", "mdi:harddisk", diagnostic=True),
                pv.device or "missing")
            add(_text(f"{ps}_drive", f"{pm} Drive", "mdi:harddisk", diagnostic=True),
                short_list([_drive_label(d) for d in disks]) if disks else "none")
            vg_pvs.append({"pv": pv.name, "uuid": pv.uuid, "device": pv.device,
                           "last_seen_as": pv.device_hint, "present": pv.present,
                           "size_gb": lvm.gb(pv.size_bytes),
                           "used_gb": lvm.gb(pv.used_bytes), "disks": disks})
        add(_listing(f"{s}_pv_list", f"{m} PV List", "mdi:harddisk"),
            WithAttributes(short_list([_pv_label(p) for p in vg_pvs]), {"pvs": vg_pvs}))
        unallocated += vg.free_bytes
        missing += vg.missing_pvs
    add(_size("lvm_unallocated", "LVM Unallocated"), lvm.gb(unallocated))
    add(_gauge("lvm_missing_pvs", "LVM Missing PVs", icon="mdi:alert"), missing)

    # LVM health (device-mapper)
    degraded = resyncing = 0
    for lv, r in sorted(dm.raid.items()):
        s, m = f"raid_{slug(lv)}", f"RAID {lv}"
        # dm-raid health: A alive and in sync, a alive but not in sync (every
        # leg while the set resyncs), D dead, - no device.
        bad = sum(1 for c in r["health"] if c in "D-")
        unsynced = sum(1 for c in r["health"] if c == "a")
        add(_text(f"{s}_health", f"{m} Health", "mdi:raid", diagnostic=False), r["health"])
        add(_gauge(f"{s}_failed_images", f"{m} Failed Images", icon="mdi:alert"), bad)
        add(_gauge(f"{s}_unsynced_images", f"{m} Images Not In Sync", icon="mdi:sync"),
            unsynced)
        add(_text(f"{s}_type", f"{m} Type", "mdi:raid"), r["type"])
        add(_pct(f"{s}_sync_pct", f"{m} Sync", "mdi:sync"),
            round(r["sync_pct"], 2) if r["sync_pct"] is not None else None)
        add(_text(f"{s}_sync_action", f"{m} Sync Action", "mdi:sync"), r["action"])
        add(_gauge(f"{s}_mismatches", f"{m} Mismatches", icon="mdi:alert-circle"),
            r["mismatches"])
        degraded += bad > 0
        resyncing += unsynced > 0 or r["action"] not in ("idle", "frozen") or (
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

    default_entity_ids = True

    def __init__(self, config=None, sysfs_root: str = "/", proc_root: str = "/",
                 backup_dir: str = "/etc/lvm/backup", fstab: str = "/etc/fstab"):
        super().__init__(config)
        self.sysfs_root, self.proc_root = sysfs_root, proc_root
        self.backup_dir, self.fstab = backup_dir, fstab
        self._current: list[tuple[SensorDef, object]] = []
        self._disk_cache: dict[str, list[dict]] = {}
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
        missing = lvm.fstab_not_mounted(fstab, lvm.all_mountpoints(self.proc_root))
        self._disk_cache: dict[str, list[dict]] = {}
        self._current = collect_logical(filesystems, missing,
                                        lvm.read_md_arrays(self.sysfs_root),
                                        lvm.read_vgs(self.backup_dir),
                                        lvm.read_dmsetup_status(),
                                        self.disks_of, self.fs_backing)
        return {}

    def disks_of(self, dev: str) -> list[dict]:
        """The drives (serial, model, device) under a block device."""
        if dev not in self._disk_cache:
            out = []
            for disk in whole_disks(dev, self.sysfs_root):
                serial, model, *_ = identity(Path(self.sysfs_root) / "sys/block" / disk)
                out.append({"dev": disk, "serial": serial, "model": model})
            self._disk_cache[dev] = out
        return self._disk_cache[dev]

    def fs_backing(self, fs: lvm.Filesystem) -> tuple[str | None, str | None]:
        """("vg/lv", None) for a filesystem on an LV, else (None, block device)."""
        if not fs.devno:
            return None, None
        blk = Path(self.sysfs_root) / "sys/dev/block" / fs.devno
        dm_name = lvm._read(blk / "dm/name")
        dm_uuid = lvm._read(blk / "dm/uuid") or ""
        if dm_name and dm_uuid.startswith("LVM-"):
            return lvm.dm_lv_name(dm_name), None
        real = os.path.realpath(blk)
        return None, (os.path.basename(real) if os.path.exists(real) else None)

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
            if isinstance(value, WithAttributes):
                print(f"{sensor.suffix} = {value.state}  [attributes: {value.attributes}]")
            else:
                print(f"{sensor.suffix} = {value}")
        return
    collector.run()


if __name__ == "__main__":
    main()
