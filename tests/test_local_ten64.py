"""Tests for the Traverse Ten64 collector."""
import os
from pathlib import Path
from unittest.mock import patch

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.local import auto_detect
from sensors2mqtt.collector.local.ten64 import Ten64Collector


def build_ten64(root: Path):
    """Minimal ten64 fake sysfs: identity + the board chips (with device symlinks
    so the two pac1934 instances resolve)."""
    (root / "proc").mkdir(parents=True)
    (root / "proc/uptime").write_text("100.0 200.0\n")
    (root / "proc/meminfo").write_text("MemTotal: 1024 kB\nMemAvailable: 512 kB\n")
    (root / "proc/loadavg").write_text("0.1 0.2 0.3 1/10 100\n")
    dt = root / "proc/device-tree"
    dt.mkdir(parents=True)
    (dt / "model").write_text("Traverse Ten64\x00")
    eth0 = root / "sys/class/net/eth0"
    eth0.mkdir(parents=True)
    (eth0 / "address").write_text("70:b3:d5:1e:aa:bb\n")

    def chip(idx, name, channels, device):
        hw = root / "sys/class/hwmon" / f"hwmon{idx}"
        hw.mkdir(parents=True)
        (hw / "name").write_text(name + "\n")
        for f, v in channels.items():
            (hw / f).write_text(f"{v}\n")
        target = root / "sys/devices" / device
        target.mkdir(parents=True, exist_ok=True)
        os.symlink(target, hw / "device")

    chip(0, "pac1934", {"in0_input": "3286680", "in1_input": "3281800",
                        "in2_input": "3294000", "in3_input": "4881952"}, "0-0011")
    chip(1, "pac1934", {"in0_input": "1199504", "in1_input": "2510272",
                        "in2_input": "603656", "in3_input": "1832440"}, "0-001a")
    chip(2, "emc1704", {"in0_input": "12046900", "in1_input": "0",
                        "temp0_input": "42375", "temp1_input": "65125",
                        "temp2_input": "34000", "temp3_input": "-128000"}, "0-0018")
    chip(3, "emc1813", {"temp1_input": "40250", "temp2_input": "53375",
                        "temp3_input": "43625"}, "0-004c")
    chip(4, "emc2301", {"fan1_input": "5201"}, "0-002f")


def make(root):
    return Ten64Collector(
        config=MqttConfig(host="t", port=1883, user="u", password="p"),
        sysfs_root=str(root))


def test_auto_detect_ten64(tmp_path):
    build_ten64(tmp_path)
    assert auto_detect(sysfs_root=str(tmp_path)) is Ten64Collector


@patch("sensors2mqtt.base.socket.gethostname", return_value="ten64")
def test_identity(_m, tmp_path):
    build_ten64(tmp_path)
    c = make(tmp_path)
    assert c.device.manufacturer == "Traverse Technologies"
    assert c.device.model == "Ten64"


def test_full_suffix_set(tmp_path):
    build_ten64(tmp_path)
    s = {ls.sensor.suffix for ls in make(tmp_path)._sensors_list}
    assert {"minipcie_p4_3v3", "minipcie_p5_3v3", "lte_m2b_3v3", "rail_5v",
            "ddr_vdd_1v2", "ddr_vpp_2v5", "ddr_vtt_0v6", "ovdd_1v8",
            "supply_voltage", "ls1088_die_temp", "board_temp",
            "emc1813_internal_temp", "phy_eth0_3_temp", "phy_eth4_7_temp",
            "fan_rpm"} <= s
    assert "0_0018_temp3" not in s and "0_0018_in1" not in s  # dead channels skipped


def test_poll_values(tmp_path):
    build_ten64(tmp_path)
    v = make(tmp_path).poll()
    assert v["supply_voltage"] == 12.047        # 12046900 µV
    assert v["ddr_vtt_0v6"] == 0.604            # 603656 µV
    assert v["rail_5v"] == 4.882
    assert v["fan_rpm"] == 5201
    assert v["board_temp"] == 34.0
