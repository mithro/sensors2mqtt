"""Tests for the generic hwmon discovery engine."""
import os
from pathlib import Path

from sensors2mqtt.collector.local.hwmon import (
    discover_hwmon_sensors,
    find_hwmon_by_name,
)


def mk_hwmon(root: Path, idx: int, name: str, channels: dict, *, device: str | None = None,
             labels: dict | None = None, wwid: str | None = None, thermal_zone: str | None = None):
    """Create a fake /sys/class/hwmon/hwmonN node. `channels` maps filename->value
    (e.g. {"temp1_input": "39850"}). `device` creates a device/ symlink basename."""
    hw = root / "sys/class/hwmon" / f"hwmon{idx}"
    hw.mkdir(parents=True)
    (hw / "name").write_text(name + "\n")
    for fname, val in channels.items():
        (hw / fname).write_text(f"{val}\n")
    for fname, val in (labels or {}).items():
        (hw / fname).write_text(f"{val}\n")
    if device or thermal_zone:
        dev_target = root / "sys/devices" / (thermal_zone or device)
        if thermal_zone:
            dev_target = root / "sys/devices/virtual/thermal" / thermal_zone
        dev_target.mkdir(parents=True, exist_ok=True)
        if wwid:
            (dev_target / "wwid").write_text(wwid + "\n")
        os.symlink(dev_target, hw / "device")


def mk_thermal_zone(root: Path, idx: int, ztype: str, temp: str = "50000"):
    z = root / "sys/class/thermal" / f"thermal_zone{idx}"
    z.mkdir(parents=True)
    (z / "type").write_text(ztype + "\n")
    (z / "temp").write_text(temp + "\n")


def suffixes(sensors):
    return {s.sensor.suffix for s in sensors}


def by_suffix(sensors, suffix):
    return next(s for s in sensors if s.sensor.suffix == suffix)


class TestGenericNaming:
    def test_nvme_composite_uses_label_and_device_instance(self, tmp_path):
        mk_hwmon(tmp_path, 0, "nvme", {"temp1_input": "39850"},
                 device="nvme0", labels={"temp1_label": "Composite"})
        out = discover_hwmon_sensors(str(tmp_path), taken_suffixes=set())
        s = by_suffix(out, "nvme0_composite")
        assert s.sensor.unit == "°C"
        assert s.sensor.device_class == "temperature"
        assert s.sensor.entity_category == "diagnostic"
        assert s.source.scale == 0.001 and s.source.precision == 1

    def test_unlabeled_temps_use_kind_index(self, tmp_path):
        # 'lm75' is unregistered -> pure generic naming from device basename.
        mk_hwmon(tmp_path, 0, "lm75", {"temp1_input": "40250", "temp2_input": "53375"},
                 device="0-0048")
        assert {"0_0048_temp1", "0_0048_temp2"} <= suffixes(
            discover_hwmon_sensors(str(tmp_path), set()))

    def test_fan_kind_metadata(self, tmp_path):
        mk_hwmon(tmp_path, 0, "genericfan", {"fan1_input": "4902"}, device="fan0")
        s = by_suffix(discover_hwmon_sensors(str(tmp_path), set()), "fan0_fan1")
        assert s.sensor.unit == "RPM" and s.sensor.icon == "mdi:fan"
        assert s.source.scale == 1.0 and s.source.precision == 0

    def test_multi_instance_disambiguates(self, tmp_path):
        mk_hwmon(tmp_path, 0, "nvme", {"temp1_input": "23850"}, device="nvme0",
                 labels={"temp1_label": "Composite"})
        mk_hwmon(tmp_path, 1, "nvme", {"temp1_input": "26850"}, device="nvme1",
                 labels={"temp1_label": "Composite"})
        assert {"nvme0_composite", "nvme1_composite"} <= suffixes(
            discover_hwmon_sensors(str(tmp_path), set()))


