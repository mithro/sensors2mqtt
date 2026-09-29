"""Tests for reading smartd's JSON state files (captured on big-storage)."""

import json
import shutil
from pathlib import Path

import pytest

from sensors2mqtt.collector.storage.smartd import (
    load_smartd_states,
    parse_device_info,
    parse_smartd_json,
    slug,
)

FIXTURES = Path(__file__).parent / "fixtures/storage/smartd"


def load(name: str) -> dict:
    (path,) = FIXTURES.glob(f"*{name}*")
    return json.loads(path.read_text())


def sensor(sd, suffix):
    return next(s for s in sd.sensors if s.suffix == suffix)


class TestDeviceInfo:
    def test_ata(self):
        assert parse_device_info(
            "HGST HUH721010ALE600, S/N:4DGZ4LBZ, WWN:5-000cca-2a2f6d17c, FW:LHGNTB01, 10.0 TB"
        ) == ("4DGZ4LBZ", "HGST HUH721010ALE600", "LHGNTB01")

    def test_scsi(self):
        assert parse_device_info(
            "[SEAGATE  ST10000NM0226    KTB5], lu id: 0x5000c500a6c9acb3, "
            "S/N: ZA2910WX0000C905S5J8, 9.93 TB"
        ) == ("ZA2910WX0000C905S5J8", "SEAGATE ST10000NM0226", "KTB5")

    def test_nvme(self):
        assert parse_device_info(
            "INTEL SSDPE2KX040T7, S/N:PHLF8094009G4P0IGN, FW:QDV10190, 4.00 TB"
        ) == ("PHLF8094009G4P0IGN", "INTEL SSDPE2KX040T7", "QDV10190")

    def test_no_serial(self):
        assert parse_device_info("something")[0] is None


class TestAta:
    def test_hdd_with_device_statistics(self):
        sd = parse_smartd_json(load("HGST_HUH721010ALE600"))
        assert sd.serial == "4DGZ4LBZ" and sd.firmware == "LHGNTB01"
        v = sd.values
        assert v["smart_status"] == "PASSED"
        assert v["power_on_hours"] == v["devstat_power_on_hours"]
        assert v["power_cycles"] == v["devstat_lifetime_power_on_resets"]
        # Lifetime written from the Device Statistics sector count
        assert v["lifetime_written"] == pytest.approx(
            v["devstat_logical_sectors_written"] * 512 / 1e9, abs=0.001)
        assert v["temperature"] == v["devstat_current_temperature"]
        assert "ata_9_power_on_hours" in v and "ata_194_temperature_celsius" in v
        assert sensor(sd, "power_on_hours").state_class == "total_increasing"
        assert sensor(sd, "ata_9_power_on_hours_value").enabled_by_default is False

    def test_temperature_raw_is_the_leading_number(self):
        # Attribute 194's raw value packs min/max; raw.string's first number is
        # the current temperature.
        sd = parse_smartd_json(load("HGST_HUH721010ALE600"))
        assert 0 < sd.values["ata_194_temperature_celsius"] < 100

    def test_old_drive_without_device_statistics(self):
        sd = parse_smartd_json(load("WDC_WD20EARS"))
        assert sd.serial == "WD-WCAZA1451226"
        assert not any(k.startswith("devstat_") for k in sd.values)
        assert sd.values["power_on_hours"] == sd.values["ata_9_power_on_hours"]
        assert sd.values["pending_sectors"] == sd.values["ata_197_current_pending_sector"]
        # Counts that fall when sectors are remapped are not totals
        assert sensor(sd, "pending_sectors").state_class == "measurement"
        assert sensor(sd, "ata_197_current_pending_sector").state_class == "measurement"
        assert sensor(sd, "ata_5_reallocated_sector_ct").state_class == "total_increasing"

    def test_sector_size_for_4kn_drives(self):
        doc = load("HGST_HUH721010ALE600")
        sd512 = parse_smartd_json(doc, 512)
        sd4k = parse_smartd_json(doc, 4096)
        assert sd4k.values["lifetime_written"] == pytest.approx(
            sd512.values["lifetime_written"] * 8, rel=1e-6)

    def test_ssd(self):
        sd = parse_smartd_json(load("Samsung_SSD_850_PRO"))
        assert sd.values["smart_status"] == "PASSED"
        assert "power_on_hours" in sd.values


class TestScsi:
    def test_lifetime_counters(self):
        sd = parse_smartd_json(load("SEAGATE-ST10000NM0226"))
        v = sd.values
        assert sd.serial == "ZA2910WX0000C905S5J8" and sd.firmware == "KTB5"
        assert v["manufactured"] == "2018-W34"
        assert v["power_on_hours"] > 40000
        assert v["scsi_load_unload_cycles"] > 0
        assert v["scsi_grown_defects"] == 0
        assert v["scsi_read_total_uncorrected_errors"] == 0
        assert v["lifetime_read"] == v["scsi_read_gigabytes_processed"]

    def test_phy_counters(self):
        sd = parse_smartd_json(load("OOS12000G"))
        v = sd.values
        assert v["sas_port0_phy0_link_rate"] == "phy enabled; 12 Gbps"
        assert v["sas_port0_phy0_invalid_dword_count"] >= 0
        assert "sas_port1_phy1_phy_reset_problem_count" in v


class TestNvme:
    def test_health_log(self):
        sd = parse_smartd_json(load("INTEL_SSDPE2KX040T7"))
        v = sd.values
        assert v["nvme_percentage_used"] >= 0
        assert v["power_on_hours"] > 0
        assert v["lifetime_written"] > v["lifetime_read"] > 0
        assert sensor(sd, "lifetime_written").device_class == "data_size"
        assert v["temperature"] == 36  # from the health log


class TestAll:
    @pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
    def test_well_formed(self, path):
        sd = parse_smartd_json(json.loads(path.read_text()))
        suffixes = [s.suffix for s in sd.sensors]
        assert len(suffixes) == len(set(suffixes)), "duplicate suffixes"
        assert set(suffixes) == set(sd.values)
        for s in sd.sensors:
            assert s.suffix == slug(s.suffix)
            numeric = isinstance(sd.values[s.suffix], (int, float))
            # HA refuses a state_class on a non-numeric sensor
            assert s.state_class is None or numeric, s.suffix
        assert sd.values["smart_updated"].endswith("+00:00")

    def test_load_keys_by_serial(self, tmp_path):
        for f in FIXTURES.glob("*.json"):
            shutil.copy(f, tmp_path)
        states = load_smartd_states(str(tmp_path / "smartd-json.*.json"))
        assert set(states) == {
            "4DGZ4LBZ", "WD-WCAZA1451226", "S2BBNWAJ125495K", "ZA2910WX0000C905S5J8",
            "000481ZR0000C915RACQ", "PHLF8094009G4P0IGN"}

    def test_unreadable_file_is_skipped(self, tmp_path):
        (tmp_path / "smartd-json.bad.ata.json").write_text("{not json")
        assert load_smartd_states(str(tmp_path / "smartd-json.*.json")) == {}
