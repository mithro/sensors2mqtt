"""Find drives and read what the kernel already knows about them.

Everything here is passive: sysfs attributes the kernel cached when it probed
the drive (VPD pages, IDENTIFY data), SES enclosure state, the SAS expander's
phy counters (an SMP request to the expander, never to the drive), and
/proc/diskstats. Nothing here sends a command to a drive or wakes one up.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from sensors2mqtt.collector.storage.pcie import (
    SmbiosSlot,
    board_slot,
    nvme_bays,
    read_smbios_slots,
)

# The enclosure id every NVMe drive of a host is placed in: the host's PCIe
# NVMe positions (see pcie.nvme_bays) are its bays.
NVME_ENCLOSURE = "nvme"

# Block devices that are not drives.
_SKIP_PREFIXES = ("loop", "ram", "zram", "dm-", "md", "sr", "nbd", "fd", "mmcblk", "zd")


@dataclass
class Drive:
    """A physical drive attached to this host, identified by its serial."""

    serial: str
    name: str  # kernel block device name, e.g. "sdn", "nvme0n1"
    transport: str  # "SAS", "SATA", "NVMe", "USB", ...
    vendor: str | None
    model: str
    firmware: str | None
    capacity_bytes: int
    logical_block_size: int
    rotational: bool
    enclosure: str | None = None  # enclosure id, e.g. "0:0:29:0", or NVME_ENCLOSURE
    enclosure_model: str | None = None
    slot: int | None = None
    slot_name: str | None = None  # the bay as labelled: "Slot07", "CPU2 SLOT3 ... port 1"
    slot_status: str | None = None
    hba: str | None = None  # SMBIOS name of the slot of the controller it's on
    pcie_link: str | None = None  # NVMe: "8.0 GT/s PCIe x4"
    used_by: str = ""
    phy: dict = field(default_factory=dict)  # expander-side phy link rate + counters


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _read_int(path: Path, default: int = 0) -> int:
    v = _read(path)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def _ata_string(ident: bytes, start_word: int, end_word: int) -> str:
    """An ATA IDENTIFY string field (byte-swapped words)."""
    raw = ident[start_word * 2:end_word * 2]
    swapped = bytes(b for i in range(0, len(raw) - 1, 2) for b in (raw[i + 1], raw[i]))
    return swapped.decode("ascii", errors="replace").strip()


def _vpd_serial(dev: Path) -> str | None:
    """Unit serial number from the cached VPD page 0x80."""
    try:
        data = (dev / "vpd_pg80").read_bytes()
    except OSError:
        return None
    if len(data) < 4:
        return None
    length = int.from_bytes(data[2:4], "big")
    s = data[4:4 + length].decode("ascii", errors="replace").strip().strip("\x00").strip()
    return s or None


def _ata_identity(dev: Path) -> tuple[str, str, str] | None:
    """(serial, model, firmware) from the cached ATA Information VPD page 0x89.

    Present for SATA drives behind a SAS HBA or libata; its IDENTIFY DEVICE
    data starts at byte 60. The SCSI model/rev attributes of such drives are
    truncated to 16/4 characters, this has the full strings.
    """
    try:
        data = (dev / "vpd_pg89").read_bytes()
    except OSError:
        return None
    ident = data[60:60 + 512]
    if len(ident) < 512:
        return None
    return (_ata_string(ident, 10, 20), _ata_string(ident, 27, 47),
            _ata_string(ident, 23, 27))


def _sas_phy(dev_real: str, sysfs_root: Path) -> dict:
    """Link rate and error counters of the SAS phy the drive is attached to.

    Walks up from the SCSI device to the nearest ``port-*`` directory that has
    ``phy-*`` children (the expander or HBA port the drive is cabled to).
    """
    p = Path(dev_real)
    while p != p.parent:
        if p.name.startswith("port-"):
            phys = sorted(c.name for c in p.iterdir() if c.name.startswith("phy-"))
            if phys:
                phy = sysfs_root / "sys/class/sas_phy" / phys[0]
                return {
                    "phy": phys[0],
                    "link_rate": _read(phy / "negotiated_linkrate"),
                    "invalid_dwords": _read_int(phy / "invalid_dword_count", -1),
                    "disparity_errors": _read_int(phy / "running_disparity_error_count", -1),
                    "loss_of_dword_sync": _read_int(phy / "loss_of_dword_sync_count", -1),
                    "phy_reset_problems": _read_int(phy / "phy_reset_problem_count", -1),
                }
        p = p.parent
    return {}


# LVM's hidden sub-LVs (RAID images, integrity, cache and thin pool parts),
# folded into the LV they belong to.
_SUB_LV_RE = re.compile(
    r"(_(rimage|rmeta|mimage|mlog|imeta|iorig|cvol|corig|cdata|cmeta|cpool|"
    r"tdata|tmeta|vorigin|vdata|pmspare)(_\d+)?)+$")


def dm_display_name(dm_name: str) -> str:
    """``storage--big-space1cache_cvol_rimage_0`` -> ``storage-big/space1cache``.

    device-mapper names an LV ``<vg>-<lv>`` with each '-' inside a name doubled.
    Names that aren't LVM's are returned as they are.
    """
    parts = re.split(r"(?<!-)-(?!-)", dm_name, maxsplit=1)
    if len(parts) != 2:
        return dm_name
    vg, lv = (p.replace("--", "-") for p in parts)
    return f"{vg}/{_SUB_LV_RE.sub('', lv)}"


def _holder_names(block: Path, sysfs_root: Path) -> list[str]:
    """What sits on top of a block device or its partitions (md / LVM names)."""
    names: set[str] = set()
    parts = [block] + [c for c in block.iterdir() if c.name.startswith(block.name)
                       and (c / "partition").exists()]
    for part in parts:
        holders = part / "holders"
        if not holders.is_dir():
            continue
        for h in holders.iterdir():
            dm_name = _read(sysfs_root / "sys/block" / h.name / "dm/name")
            names.add(dm_display_name(dm_name) if dm_name else h.name)
    return sorted(names)


def _mounted_devices(proc_root: Path) -> dict[str, str]:
    """major:minor -> mount point, from /proc/self/mountinfo."""
    out: dict[str, str] = {}
    text = _read(proc_root / "proc/self/mountinfo") or ""
    for line in text.splitlines():
        f = line.split()
        if len(f) > 4:
            out.setdefault(f[2], f[4])
    return out


def _mounts_of(block: Path, mounted: dict[str, str]) -> list[str]:
    out = []
    for part in [block] + [c for c in block.iterdir() if (c / "partition").exists()]:
        dev = _read(part / "dev")
        if dev and dev in mounted:
            out.append(mounted[dev])
    return out


def identity(block: Path) -> tuple[str | None, str, str | None, str | None, str]:
    """(serial, model, firmware, vendor, transport) of a whole-disk block device."""
    dev = block / "device"
    dev_real = os.path.realpath(dev)
    if block.name.startswith("nvme"):
        # /sys/block/nvmeXnY/device is the controller, or with native NVMe
        # multipath the subsystem; both have serial, model and firmware_rev.
        return (_read(dev / "serial"), _read(dev / "model") or "NVMe",
                _read(dev / "firmware_rev"), None, "NVMe")
    vendor = _read(dev / "vendor")
    ata = _ata_identity(dev)
    if ata:
        serial, model, firmware = ata
        transport = "SATA"
    else:
        serial = _vpd_serial(dev)
        model = _read(dev / "model") or "Unknown"
        firmware = _read(dev / "rev")
        transport = "SAS" if "/end_device-" in dev_real else "SCSI"
    if "/usb" in dev_real:
        transport = "USB"
    if vendor in ("ATA", ""):
        vendor = None
    return serial, model, firmware, vendor, transport


def whole_disks(name: str, sysfs_root: str = "/") -> list[str]:
    """The physical disks a block device is built on (itself if it is one).

    Follows ``slaves`` down through device-mapper and md; a partition is its
    disk. ``sdb1`` -> ``["sdb"]``, ``dm-5`` (a RAID LV) -> every disk under it.
    """
    cls = Path(sysfs_root) / "sys/class/block"
    out: list[str] = []
    todo, seen = [name], set()
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        d = cls / n
        if (d / "partition").exists():
            n = Path(os.path.realpath(d)).parent.name
            d = cls / n
        slaves = sorted(s.name for s in (d / "slaves").iterdir()) \
            if (d / "slaves").is_dir() else []
        if slaves:
            todo.extend(slaves)
        elif (d / "device").exists() and n not in out:
            out.append(n)
    return sorted(out)


def discover_drives(sysfs_root: str = "/", proc_root: str = "/",
                    smbios: list[SmbiosSlot] | None = None) -> list[Drive]:
    """Every drive the kernel knows, with a serial number."""
    root = Path(sysfs_root)
    mounted = _mounted_devices(Path(proc_root))
    if smbios is None:
        smbios = read_smbios_slots(sysfs_root)
    bays = {b.controller: (i, b) for i, b in enumerate(nvme_bays(sysfs_root, smbios))
            if b.controller}
    drives: list[Drive] = []
    for block in sorted((root / "sys/block").iterdir()):
        name = block.name
        if name.startswith(_SKIP_PREFIXES) or not (block / "device").exists():
            continue
        dev = block / "device"
        dev_real = os.path.realpath(dev)
        capacity = _read_int(block / "size") * 512
        lbs = _read_int(block / "queue/logical_block_size", 512) or 512
        rotational = _read(block / "queue/rotational") == "1"
        serial, model, firmware, vendor, transport = identity(block)
        if not serial:
            continue

        drive = Drive(serial=serial, name=name, transport=transport, vendor=vendor,
                      model=model, firmware=firmware, capacity_bytes=capacity,
                      logical_block_size=lbs, rotational=rotational)

        for link in dev.glob("enclosure_device:*"):
            # .../enclosure/<enclosure id>/<slot>
            slot_dir = Path(os.path.realpath(link))
            drive.enclosure = slot_dir.parent.name
            drive.enclosure_model = _read(slot_dir.parent / "device/model")
            drive.slot = _read_int(slot_dir / "slot", -1)
            drive.slot_name = slot_dir.name  # the enclosure's own label, "Slot07"
            drive.slot_status = _read(slot_dir / "status")
            break
        if transport == "NVMe" and Path(dev_real).name in bays:
            i, bay = bays[Path(dev_real).name]
            drive.enclosure, drive.enclosure_model = NVME_ENCLOSURE, "NVMe"
            drive.slot, drive.slot_name, drive.slot_status = i, bay.label, "OK"
            drive.pcie_link = bay.link
        drive.hba = board_slot(dev_real, smbios) if transport != "NVMe" else None

        if transport in ("SAS", "SATA"):
            drive.phy = _sas_phy(dev_real, root)

        users = _holder_names(block, root) + _mounts_of(block, mounted)
        drive.used_by = ", ".join(users)
        drives.append(drive)
    return drives


# /proc/diskstats columns (Documentation/admin-guide/iostats.rst), 0-based
# after major, minor, name: reads, reads merged, sectors read, ms reading,
# writes, writes merged, sectors written, ms writing, in flight, ms doing I/O.
_DISKSTAT_RE = re.compile(r"^\s*\d+\s+\d+\s+(\S+)\s+(.*)$")


@dataclass
class DiskStats:
    reads: int
    sectors_read: int
    writes: int
    sectors_written: int
    in_flight: int
    io_ms: int


def read_diskstats(proc_root: str = "/") -> dict[str, DiskStats]:
    out: dict[str, DiskStats] = {}
    text = _read(Path(proc_root) / "proc/diskstats") or ""
    for line in text.splitlines():
        m = _DISKSTAT_RE.match(line)
        if not m:
            continue
        f = [int(x) for x in m.group(2).split()]
        if len(f) < 10:
            continue
        out[m.group(1)] = DiskStats(reads=f[0], sectors_read=f[2], writes=f[4],
                                    sectors_written=f[6], in_flight=f[8], io_ms=f[9])
    return out


@dataclass
class Slot:
    number: int
    status: str | None
    fault: bool
    locate: bool
    serial: str | None  # drive the enclosure says is in the slot
    name: str | None = None  # the slot's label: SES element name, PCIe position


@dataclass
class Enclosure:
    id: str  # sysfs name, e.g. "0:0:29:0"
    logical_id: str  # the enclosure's SAS address, e.g. "0x500304801f06a3ff"
    vendor: str | None
    model: str | None
    slots: list[Slot]


def discover_enclosures(sysfs_root: str = "/") -> list[Enclosure]:
    """SES enclosures and the state of each of their slots."""
    root = Path(sysfs_root)
    edir = root / "sys/class/enclosure"
    if not edir.is_dir():
        return []
    out = []
    for e in sorted(edir.iterdir()):
        slots = []
        for comp in sorted(e.iterdir()):
            # Array device slots have a slot number; other components don't.
            if not (comp / "slot").exists() or not (comp / "status").exists():
                continue
            serial = None
            dev = comp / "device"
            if dev.exists():
                ata = _ata_identity(dev)
                serial = ata[0] if ata else _vpd_serial(dev)
            slots.append(Slot(number=_read_int(comp / "slot", -1),
                              status=_read(comp / "status"),
                              fault=_read(comp / "fault") == "1",
                              locate=_read(comp / "locate") == "1",
                              serial=serial, name=comp.name))
        slots.sort(key=lambda s: s.number)
        vendor = _read(e / "device/vendor")
        out.append(Enclosure(id=e.name, logical_id=_read(e / "id") or e.name,
                             vendor=vendor, model=_read(e / "device/model"), slots=slots))
    return out


def discover_nvme_enclosure(host: str, sysfs_root: str = "/",
                            smbios: list[SmbiosSlot] | None = None) -> Enclosure | None:
    """The host's NVMe positions as one enclosure (None if it has none).

    Slots are numbered in PCI address order, as discover_drives numbers them.
    """
    bays = nvme_bays(sysfs_root, smbios)
    if not bays:
        return None
    cls = Path(sysfs_root) / "sys/class/nvme"
    slots = []
    for i, b in enumerate(bays):
        serial = _read(cls / b.controller / "serial") if b.controller else None
        slots.append(Slot(number=i, status="OK" if b.controller else "not installed",
                          fault=False, locate=False, serial=serial or None, name=b.label))
    return Enclosure(id=NVME_ENCLOSURE, logical_id=f"{host}_nvme", vendor=None,
                     model="NVMe", slots=slots)
