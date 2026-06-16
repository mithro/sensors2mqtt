"""Tests for IPMI sensor collector."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sensors2mqtt.collector.ipmi_sensors import (
    IPMI_SENSOR_MAP,
    PSU_SENSORS,
    fetch_bmc_fru,
    parse_bmc_psu_xml,
    parse_fru_identity,
    parse_ipmi_sensors,
    poll_ipmi_sensors,
    publish_psu_discovery,
    resolve_device_identity,
)
from sensors2mqtt.discovery import EXPIRE_AFTER, DeviceInfo

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseIpmiSensors:
    def test_parse_fixture(self):
        text = (FIXTURES / "ipmitool_sdr_big_storage.txt").read_text()
        values = parse_ipmi_sensors(text)
        assert len(values) > 0
        assert "cpu1_temp" in values
        assert "cpu2_temp" in values
        assert "inlet_temp" in values
        assert "fan1_rpm" in values

    def test_fixture_values_reasonable(self):
        text = (FIXTURES / "ipmitool_sdr_big_storage.txt").read_text()
        values = parse_ipmi_sensors(text)
        # Temps between 0 and 120 °C
        for key, val in values.items():
            if key.endswith("_temp"):
                assert 0 <= val <= 120, f"{key}={val}"
            elif key.endswith("_rpm"):
                assert 0 <= val <= 30000, f"{key}={val}"

    def test_all_mapped_sensors_found(self):
        """All IPMI sensors in the map should appear in fixture output."""
        text = (FIXTURES / "ipmitool_sdr_big_storage.txt").read_text()
        values = parse_ipmi_sensors(text)
        # At least most sensors should be present (some may say "no reading")
        expected = {v[0] for v in IPMI_SENSOR_MAP.values()}
        found = set(values.keys())
        # Allow a few missing (disabled sensors, etc.)
        assert len(found) >= len(expected) * 0.8, (
            f"Only found {len(found)}/{len(expected)} sensors"
        )

    def test_no_reading_skipped(self):
        output = "CPU1 Temp        | no reading        | ns\n"
        values = parse_ipmi_sensors(output)
        assert "cpu1_temp" not in values

    def test_unknown_sensor_skipped(self):
        output = "Unknown Sensor   | 42 degrees C      | ok\n"
        values = parse_ipmi_sensors(output)
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


class TestPollIpmiSensors:
    @patch("sensors2mqtt.collector.ipmi_sensors.subprocess.run")
    def test_success(self, mock_run):
        text = (FIXTURES / "ipmitool_sdr_big_storage.txt").read_text()
        mock_run.return_value = MagicMock(returncode=0, stdout=text, stderr="")
        values = poll_ipmi_sensors("bmc.example.com", "testuser", "testpass")
        assert values is not None
        assert "cpu1_temp" in values

    @patch("sensors2mqtt.collector.ipmi_sensors.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Connection timed out")
        values = poll_ipmi_sensors("bmc.example.com", "testuser", "testpass")
        assert values is None

    @patch("sensors2mqtt.collector.ipmi_sensors.subprocess.run")
    def test_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ipmitool", timeout=30)
        values = poll_ipmi_sensors("bmc.example.com", "testuser", "testpass")
        assert values is None


class TestSensorDefinitions:
    def test_ipmi_map_suffixes_unique(self):
        suffixes = [v[0] for v in IPMI_SENSOR_MAP.values()]
        assert len(suffixes) == len(set(suffixes))

    def test_psu_sensors_value_keys_unique(self):
        keys = [s[6] for s in PSU_SENSORS]
        assert len(keys) == len(set(keys))

    def test_psu_sensors_suffixes_unique(self):
        suffixes = [s[0] for s in PSU_SENSORS]
        assert len(suffixes) == len(set(suffixes))


# `ipmitool fru` output for a Supermicro board. Note "Board Mfg Date" shares a
# prefix with "Board Mfg", and "Product Name" is empty (common on Supermicro) —
# both are realistic parser hazards.
SUPERMICRO_FRU = """\
FRU Device Description : Builtin FRU Device (ID 0)
 Chassis Type          : Other
 Chassis Part Number   : CSE-826
 Board Mfg Date        : Mon Jan  1 00:00:00 2018
 Board Mfg             : Supermicro
 Board Product         : X11DSC+
 Board Serial          : OM18AS012345
 Board Part Number     : X11DSC+
 Product Manufacturer  : Supermicro
 Product Name          :
 Product Part Number   : SYS-6029
