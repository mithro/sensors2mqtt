"""Tests for SNMP collector."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from sensors2mqtt.collector.snmp import (
    MODELS,
    SnmpCollector,
    SwitchConfig,
    load_config,
    parse_lldp_walk,
    parse_snmpget_value,
    parse_snmpwalk,
    snmpget_value,
)

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_FILE = Path(__file__).parent.parent / "snmp.toml"


def _make_switch(name: str, model_name: str) -> SwitchConfig:
    """Helper to build a SwitchConfig from model for tests."""
    model = MODELS[model_name]
    return SwitchConfig(
        node_id=name.replace("-", "_"),
        name=name,
        host=f"{name}.test",
        community="public",
        manufacturer=model.manufacturer,
        model=model.model,
        sensors=list(model.sensors),
        walk_sensors=list(model.walk_sensors),
    )


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
        speeds = [(idx, val) for idx, val in result if val.isdigit() and int(val) > 100]
        assert len(speeds) >= 2

    def test_fixture_gsm7252ps_poe(self):
        text = (FIXTURES / "snmpwalk_gsm7252ps_poe.txt").read_text()
        result = parse_snmpwalk(text)
        assert len(result) > 0
        indices = {idx for idx, _ in result}
        assert len(indices) >= 48

    def test_fixture_s3300_fans(self):
        text = (FIXTURES / "snmpwalk_s3300_fans.txt").read_text()
        result = parse_snmpwalk(text)
        assert len(result) > 0
        speeds = [(idx, val) for idx, val in result if val.isdigit() and int(val) > 100]
        assert len(speeds) >= 3

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


class TestModelDefinitions:
    def test_m4300_model(self):
        m = MODELS["m4300"]
        assert m.manufacturer == "Netgear"
        assert len(m.sensors) >= 4
        suffixes = {s.suffix for s in m.sensors}
        assert "fan1_rpm" in suffixes
        assert "fan2_rpm" in suffixes
        assert "temp" in suffixes
        assert "psu_power" in suffixes
        # M4300 has no PoE
        assert len(m.walk_sensors) == 0

    def test_gsm7252ps_model(self):
        m = MODELS["gsm7252ps"]
        assert len(m.walk_sensors) >= 1
        walk = m.walk_sensors[0]
        assert "poe" in walk.suffix_template
        # GSM7252PS has no boxServices
        assert len(m.sensors) == 0

    def test_s3300_model(self):
        m = MODELS["s3300"]
        assert len(m.sensors) >= 5
        suffixes = {s.suffix for s in m.sensors}
        assert "fan3_rpm" in suffixes  # 3 fans
        # S3300 has both boxServices AND PoE
        assert len(m.walk_sensors) >= 1

    def test_s3300_uses_dot11_oids(self):
        """S3300 uses 4526.11 (Smart Managed Pro), not 4526.10."""
        m = MODELS["s3300"]
        for sensor in m.sensors:
            assert ".4526.11." in sensor.oid, (
                f"{sensor.suffix} OID should use .4526.11.: {sensor.oid}"
            )
        for walk in m.walk_sensors:
            assert ".4526.11." in walk.base_oid

    def test_m4300_uses_dot10_oids(self):
        m = MODELS["m4300"]
        for sensor in m.sensors:
            assert ".4526.10." in sensor.oid


class TestConfigLoading:
    def test_load_real_config(self):
        """Load the actual snmp.toml shipped with the repo."""
        switches = load_config(CONFIG_FILE)
        assert len(switches) == 3
        names = {s.name for s in switches}
        assert "sw-netgear-m4300-24x" in names
        assert "sw-netgear-gsm7252ps-s2" in names
        assert "sw-netgear-s3300-1" in names

    def test_config_node_ids(self):
        switches = load_config(CONFIG_FILE)
        ids = {s.node_id for s in switches}
        assert "sw_netgear_m4300_24x" in ids

    def test_config_hosts_are_dns(self):
        switches = load_config(CONFIG_FILE)
        for sw in switches:
            assert not sw.host[0].isdigit(), (
                f"{sw.name} uses IP {sw.host} instead of DNS hostname"
            )

    def test_config_sensors_populated(self):
        """Config loading should populate sensors from model definitions."""
        switches = load_config(CONFIG_FILE)
        by_name = {s.name: s for s in switches}
        m4300 = by_name["sw-netgear-m4300-24x"]
        assert len(m4300.sensors) >= 4
        assert len(m4300.walk_sensors) == 0
        s3300 = by_name["sw-netgear-s3300-1"]
        assert len(s3300.sensors) >= 5
        assert len(s3300.walk_sensors) >= 1

    def test_load_missing_config_raises(self):
        """Explicit path that doesn't exist should raise FileNotFoundError."""
        import pytest
        with pytest.raises(FileNotFoundError):
            load_config(Path("/nonexistent/path.toml"))

    def test_builtin_defaults(self):
        """When no config file found via default paths, builtins are used."""
        from sensors2mqtt.collector.snmp import _builtin_defaults
        switches = _builtin_defaults()
        assert len(switches) == 3


