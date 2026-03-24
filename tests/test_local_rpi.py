"""Tests for RpiCollector specialization."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.local.rpi import THROTTLE_BITS, RpiCollector

FIXTURES = Path(__file__).parent / "fixtures"


def make_config():
    return MqttConfig(host="test", port=1883, user="u", password="p")


def make_rpi(fixture: str, vcgencmd_available: bool = False):
    """Create RpiCollector with mocked vcgencmd availability."""
    which_ret = "/usr/bin/vcgencmd" if vcgencmd_available else None
    with patch("sensors2mqtt.collector.local.rpi.shutil.which", return_value=which_ret):
        return RpiCollector(config=make_config(), sysfs_root=str(FIXTURES / fixture))


# ---------------------------------------------------------------------------
# Device identification
# ---------------------------------------------------------------------------


class TestDeviceInfo:
    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="rpi5-pmod")
    def test_manufacturer(self, _mock):
        c = make_rpi("rpi5_sysfs")
        assert c.device.manufacturer == "Raspberry Pi"

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="rpi5-pmod")
    def test_model_rpi5(self, _mock):
        c = make_rpi("rpi5_sysfs")
        assert "Raspberry Pi 5" in c.device.model

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="rpi4-pmod")
    def test_model_rpi4(self, _mock):
        c = make_rpi("rpi4_sysfs")
        assert "Raspberry Pi 4" in c.device.model

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="rpiz-serial")
    def test_model_rpizero(self, _mock):
        c = make_rpi("rpizero_sysfs")
        assert "Raspberry Pi Zero" in c.device.model

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="rpi5-pmod")
    def test_mac_from_eth0(self, _mock):
        c = make_rpi("rpi5_sysfs")
        assert c.device.connections == (("mac", "88:a2:9e:80:87:9b"),)

    @patch("sensors2mqtt.collector.local.base.socket.gethostname", return_value="rpiz-serial")
    def test_mac_fallback_to_wlan0(self, _mock):
        """RPi Zero has no eth0, should fall back to wlan0."""
        c = make_rpi("rpizero_sysfs")
        assert c.device.connections == (("mac", "b8:27:eb:dd:ee:ff"),)


# ---------------------------------------------------------------------------
# RPi 5 sensor probing
# ---------------------------------------------------------------------------


class TestRpi5Sensors:
    def test_has_rp1_adc_voltages(self):
        """Real RPi 5 has 4 RP1 ADC voltage channels (in1-in4)."""
        c = make_rpi("rpi5_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "rp1_v1" in suffixes
        assert "rp1_v2" in suffixes
        assert "rp1_v3" in suffixes
        assert "rp1_v4" in suffixes

    def test_has_rp1_temp(self):
        c = make_rpi("rpi5_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "rp1_temp" in suffixes

    def test_has_supply_undervoltage_alarm(self):
        """Real RPi 5 has in0_lcrit_alarm (not in0_input)."""
        c = make_rpi("rpi5_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "supply_undervoltage" in suffixes
        # No supply_voltage on RPi 5 (only alarm flag)
        assert "supply_voltage" not in suffixes

    def test_no_fan_without_active_cooler(self):
        """Real rpi5-pmod has no active cooler attached."""
        c = make_rpi("rpi5_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "fan_rpm" not in suffixes

    def test_sensor_count(self):
        """RPi 5 (real): 8 common + 4 rp1_adc voltages + 1 rp1_temp + 1 undervoltage = 14."""
        c = make_rpi("rpi5_sysfs", vcgencmd_available=False)
        assert len(c._sensors_list) == 14

    def test_sensor_count_with_vcgencmd(self):
        """RPi 5 with vcgencmd: 14 + 4 throttle bits + 1 raw = 19."""
        c = make_rpi("rpi5_sysfs", vcgencmd_available=True)
        assert len(c._sensors_list) == 19


# ---------------------------------------------------------------------------
# RPi 4 sensor probing
# ---------------------------------------------------------------------------


class TestRpi4Sensors:
    def test_has_supply_voltage(self):
        c = make_rpi("rpi4_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "supply_voltage" in suffixes

    def test_no_rp1_adc(self):
        c = make_rpi("rpi4_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "vddio_voltage" not in suffixes
        assert "rp1_temp" not in suffixes

    def test_no_fan(self):
        c = make_rpi("rpi4_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "fan_rpm" not in suffixes

    def test_sensor_count(self):
        """RPi 4: 8 common + 1 rpi_volt = 9 (no vcgencmd)."""
        c = make_rpi("rpi4_sysfs", vcgencmd_available=False)
        assert len(c._sensors_list) == 9


# ---------------------------------------------------------------------------
# RPi 3B+ sensor probing
# ---------------------------------------------------------------------------


class TestRpi3Sensors:
    def test_no_rp1_adc(self):
        c = make_rpi("rpi3_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "vddio_voltage" not in suffixes
        assert "rp1_temp" not in suffixes

    def test_no_rpi_volt(self):
        """RPi 3B+ fixture has no rpi_volt hwmon driver."""
        c = make_rpi("rpi3_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "supply_voltage" not in suffixes

    def test_no_fan(self):
        c = make_rpi("rpi3_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "fan_rpm" not in suffixes

    def test_sensor_count(self):
        """RPi 3B+: 8 common only (no RPi-specific hw sensors)."""
        c = make_rpi("rpi3_sysfs", vcgencmd_available=False)
        assert len(c._sensors_list) == 8


# ---------------------------------------------------------------------------
# RPi Zero W sensor probing
# ---------------------------------------------------------------------------


class TestRpiZeroSensors:
    def test_has_cpu_temp(self):
        c = make_rpi("rpizero_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        assert "cpu_temp" in suffixes

    def test_no_hardware_specific_sensors(self):
        c = make_rpi("rpizero_sysfs")
        suffixes = [ls.sensor.suffix for ls in c._sensors_list]
        for hw_suffix in ("vddio_voltage", "rp1_temp", "supply_voltage", "fan_rpm"):
            assert hw_suffix not in suffixes

    def test_sensor_count(self):
        """RPi Zero W: 8 common only."""
        c = make_rpi("rpizero_sysfs", vcgencmd_available=False)
        assert len(c._sensors_list) == 8

    def test_512mb_memory(self):
        c = make_rpi("rpizero_sysfs")
        values = c.poll()
        # 443816 kB ≈ 433 MB
        assert values["mem_total_mb"] == round(443816 / 1024)


# ---------------------------------------------------------------------------
# vcgencmd throttle parsing
# ---------------------------------------------------------------------------


class TestThrottleParsing:
    @patch("sensors2mqtt.collector.local.rpi.subprocess.run")
    def test_parse_no_throttle(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="throttled=0x0\n", stderr=""
        )
        c = make_rpi("rpi5_sysfs", vcgencmd_available=True)
        values = c.poll()
        assert values["throttle_under_voltage"] == "OFF"
        assert values["throttle_freq_capped"] == "OFF"
        assert values["throttle_throttled"] == "OFF"
        assert values["throttle_soft_temp"] == "OFF"
        assert values["throttle_raw"] == "0x0"

    @patch("sensors2mqtt.collector.local.rpi.subprocess.run")
    def test_parse_under_voltage(self, mock_run):
        """Bit 0 = under-voltage detected."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="throttled=0x1\n", stderr=""
        )
        c = make_rpi("rpi5_sysfs", vcgencmd_available=True)
        values = c.poll()
        assert values["throttle_under_voltage"] == "ON"
        assert values["throttle_freq_capped"] == "OFF"

    @patch("sensors2mqtt.collector.local.rpi.subprocess.run")
    def test_parse_all_current_bits(self, mock_run):
        """0xF = all 4 current-state bits set."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="throttled=0xF\n", stderr=""
        )
        c = make_rpi("rpi5_sysfs", vcgencmd_available=True)
        values = c.poll()
        assert values["throttle_under_voltage"] == "ON"
        assert values["throttle_freq_capped"] == "ON"
        assert values["throttle_throttled"] == "ON"
        assert values["throttle_soft_temp"] == "ON"

    @patch("sensors2mqtt.collector.local.rpi.subprocess.run")
    def test_parse_historical_only(self, mock_run):
        """0x50000 = historical under-voltage + throttled, no current."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="throttled=0x50000\n", stderr=""
        )
        c = make_rpi("rpi5_sysfs", vcgencmd_available=True)
        values = c.poll()
        # Current bits (0-3) are all off
        assert values["throttle_under_voltage"] == "OFF"
        assert values["throttle_freq_capped"] == "OFF"
        assert values["throttle_throttled"] == "OFF"
        assert values["throttle_soft_temp"] == "OFF"
        assert values["throttle_raw"] == "0x50000"

    @patch("sensors2mqtt.collector.local.rpi.subprocess.run")
    def test_parse_combined_current_and_historical(self, mock_run):
        """0x50005 = current under-voltage + throttled + historicals."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="throttled=0x50005\n", stderr=""
        )
        c = make_rpi("rpi5_sysfs", vcgencmd_available=True)
        values = c.poll()
        assert values["throttle_under_voltage"] == "ON"
        assert values["throttle_freq_capped"] == "OFF"
        assert values["throttle_throttled"] == "ON"
        assert values["throttle_soft_temp"] == "OFF"

    @patch("sensors2mqtt.collector.local.rpi.subprocess.run")
    def test_vcgencmd_failure_graceful(self, mock_run):
        """If vcgencmd fails, throttle values should not be in output."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        c = make_rpi("rpi5_sysfs", vcgencmd_available=True)
        values = c.poll()
        assert "throttle_under_voltage" not in values
        assert "throttle_raw" not in values
        # But other sensors should still work
        assert "cpu_temp" in values

    @patch("sensors2mqtt.collector.local.rpi.subprocess.run")
    def test_vcgencmd_timeout_graceful(self, mock_run):
        import subprocess as sp

        mock_run.side_effect = sp.TimeoutExpired(cmd="vcgencmd", timeout=5)
        c = make_rpi("rpi5_sysfs", vcgencmd_available=True)
        values = c.poll()
        assert "throttle_raw" not in values
        assert "cpu_temp" in values

    @patch("sensors2mqtt.collector.local.rpi.subprocess.run")
    def test_all_16_bit_combinations(self, mock_run):
        """Verify all 16 combinations of the 4 current throttle bits."""
        c = make_rpi("rpi5_sysfs", vcgencmd_available=True)
        for bitfield in range(16):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"throttled=0x{bitfield:x}\n",
                stderr="",
            )
            values = c.poll()
            for bit_idx, suffix, _name in THROTTLE_BITS:
                expected = "ON" if (bitfield & (1 << bit_idx)) else "OFF"
                assert values[suffix] == expected, (
                    f"bitfield=0x{bitfield:x}, {suffix}: "
                    f"expected {expected}, got {values[suffix]}"
                )


