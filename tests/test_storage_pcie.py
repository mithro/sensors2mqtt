"""Tests for SMBIOS slot names and NVMe bays (fake sysfs trees)."""

import os
from pathlib import Path

from sensors2mqtt.collector.storage.drives import discover_drives, discover_nvme_enclosure
from sensors2mqtt.collector.storage.pcie import (
    SmbiosSlot,
    board_slot,
    nvme_bays,
    parse_smbios_slots,
)


def smbios_slot(handle: int, name: str, bus: int, devfn: int = 0) -> bytes:
    """A type 9 record (SMBIOS 2.6+: 17 bytes) and its string set."""
    rec = bytearray(0x11)
    rec[0], rec[1] = 9, 0x11
    rec[2:4] = handle.to_bytes(2, "little")
    rec[4] = 1  # designation: string 1
    rec[0x0F], rec[0x10] = bus, devfn
    return bytes(rec) + name.encode() + b"\0\0"


def smbios_table(*records: bytes) -> bytes:
    bios = bytes([0, 4, 0, 0]) + b"\0\0"  # a type 0 record with no strings
    end = bytes([127, 4, 0xFF, 0xFF]) + b"\0\0"
    return bios + b"".join(records) + end


def test_parse_smbios_slots():
    table = smbios_table(smbios_slot(8, "CPU2 SLOT1 PCI-E 3.0 X8", 0xFF),
                         smbios_slot(10, "CPU2 SLOT3 PCI-E 3.0 X16", 0xAF),
                         smbios_slot(11, "CPU1 JMEZZ1 PCI-E 3.0 X8", 0x3B, devfn=0x09))
    assert parse_smbios_slots(table) == [
        SmbiosSlot("CPU2 SLOT3 PCI-E 3.0 X16", "0000:af:00.0"),
        SmbiosSlot("CPU1 JMEZZ1 PCI-E 3.0 X8", "0000:3b:01.1")]
    assert parse_smbios_slots(b"\x09") == []


SLOTS = [SmbiosSlot("CPU2 SLOT3 PCI-E 3.0 X16", "0000:af:00.0")]
SWITCH = "sys/devices/pci0000:ae/0000:ae:00.0/0000:af:00.0"


def mk_port(root: Path, parent: str, bridge: str, secondary: int) -> Path:
    b = root / parent / bridge
    b.mkdir(parents=True)
    (b / "secondary_bus_number").write_text(f"{secondary}\n")
    devs = root / "sys/bus/pci/devices"
    devs.mkdir(parents=True, exist_ok=True)
    os.symlink(b, devs / bridge)
    return b


def mk_nvme_at(root: Path, port: Path, fn: str, ctrl: str, serial: str, ns: str) -> None:
    dev = port / fn
    (dev / "nvme" / ctrl).mkdir(parents=True)
    for f, v in (("current_link_speed", "8.0 GT/s PCIe"), ("current_link_width", "4")):
        (dev / f).write_text(v + "\n")
    c = dev / "nvme" / ctrl
    (c / "serial").write_text(serial + "\n")
    (c / "model").write_text("INTEL SSDPE2KX040T7\n")
    (c / "firmware_rev").write_text("QDV10190\n")
    os.symlink(dev, c / "device")
    cls = root / "sys/class/nvme"
    cls.mkdir(parents=True, exist_ok=True)
    os.symlink(c, cls / ctrl)
    block = root / "sys/block" / ns
    block.mkdir(parents=True)
    os.symlink(c, block / "device")
    (block / "size").write_text("100\n")
    (block / "queue").mkdir()


def mk_hotplug(root: Path, name: str, address: str, adapter: int) -> None:
    s = root / "sys/bus/pci/slots" / name
    s.mkdir(parents=True)
    (s / "address").write_text(address + "\n")
    (s / "adapter").write_text(f"{adapter}\n")


def fake_host(root: Path) -> None:
    """A switch card in CPU2 SLOT3 with a drive and an empty hot-plug port,
    and a drive on a CPU1 root port."""
    p1 = mk_port(root, SWITCH, "0000:b0:08.0", 0xB1)
    mk_port(root, SWITCH, "0000:b0:09.0", 0xB2)
    mk_nvme_at(root, p1, "0000:b1:00.0", "nvme0", "SWITCHED", "nvme0n1")
    mk_hotplug(root, "0-7", "0000:b1:00", 1)
    mk_hotplug(root, "0-8", "0000:b2:00", 0)
    # A card that is present but didn't come up: a phantom bay
    mk_port(root, SWITCH, "0000:b0:0a.0", 0xB3)
    mk_hotplug(root, "0-9", "0000:b3:00", 1)
    # A NIC in a hot-plug slot is not a bay
    mk_port(root, SWITCH, "0000:b0:0b.0", 0xB4)
    mk_hotplug(root, "0-10", "0000:b4:00", 1)
    nic = root / "sys/bus/pci/devices/0000:b4:00.0"
    nic.mkdir()
    (nic / "class").write_text("0x020000\n")
    rp = mk_port(root, "sys/devices/pci0000:17", "0000:17:00.0", 0x18)
    (rp / "numa_node").write_text("0\n")
    mk_nvme_at(root, rp, "0000:18:00.0", "nvme1", "ONBOARD", "nvme1n1")


