"""Tests for MellanoxCollector specialization.

Verifies backwards compatibility: same sensor suffixes, same node_id
derivation from hostname, same sensors -j extraction as old hwmon.py.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.local.mellanox import MELLANOX_SENSORS, MellanoxCollector

FIXTURES = Path(__file__).parent / "fixtures"


def load_sensors_json_fixture():
    return json.loads((FIXTURES / "sensors_j_sw_bb_25g.json").read_text())


def make_config():
    return MqttConfig(host="test", port=1883, user="u", password="p")


def make_mellanox():
    return MellanoxCollector(
        config=make_config(),
        sysfs_root=str(FIXTURES / "mellanox_sysfs"),
    )


# ---------------------------------------------------------------------------
# Sensor definitions
# ---------------------------------------------------------------------------


class TestMellanoxSensorDefs:
    def test_all_have_json_path(self):
        for sensor_def, (chip, sensor_key, value_key) in MELLANOX_SENSORS:
            assert isinstance(chip, str)
            assert isinstance(sensor_key, str)
            assert isinstance(value_key, str)

    def test_suffixes_unique(self):
        suffixes = [sd.suffix for sd, _ in MELLANOX_SENSORS]
        assert len(suffixes) == len(set(suffixes))

    def test_json_paths_exist_in_fixture(self):
        """All defined json_paths resolve to a value in the fixture data."""
        data = load_sensors_json_fixture()
        for sensor_def, (chip, sensor_key, value_key) in MELLANOX_SENSORS:
            assert chip in data, f"Chip {chip} not in fixture"
            assert sensor_key in data[chip], f"Sensor {sensor_key} not in {chip}"
            assert value_key in data[chip][sensor_key], (
                f"Value {value_key} not in {chip}/{sensor_key}"
            )

    def test_backwards_compatible_suffixes(self):
        """The old hwmon.py had these exact suffixes — they must be preserved."""
        expected = {
            "asic_temp", "board_temp",
            "fan1_rpm", "fan2_rpm", "fan3_rpm", "fan4_rpm",
            "fan5_rpm", "fan6_rpm", "fan7_rpm", "fan8_rpm",
        }
        actual = {sd.suffix for sd, _ in MELLANOX_SENSORS}
        assert expected == actual

    def test_cpu_temp_not_in_mellanox_sensors(self):
        """cpu_temp comes from base class thermal zone, not sensors -j."""
        suffixes = {sd.suffix for sd, _ in MELLANOX_SENSORS}
        assert "cpu_temp" not in suffixes


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------


class TestMellanoxDeviceInfo:
    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="sw-bb-25g")
    def test_node_id(self, _mock):
        c = make_mellanox()
        assert c.device.node_id == "sw_bb_25g"

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="sw-bb-25g")
    def test_manufacturer(self, _mock):
        c = make_mellanox()
        assert c.device.manufacturer == "Mellanox"

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="sw-bb-25g")
    def test_model(self, _mock):
        c = make_mellanox()
        assert c.device.model == "SN2410"

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="sw-bb-25g")
    def test_mac_prefers_bmc(self, _mock):
        """Mellanox has bmc and eth0 — should prefer bmc."""
        c = make_mellanox()
        assert c.device.connections == (("mac", "1c:34:da:42:e8:8c"),)

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="sw-bb-25g")
    def test_backwards_compatible_topics(self, _mock):
        c = make_mellanox()
        assert c.state_topic == "sensors2mqtt/sw_bb_25g/state"
        assert c.avail_topic == "sensors2mqtt/sw_bb_25g/status"


# ---------------------------------------------------------------------------
# Sensor extraction from sensors -j fixture
# ---------------------------------------------------------------------------


class TestMellanoxExtraction:
    @patch("sensors2mqtt.collector.local.mellanox.subprocess.run")
    def test_poll_extracts_all_sensors(self, mock_run):
        data = load_sensors_json_fixture()
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(data), stderr=""
        )
        c = make_mellanox()
        values = c.poll()
        assert values is not None
        assert "asic_temp" in values
        assert "board_temp" in values
        for i in range(1, 9):
            assert f"fan{i}_rpm" in values

    @patch("sensors2mqtt.collector.local.mellanox.subprocess.run")
    def test_poll_values_reasonable(self, mock_run):
        data = load_sensors_json_fixture()
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(data), stderr=""
        )
        c = make_mellanox()
        values = c.poll()
        # Temps between 0 and 120 °C
        assert 0 <= values["asic_temp"] <= 120
        assert 0 <= values["board_temp"] <= 120
        # Fan RPMs between 0 and 30000
        for i in range(1, 9):
            assert 0 <= values[f"fan{i}_rpm"] <= 30000

    @patch("sensors2mqtt.collector.local.mellanox.subprocess.run")
    def test_poll_values_rounded_to_1dp(self, mock_run):
        data = load_sensors_json_fixture()
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(data), stderr=""
        )
        c = make_mellanox()
        values = c.poll()
        for suffix in ("asic_temp", "board_temp"):
            assert values[suffix] == round(values[suffix], 1)

    @patch("sensors2mqtt.collector.local.mellanox.subprocess.run")
    def test_sensors_failure_still_returns_base_sensors(self, mock_run):
        """If sensors -j fails, base sensors (sysfs/proc) still work."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="sensors not found"
        )
        c = make_mellanox()
        values = c.poll()
        assert values is not None
        # Base sensors from sysfs fixture should still be present
        assert "mlxsw_temp" in values  # from thermal_zone0
        assert "uptime" in values
        assert "mem_total_mb" in values
        # But sensors -j values should be absent
        assert "asic_temp" not in values

    @patch("sensors2mqtt.collector.local.mellanox.subprocess.run")
    def test_sensors_timeout(self, mock_run):
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sensors", timeout=10)
        c = make_mellanox()
        values = c.poll()
        assert values is not None
        # Base sensors still work
        assert "mlxsw_temp" in values

    @patch("sensors2mqtt.collector.local.mellanox.subprocess.run")
    def test_sensors_bad_json(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="not json", stderr=""
        )
        c = make_mellanox()
        values = c.poll()
        assert values is not None
        assert "mlxsw_temp" in values


# ---------------------------------------------------------------------------
# Common sensors inherited from base
# ---------------------------------------------------------------------------


class TestMellanoxCommonSensors:
    def test_has_system_diagnostics(self):
        c = make_mellanox()
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "uptime" in suffixes
        assert "mem_total_mb" in suffixes
        assert "load_1m" in suffixes

    def test_has_thermal_zone(self):
        """Mellanox thermal zone is 'mlxsw', not 'cpu-thermal'."""
        c = make_mellanox()
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "mlxsw_temp" in suffixes

    def test_total_sensor_count(self):
        """8 common + 10 Mellanox-specific = 18 total."""
        c = make_mellanox()
        assert len(c._sensors_list) == 18
