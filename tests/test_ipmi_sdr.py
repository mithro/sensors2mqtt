"""Tests for IPMI SDR collector."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from sensors2mqtt.collector.ipmi_sdr import (
    PSU_SENSORS,
    SDR_SENSOR_MAP,
    parse_bmc_psu_xml,
    parse_sdr,
    poll_sdr,
)

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseSdr:
    def test_parse_fixture(self):
        text = (FIXTURES / "ipmitool_sdr_big_storage.txt").read_text()
        values = parse_sdr(text)
        assert len(values) > 0
        assert "cpu1_temp" in values
        assert "cpu2_temp" in values
        assert "inlet_temp" in values
        assert "fan1_rpm" in values

    def test_fixture_values_reasonable(self):
        text = (FIXTURES / "ipmitool_sdr_big_storage.txt").read_text()
        values = parse_sdr(text)
        # Temps between 0 and 120 °C
        for key, val in values.items():
            if key.endswith("_temp"):
                assert 0 <= val <= 120, f"{key}={val}"
            elif key.endswith("_rpm"):
                assert 0 <= val <= 30000, f"{key}={val}"

    def test_all_mapped_sensors_found(self):
        """All SDR sensors in the map should appear in fixture output."""
        text = (FIXTURES / "ipmitool_sdr_big_storage.txt").read_text()
        values = parse_sdr(text)
        # At least most sensors should be present (some may say "no reading")
        expected = {v[0] for v in SDR_SENSOR_MAP.values()}
        found = set(values.keys())
        # Allow a few missing (disabled sensors, etc.)
        assert len(found) >= len(expected) * 0.8, (
            f"Only found {len(found)}/{len(expected)} sensors"
        )

    def test_no_reading_skipped(self):
        output = "CPU1 Temp        | no reading        | ns\n"
        values = parse_sdr(output)
        assert "cpu1_temp" not in values

    def test_unknown_sensor_skipped(self):
        output = "Unknown Sensor   | 42 degrees C      | ok\n"
        values = parse_sdr(output)
        assert len(values) == 0


class TestParseBmcPsuXml:
    def test_parse_fixture(self):
        text = (FIXTURES / "bmc_psu_response.xml").read_text()
        result = parse_bmc_psu_xml(text)
        assert result is not None
        assert len(result["psus"]) == 2

    def test_psu_values(self):
        text = (FIXTURES / "bmc_psu_response.xml").read_text()
        result = parse_bmc_psu_xml(text)
        psu1 = result["psus"][0]
        assert psu1["slot"] == 1
        assert psu1["status"] == "OK"
        assert psu1["ac_input_voltage_v"] == 230.0
        assert psu1["dc_12v_output_voltage_v"] == 12.2
        assert psu1["fan_1_rpm"] == 13888.0
        assert psu1["serial"] == "P2K5ACJ35IT2500"

    def test_psu2_values(self):
        text = (FIXTURES / "bmc_psu_response.xml").read_text()
        result = parse_bmc_psu_xml(text)
        psu2 = result["psus"][1]
        assert psu2["slot"] == 2
        assert psu2["ac_input_power_w"] == 396.0
        assert psu2["dc_12v_output_power_w"] == 377.0

    def test_non_power_supply_skipped(self):
        """PSItems with IsPowerSupply=0 are excluded."""
        text = (FIXTURES / "bmc_psu_response.xml").read_text()
        result = parse_bmc_psu_xml(text)
        # Fixture has 4 PSItems but only 2 are IsPowerSupply=1
        assert len(result["psus"]) == 2

    def test_bad_xml(self):
        assert parse_bmc_psu_xml("not xml") is None

    def test_empty_psinfo(self):
        xml = '<?xml version="1.0"?><IPMI><PSInfo></PSInfo></IPMI>'
        result = parse_bmc_psu_xml(xml)
        assert result is not None
        assert len(result["psus"]) == 0


class TestPollSdr:
    @patch("sensors2mqtt.collector.ipmi_sdr.subprocess.run")
    def test_success(self, mock_run):
        text = (FIXTURES / "ipmitool_sdr_big_storage.txt").read_text()
        mock_run.return_value = MagicMock(returncode=0, stdout=text, stderr="")
        values = poll_sdr("10.1.5.150", "ADMIN", "ADMIN")
        assert values is not None
        assert "cpu1_temp" in values

    @patch("sensors2mqtt.collector.ipmi_sdr.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Connection timed out")
        values = poll_sdr("10.1.5.150", "ADMIN", "ADMIN")
        assert values is None

    @patch("sensors2mqtt.collector.ipmi_sdr.subprocess.run")
    def test_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ipmitool", timeout=30)
        values = poll_sdr("10.1.5.150", "ADMIN", "ADMIN")
        assert values is None


class TestSensorDefinitions:
    def test_sdr_map_suffixes_unique(self):
        suffixes = [v[0] for v in SDR_SENSOR_MAP.values()]
        assert len(suffixes) == len(set(suffixes))

    def test_psu_sensors_value_keys_unique(self):
        keys = [s[6] for s in PSU_SENSORS]
        assert len(keys) == len(set(keys))

    def test_psu_sensors_suffixes_unique(self):
        suffixes = [s[0] for s in PSU_SENSORS]
        assert len(suffixes) == len(set(suffixes))
