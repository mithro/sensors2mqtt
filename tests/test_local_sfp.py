"""Tests for the SFP/transceiver DDM probe."""
import os
from pathlib import Path

from sensors2mqtt.collector.local.sfp import (
    _dbm,
    parse_ethtool_ddm,
    probe_sfp_hwmon,
    probe_sfp_mlxsw,
)


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
    assert s["sfp_cage1_tx_power"] == _dbm(501)   # power1_input -> TX optical power (dBm)
    assert s["sfp_cage1_rx_power"] == _dbm(398)   # power2_input -> RX optical power (dBm)


def test_two_cages_distinct(tmp_path):
    mk_sfp(tmp_path, 0, "dpmac1_sfp", {"temp1_input": "35000"})
    mk_sfp(tmp_path, 1, "dpmac2_sfp", {"temp1_input": "40000"})
    s = suffixes(probe_sfp_hwmon(str(tmp_path)))
    assert s["sfp_cage1_temp"] == 35.0 and s["sfp_cage2_temp"] == 40.0


def test_no_sfp_node(tmp_path):
    (tmp_path / "sys/class/hwmon").mkdir(parents=True)
    assert probe_sfp_hwmon(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# Mellanox mlxsw backend tests (Task 3)
# ---------------------------------------------------------------------------

ETHTOOL_SAMPLE = """\
\tModule temperature                        : 35.00 degrees C / 95.00 degrees F
\tModule voltage                            : 3.3000 V
\tLaser bias current                        : 6.000 mA
\tLaser output power                        : 0.5012 mW / -2.99 dBm
\tReceiver signal average optical power      : 0.4000 mW / -3.98 dBm
"""


def mk_mlxsw(root: Path, ports_with_module: dict[int, int]):
    """ports_with_module: {port -> temp_milli_c}; those get crit!=0 (DDM present)."""
    hw = root / "sys/class/hwmon/hwmon1"
    hw.mkdir(parents=True)
    (hw / "name").write_text("mlxsw\n")
    for n in range(2, 58):
        port = n - 1
        present = port in ports_with_module
        (hw / f"temp{n}_input").write_text(f"{ports_with_module.get(port, 0)}\n")
        (hw / f"temp{n}_crit").write_text(("90000" if present else "0") + "\n")


def test_parse_ethtool_ddm():
    d = parse_ethtool_ddm(ETHTOOL_SAMPLE)
    assert d["temp"] == 35.0 and d["vcc"] == 3.3
    assert d["bias"] == 6.0
    assert d["tx_power"] == -2.99 and d["rx_power"] == -3.98


def test_mlxsw_populated_port_full_ddm(tmp_path):
    mk_mlxsw(tmp_path, {1: 36000})
    s = suffixes(probe_sfp_mlxsw(str(tmp_path), ethtool=lambda iface: ETHTOOL_SAMPLE))
    assert s["sfp_port01_temp"] == 36.0
    assert s["sfp_port01_vcc"] == 3.3 and s["sfp_port01_tx_power"] == -2.99


def test_mlxsw_empty_port_skipped(tmp_path):
    mk_mlxsw(tmp_path, {})  # no DDM modules (all crit=0)
    assert probe_sfp_mlxsw(str(tmp_path), ethtool=lambda iface: "") == []


def test_mlxsw_ethtool_failure_temp_only(tmp_path):
    mk_mlxsw(tmp_path, {1: 36000})
    s = suffixes(probe_sfp_mlxsw(str(tmp_path), ethtool=lambda iface: ""))
    assert s["sfp_port01_temp"] == 36.0
    assert "sfp_port01_vcc" not in s  # ethtool gave nothing -> temp only