class TestScaleOverrides:
    def test_pac1934_microvolt_scale(self, tmp_path):
        mk_hwmon(tmp_path, 0, "pac1934", {"in0_input": "3286680"}, device="0-0011")
        s = by_suffix(discover_hwmon_sensors(str(tmp_path), set()), "minipcie_p4_3v3")
        assert s.sensor.unit == "V"
        assert s.source.scale == 1e-6  # microvolts -> 3.29 V

    def test_emc1704_microvolt_scale(self, tmp_path):
        # emc1704 in0 is always mapped to supply_voltage (channels-level override);
        # the 1e-6 scale still applies regardless of device address.
        mk_hwmon(tmp_path, 0, "emc1704", {"in0_input": "12046900"}, device="0-0018")
        s = by_suffix(discover_hwmon_sensors(str(tmp_path), set()), "supply_voltage")
        assert s.sensor.unit == "V"
        assert s.source.scale == 1e-6  # microvolts, like pac1934

    def test_override_channel_is_non_diagnostic(self, tmp_path):
        # jc42 temp1 -> board_temp override carries diagnostic=False, so the
        # sensor must be a PRIMARY entity (entity_category None), not diagnostic.
        mk_hwmon(tmp_path, 0, "jc42", {"temp1_input": "29375"}, device="0-001b")
        s = by_suffix(discover_hwmon_sensors(str(tmp_path), set()), "board_temp")
        assert s.sensor.name == "Board Temperature"
        assert s.sensor.entity_category is None


class TestChannelOverrides:
    def test_ath11k_renamed_to_wifi(self, tmp_path):
        mk_hwmon(tmp_path, 0, "ath11k_hwmon", {"temp1_input": "58000"}, device="phy1")
        out = discover_hwmon_sensors(str(tmp_path), set())
        assert "wifi_temp" in suffixes(out)
        assert by_suffix(out, "wifi_temp").sensor.name == "WiFi Temperature"

    def test_drivetemp_uses_wwid(self, tmp_path):
        mk_hwmon(tmp_path, 0, "drivetemp", {"temp1_input": "17000"},
                 device="0:0:1:0", wwid="naa.5000cca273c8468f")
        assert "disk_naa_5000cca273c8468f_temp1" in suffixes(
            discover_hwmon_sensors(str(tmp_path), set()))

    def test_drivetemp_without_wwid_uses_device_basename(self, tmp_path):
        # No wwid file -> instance falls back to slug(device basename), with no
        # "disk_" prefix (the prefix is added only for the wwid-derived form).
        mk_hwmon(tmp_path, 0, "drivetemp", {"temp1_input": "31000"}, device="2:0:0:0")
        assert "2_0_0_0_temp1" in suffixes(
            discover_hwmon_sensors(str(tmp_path), set()))


class TestThermalBacked:
    def test_skips_symlinked_thermal_zone_primary(self, tmp_path):
        mk_hwmon(tmp_path, 0, "core_cluster", {"temp1_input": "67000"},
                 thermal_zone="thermal_zone0")
        assert discover_hwmon_sensors(str(tmp_path), set()) == []

    def test_skips_name_matched_thermal_zone(self, tmp_path):
        # No device symlink (fixture-style); name matches a registered thermal type.
        mk_thermal_zone(tmp_path, 0, "mlxsw", "42000")
        mk_hwmon(tmp_path, 0, "mlxsw", {"temp1_input": "42000"})
        assert discover_hwmon_sensors(str(tmp_path), set()) == []

    def test_thermal_backed_publishes_secondary_channel(self, tmp_path):
        mk_thermal_zone(tmp_path, 0, "acpitz", "27800")
        mk_hwmon(tmp_path, 0, "acpitz", {"temp1_input": "27800", "temp2_input": "29800"})
        out = suffixes(discover_hwmon_sensors(str(tmp_path), set()))
        assert "acpitz_temp2" in out
        assert "acpitz_temp1" not in out


class TestDedup:
    def test_taken_suffix_skipped(self, tmp_path):
        mk_hwmon(tmp_path, 0, "nvme", {"temp1_input": "39850"}, device="nvme0",
                 labels={"temp1_label": "Composite"})
        assert discover_hwmon_sensors(str(tmp_path), {"nvme0_composite"}) == []


class TestInstanceChannels:
    def test_same_driver_two_instances_distinct_names(self, tmp_path, monkeypatch):
        from sensors2mqtt.collector.local import hwmon

        monkeypatch.setitem(hwmon.PERIPHERAL_HWMON, "fakemon", hwmon.DriverSpec(
            instance_channels={
                "0_0011": {"in0": hwmon.ChannelSpec(suffix="rail_a", name="Rail A")},
                "0_0022": {"in0": hwmon.ChannelSpec(suffix="rail_b", name="Rail B")},
            },
        ))
        mk_hwmon(tmp_path, 0, "fakemon", {"in0_input": "1000"}, device="0-0011")
        mk_hwmon(tmp_path, 1, "fakemon", {"in0_input": "2000"}, device="0-0022")
        out = suffixes(hwmon.discover_hwmon_sensors(str(tmp_path), set()))
        assert {"rail_a", "rail_b"} <= out

    def test_instance_channels_fall_through_to_channels(self, tmp_path, monkeypatch):
        from sensors2mqtt.collector.local import hwmon

        monkeypatch.setitem(hwmon.PERIPHERAL_HWMON, "fakemon2", hwmon.DriverSpec(
            channels={"temp1": hwmon.ChannelSpec(suffix="generic_temp")},
            instance_channels={"0_0099": {"temp1": hwmon.ChannelSpec(suffix="special_temp")}},
        ))
        mk_hwmon(tmp_path, 0, "fakemon2", {"temp1_input": "40000"}, device="0-0050")
        out = suffixes(hwmon.discover_hwmon_sensors(str(tmp_path), set()))
        assert "generic_temp" in out  # no instance match -> falls through to channels