# ---------------------------------------------------------------------------
# Poll integration
# ---------------------------------------------------------------------------


class TestPollIntegration:
    def test_rpi5_poll_has_all_hw_sensors(self):
        c = make_rpi("rpi5_sysfs")
        values = c.poll()
        assert values is not None
        # RP1 ADC (4 voltage channels + temp)
        assert "rp1_v1" in values
        assert "rp1_v2" in values
        assert "rp1_v3" in values
        assert "rp1_v4" in values
        assert "rp1_temp" in values
        # rpi_volt (undervoltage alarm, not voltage reading)
        assert "supply_undervoltage" in values
        # Common
        assert "cpu_temp" in values
        assert "uptime" in values

    def test_rpi5_voltage_values_reasonable(self):
        c = make_rpi("rpi5_sysfs")
        values = c.poll()
        # Real RPi 5 PMIC rail voltages: ~1-3V range
        assert 0.5 < values["rp1_v1"] < 4.0  # 1.480V
        assert 0.5 < values["rp1_v2"] < 4.0  # 2.564V
        assert 0.5 < values["rp1_v3"] < 4.0  # 1.405V
        assert 0.5 < values["rp1_v4"] < 4.0  # 1.415V
        # Undervoltage alarm: 0 = OK
        assert values["supply_undervoltage"] == 0.0

    def test_rpizero_poll_minimal(self):
        c = make_rpi("rpizero_sysfs")
        values = c.poll()
        assert values is not None
        assert "cpu_temp" in values
        assert "uptime" in values
        # No RPi-specific hw sensors
        assert "rp1_v1" not in values
        assert "fan_rpm" not in values
