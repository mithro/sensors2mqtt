"""Tests for the SFP/transceiver DDM probe."""
import os
from pathlib import Path

from sensors2mqtt.collector.local.sfp import _dbm, probe_sfp_hwmon


def mk_sfp(root: Path, idx: int, cage_dev: str, channels: dict):
    hw = root / "sys/class/hwmon" / f"hwmon{idx}"
    hw.mkdir(parents=True)
    (hw / "name").write_text("sfp\n")
    for f, v in channels.items():
        (hw / f).write_text(f"{v}\n")
    target = root / "sys/devices/platform" / cage_dev
    target.mkdir(parents=True, exist_ok=True)
    os.symlink(target, hw / "device")


def suffixes(pairs):
    return {sd.suffix: val for sd, val in pairs}


def test_dbm_math():
    assert _dbm(1000) == 0.0       # 1 mW -> 0 dBm
    assert _dbm(500) == -3.01      # 0.5 mW
    assert _dbm(0) == -40.0        # dark -> floor


def test_full_ddm_one_cage(tmp_path):
    mk_sfp(tmp_path, 0, "dpmac1_sfp", {
        "temp1_input": "35000", "in1_input": "3300",
        "curr1_input": "6000", "power1_input": "501", "power2_input": "398",
    })
    s = suffixes(probe_sfp_hwmon(str(tmp_path)))
    assert s["sfp_cage1_temp"] == 35.0
    assert s["sfp_cage1_vcc"] == 3.3
    assert "sfp_cage1_bias" in s
    assert "sfp_cage1_tx_power" in s and "sfp_cage1_rx_power" in s


def test_two_cages_distinct(tmp_path):
    mk_sfp(tmp_path, 0, "dpmac1_sfp", {"temp1_input": "35000"})
    mk_sfp(tmp_path, 1, "dpmac2_sfp", {"temp1_input": "40000"})
    s = suffixes(probe_sfp_hwmon(str(tmp_path)))
    assert s["sfp_cage1_temp"] == 35.0 and s["sfp_cage2_temp"] == 40.0


def test_no_sfp_node(tmp_path):
    (tmp_path / "sys/class/hwmon").mkdir(parents=True)
    assert probe_sfp_hwmon(str(tmp_path)) == []