def test_nvme_bays(tmp_path):
    fake_host(tmp_path)
    bays = nvme_bays(str(tmp_path), SLOTS)
    assert [(b.key, b.label, b.controller, b.hotplug_slot, b.occupied) for b in bays] == [
        ("0000:18:00", "CPU1 root port 17:00.0", "nvme1", None, True),
        ("0000:b1:00", "CPU2 SLOT3 PCI-E 3.0 X16 port 1", "nvme0", "0-7", True),
        ("0000:b2:00", "CPU2 SLOT3 PCI-E 3.0 X16 port 2", None, "0-8", False),
        ("0000:b3:00", "CPU2 SLOT3 PCI-E 3.0 X16 port 3", None, "0-9", True)]
    assert bays[0].link == "8.0 GT/s PCIe x4"
    # The slot number is the PCI position, not a count
    assert [b.number for b in bays] == [0x18 * 32, 0xB1 * 32, 0xB2 * 32, 0xB3 * 32]


def test_nvme_drives_are_in_one_enclosure(tmp_path):
    fake_host(tmp_path)
    by_serial = {d.serial: d for d in discover_drives(str(tmp_path), str(tmp_path), SLOTS)}
    d = by_serial["SWITCHED"]
    assert (d.enclosure, d.slot, d.slot_name, d.pcie_link) == (
        "nvme", 0xB1 * 32, "CPU2 SLOT3 PCI-E 3.0 X16 port 1", "8.0 GT/s PCIe x4")
    assert by_serial["ONBOARD"].slot == 0x18 * 32
    e = discover_nvme_enclosure("h", str(tmp_path), SLOTS)
    assert (e.id, e.logical_id, e.model) == ("nvme", "h_nvme", "NVMe")
    assert [(s.number, s.status, s.serial) for s in e.slots] == [
        (0x18 * 32, "OK", "ONBOARD"), (0xB1 * 32, "OK", "SWITCHED"),
        (0xB2 * 32, "not installed", None), (0xB3 * 32, "OK", None)]


def test_bay_numbers_survive_a_drive_leaving(tmp_path):
    fake_host(tmp_path)
    before = {b.key: b.number for b in nvme_bays(str(tmp_path), SLOTS)}
    os.unlink(tmp_path / "sys/class/nvme/nvme1")  # the onboard drive dies
    after = {b.key: b.number for b in nvme_bays(str(tmp_path), SLOTS)}
    assert "0000:18:00" not in after
    assert all(before[k] == v for k, v in after.items())


def test_card_in_a_named_slot_vmd_and_multipath(tmp_path):
    # An NVMe card straight in CPU1 SLOT2 (SMBIOS gives the NVMe's address)
    rp = mk_port(tmp_path, "sys/devices/pci0000:17", "0000:17:00.0", 0x18)
    mk_nvme_at(tmp_path, rp, "0000:18:00.0", "nvme0", "INSLOT", "nvme0n1")
    # Two drives behind Intel VMD (PCI domain 10000)
    vmd = "sys/devices/pci0000:64/0000:64:05.5/pci10000:00"
    for i, (bridge, bus) in enumerate((("10000:00:02.0", 1), ("10000:00:03.0", 2))):
        p = mk_port(tmp_path, vmd, bridge, bus)
        mk_nvme_at(tmp_path, p, f"10000:0{bus}:00.0", f"nvme{i + 1}", f"VMD{i}", f"nvme{i + 1}n1")
    slots = [SmbiosSlot("CPU1 SLOT2 PCI-E 3.0 X4", "0000:18:00.0")]
    bays = nvme_bays(str(tmp_path), slots)
    assert [(b.key, b.label, b.controller) for b in bays] == [
        ("0000:18:00", "CPU1 SLOT2 PCI-E 3.0 X4", "nvme0"),
        ("10000:01:00", "root port 00:02.0", "nvme1"),
        ("10000:02:00", "root port 00:03.0", "nvme2")]
    # Native multipath: the namespace's device is the subsystem
    subsys = tmp_path / "sys/devices/virtual/nvme-subsystem/nvme-subsys0"
    subsys.mkdir(parents=True)
    os.symlink(tmp_path / "sys/class/nvme/nvme0", subsys / "nvme0")
    (subsys / "serial").write_text("INSLOT\n")
    (subsys / "model").write_text("M\n")
    blk = tmp_path / "sys/block/nvme0n1/device"
    os.unlink(blk)
    os.symlink(subsys, blk)
    by_serial = {d.serial: d for d in discover_drives(str(tmp_path), str(tmp_path), slots)}
    assert by_serial["INSLOT"].slot_name == "CPU1 SLOT2 PCI-E 3.0 X4"


def test_board_slot_of_a_device_behind_the_slot():
    path = "/sys/devices/pci0000:3a/0000:3a:00.0/0000:3b:00.0/host0/port-0:0"
    assert board_slot(path, [SmbiosSlot("CPU1 JMEZZ1", "0000:3b:00.0")]) == "CPU1 JMEZZ1"
    assert board_slot(path, []) is None
