"""Logical storage: filesystems, md arrays and LVM, without touching a disk.

- Filesystems: /proc/self/mountinfo and statvfs().
- md arrays: /sys/block/md*/md/.
- LVM layout: LVM's metadata backups in /etc/lvm/backup/ (LVM rewrites a VG's
  backup on every metadata change). Reading them, unlike running vgs/lvs,
  never reads a PV label, so it can't keep a spun-down drive awake.
- LVM health: `dmsetup status` (kernel state only): RAID health and sync,
  dm-integrity mismatches, cache and thin pool usage.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sensors2mqtt.collector.storage.smartd import GB, slug

log = logging.getLogger(__name__)

SECTOR = 512

# ---------------------------------------------------------------------------
# LVM text metadata format
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r'''
    \s+ | \#[^\n]* |                       # whitespace, comments
    (?P<str>"(?:[^"\\]|\\.)*") |
    (?P<word>[A-Za-z0-9_.+\-]+) |          # a name or a number (see _tokens)
    (?P<punct>[={}\[\],])
''', re.VERBOSE)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _tokens(text: str):
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ValueError(f"unexpected character {text[pos]!r} at {pos}")
        pos = m.end()
        if m.group("str") is not None:
            yield ("str", re.sub(r"\\(.)", r"\1", m.group("str")[1:-1]))
        elif m.group("word") is not None:
            # A whole word: LV and VG names may start with digits ("1data").
            w = m.group("word")
            if _NUMBER_RE.fullmatch(w):
                yield ("num", float(w) if "." in w else int(w))
            else:
                yield ("name", w)
        elif m.group("punct") is not None:
            yield ("punct", m.group("punct"))


def parse_lvm_metadata(text: str) -> dict:
    """Parse LVM's text format (``key = value``, ``section { ... }``)."""
    toks = list(_tokens(text))
    pos = 0

    def value():
        nonlocal pos
        kind, v = toks[pos]
        pos += 1
        if kind in ("str", "num"):
            return v
        if (kind, v) == ("punct", "["):
            items = []
            while toks[pos] != ("punct", "]"):
                if toks[pos] == ("punct", ","):
                    pos += 1
                    continue
                items.append(value())
            pos += 1
            return items
        return v

    def section(end):
        nonlocal pos
        out: dict = {}
        while pos < len(toks) and toks[pos] != end:
            kind, key = toks[pos]
            pos += 1
            if toks[pos] == ("punct", "="):
                pos += 1
                out[key] = value()
            elif toks[pos] == ("punct", "{"):
                pos += 1
                out[key] = section(("punct", "}"))
                pos += 1
            else:
                raise ValueError(f"unexpected token after {key!r}: {toks[pos]}")
        return out

    return section(None)


@dataclass
class PV:
    name: str  # the metadata's name for it, "pv3"
    uuid: str
    device_hint: str | None  # where LVM last saw it
    size_bytes: int
    used_bytes: int
    present: bool
    device: str | None = None  # block device it is now, "sdo", "md127"


@dataclass
class LV:
    name: str
    type: str
    size_bytes: int
    # What the LV is made of: {"name", "type", "role", "gb", "children": [...]}
    # for an LV or sub-LV, {"pv", "uuid", "device", "present", "gb"} for a PV.
    tree: dict = field(default_factory=dict)

    def pvs(self) -> list[dict]:
        """The PVs the LV is on, each once, with the space it uses on each."""
        out: dict[str, dict] = {}
        todo = [self.tree]
        while todo:
            n = todo.pop(0)
            if "pv" in n:
                if n["pv"] in out:
                    out[n["pv"]]["gb"] = round(out[n["pv"]]["gb"] + n["gb"], 3)
                else:
                    out[n["pv"]] = dict(n)
            todo.extend(n.get("children", []))
        return list(out.values())


@dataclass
class VG:
    name: str
    extent_bytes: int
    pvs: list[PV] = field(default_factory=list)
    lvs: list[LV] = field(default_factory=list)

    @property
    def size_bytes(self) -> int:
        return sum(p.size_bytes for p in self.pvs)

    @property
    def free_bytes(self) -> int:
        return sum(p.size_bytes - p.used_bytes for p in self.pvs)

    @property
    def missing_pvs(self) -> int:
        return sum(1 for p in self.pvs if not p.present)


# Segment keys that name the sub-LVs (or PVs) a segment is built from
_REF_KEYS = ("raids", "mirrors", "origin", "meta_dev", "cache_pool", "pool", "thin_pool",
             "data", "metadata", "log", "mirror_log", "external_origin", "writecache",
             "vdo_pool")
