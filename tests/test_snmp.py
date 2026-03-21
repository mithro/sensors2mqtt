"""Tests for SNMP collector."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from sensors2mqtt.collector.snmp import (
    GSM7252PS_S2,
    M4300_24X,
    S3300_1,
    SWITCHES,
    SnmpCollector,
    parse_snmpget_value,
    parse_snmpwalk,
    snmpget_value,
)

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseSnmpgetValue:
    def test_integer(self):
        assert parse_snmpget_value("iso.3.6.1... = INTEGER: 42") == "42"

    def test_gauge32(self):
        assert parse_snmpget_value("iso.3.6.1... = Gauge32: 1234") == "1234"

    def test_string_quoted(self):
        assert parse_snmpget_value('iso.3.6.1... = STRING: "5280"') == "5280"

    def test_no_match(self):
        assert parse_snmpget_value("No Such Object") is None

    def test_empty_value(self):
        assert parse_snmpget_value('iso.3.6.1... = STRING: ""') is None


class TestParseSnmpwalk:
    def test_parses_gauge32(self):
        output = (
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.1 = Gauge32: 3300\n"
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.2 = Gauge32: 2500\n"
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.3 = Gauge32: 0\n"
        )
        result = parse_snmpwalk(output)
        assert result == [(1, "3300"), (2, "2500"), (3, "0")]

    def test_parses_integer(self):
        output = "iso.3.6.1.4.1.4526.10.43.1.15.1.3.1 = INTEGER: 65\n"
        result = parse_snmpwalk(output)
        assert result == [(1, "65")]

    def test_empty_output(self):
        assert parse_snmpwalk("") == []

    def test_fixture_m4300_fans(self):
        text = (FIXTURES / "snmpwalk_m4300_fans.txt").read_text()
        result = parse_snmpwalk(text)
        assert len(result) > 0
        # Fan speed entries are STRING values like "5280"
        speeds = [(idx, val) for idx, val in result if val.isdigit() and int(val) > 100]
        assert len(speeds) >= 2  # Two fans

    def test_fixture_gsm7252ps_poe(self):
        text = (FIXTURES / "snmpwalk_gsm7252ps_poe.txt").read_text()
        result = parse_snmpwalk(text)
        assert len(result) > 0
        # Should have entries for 48 ports
        indices = {idx for idx, _ in result}
        assert len(indices) >= 48

    def test_fixture_s3300_fans(self):
        text = (FIXTURES / "snmpwalk_s3300_fans.txt").read_text()
        result = parse_snmpwalk(text)
        assert len(result) > 0
        speeds = [(idx, val) for idx, val in result if val.isdigit() and int(val) > 100]
        assert len(speeds) >= 3  # Three fans

    def test_fixture_s3300_poe(self):
        text = (FIXTURES / "snmpwalk_s3300_poe.txt").read_text()
        result = parse_snmpwalk(text)
        assert len(result) > 0
        indices = {idx for idx, _ in result}
        assert len(indices) >= 48


class TestSnmpgetValue:
    def test_int(self):
        assert snmpget_value("42", "int", 1.0) == 42.0

    def test_string_int(self):
        assert snmpget_value("5280", "string_int", 1.0) == 5280

    def test_float(self):
        assert snmpget_value("42.5", "float", 1.0) == 42.5

    def test_scale(self):
        assert abs(snmpget_value("3300", "int", 0.001) - 3.3) < 0.001

    def test_none_input(self):
        assert snmpget_value(None, "int", 1.0) is None

    def test_non_numeric(self):
        assert snmpget_value("No Such Instance", "int", 1.0) is None


class TestSwitchDefinitions:
    def test_m4300_has_sensors(self):
        assert len(M4300_24X.sensors) >= 4
        suffixes = {s.suffix for s in M4300_24X.sensors}
        assert "fan1_rpm" in suffixes
        assert "fan2_rpm" in suffixes
        assert "temp" in suffixes
        assert "psu_power" in suffixes

    def test_m4300_uses_hostname(self):
        assert M4300_24X.node_id == "sw_netgear_m4300_24x"
        assert M4300_24X.name == "sw-netgear-m4300-24x"
        assert "welland.mithis.com" in M4300_24X.host

    def test_gsm7252ps_has_walk_sensors(self):
        assert len(GSM7252PS_S2.walk_sensors) >= 1
        walk = GSM7252PS_S2.walk_sensors[0]
        assert "poe" in walk.suffix_template
        assert walk.min_index == 1
        assert walk.max_index == 48

    def test_gsm7252ps_uses_hostname(self):
        assert GSM7252PS_S2.node_id == "sw_netgear_gsm7252ps_s2"
        assert GSM7252PS_S2.name == "sw-netgear-gsm7252ps-s2"
        assert "welland.mithis.com" in GSM7252PS_S2.host

    def test_s3300_has_sensors(self):
        assert len(S3300_1.sensors) >= 5
        suffixes = {s.suffix for s in S3300_1.sensors}
        assert "fan1_rpm" in suffixes
        assert "fan2_rpm" in suffixes
        assert "fan3_rpm" in suffixes
        assert "temp" in suffixes
        assert "psu_power" in suffixes

    def test_s3300_has_walk_sensors(self):
        assert len(S3300_1.walk_sensors) >= 1
        walk = S3300_1.walk_sensors[0]
        assert "poe" in walk.suffix_template

    def test_s3300_uses_hostname(self):
        assert S3300_1.node_id == "sw_netgear_s3300_1"
        assert S3300_1.name == "sw-netgear-s3300-1"
        assert "welland.mithis.com" in S3300_1.host

    def test_s3300_uses_dot11_oids(self):
        """S3300 uses 4526.11 (Smart Managed Pro), not 4526.10."""
        for sensor in S3300_1.sensors:
            assert ".4526.11." in sensor.oid, (
                f"{sensor.suffix} OID should use .4526.11.: {sensor.oid}"
            )
        for walk in S3300_1.walk_sensors:
            assert ".4526.11." in walk.base_oid

    def test_switches_list(self):
        assert len(SWITCHES) == 3
        ids = {s.node_id for s in SWITCHES}
        assert "sw_netgear_m4300_24x" in ids
        assert "sw_netgear_gsm7252ps_s2" in ids
        assert "sw_netgear_s3300_1" in ids

    def test_all_hosts_use_dns(self):
        """All switches must use DNS hostnames, not hardcoded IPs."""
        for switch in SWITCHES:
            assert not switch.host[0].isdigit(), (
                f"{switch.name} uses IP address {switch.host} instead of DNS hostname"
            )


class TestSnmpCollector:
    def make_collector(self, switches=None):
        from sensors2mqtt.base import MqttConfig
        config = MqttConfig(host="test", port=1883, user="u", password="p")
        return SnmpCollector(config=config, switches=switches or [M4300_24X])

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_switch_success(self, mock_run):
        """Successful snmpget returns values."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='iso.3.6.1... = STRING: "5280"\n',
            stderr="",
        )
        collector = self.make_collector()
        values = collector.poll_switch(M4300_24X)
        assert values is not None
        assert len(values) > 0

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_switch_all_fail(self, mock_run):
        """All snmpget failures return None."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Timeout",
        )
        collector = self.make_collector()
        values = collector.poll_switch(M4300_24X)
        assert values is None

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_switch_timeout(self, mock_run):
        """Subprocess timeout is handled gracefully."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="snmpget", timeout=10)
        collector = self.make_collector()
        values = collector.poll_switch(M4300_24X)
        assert values is None

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_switch_partial_failure(self, mock_run):
        """Some sensors failing doesn't prevent others from succeeding."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(returncode=1, stdout="", stderr="Timeout")
            return MagicMock(
                returncode=0,
                stdout='iso.3.6.1... = INTEGER: 65\n',
                stderr="",
            )

        mock_run.side_effect = side_effect
        collector = self.make_collector()
        values = collector.poll_switch(M4300_24X)
        # Should have values from the sensors that succeeded
        assert values is not None
        assert len(values) < len(M4300_24X.sensors)

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_walk_switch(self, mock_run):
        """Walk-based sensors parse correctly."""
        poe_output = (
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.1 = Gauge32: 3300\n"
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.2 = Gauge32: 2500\n"
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.5 = Gauge32: 5600\n"
        )
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=poe_output,
            stderr="",
        )
        collector = self.make_collector(switches=[GSM7252PS_S2])
        values = collector.poll_switch(GSM7252PS_S2)
        assert values is not None
        assert "port1_poe_mw" in values
        assert values["port1_poe_mw"] == 3300.0
        assert "port5_poe_mw" in values
        assert values["port5_poe_mw"] == 5600.0

    def test_get_sensors_for_switch_static(self):
        collector = self.make_collector()
        values = {"fan1_rpm": 5280, "fan2_rpm": 4560, "temp": 65, "psu_power": 65}
        sensors = collector.get_sensors_for_switch(M4300_24X, values)
        assert len(sensors) == len(M4300_24X.sensors)
        suffixes = {s.suffix for s in sensors}
        assert "fan1_rpm" in suffixes

    def test_get_sensors_for_switch_dynamic(self):
        collector = self.make_collector(switches=[GSM7252PS_S2])
        values = {"port1_poe_mw": 3300, "port5_poe_mw": 5600}
        sensors = collector.get_sensors_for_switch(GSM7252PS_S2, values)
        assert len(sensors) == 2
        names = {s.name for s in sensors}
        assert "Port 1 PoE Power" in names
        assert "Port 5 PoE Power" in names

    def test_topics(self):
        collector = self.make_collector()
        assert collector.state_topic(M4300_24X) == "sensors2mqtt/sw_netgear_m4300_24x/state"
        assert collector.avail_topic(M4300_24X) == "sensors2mqtt/sw_netgear_m4300_24x/status"
