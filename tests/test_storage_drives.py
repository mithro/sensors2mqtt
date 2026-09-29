"""Tests for passive drive discovery (fake sysfs trees)."""

import os
from pathlib import Path

from sensors2mqtt.collector.storage.drives import (
    discover_drives,
    discover_enclosures,
    dm_display_name,
    read_diskstats,
)


def ata_identify(serial: str, model: str, firmware: str) -> bytes:
    """VPD page 0x89: 60-byte header then IDENTIFY DEVICE (byte-swapped strings)."""
    ident = bytearray(512)

    def put(word: int, text: str, words: int):
        raw = text.ljust(words * 2).encode()
        for i in range(0, len(raw), 2):
            ident[word * 2 + i] = raw[i + 1]
            ident[word * 2 + i + 1] = raw[i]

    put(10, serial, 10)
    put(23, firmware, 4)
    put(27, model, 20)
    return bytes(60) + bytes(ident)


def vpd80(serial: str) -> bytes:
    s = serial.encode()
    return bytes([0, 0x80, 0, len(s)]) + s


HBA = "sys/devices/pci0000:3a/0000:3b:00.0/host0/port-0:0/expander-0:0"


def mk_sas_attached(root: Path, name: str, port: int, phy: int, *, ata=None, serial=None,
                    model="ST10000NM0226", rev="KTB5", slot=None, size=19532873728):
    """A disk behind a SAS expander, optionally in an enclosure slot."""
    port_dir = root / HBA / f"port-0:0:{port}"
    (port_dir / f"phy-0:0:{phy}").mkdir(parents=True)
    phy_class = root / "sys/class/sas_phy" / f"phy-0:0:{phy}"
    phy_class.mkdir(parents=True)
    for f, v in (("negotiated_linkrate", "12.0 Gbit"), ("invalid_dword_count", "3"),
                 ("running_disparity_error_count", "2"), ("loss_of_dword_sync_count", "1"),
                 ("phy_reset_problem_count", "0")):
        (phy_class / f).write_text(v + "\n")
    dev = port_dir / f"end_device-0:0:{port}/target0:0:{port}/0:0:{port}:0"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text("ATA     \n" if ata else "SEAGATE \n")
    (dev / "model").write_text(model[:16] + "\n")
    (dev / "rev").write_text(rev + "\n")
    if ata:
        (dev / "vpd_pg89").write_bytes(ata_identify(*ata))
    else:
        (dev / "vpd_pg80").write_bytes(vpd80(serial))
    block = root / "sys/block" / name
    block.mkdir(parents=True)
    os.symlink(dev, block / "device")
    (block / "size").write_text(f"{size}\n")
    (block / "dev").write_text("8:0\n")
    (block / "queue").mkdir()
    (block / "queue/logical_block_size").write_text("512\n")
    (block / "queue/rotational").write_text("1\n")
    (block / "holders").mkdir()
    if slot is not None:
        encl, number = slot
        slot_dir = root / "sys/devices/encl" / "enclosure" / encl / f"Slot{number + 1:02d}"
        slot_dir.mkdir(parents=True)
        (slot_dir / "slot").write_text(f"{number}\n")
        (slot_dir / "status").write_text("OK\n")
        (slot_dir / "fault").write_text("0\n")
        (slot_dir / "locate").write_text("0\n")
        os.symlink(dev, slot_dir / "device")
        os.symlink(slot_dir, dev / f"enclosure_device:Slot{number + 1:02d}")
        encl_dev = slot_dir.parent / "device"
        if not encl_dev.exists():
            encl_dev.mkdir()
            (encl_dev / "vendor").write_text("LSI-F   \n")
            (encl_dev / "model").write_text("SAS3x48Front    \n")
        (slot_dir.parent / "id").write_text("0x500304801f06a3ff\n")
        cls = root / "sys/class/enclosure"
        cls.mkdir(parents=True, exist_ok=True)
        if not (cls / encl).exists():
            os.symlink(slot_dir.parent, cls / encl)
    return block


def mk_nvme(root: Path, name="nvme0n1", ctrl="nvme0", serial="PHLF8094009G4P0IGN   "):
    c = root / "sys/devices/pci/nvme" / ctrl
    c.mkdir(parents=True)
    (c / "serial").write_text(serial + "\n")
    (c / "model").write_text("INTEL SSDPE2KX040T7\n")
    (c / "firmware_rev").write_text("QDV10190\n")
    block = root / "sys/block" / name
    block.mkdir(parents=True)
    os.symlink(c, block / "device")
    (block / "size").write_text("7814037168\n")
    (block / "dev").write_text("259:0\n")
    (block / "queue").mkdir()
    (block / "queue/logical_block_size").write_text("512\n")
    (block / "queue/rotational").write_text("0\n")
    (block / "holders").mkdir()
    return block