class TestTen64Naming:
    def test_pac1934_two_chips_distinct_rails(self, tmp_path):
        mk_hwmon(tmp_path, 0, "pac1934",
                 {"in0_input": "3286680", "in1_input": "3281800",
                  "in2_input": "3294000", "in3_input": "4881952"}, device="0-0011")
        mk_hwmon(tmp_path, 1, "pac1934",
                 {"in0_input": "1199504", "in1_input": "2510272",
                  "in2_input": "603656", "in3_input": "1832440"}, device="0-001a")
        s = suffixes(discover_hwmon_sensors(str(tmp_path), set()))
        assert {"minipcie_p4_3v3", "minipcie_p5_3v3", "lte_m2b_3v3", "rail_5v"} <= s
        assert {"ddr_vdd_1v2", "ddr_vpp_2v5", "ddr_vtt_0v6", "ovdd_1v8"} <= s

    def test_emc1704_names_and_skips(self, tmp_path):
        mk_hwmon(tmp_path, 0, "emc1704",
                 {"in0_input": "12046900", "in1_input": "0",
                  "temp0_input": "42375", "temp1_input": "65125",
                  "temp2_input": "34000", "temp3_input": "-128000"}, device="0-0018")
        out = discover_hwmon_sensors(str(tmp_path), set())
        s = suffixes(out)
        assert {"supply_voltage", "ls1088_die_temp", "board_temp",
                "emc1704_internal_temp"} <= s
        assert "0_0018_in1" not in s and "0_0018_temp3" not in s  # skipped
        sv = by_suffix(out, "supply_voltage")
        assert sv.sensor.entity_category is None  # primary, not diagnostic
        assert sv.source.scale == 1e-6

    def test_emc1813_phy_temps(self, tmp_path):
        mk_hwmon(tmp_path, 0, "emc1813",
                 {"temp1_input": "40250", "temp2_input": "53375", "temp3_input": "43625"},
                 device="0-004c")
        s = suffixes(discover_hwmon_sensors(str(tmp_path), set()))
        assert {"emc1813_internal_temp", "phy_eth0_3_temp", "phy_eth4_7_temp"} <= s

    def test_emc2301_fan(self, tmp_path):
        mk_hwmon(tmp_path, 0, "emc2301", {"fan1_input": "4902"}, device="0-002f")
        out = discover_hwmon_sensors(str(tmp_path), set())
        f = by_suffix(out, "fan_rpm")
        assert f.sensor.unit == "RPM" and f.sensor.entity_category is None


class TestFindHwmon:
    def test_find_by_name(self, tmp_path):
        mk_hwmon(tmp_path, 0, "nvme", {"temp1_input": "1"}, device="nvme0")
        mk_hwmon(tmp_path, 1, "emc2301", {"fan1_input": "1"}, device="0-002f")
        hw = find_hwmon_by_name(tmp_path / "sys/class/hwmon", "emc2301")
        assert hw is not None and (hw / "name").read_text().strip() == "emc2301"

    def test_find_missing_returns_none(self, tmp_path):
        (tmp_path / "sys/class/hwmon").mkdir(parents=True)
        assert find_hwmon_by_name(tmp_path / "sys/class/hwmon", "nope") is None

    def test_iter_hwmon_skips_non_directories(self, tmp_path):
        # A stray non-directory matching hwmon* must not be yielded (so callers
        # never iterdir() a file). hwmonN are always directories in real sysfs.
        from sensors2mqtt.collector.local.hwmon import iter_hwmon

        hwroot = tmp_path / "sys/class/hwmon"
        hwroot.mkdir(parents=True)
        (hwroot / "hwmon0").mkdir()
        (hwroot / "hwmon0" / "name").write_text("real\n")
        (hwroot / "hwmonbogus").write_text("stray non-dir\n")
        assert [p.name for p in iter_hwmon(hwroot)] == ["hwmon0"]