# Sub-LV name suffix -> its role in the LV
_ROLE_RE = re.compile(r"_(rimage|rmeta|mimage|mlog|imeta|iorig|cvol|corig|cdata|cmeta|"
                      r"cpool|tdata|tmeta|vorigin|vdata|pmspare|wcorig)(_\d+)?$")


def _segments(lv: dict) -> list[dict]:
    segs = [(int(k[7:]) if k[7:].isdigit() else 0, v) for k, v in lv.items()
            if k.startswith("segment") and isinstance(v, dict)]
    return [v for _, v in sorted(segs, key=lambda s: s[0])]


def lv_tree(name: str, lvs: dict, pvs: dict[str, PV], extent: int,
            depth: int = 0) -> dict:
    """What an LV is made of, from its metadata segments, down to PVs."""
    segs = _segments(lvs[name])
    extents = sum(int(s.get("extent_count", 0)) for s in segs)
    types = []
    for s in segs:
        t = str(s.get("type", "")).split("+")[0]
        if t and t not in types:
            types.append(t)
    m = _ROLE_RE.search(name)
    node = {"name": name, "type": "/".join(types), "role": m.group(1) if m else None,
            "gb": round(extents * extent / GB, 3), "children": []}
    kids: dict[str, dict] = {}  # merge a PV used by several segments
    for s in segs:
        count = int(s.get("extent_count", 0))
        refs: list[tuple[str, int | None]] = []
        stripes = s.get("stripes") or []
        areas = [stripes[i] for i in range(0, len(stripes), 2)]
        refs += [(str(a), count // max(1, len(areas))) for a in areas]
        for key in _REF_KEYS:
            v = s.get(key)
            for r in (v if isinstance(v, list) else [v] if isinstance(v, str) else []):
                refs.append((str(r), None))
        for ref, n in refs:
            if ref in pvs:
                pv = pvs[ref]
                leaf = kids.get(ref)
                if leaf is None:
                    leaf = kids[ref] = {"pv": ref, "uuid": pv.uuid, "device": pv.device,
                                        "present": pv.present, "gb": 0.0}
                    node["children"].append(leaf)
                leaf["gb"] = round(leaf["gb"] + (n or 0) * extent / GB, 3)
            elif ref in lvs and ref not in kids and depth < 16:
                kids[ref] = lv_tree(ref, lvs, pvs, extent, depth + 1)
                node["children"].append(kids[ref])
    return node


def vg_from_metadata(meta: dict, pv_present, pv_device=None) -> VG | None:
    """The VG described by a parsed metadata backup.

    ``pv_present(uuid) -> bool`` says whether a PV is attached and
    ``pv_device(uuid) -> str | None`` which block device it is. A PV's used
    space is the extents that segment stripes place on it; RAID, cache and thin
    segments place theirs through sub-LVs, whose own segments are counted.
    """
    names = [k for k, v in meta.items() if isinstance(v, dict) and "physical_volumes" in v]
    if not names:
        return None
    name = names[0]
    body = meta[name]
    extent = int(body.get("extent_size", 0)) * SECTOR
    used: dict[str, int] = {}
    lvs: list[LV] = []
    all_lvs = body.get("logical_volumes") or {}
    for lv_name, lv in all_lvs.items():
        size = 0
        seg_type = None
        for key, seg in lv.items():
            if not (key.startswith("segment") and isinstance(seg, dict)):
                continue
            count = int(seg.get("extent_count", 0))
            size += count
            seg_type = seg_type or str(seg.get("type", ""))
            stripes = seg.get("stripes") or []
            areas = [stripes[i] for i in range(0, len(stripes), 2)]
            for pv in areas:
                used[pv] = used.get(pv, 0) + count // max(1, len(areas))
        if "VISIBLE" in (lv.get("status") or []):
            lvs.append(LV(name=lv_name, type=(seg_type or "").split("+")[0],
                          size_bytes=size * extent))
    vg = VG(name=name, extent_bytes=extent, lvs=sorted(lvs, key=lambda x: x.name))
    for pv_name, pv in (body.get("physical_volumes") or {}).items():
        uuid = str(pv.get("id", ""))
        present = pv_present(uuid)
        vg.pvs.append(PV(name=pv_name, uuid=uuid, device_hint=pv.get("device"),
                         size_bytes=int(pv.get("pe_count", 0)) * extent,
                         used_bytes=used.get(pv_name, 0) * extent,
                         present=present,
                         device=pv_device(uuid) if pv_device and present else None))
    by_name = {p.name: p for p in vg.pvs}
    for lv in vg.lvs:
        lv.tree = lv_tree(lv.name, all_lvs, by_name, extent)
    return vg


def read_vgs(backup_dir: str = "/etc/lvm/backup", by_id_dir: str = "/dev/disk/by-id") -> list[VG]:
    """VGs from LVM's metadata backups; a backup none of whose PVs is attached
    (a VG that was removed or moved away) is ignored."""
    def present(uuid: str) -> bool:
        # The udev link itself says udev saw the PV (lexists: whatever it points at)
        return os.path.lexists(os.path.join(by_id_dir, f"lvm-pv-uuid-{uuid}"))

    def device(uuid: str) -> str | None:
        try:
            return os.path.basename(os.readlink(
                os.path.join(by_id_dir, f"lvm-pv-uuid-{uuid}")))
        except OSError:
            return None

    out = []
    d = Path(backup_dir)
    if not d.is_dir():
        return out
    for f in sorted(d.iterdir()):
        try:
            vg = vg_from_metadata(parse_lvm_metadata(f.read_text()), present, device)
        except (OSError, ValueError, IndexError) as e:
            log.warning("Cannot parse LVM metadata backup %s: %s", f, e)
            continue
        if vg is not None and any(p.present for p in vg.pvs):
            out.append(vg)
    return out


# ---------------------------------------------------------------------------
# device-mapper status
# ---------------------------------------------------------------------------


def dm_lv_name(dm_name: str) -> str:
    """``storage--big-space--1_corig`` -> ``storage-big/space-1_corig``."""
    parts = re.split(r"(?<!-)-(?!-)", dm_name, maxsplit=1)
    if len(parts) != 2:
        return dm_name
    return "/".join(p.replace("--", "-") for p in parts)


@dataclass
class DmStatus:
    raid: dict = field(default_factory=dict)  # lv -> {type, health, sync_pct, action, mismatches}
    integrity: dict = field(default_factory=dict)  # lv -> mismatches (summed per LV)
    cache: dict = field(default_factory=dict)  # lv -> {used_pct, dirty, read_hit_pct}
    thin_pool: dict = field(default_factory=dict)  # lv -> {data_pct, metadata_pct}


def _ratio(text: str) -> float | None:
    try:
        a, b = text.split("/")
        return 100.0 * int(a) / int(b) if int(b) else None
    except ValueError:
        return None


# Integrity devices are the RAID images' sub-LVs: fold them into their LV.
_INTEGRITY_SUB_RE = re.compile(r"_rimage_\d+$|_rimage_\d+_imeta$|_rimage_\d+_iorig$")


def parse_dmsetup_status(text: str) -> DmStatus:
    out = DmStatus()
    for line in text.splitlines():
        name, _, rest = line.partition(": ")
        f = rest.split()
        if len(f) < 3:
            continue
        target, args = f[2], f[3:]
        lv = dm_lv_name(name)
        if target == "raid" and len(args) >= 5:
            out.raid[lv] = {
                "type": args[0], "health": args[2], "sync_pct": _ratio(args[3]),
                "action": args[4],
                "mismatches": int(args[5]) if len(args) > 5 and args[5].isdigit() else None,
            }
        elif target == "integrity" and args and args[0].isdigit():
            base = _INTEGRITY_SUB_RE.sub("", lv)
            out.integrity[base] = out.integrity.get(base, 0) + int(args[0])
        elif target == "cache" and len(args) >= 10:
            reads = int(args[4]) + int(args[5])
            out.cache[lv] = {
                "used_pct": _ratio(args[3]),
                "dirty": int(args[10]) if len(args) > 10 and args[10].isdigit() else None,
                "read_hit_pct": 100.0 * int(args[4]) / reads if reads else None,
            }
        elif target == "thin-pool" and len(args) >= 3:
            out.thin_pool[lv] = {"metadata_pct": _ratio(args[1]), "data_pct": _ratio(args[2])}
    return out


def read_dmsetup_status() -> DmStatus:
    try:
        r = subprocess.run(["dmsetup", "status"], capture_output=True, text=True,
                           timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("dmsetup status failed: %s", e)
        return DmStatus()
    if r.returncode != 0:
        log.warning("dmsetup status failed: %s", r.stderr.strip())
        return DmStatus()
    if r.stdout.strip() == "No devices found":
        return DmStatus()
    return parse_dmsetup_status(r.stdout)


# ---------------------------------------------------------------------------
# md arrays and filesystems
# ---------------------------------------------------------------------------


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


@dataclass
class MdArray:
    name: str
    level: str | None
    state: str | None
    raid_disks: int | None
    degraded: int | None
    sync_action: str | None
    sync_pct: float | None
    mismatches: int | None
    size_bytes: int


def read_md_arrays(sysfs_root: str = "/") -> list[MdArray]:
    out = []
    for b in sorted((Path(sysfs_root) / "sys/block").glob("md*")):
        md = b / "md"
        if not md.is_dir():
            continue

        def num(n):
            v = _read(md / n)
            return int(v) if v is not None and v.isdigit() else None

        done = _read(md / "sync_completed")
        action = _read(md / "sync_action")
        sync = None
        if done and "/" in done:
            sync = _ratio(done.replace(" ", ""))
        elif action == "idle":
            sync = 100.0  # sync_completed is "none" when nothing is running
        out.append(MdArray(name=b.name, level=_read(md / "level"), state=_read(md / "array_state"),
                           raid_disks=num("raid_disks"), degraded=num("degraded"),
                           sync_action=action, sync_pct=sync,
                           mismatches=num("mismatch_cnt"),
                           size_bytes=int(_read(b / "size") or 0) * SECTOR))
    return out


FS_TYPES = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "f2fs", "zfs", "ntfs",
            "ntfs3", "jfs", "reiserfs", "nilfs2", "bcachefs"}


@dataclass
class Filesystem:
    mountpoint: str
    source: str
    fstype: str
    readonly: bool
    size_bytes: int
    used_bytes: int
    avail_bytes: int
    inodes_used_pct: float | None
    devno: str | None = None  # "253:5"

    @property
    def used_pct(self) -> float:
        total = self.used_bytes + self.avail_bytes
        return round(100.0 * self.used_bytes / total, 1) if total else 0.0


def _mountinfo(proc_root: str):
    """(devno, root, mount point, options, fstype, source) per mountinfo line."""
    text = _read(Path(proc_root) / "proc/self/mountinfo") or ""
    for line in text.splitlines():
        pre, _, post = line.partition(" - ")
        f, g = pre.split(), post.split()
        if len(f) < 6 or len(g) < 2:
            continue
        yield f[2], f[3], f[4].replace("\\040", " "), f[5], g[0], g[1]


def all_mountpoints(proc_root: str = "/") -> set[str]:
    """Every mount point, whatever is mounted there."""
    return {m[2] for m in _mountinfo(proc_root)}


def read_filesystems(proc_root: str = "/", statvfs=os.statvfs) -> list[Filesystem]:
    """Mounted block-backed filesystems, once each.

    A bind mount of a directory (mountinfo root not "/") repeats a filesystem
    already listed and is skipped, except on btrfs, where each subvolume mount
    (root "/@home") is its own tree.
    """
    out, seen = [], set()
    for devno, root, mnt, opts, fstype, source in _mountinfo(proc_root):
        if fstype not in FS_TYPES:
            continue
        key = (devno, root) if fstype == "btrfs" else devno
        if key in seen or (root != "/" and fstype != "btrfs"):
            continue
        seen.add(key)
        try:
            st = statvfs(mnt)
        except OSError as e:
            log.warning("statvfs(%s) failed: %s", mnt, e)
            continue
        inodes = None
        if st.f_files:
            inodes = round(100.0 * (st.f_files - st.f_ffree) / st.f_files, 1)
        out.append(Filesystem(mountpoint=mnt, source=source, fstype=fstype,
                              readonly="ro" in opts.split(","),
                              size_bytes=st.f_blocks * st.f_frsize,
                              used_bytes=(st.f_blocks - st.f_bfree) * st.f_frsize,
                              avail_bytes=st.f_bavail * st.f_frsize,
                              inodes_used_pct=inodes, devno=devno))
    return out


def fstab_not_mounted(fstab: str, mounted: set[str]) -> list[str]:
    """Mount points fstab mounts at boot that aren't mounted now."""
    out = []
    for line in fstab.splitlines():
        f = line.split("#", 1)[0].split()
        if len(f) < 4:
            continue
        mnt, fstype, opts = f[1], f[2], f[3].split(",")
        if fstype not in FS_TYPES or "noauto" in opts or mnt in ("none", "swap"):
            continue
        if mnt not in mounted:
            out.append(mnt)
    return out


def mount_slug(mountpoint: str) -> str:
    return slug(mountpoint) or "root"


def gb(n: int) -> float:
    return round(n / GB, 2)
