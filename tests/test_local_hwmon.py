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
        mk_hwmon(tmp_path, 0, "emc1813", {"temp1_input": "40250", "temp2_input": "53375"},
                 device="0-004c")
        out = discover_hwmon_sensors(str(tmp_path), taken_suffixes=set())
        assert {"0_004c_temp1", "0_004c_temp2"} <= suffixes(out)

    def test_fan_kind_metadata(self, tmp_path):
        mk_hwmon(tmp_path, 0, "emc2301", {"fan1_input": "4902"}, device="0-002f")
        s = by_suffix(discover_hwmon_sensors(str(tmp_path), set()), "0_002f_fan1")
        assert s.sensor.unit == "RPM"
        assert s.sensor.icon == "mdi:fan"
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
        s = by_suffix(discover_hwmon_sensors(str(tmp_path), set()), "0_0011_in0")
        assert s.sensor.unit == "V"
        assert s.source.scale == 1e-6  # microvolts, not the ABI millivolts

    def test_emc1704_microvolt_scale(self, tmp_path):
        mk_hwmon(tmp_path, 0, "emc1704", {"in0_input": "3286680"}, device="2-0067")
        s = by_suffix(discover_hwmon_sensors(str(tmp_path), set()), "2_0067_in0")
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


class TestFindHwmon:
    def test_find_by_name(self, tmp_path):
        mk_hwmon(tmp_path, 0, "nvme", {"temp1_input": "1"}, device="nvme0")
        mk_hwmon(tmp_path, 1, "emc2301", {"fan1_input": "1"}, device="0-002f")
        hw = find_hwmon_by_name(tmp_path / "sys/class/hwmon", "emc2301")
        assert hw is not None and (hw / "name").read_text().strip() == "emc2301"

    def test_find_missing_returns_none(self, tmp_path):
        (tmp_path / "sys/class/hwmon").mkdir(parents=True)
        assert find_hwmon_by_name(tmp_path / "sys/class/hwmon", "nope") is None
