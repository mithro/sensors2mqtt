"""Where a PCIe device sits: the motherboard slot names from SMBIOS, and bays.

SMBIOS (DMI) type 9 "System Slot" records name each slot as it is printed on
the board ("CPU2 SLOT3 PCI-E 3.0 X16") with the PCI address of the device in
it. The table is only readable by root; the storage service gets a copy as a
systemd credential (``LoadCredential=smbios:/sys/firmware/dmi/tables/DMI``).

An NVMe "bay" is a PCIe port that has an NVMe controller behind it, or a
hot-plug capable port (``/sys/bus/pci/slots/*/adapter``) that could have one:
the ports of a PCIe switch card or a U.2 backplane. Nothing here touches a
drive: it is all sysfs and the firmware's own tables.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_PCI_ADDR_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")


@dataclass(frozen=True)
class SmbiosSlot:
    designation: str  # "CPU2 SLOT3 PCI-E 3.0 X16"
    address: str  # PCI address of the device in the slot, "0000:af:00.0"


def parse_smbios_slots(table: bytes) -> list[SmbiosSlot]:
    """The System Slot (type 9) records of a raw SMBIOS structure table."""
    out = []
    pos = 0
    while pos + 4 <= len(table):
        stype, length = table[pos], table[pos + 1]
        if length < 4:
            break
        end = table.find(b"\0\0", pos + length)
        if end < 0:
            break
        strings = table[pos + length:end].split(b"\0")
        if stype == 9 and length >= 0x11:
            s = table[pos:pos + length]
            idx = s[4]
            name = (strings[idx - 1].decode("ascii", "replace").strip()
                    if 0 < idx <= len(strings) else "")
            segment = int.from_bytes(s[0x0D:0x0F], "little")
            bus, devfn = s[0x0F], s[0x10]
            # 0xff bus: the slot is empty or the firmware doesn't say
            if name and bus != 0xFF:
                out.append(SmbiosSlot(name, f"{segment:04x}:{bus:02x}:"
                                            f"{devfn >> 3:02x}.{devfn & 7}"))
        if stype == 127:
            break
        pos = end + 2
    return out


def read_smbios_slots(sysfs_root: str = "/") -> list[SmbiosSlot]:
    """SMBIOS slots from the systemd credential, else the kernel's copy (root)."""
    paths = []
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred:
        paths.append(Path(cred) / "smbios")
    paths.append(Path(sysfs_root) / "sys/firmware/dmi/tables/DMI")
    for p in paths:
        try:
            return parse_smbios_slots(p.read_bytes())
        except OSError:
            continue
    return []


def _pci_chain(dev_real: str) -> list[str]:
    """PCI addresses from the root port down to the device (a sysfs path)."""
    return [p for p in Path(dev_real).parts if _PCI_ADDR_RE.match(p)]


def board_slot(dev_real: str, slots: list[SmbiosSlot]) -> str | None:
    """The SMBIOS name of the motherboard slot a device is (behind) in."""
    by_addr = {s.address: s.designation for s in slots}
    for addr in reversed(_pci_chain(dev_real)):
        if addr in by_addr:
            return by_addr[addr]
    return None


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def port_label(port_real: str, slots: list[SmbiosSlot], sysfs_root: str = "/") -> str:
    """A name for the PCIe port (a bridge, as a sysfs path) a device hangs off.

    ``CPU2 SLOT3 PCI-E 3.0 X16 port 1`` for a port of a switch card in a named
    slot (ports numbered in PCI order from 1); ``CPU1 root port 17:00.0`` for
    a CPU port with no slot record (onboard connectors, risers).
    """
    chain = _pci_chain(port_real)
    if not chain:
        return "PCIe"
    port = chain[-1]
    slot = board_slot(port_real, slots)
    if slot and len(chain) > 1:
        # The switch's downstream ports are the bridges beside this one
        siblings = sorted(c.name for c in Path(port_real).parent.iterdir()
                          if _PCI_ADDR_RE.match(c.name)
                          and (c / "secondary_bus_number").exists())
        n = siblings.index(port) + 1 if port in siblings else 0
        return f"{slot} port {n}" if n else slot
    if slot:
        return slot
    numa = _read(Path(sysfs_root) / "sys/bus/pci/devices" / port / "numa_node")
    cpu = f"CPU{int(numa) + 1} " if numa and numa.lstrip("-").isdigit() and int(numa) >= 0 \
        else ""
    return f"{cpu}root port {port[5:]}"


@dataclass
class Bay:
    key: str  # PCI "domain:bus:device" of the device position, "0000:b1:00"
    label: str
    controller: str | None  # NVMe controller in it ("nvme0"), None if empty
    link: str | None  # "8.0 GT/s PCIe x4"
    hotplug_slot: str | None  # /sys/bus/pci/slots name


def _link(dev: Path) -> str | None:
    speed, width = _read(dev / "current_link_speed"), _read(dev / "current_link_width")
    if not speed or not width or speed.startswith("Unknown"):
        return None
    return f"{speed} x{width}"


def nvme_bays(sysfs_root: str = "/", slots: list[SmbiosSlot] | None = None) -> list[Bay]:
    """Every NVMe controller's PCIe position plus every empty hot-plug port."""
    root = Path(sysfs_root)
    if slots is None:
        slots = read_smbios_slots(sysfs_root)
    bays: dict[str, Bay] = {}
    cls = root / "sys/class/nvme"
    for ctrl in sorted(cls.iterdir()) if cls.is_dir() else []:
        dev = Path(os.path.realpath(ctrl / "device"))
        chain = _pci_chain(str(dev))
        if not chain:
            continue  # NVMe over fabrics: no PCIe position
        key = chain[-1].rsplit(".", 1)[0]
        port = str(dev.parent) if len(chain) > 1 else str(dev)
        bays[key] = Bay(key, port_label(port, slots, sysfs_root), ctrl.name, _link(dev), None)
    pci_slots = root / "sys/bus/pci/slots"
    for s in sorted(pci_slots.iterdir()) if pci_slots.is_dir() else []:
        addr = _read(s / "address")
        if not addr or not (s / "adapter").exists():
            continue
        if addr in bays:
            bays[addr].hotplug_slot = s.name
            continue
        if _read(s / "adapter") == "1":
            continue  # something other than an NVMe drive is in it
        bridge = _bridge_to(root, addr)
        label = port_label(bridge, slots, sysfs_root) if bridge else f"hot-plug slot {s.name}"
        bays[addr] = Bay(addr, label, None, None, s.name)
    return [bays[k] for k in sorted(bays)]


def _bridge_to(root: Path, addr: str) -> str | None:
    """sysfs path of the bridge whose secondary bus is ``addr``'s bus."""
    domain, bus = addr.split(":")[0], int(addr.split(":")[1], 16)
    devs = root / "sys/bus/pci/devices"
    for d in sorted(devs.iterdir()) if devs.is_dir() else []:
        if not d.name.startswith(domain):
            continue
        sec = _read(d / "secondary_bus_number")
        if sec is not None and sec.isdigit() and int(sec) == bus:
            return os.path.realpath(d)
    return None