"""


class TestParseFruIdentity:
    def test_board_fields(self):
        assert parse_fru_identity(SUPERMICRO_FRU) == ("Supermicro", "X11DSC+")

    def test_product_fallback_when_no_board_fields(self):
        output = (
            " Product Manufacturer  : Dell Inc.\n"
            " Product Name          : PowerEdge R740\n"
        )
        assert parse_fru_identity(output) == ("Dell Inc.", "PowerEdge R740")

    def test_board_preferred_over_product(self):
        output = (
            " Board Mfg             : BoardMfg\n"
            " Board Product         : BoardModel\n"
            " Product Manufacturer  : ProdMfg\n"
            " Product Name          : ProdModel\n"
        )
        assert parse_fru_identity(output) == ("BoardMfg", "BoardModel")

    def test_board_mfg_date_not_mistaken_for_board_mfg(self):
        """Exact-key match: 'Board Mfg Date' must not satisfy 'Board Mfg'."""
        output = " Board Mfg Date        : Mon Jan  1 2018\n Board Product : X11\n"
        assert parse_fru_identity(output) == (None, "X11")

    def test_empty_output(self):
        assert parse_fru_identity("") == (None, None)

    def test_no_identity_fields(self):
        assert parse_fru_identity(" Chassis Type : Other\nrandom noise\n") == (None, None)


class TestFetchBmcFru:
    @patch("sensors2mqtt.collector.ipmi_sensors.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=SUPERMICRO_FRU, stderr="")
        assert fetch_bmc_fru() == ("Supermicro", "X11DSC+")

    @patch("sensors2mqtt.collector.ipmi_sensors.subprocess.run")
    def test_failure_returns_none_pair(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no access")
        assert fetch_bmc_fru() == (None, None)

    @patch("sensors2mqtt.collector.ipmi_sensors.subprocess.run")
    def test_timeout_returns_none_pair(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ipmitool", timeout=10)
        assert fetch_bmc_fru() == (None, None)


class TestResolveDeviceIdentity:
    @patch("sensors2mqtt.collector.ipmi_sensors.fetch_bmc_fru")
    def test_uses_fru_probe(self, mock_fru):
        mock_fru.return_value = ("Dell Inc.", "PowerEdge R740")
        assert resolve_device_identity() == ("Dell Inc.", "PowerEdge R740")

    @patch("sensors2mqtt.collector.ipmi_sensors.fetch_bmc_fru")
    def test_unknown_fallback(self, mock_fru):
        mock_fru.return_value = (None, None)
        assert resolve_device_identity() == ("Unknown", "Unknown")

    @patch("sensors2mqtt.collector.ipmi_sensors.fetch_bmc_fru")
    def test_partial_fru_falls_back_per_field(self, mock_fru):
        mock_fru.return_value = ("Supermicro", None)
        assert resolve_device_identity() == ("Supermicro", "Unknown")


def test_psu_discovery_has_expire_after():
    client = MagicMock()
    device = DeviceInfo(node_id="big_storage", name="big-storage", manufacturer="x", model="y")
    psu_data = {"psus": [{"slot": 1}]}
    publish_psu_discovery(client, device, psu_data, "sensors2mqtt/big_storage/ipmi_sensors/status")
    payloads = [json.loads(c.args[1]) for c in client.publish.call_args_list]
    assert payloads and all(p.get("expire_after") == EXPIRE_AFTER for p in payloads)
    assert payloads[0]["state_topic"] == "sensors2mqtt/big_storage/ipmi_sensors/psu1/state"
