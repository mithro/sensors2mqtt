"""Tests for LocalCollector base class."""

from pathlib import Path
from unittest.mock import patch

import pytest

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.local.base import (
    LocalCollector,
    LocalSensor,
    ProcSource,
    SysfsSource,
)

FIXTURES = Path(__file__).parent / "fixtures"


def make_config():
    return MqttConfig(host="test", port=1883, user="u", password="p")


# ---------------------------------------------------------------------------
# Thermal zone probing
# ---------------------------------------------------------------------------


class TestProbeThermalZones:
    def test_rpi5_finds_cpu_thermal(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "cpu_temp" in suffixes

    def test_rpi4_finds_cpu_thermal(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi4_sysfs"))
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "cpu_temp" in suffixes

    def test_rpizero_finds_cpu_thermal(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpizero_sysfs"))
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "cpu_temp" in suffixes

    def test_mellanox_finds_mlxsw_thermal(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "mellanox_sysfs"))
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        # mlxsw type → "mlxsw_temp" suffix
        assert "mlxsw_temp" in suffixes

    def test_thermal_sensor_has_correct_unit(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        for ls in c._sensors_list:
            if ls.sensor.suffix == "cpu_temp":
                assert ls.sensor.unit == "°C"
                assert ls.sensor.device_class == "temperature"
                assert ls.sensor.state_class == "measurement"
                break
        else:
            pytest.fail("cpu_temp sensor not found")

    def test_no_thermal_dir_produces_no_temp_sensors(self, tmp_path):
        # Empty fixture with no sys/class/thermal
        (tmp_path / "proc/uptime").parent.mkdir(parents=True)
        (tmp_path / "proc/uptime").write_text("100.0 200.0\n")
        (tmp_path / "proc/meminfo").write_text("MemTotal: 1024 kB\nMemAvailable: 512 kB\n")
        (tmp_path / "proc/loadavg").write_text("0.1 0.2 0.3 1/10 100\n")
        c = LocalCollector(config=make_config(), sysfs_root=str(tmp_path))
        temp_sensors = [ls for ls in c._sensors_list if "temp" in ls.sensor.suffix]
        assert len(temp_sensors) == 0


# ---------------------------------------------------------------------------
# System diagnostics probing
# ---------------------------------------------------------------------------


class TestProbeSystemDiagnostics:
    def test_uptime_registered(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "uptime" in suffixes

    def test_memory_sensors_registered(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "mem_total_mb" in suffixes
        assert "mem_available_mb" in suffixes
        assert "mem_used_percent" in suffixes

    def test_load_averages_registered(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "load_1m" in suffixes
        assert "load_5m" in suffixes
        assert "load_15m" in suffixes

    def test_diagnostic_sensors_have_entity_category(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        for ls in c._sensors_list:
            if ls.sensor.suffix in ("uptime", "mem_total_mb", "load_1m"):
                assert ls.sensor.entity_category == "diagnostic"


# ---------------------------------------------------------------------------
# Sysfs reading
# ---------------------------------------------------------------------------


class TestReadSysfs:
    def test_read_thermal_zone_temp(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        source = SysfsSource(path="sys/class/thermal/thermal_zone0/temp", scale=0.001, precision=1)
        val = c._read_sysfs(source)
        assert val == 48.3  # 48312 * 0.001 = 48.312, rounded to 48.3

    def test_read_missing_file_returns_none(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        source = SysfsSource(path="sys/class/thermal/nonexistent/temp")
        assert c._read_sysfs(source) is None


# ---------------------------------------------------------------------------
# Proc reading
# ---------------------------------------------------------------------------


class TestReadProc:
    def test_read_uptime(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        source = ProcSource(path="proc/uptime", key="_uptime_")
        val = c._read_proc_key(source)
        assert val == 86412

    def test_read_meminfo_total(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        source = ProcSource(path="proc/meminfo", key="MemTotal", scale=1 / 1024, precision=0)
        val = c._read_proc_key(source)
        assert val == round(8108596 / 1024)

    def test_read_meminfo_available(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        source = ProcSource(path="proc/meminfo", key="MemAvailable", scale=1 / 1024, precision=0)
        val = c._read_proc_key(source)
        assert val == round(6892340 / 1024)

    def test_read_loadavg(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        source = ProcSource(path="proc/loadavg", key="_loadavg_0_", precision=2)
        val = c._read_proc_key(source)
        assert val == 0.15

    def test_read_loadavg_5m(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        source = ProcSource(path="proc/loadavg", key="_loadavg_1_", precision=2)
        val = c._read_proc_key(source)
        assert val == 0.10

    def test_read_computed_returns_none(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        source = ProcSource(path="proc/meminfo", key="_computed_")
        assert c._read_proc_key(source) is None


# ---------------------------------------------------------------------------
# MAC reading
# ---------------------------------------------------------------------------


class TestReadMac:
    def test_reads_eth0_mac(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        assert c._read_mac() == "dc:a6:32:ab:cd:ef"

    def test_rpizero_no_eth0_returns_none(self):
        """RPi Zero only has wlan0, base class only checks eth0."""
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpizero_sysfs"))
        # Base class _mac_interfaces returns ("eth0",) which doesn't exist on Zero
        assert c._read_mac() is None

    def test_mellanox_prefers_bmc(self):
        """Mellanox fixture has both bmc and eth0, but base only checks eth0."""
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "mellanox_sysfs"))
        # Base class only looks at eth0
        mac = c._read_mac()
        assert mac == "1c:34:da:42:e8:90"  # eth0, not bmc


# ---------------------------------------------------------------------------
# Device identification
# ---------------------------------------------------------------------------


class TestDeviceIdentification:
    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="rpi5-pmod")
    def test_node_id_from_hostname(self, _mock):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        assert c.device.node_id == "rpi5_pmod"

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="rpi5-pmod")
    def test_name_is_hostname(self, _mock):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        assert c.device.name == "rpi5-pmod"

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="test-host")
    def test_base_manufacturer_is_unknown(self, _mock):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        assert c.device.manufacturer == "Unknown"

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="test-host")
    def test_mac_in_connections(self, _mock):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        assert c.device.connections == (("mac", "dc:a6:32:ab:cd:ef"),)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="test")
    def test_via_device_from_config(self, _mock, tmp_path):
        config_file = tmp_path / "local.toml"
        config_file.write_text('via_device = "sensors2mqtt_sw_netgear_gsm7252ps_s1"\n')
        c = LocalCollector(
            config=make_config(),
            config_path=config_file,
            sysfs_root=str(FIXTURES / "rpi5_sysfs"),
        )
        assert c.device.via_device == "sensors2mqtt_sw_netgear_gsm7252ps_s1"

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="original")
    def test_node_id_override_from_config(self, _mock, tmp_path):
        config_file = tmp_path / "local.toml"
        config_file.write_text('node_id = "custom_id"\n')
        c = LocalCollector(
            config=make_config(),
            config_path=config_file,
            sysfs_root=str(FIXTURES / "rpi5_sysfs"),
        )
        assert c.device.node_id == "custom_id"

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="test")
    def test_missing_config_uses_defaults(self, _mock):
        c = LocalCollector(
            config=make_config(),
            config_path=Path("/nonexistent/local.toml"),
            sysfs_root=str(FIXTURES / "rpi5_sysfs"),
        )
        assert c.device.via_device is None
        assert c.device.node_id == "test"


# ---------------------------------------------------------------------------
# Poll integration
# ---------------------------------------------------------------------------


class TestPoll:
    def test_poll_returns_cpu_temp(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        values = c.poll()
        assert values is not None
        assert "cpu_temp" in values
        assert values["cpu_temp"] == 48.3

    def test_poll_returns_uptime(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        values = c.poll()
        assert values["uptime"] == 86412

    def test_poll_computes_mem_used_percent(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        values = c.poll()
        assert "mem_used_percent" in values
        total = values["mem_total_mb"]
        avail = values["mem_available_mb"]
        expected = round(100.0 * (total - avail) / total, 1)
        assert values["mem_used_percent"] == expected

    def test_poll_returns_load_averages(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        values = c.poll()
        assert values["load_1m"] == 0.15
        assert values["load_5m"] == 0.10
        assert values["load_15m"] == 0.05

    def test_rpizero_poll_works_with_512mb(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpizero_sysfs"))
        values = c.poll()
        assert values is not None
        assert values["mem_total_mb"] == round(443816 / 1024)

    def test_mellanox_poll_gets_mlxsw_temp(self):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "mellanox_sysfs"))
        values = c.poll()
        assert values is not None
        assert "mlxsw_temp" in values
        assert values["mlxsw_temp"] == 42.0

    def test_poll_all_values_have_matching_sensor(self):
        """Every value key in poll() output should match a sensor suffix."""
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        values = c.poll()
        sensor_suffixes = {ls.sensor.suffix for ls in c._sensors_list}
        for key in values:
            assert key in sensor_suffixes, f"poll() returned '{key}' but no matching sensor"


# ---------------------------------------------------------------------------
# Client ID and topics
# ---------------------------------------------------------------------------


class TestTopics:
    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="rpi5-pmod")
    def test_client_id(self, _mock):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        assert c.client_id == "sensors2mqtt-local-rpi5_pmod"

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="rpi5-pmod")
    def test_state_topic(self, _mock):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        assert c.state_topic == "sensors2mqtt/rpi5_pmod/state"

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="rpi5-pmod")
    def test_avail_topic(self, _mock):
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        assert c.avail_topic == "sensors2mqtt/rpi5_pmod/status"


# ---------------------------------------------------------------------------
# Sensor count per fixture
# ---------------------------------------------------------------------------


class TestSensorCounts:
    def test_rpi5_common_sensor_count(self):
        """RPi 5 base probe: 1 thermal + 7 diagnostics = 8 common sensors."""
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        # cpu_temp + uptime + mem_total + mem_available + mem_used_pct + load_1/5/15
        assert len(c._sensors_list) == 8

    def test_rpizero_common_sensor_count(self):
        """RPi Zero base probe: 1 thermal + 7 diagnostics = 8."""
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "rpizero_sysfs"))
        assert len(c._sensors_list) == 8

    def test_mellanox_common_sensor_count(self):
        """Mellanox base probe: 1 thermal (mlxsw) + 7 diagnostics = 8."""
        c = LocalCollector(config=make_config(), sysfs_root=str(FIXTURES / "mellanox_sysfs"))
        assert len(c._sensors_list) == 8
