"""Tests for MellanoxCollector (refactored onto the generic hwmon engine)."""
from pathlib import Path
from unittest.mock import patch

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.local.mellanox import MellanoxCollector

FIXTURES = Path(__file__).parent / "fixtures"


def make_mellanox():
    return MellanoxCollector(config=MqttConfig(host="t", port=1883, user="u", password="p"),
                             sysfs_root=str(FIXTURES / "mellanox_sysfs"))


class TestMellanoxDeviceInfo:
    @patch("sensors2mqtt.base.socket.gethostname", return_value="sw-bb-25g")
    def test_identity(self, _m):
        c = make_mellanox()
        assert c.device.node_id == "sw_bb_25g"
        assert c.device.manufacturer == "Mellanox"
        assert c.device.model == "SN2410"


class TestMellanoxSensors:
    def test_preserved_suffixes(self):
        sensors = {ls.sensor.suffix: ls.sensor for ls in make_mellanox()._sensors_list}
        assert {"asic_temp", "board_temp"} <= set(sensors)
        assert {f"fan{i}_rpm" for i in range(1, 9)} <= set(sensors)
        # Preserved primary sensors stay non-diagnostic (continuity with sensors -j).
        assert sensors["asic_temp"].entity_category is None
        assert sensors["board_temp"].entity_category is None

    def test_front_panel_module_temps_generic(self):
        # Per-port transceiver temps publish generically; #41 owns sfp_portNN naming + DDM.
        s = {ls.sensor.suffix for ls in make_mellanox()._sensors_list}
        assert "mlxsw_front_panel_001" in s
        assert "mlxsw_front_panel_056" in s

    def test_poll_reads_from_sysfs(self):
        v = make_mellanox().poll()
        assert v["asic_temp"] == 42.0
        assert v["board_temp"] == round(29375 * 0.001, 1)
        assert v["fan1_rpm"] == 6239
        assert v["mlxsw_front_panel_001"] == 0.0  # empty cage

    def test_cpu_temp_from_thermal_zone(self):
        # acpitz thermal zone -> acpitz_temp (not cpu_temp on this box)
        s = {ls.sensor.suffix for ls in make_mellanox()._sensors_list}
        assert "acpitz_temp" in s