def test_sata_behind_expander_uses_identify_data(tmp_path):
    mk_sas_attached(tmp_path, "sdn", 17, 16, ata=("ZHZ598DY", "ST12000NM0008-2H3101", "SN03"),
                    model="ST12000NM0008-2H", rev="SN03", slot=("0:0:29:0", 16))
    (d,) = discover_drives(str(tmp_path), str(tmp_path))
    assert (d.serial, d.model, d.firmware, d.transport) == (
        "ZHZ598DY", "ST12000NM0008-2H3101", "SN03", "SATA")
    assert d.vendor is None
    assert (d.enclosure, d.enclosure_model, d.slot, d.slot_status) == (
        "0:0:29:0", "SAS3x48Front", 16, "OK")
    assert d.phy["link_rate"] == "12.0 Gbit"
    assert d.phy["invalid_dwords"] == 3
    assert d.capacity_bytes == 19532873728 * 512


def test_sas_drive_uses_vpd_serial(tmp_path):
    mk_sas_attached(tmp_path, "sdb", 3, 2, serial="ZA2910WX0000C905S5J8")
    (d,) = discover_drives(str(tmp_path), str(tmp_path))
    assert (d.serial, d.transport, d.vendor, d.firmware) == (
        "ZA2910WX0000C905S5J8", "SAS", "SEAGATE", "KTB5")
    assert d.enclosure is None


def test_nvme_and_holders(tmp_path):
    block = mk_nvme(tmp_path)
    part = block / "nvme0n1p1"
    (part / "holders").mkdir(parents=True)
    (part / "partition").write_text("1\n")
    (part / "dev").write_text("259:1\n")
    dm = tmp_path / "sys/block/dm-3/dm"
    dm.mkdir(parents=True)
    (dm / "name").write_text("storage--big-root--debian_rimage_0_iorig\n")
    (part / "holders/dm-3").mkdir()
    (tmp_path / "proc/self").mkdir(parents=True)
    (tmp_path / "proc/self/mountinfo").write_text(
        "22 1 259:1 / /boot/efi rw,relatime - vfat /dev/nvme0n1p1 rw\n")
    (d,) = discover_drives(str(tmp_path), str(tmp_path))
    assert (d.serial, d.transport, d.firmware) == ("PHLF8094009G4P0IGN", "NVMe", "QDV10190")
    assert d.used_by == "storage-big/root-debian, /boot/efi"


def test_skips_virtual_and_serial_less_devices(tmp_path):
    (tmp_path / "sys/block/loop0").mkdir(parents=True)
    (tmp_path / "sys/block/dm-0/device").mkdir(parents=True)
    mk_sas_attached(tmp_path, "sdz", 5, 4, serial="")
    assert discover_drives(str(tmp_path), str(tmp_path)) == []


def test_dm_display_name():
    assert dm_display_name("storage--big-space1cache_cvol_rimage_0_imeta") == \
        "storage-big/space1cache"
    assert dm_display_name("storage--more-space") == "storage-more/space"
    assert dm_display_name("cryptroot") == "cryptroot"


def test_enclosure_slots(tmp_path):
    mk_sas_attached(tmp_path, "sdn", 17, 16, ata=("ZHZ598DY", "ST12000NM0008-2H3101", "SN03"),
                    slot=("0:0:29:0", 16))
    encl = tmp_path / "sys/class/enclosure/0:0:29:0"
    empty = Path(os.path.realpath(encl)) / "Slot01"
    empty.mkdir()
    (empty / "slot").write_text("0\n")
    (empty / "status").write_text("not installed\n")
    (e,) = discover_enclosures(str(tmp_path))
    assert (e.id, e.logical_id, e.model) == ("0:0:29:0", "0x500304801f06a3ff", "SAS3x48Front")
    assert [(s.number, s.status, s.serial) for s in e.slots] == [
        (0, "not installed", None), (16, "OK", "ZHZ598DY")]


def test_diskstats(tmp_path):
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/diskstats").write_text(
        "   8      16 sdb 100 5 2000 30 200 6 4000 40 1 70 90 0 0 0 0\n"
        "   8      17 sdb1 1 0 2 0 0 0 0 0 0 0 0\n")
    s = read_diskstats(str(tmp_path))
    assert (s["sdb"].reads, s["sdb"].sectors_read, s["sdb"].writes,
            s["sdb"].sectors_written, s["sdb"].in_flight, s["sdb"].io_ms) == (
        100, 2000, 200, 4000, 1, 70)