class TestVlanNameLookup:
    def test_fixture_gsm7252ps_vlan_names(self):
        """VLAN name fixture parses correctly."""
        text = (FIXTURES / "snmpwalk_gsm7252ps_vlan_names.txt").read_text()
        from sensors2mqtt.collector.snmp import parse_snmpwalk
        result = parse_snmpwalk(text)
        names = {idx: val for idx, val in result}
        assert names[1] == "default"
        assert names[90] == "iot"
        assert names[121] == "t-fpgas"
        assert len(names) >= 10

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_fetch_vlan_names(self, mock_run):
        text = (FIXTURES / "snmpwalk_gsm7252ps_vlan_names.txt").read_text()
        mock_run.return_value = MagicMock(returncode=0, stdout=text, stderr="")
        from sensors2mqtt.base import MqttConfig
        config = MqttConfig(host="test", port=1883, user="u", password="p")
        sw = _make_switch("test-gsm7252ps", "gsm7252ps")
        collector = SnmpCollector(config=config, switches=[sw])
        names = collector.fetch_vlan_names(sw)
        assert names[90] == "iot"
        assert names[1] == "default"
        # Second call should use cache
        names2 = collector.fetch_vlan_names(sw)
        assert names2 is names
        assert mock_run.call_count == 1


class TestLldpParsing:
    def test_parse_lldp_walk_sysname(self):
        text = (FIXTURES / "snmpwalk_gsm7252ps_lldp_sysname.txt").read_text()
        result = parse_lldp_walk(text, "9")
        # Port 1 → rpi5-pmod (from OID ...9.150.1.21)
        assert result[1] == "rpi5-pmod"
        # Port 50 → sw-netgear-s3300-1 (from OID ...9.44.50.1)
        assert result[50] == "sw-netgear-s3300-1"
        # Port 49 → sw-netgear-m4300-24x
        assert result[49] == "sw-netgear-m4300-24x"
        assert len(result) >= 8

    def test_parse_lldp_walk_portdesc(self):
        text = (FIXTURES / "snmpwalk_gsm7252ps_lldp_portdesc.txt").read_text()
        result = parse_lldp_walk(text, "8")
        # Port 1 → eth0 (rpi5-pmod's interface)
        assert result[1] == "eth0"
        # Port 50 → 1/xg51 (S3300 uplink port)
        assert result[50] == "1/xg51"

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_fetch_lldp_neighbors(self, mock_run):
        sysname_text = (FIXTURES / "snmpwalk_gsm7252ps_lldp_sysname.txt").read_text()
        portdesc_text = (FIXTURES / "snmpwalk_gsm7252ps_lldp_portdesc.txt").read_text()

        def side_effect(*args, **kwargs):
            cmd = args[0]
            oid = cmd[-1]
            if oid.endswith(".9"):
                return MagicMock(returncode=0, stdout=sysname_text, stderr="")
            elif oid.endswith(".8"):
                return MagicMock(returncode=0, stdout=portdesc_text, stderr="")
            return MagicMock(returncode=1, stdout="", stderr="unknown OID")

        mock_run.side_effect = side_effect
        from sensors2mqtt.base import MqttConfig
        config = MqttConfig(host="test", port=1883, user="u", password="p")
        sw = _make_switch("test-gsm7252ps", "gsm7252ps")
        collector = SnmpCollector(config=config, switches=[sw])
        neighbors = collector.fetch_lldp_neighbors(sw)
        # Port 1 should be "rpi5-pmod / eth0"
        assert "rpi5-pmod" in neighbors[1]
        assert "eth0" in neighbors[1]
        # Port 49 should be "sw-netgear-m4300-24x / trunk.gsm7252ps-s1"
        assert "sw-netgear-m4300-24x" in neighbors[49]
        # Cached on second call
        neighbors2 = collector.fetch_lldp_neighbors(sw)
        assert neighbors2 is neighbors


