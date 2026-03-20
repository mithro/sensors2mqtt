"""Tests for hwmon collector."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.hwmon import HWMON_SENSORS, HwmonCollector

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture():
    return json.loads((FIXTURES / "sensors_j_sw_bb_25g.json").read_text())


class TestHwmonSensorDefs:
    def test_all_have_json_path(self):
        for hs in HWMON_SENSORS:
            assert len(hs.json_path) == 3
            assert all(isinstance(p, str) for p in hs.json_path)

    def test_suffixes_unique(self):
        suffixes = [hs.sensor.suffix for hs in HWMON_SENSORS]
        assert len(suffixes) == len(set(suffixes))

    def test_json_paths_exist_in_fixture(self):
        """All defined json_paths resolve to a value in the fixture data."""
        data = load_fixture()
        for hs in HWMON_SENSORS:
            chip, sensor_key, value_key = hs.json_path
            assert chip in data, f"Chip {chip} not in fixture"
            assert sensor_key in data[chip], f"Sensor {sensor_key} not in {chip}"
            assert value_key in data[chip][sensor_key], (
                f"Value {value_key} not in {chip}/{sensor_key}"
            )


class TestHwmonCollector:
    def make_collector(self):
        return HwmonCollector(config=MqttConfig(host="test", port=1883, user="u", password="p"))

    def test_device_info(self):
        c = self.make_collector()
        assert c.device.node_id == "sw_bb_25g"
        assert c.device.manufacturer == "Mellanox"

    def test_sensors_list(self):
        c = self.make_collector()
        assert len(c.sensors) == len(HWMON_SENSORS)

    def test_topics(self):
        c = self.make_collector()
        assert c.state_topic == "sensors2mqtt/sw_bb_25g/state"
        assert c.avail_topic == "sensors2mqtt/sw_bb_25g/status"

    def test_extract_values_from_fixture(self):
        c = self.make_collector()
        data = load_fixture()
        values = c._extract_values(data)
        assert "asic_temp" in values
        assert "cpu_temp" in values
        assert "board_temp" in values
        assert "fan1_rpm" in values
        assert "fan8_rpm" in values
        # All values should be rounded to 1 decimal
        for v in values.values():
            assert v == round(v, 1)

    def test_extract_values_reasonable(self):
        """Fixture values should be in reasonable ranges."""
        c = self.make_collector()
        data = load_fixture()
        values = c._extract_values(data)
        # Temps between 0 and 120 °C
        for key in ["asic_temp", "cpu_temp", "board_temp"]:
            assert 0 <= values[key] <= 120, f"{key}={values[key]}"
        # Fan RPMs between 0 and 30000
        for key in [f"fan{i}_rpm" for i in range(1, 9)]:
            assert 0 <= values[key] <= 30000, f"{key}={values[key]}"

    @patch("sensors2mqtt.collector.hwmon.subprocess.run")
    def test_poll_success(self, mock_run):
        data = load_fixture()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(data),
            stderr="",
        )
        c = self.make_collector()
        values = c.poll()
        assert values is not None
        assert len(values) == len(HWMON_SENSORS)

    @patch("sensors2mqtt.collector.hwmon.subprocess.run")
    def test_poll_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="sensors not found",
        )
        c = self.make_collector()
        values = c.poll()
        assert values is None

    @patch("sensors2mqtt.collector.hwmon.subprocess.run")
    def test_poll_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sensors", timeout=10)
        c = self.make_collector()
        values = c.poll()
        assert values is None

    @patch("sensors2mqtt.collector.hwmon.subprocess.run")
    def test_poll_bad_json(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not json",
            stderr="",
        )
        c = self.make_collector()
        values = c.poll()
        assert values is None