class TestSnmpCollector:
    def make_collector(self, switches=None):
        from sensors2mqtt.base import MqttConfig
        config = MqttConfig(host="test", port=1883, user="u", password="p")
        if switches is None:
            switches = [_make_switch("test-m4300", "m4300")]
        return SnmpCollector(config=config, switches=switches)

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_switch_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='iso.3.6.1... = STRING: "5280"\n',
            stderr="",
        )
        sw = _make_switch("test-m4300", "m4300")
        collector = self.make_collector(switches=[sw])
        values = collector.poll_switch(sw)
        assert values is not None
        assert len(values) > 0

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_switch_all_fail(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Timeout")
        sw = _make_switch("test-m4300", "m4300")
        collector = self.make_collector(switches=[sw])
        values = collector.poll_switch(sw)
        assert values is None

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_switch_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="snmpget", timeout=10)
        sw = _make_switch("test-m4300", "m4300")
        collector = self.make_collector(switches=[sw])
        values = collector.poll_switch(sw)
        assert values is None

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_switch_partial_failure(self, mock_run):
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(returncode=1, stdout="", stderr="Timeout")
            return MagicMock(returncode=0, stdout='iso.3.6.1... = INTEGER: 65\n', stderr="")

        mock_run.side_effect = side_effect
        sw = _make_switch("test-m4300", "m4300")
        collector = self.make_collector(switches=[sw])
        values = collector.poll_switch(sw)
        assert values is not None
        assert len(values) < len(sw.sensors)

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_walk_switch(self, mock_run):
        poe_output = (
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.1 = Gauge32: 3300\n"
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.2 = Gauge32: 2500\n"
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.5 = Gauge32: 5600\n"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=poe_output, stderr="")
        sw = _make_switch("test-gsm7252ps", "gsm7252ps")
        collector = self.make_collector(switches=[sw])
        values = collector.poll_switch(sw)
        assert values is not None
        assert "port01_poe_mw" in values
        assert values["port01_poe_mw"] == 3300.0
        assert values["port05_poe_mw"] == 5600.0

    def test_get_sensors_for_switch_static(self):
        sw = _make_switch("test-m4300", "m4300")
        collector = self.make_collector(switches=[sw])
        values = {"fan1_rpm": 5280, "fan2_rpm": 4560, "temp": 65, "psu_power": 65}
        sensors = collector.get_sensors_for_switch(sw, values)
        assert len(sensors) == len(sw.sensors)

    def test_get_sensors_for_switch_dynamic(self):
        sw = _make_switch("test-gsm7252ps", "gsm7252ps")
        collector = self.make_collector(switches=[sw])
        values = {"port01_poe_mw": 3300, "port05_poe_mw": 5600}
        sensors = collector.get_sensors_for_switch(sw, values)
        assert len(sensors) == 2
        # No port descriptions cached → generic names
        names = {s.name for s in sensors}
        assert "Port 01 PoE Power" in names
        assert "Port 05 PoE Power" in names

    def test_get_sensors_with_port_descriptions(self):
        sw = _make_switch("test-gsm7252ps", "gsm7252ps")
        collector = self.make_collector(switches=[sw])
        # Simulate cached port descriptions (as if fetched from SNMP ifAlias)
        collector._port_descriptions[sw.node_id] = {
            1: "rpi5-pmod",
            5: "rpi4-usbdev",
        }
        values = {"port01_poe_mw": 3300, "port05_poe_mw": 5600, "port10_poe_mw": 0}
        sensors = collector.get_sensors_for_switch(sw, values)
        names = {s.name for s in sensors}
        # Ports with descriptions get friendly names
        assert "(Port 01 PoE) rpi5-pmod" in names
        assert "(Port 05 PoE) rpi4-usbdev" in names
        # Port without description gets generic name
        assert "Port 10 PoE Power" in names

    def test_topics(self):
        sw = _make_switch("test-m4300", "m4300")
        collector = self.make_collector(switches=[sw])
        assert collector.state_topic(sw) == "sensors2mqtt/test_m4300/state"
        assert collector.avail_topic(sw) == "sensors2mqtt/test_m4300/status"
