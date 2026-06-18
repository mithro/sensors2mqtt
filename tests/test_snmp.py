"""Tests for SNMP collector."""

import logging
import os
from pathlib import Path

import pytest

from sensors2mqtt.collector.snmp import (
    MODELS,
    SnmpCollector,
    SwitchConfig,
    _box_walks,
    box_entity,
    load_config,
    parse_box_walk,
    parse_hex_mac,
    parse_lldp_chassis_ids,
    parse_lldp_walk,
    parse_snmpwalk,
    snmpget_value,
)
from sensors2mqtt.security import InsecureFilePermissionsError
from snmp_helpers import FakeSnmpClient, rows_from_snmpwalk_txt

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_FILE = Path(__file__).parent / "fixtures" / "snmp_test.toml"


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
        port_count=model.port_count,
        poe_port_count=model.poe_port_count,
        sensors=list(model.sensors),
        walk_sensors=list(model.walk_sensors),
        box_walks=list(model.box_walks),
    )


def _box_test_switch(base: str | None = None) -> SwitchConfig:
    """A switch with only box walks (FM OID base by default), independent of MODELS."""
    from sensors2mqtt.collector.snmp import _FM_BOX
    return SwitchConfig(
        node_id="test_box",
        name="test-box",
        host="test-box.test",
        community="public",
        manufacturer="Netgear",
        model="TEST",
        box_walks=_box_walks(base if base is not None else _FM_BOX),
    )


def collector_with(switch, *, walk_rows=None, get_rows=None):
    from sensors2mqtt.base import MqttConfig
    cfg = MqttConfig(host="test", port=1883, user="u", password="p")
    fake = FakeSnmpClient(walk_rows=walk_rows or {}, get_rows=get_rows or {})
    return SnmpCollector(config=cfg, switches=[switch],
                         client_factory=lambda sw: fake)


class TestParseSnmpwalk:
    def test_parses_gauge32(self):
        rows = rows_from_snmpwalk_txt(
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.1 = Gauge32: 3300\n"
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.2 = Gauge32: 2500\n"
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.3 = Gauge32: 0\n"
        )
        assert parse_snmpwalk(rows) == [(1, "3300"), (2, "2500"), (3, "0")]

    def test_parses_integer(self):
        rows = rows_from_snmpwalk_txt(
            "iso.3.6.1.4.1.4526.10.43.1.15.1.3.1 = INTEGER: 65\n"
        )
        assert parse_snmpwalk(rows) == [(1, "65")]

    def test_empty_output(self):
        assert parse_snmpwalk([]) == []

    def test_fixture_m4300_fans(self):
        rows = rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_m4300_fans.txt").read_text())
        result = parse_snmpwalk(rows)
        assert len(result) > 0
        speeds = [(i, v) for i, v in result if v.isdigit() and int(v) > 100]
        assert len(speeds) >= 2

    def test_fixture_gsm7252ps_poe(self):
        rows = rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_gsm7252ps_poe.txt").read_text())
        result = parse_snmpwalk(rows)
        assert len(result) > 0
        indices = {idx for idx, _ in result}
        assert len(indices) >= 48

    def test_fixture_s3300_fans(self):
        rows = rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_s3300_fans.txt").read_text())
        result = parse_snmpwalk(rows)
        assert len(result) > 0
        speeds = [(i, v) for i, v in result if v.isdigit() and int(v) > 100]
        assert len(speeds) >= 3

    def test_fixture_s3300_poe(self):
        rows = rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_s3300_poe.txt").read_text())
        result = parse_snmpwalk(rows)
        assert len(result) > 0
        indices = {idx for idx, _ in result}
        assert len(indices) >= 48


class TestParseBoxWalk:
    BASE = "1.3.6.1.4.1.4526.10.43.1.6.1.4"

    def test_single_component_instance(self):
        rows = rows_from_snmpwalk_txt(
            'iso.3.6.1.4.1.4526.10.43.1.6.1.4.0 = STRING: "3500"\n'
        )
        assert parse_box_walk(rows, self.BASE) == [("0", "3500")]

    def test_multi_component_instance(self):
        rows = rows_from_snmpwalk_txt(
            'iso.3.6.1.4.1.4526.10.43.1.6.1.4.1.0 = STRING: "5280"\n'
            'iso.3.6.1.4.1.4526.10.43.1.6.1.4.1.1 = STRING: "4560"\n'
        )
        assert parse_box_walk(rows, self.BASE) == [("1.0", "5280"), ("1.1", "4560")]

    def test_integer_values_unquoted(self):
        base = "1.3.6.1.4.1.4526.10.43.1.8.1.5"
        rows = rows_from_snmpwalk_txt(
            "iso.3.6.1.4.1.4526.10.43.1.8.1.5.1.0 = INTEGER: 53\n"
            "iso.3.6.1.4.1.4526.10.43.1.8.1.5.1.3 = INTEGER: 35\n"
        )
        assert parse_box_walk(rows, base) == [("1.0", "53"), ("1.3", "35")]

    def test_not_supported_passed_through(self):
        """The parser returns the raw marker; skipping is the poller's job."""
        rows = rows_from_snmpwalk_txt(
            'iso.3.6.1.4.1.4526.10.43.1.6.1.4.1 = STRING: "Not Supported"\n'
        )
        assert parse_box_walk(rows, self.BASE) == [("1", "Not Supported")]

    def test_filters_lines_outside_base(self):
        """Feeding a full-table walk only yields rows under the value column."""
        rows = rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_m4300_fans.txt").read_text())
        result = parse_box_walk(rows, self.BASE)
        # Fixture has columns 1-6; only column 4 (speed) rows are under BASE
        assert result == [("1.0", "5280"), ("1.1", "4560")]

    def test_no_false_prefix_match(self):
        """...6.1.4 must not match ...6.1.40 or bare ...6.1.4 itself."""
        rows = rows_from_snmpwalk_txt(
            "iso.3.6.1.4.1.4526.10.43.1.6.1.40.1 = INTEGER: 7\n"
            "iso.3.6.1.4.1.4526.10.43.1.6.1.4 = INTEGER: 8\n"
        )
        assert parse_box_walk(rows, self.BASE) == []

    def test_no_such_object_line_ignored(self):
        rows = rows_from_snmpwalk_txt(
            "iso.3.6.1.4.1.4526.10.43.1.6.1.4 = "
            "No Such Object available on this agent at this OID\n"
        )
        assert parse_box_walk(rows, self.BASE) == []

    def test_empty_output(self):
        assert parse_box_walk([], self.BASE) == []


class TestBoxEntity:
    def test_fans_are_numbered_from_one(self):
        assert box_entity("fan", 0) == ("fan1_rpm", "Fan 1")
        assert box_entity("fan", 1) == ("fan2_rpm", "Fan 2")
        assert box_entity("fan", 2) == ("fan3_rpm", "Fan 3")

    def test_first_temp_keeps_historic_suffix(self):
        assert box_entity("temp", 0) == ("temp", "Temperature")

    def test_extra_temp_numbered(self):
        assert box_entity("temp", 1) == ("temp2", "Temperature 2")

    def test_first_psu_keeps_historic_suffix(self):
        assert box_entity("psu_power", 0) == ("psu_power", "PSU Power")

    def test_extra_psu_rails_numbered(self):
        assert box_entity("psu_power", 1) == ("psu_power2", "PSU Power 2")
        assert box_entity("psu_power", 3) == ("psu_power4", "PSU Power 4")


class TestBoxWalks:
    def test_builds_three_walks(self):
        from sensors2mqtt.collector.snmp import _FM_BOX
        walks = _box_walks(_FM_BOX)
        by_kind = {w.kind: w for w in walks}
        assert set(by_kind) == {"fan", "temp", "psu_power"}
        assert by_kind["fan"].base_oid == "1.3.6.1.4.1.4526.10.43.1.6.1.4"
        assert by_kind["temp"].base_oid == "1.3.6.1.4.1.4526.10.43.1.15.1.3"
        assert by_kind["psu_power"].base_oid == "1.3.6.1.4.1.4526.10.43.1.8.1.5"

    def test_sensor_metadata(self):
        from sensors2mqtt.collector.snmp import _FM_BOX
        by_kind = {w.kind: w for w in _box_walks(_FM_BOX)}
        assert by_kind["fan"].unit == "RPM"
        assert by_kind["fan"].icon == "mdi:fan"
        assert by_kind["temp"].unit == "°C"
        assert by_kind["temp"].device_class == "temperature"
        assert by_kind["psu_power"].unit == "W"
        assert by_kind["psu_power"].device_class == "power"

    def test_switch_config_defaults_empty(self):
        # Build a raw SwitchConfig, NOT one derived from MODELS — model
        # entries gain box_walks in a later task and this must stay true.
        sw = SwitchConfig(node_id="x", name="x", host="x", community="public",
                          manufacturer="m", model="m")
        assert sw.box_walks == []


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
        assert len(m.sensors) == 0
        assert {b.kind for b in m.box_walks} == {"fan", "temp", "psu_power"}
        # M4300 has no PoE
        assert len(m.walk_sensors) == 0

    def test_gsm7252ps_model(self):
        m = MODELS["gsm7252ps"]
        assert len(m.walk_sensors) >= 1
        walk = m.walk_sensors[0]
        assert "poe" in walk.suffix_template
        # Box sensors are walk-discovered (fans + 4 PSU rails on this model)
        assert {b.kind for b in m.box_walks} == {"fan", "temp", "psu_power"}

    def test_s3300_model(self):
        m = MODELS["s3300"]
        assert {b.kind for b in m.box_walks} == {"fan", "temp", "psu_power"}
        # S3300 has both boxServices AND PoE
        assert len(m.walk_sensors) >= 1

    def test_s3300_uses_dot11_oids(self):
        """S3300 uses 4526.11 (Smart Managed Pro), not 4526.10."""
        m = MODELS["s3300"]
        for box in m.box_walks:
            assert ".4526.11." in box.base_oid, (
                f"{box.kind} OID should use .4526.11.: {box.base_oid}"
            )
        for walk in m.walk_sensors:
            assert ".4526.11." in walk.base_oid

    def test_m4300_uses_dot10_oids(self):
        m = MODELS["m4300"]
        for box in m.box_walks:
            assert ".4526.10." in box.base_oid

    def test_gsm7252ps_uses_dot10_oids(self):
        """GSM7252PS runs Fully Managed firmware — 4526.10, not .11."""
        m = MODELS["gsm7252ps"]
        for box in m.box_walks:
            assert ".4526.10." in box.base_oid


class TestConfigLoading:
    @pytest.fixture(autouse=True)
    def _secure_shared_fixture(self):
        # load_config now refuses group/world-readable files. The committed
        # fixture checks out 0664; tighten it to 0600 for these tests, then
        # restore so the test leaves no on-disk side effect. (git tracks only
        # the executable bit, so the chmod itself produces no diff either way.)
        original_mode = CONFIG_FILE.stat().st_mode
        os.chmod(CONFIG_FILE, 0o600)
        try:
            yield
        finally:
            os.chmod(CONFIG_FILE, original_mode)

    def test_load_config(self):
        """Load the test config fixture."""
        switches = load_config(CONFIG_FILE)
        assert len(switches) == 3
        names = {s.name for s in switches}
        assert "test-m4300" in names
        assert "test-gsm7252ps" in names
        assert "test-s3300" in names

    def test_config_node_ids(self):
        switches = load_config(CONFIG_FILE)
        ids = {s.node_id for s in switches}
        assert "test_m4300" in ids

    def test_config_hosts_are_dns(self):
        switches = load_config(CONFIG_FILE)
        for sw in switches:
            assert not sw.host[0].isdigit(), (
                f"{sw.name} uses IP {sw.host} instead of DNS hostname"
            )

    def test_config_box_walks_populated(self):
        """Config loading should populate box walks from model definitions."""
        switches = load_config(CONFIG_FILE)
        by_name = {s.name: s for s in switches}
        for name in ("test-m4300", "test-gsm7252ps", "test-s3300"):
            assert len(by_name[name].box_walks) == 3, name
        assert len(by_name["test-m4300"].walk_sensors) == 0
        assert len(by_name["test-s3300"].walk_sensors) >= 1

    def test_load_missing_config_raises(self):
        """Explicit path that doesn't exist should raise FileNotFoundError."""
        import pytest
        with pytest.raises(FileNotFoundError):
            load_config(Path("/nonexistent/path.toml"))

    def test_write_community_loaded(self):
        """PoE switches should have write_community from config."""
        switches = load_config(CONFIG_FILE)
        by_name = {s.name: s for s in switches}
        assert by_name["test-gsm7252ps"].write_community == "private"
        assert by_name["test-s3300"].write_community == "private"
        assert by_name["test-m4300"].write_community is None

    def test_no_config_raises(self):
        """When no config file found, FileNotFoundError is raised."""
        with pytest.raises(FileNotFoundError):
            load_config(Path("/nonexistent/snmp.toml"))

    def test_load_config_insecure_perms_raises(self, tmp_path):
        """A group/world-readable config must refuse to load."""
        cfg = tmp_path / "snmp.toml"
        cfg.write_text(
            '[switches.test-m4300]\n'
            'model = "m4300"\n'
            'host = "test-m4300.example.com"\n'
            'community = "public"\n'
        )
        os.chmod(cfg, 0o644)
        with pytest.raises(InsecureFilePermissionsError):
            load_config(cfg)

    def test_load_config_secure_perms_loads(self, tmp_path):
        """A 0600 config loads normally."""
        cfg = tmp_path / "snmp.toml"
        cfg.write_text(
            '[switches.test-m4300]\n'
            'model = "m4300"\n'
            'host = "test-m4300.example.com"\n'
            'community = "public"\n'
        )
        os.chmod(cfg, 0o600)
        switches = load_config(cfg)
        assert len(switches) == 1
        assert switches[0].name == "test-m4300"

    def test_load_config_unknown_model_raises(self, tmp_path):
        """An unrecognised model must fail hard with a clear message."""
        cfg = tmp_path / "snmp.toml"
        cfg.write_text(
            '[switches.test-typo]\n'
            'model = "m4300x"\n'
            'host = "test-typo.example.com"\n'
            'community = "public"\n'
        )
        os.chmod(cfg, 0o600)
        with pytest.raises(ValueError) as exc:
            load_config(cfg)
        msg = str(exc.value)
        assert "test-typo" in msg      # names the offending switch
        assert "m4300x" in msg         # names the bad model value
        assert "s3300" in msg          # lists the valid models

    def test_load_config_unknown_model_not_silently_dropped(self, tmp_path):
        """A typo'd switch must abort the whole load, not silently drop."""
        cfg = tmp_path / "snmp.toml"
        cfg.write_text(
            '[switches.good]\n'
            'model = "m4300"\n'
            'host = "good.example.com"\n'
            'community = "public"\n'
            '\n'
            '[switches.bad]\n'
            'model = "nope"\n'
            'host = "bad.example.com"\n'
            'community = "public"\n'
        )
        os.chmod(cfg, 0o600)
        with pytest.raises(ValueError):
            load_config(cfg)


class TestVlanNameLookup:
    def test_fixture_gsm7252ps_vlan_names(self):
        """VLAN name fixture parses correctly."""
        rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_gsm7252ps_vlan_names.txt").read_text()
        )
        result = parse_snmpwalk(rows)
        names = {idx: val for idx, val in result}
        assert names[1] == "default"
        assert names[90] == "iot"
        assert names[121] == "t-fpgas"
        assert len(names) >= 10

    def test_fetch_vlan_names(self):
        text = (FIXTURES / "snmpwalk_gsm7252ps_vlan_names.txt").read_text()
        sw = _make_switch("test-gsm7252ps", "gsm7252ps")
        collector = collector_with(sw, walk_rows={
            "4.3.1.1": rows_from_snmpwalk_txt(text),
        })
        names = collector.fetch_vlan_names(sw)
        assert names[90] == "iot"
        assert names[1] == "default"
        # Second call should use cache
        names2 = collector.fetch_vlan_names(sw)
        assert names2 is names


class TestLldpParsing:
    def test_parse_lldp_walk_sysname(self):
        rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_gsm7252ps_lldp_sysname.txt").read_text()
        )
        result = parse_lldp_walk(rows, "9")
        # Port 1 → rpi5-pmod (from OID ...9.150.1.21)
        assert result[1] == "rpi5-pmod"
        # Port 50 → sw-netgear-s3300-1 (from OID ...9.44.50.1)
        assert result[50] == "sw-netgear-s3300-1"
        # Port 49 → sw-netgear-m4300-24x
        assert result[49] == "sw-netgear-m4300-24x"
        assert len(result) >= 8

    def test_parse_lldp_walk_portdesc(self):
        rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_gsm7252ps_lldp_portdesc.txt").read_text()
        )
        result = parse_lldp_walk(rows, "8")
        # Port 1 → eth0 (rpi5-pmod's interface)
        assert result[1] == "eth0"
        # Port 50 → 1/xg51 (S3300 uplink port)
        assert result[50] == "1/xg51"

    def test_parse_hex_mac_valid(self):
        assert parse_hex_mac("E0 91 F5 0C D5 C7") == "e0:91:f5:0c:d5:c7"

    def test_parse_hex_mac_already_lower(self):
        assert parse_hex_mac("ac 1f 6b aa 50 53") == "ac:1f:6b:aa:50:53"

    def test_parse_hex_mac_not_6_bytes(self):
        assert parse_hex_mac("E0 91 F5 0C D5") is None  # 5 bytes
        assert parse_hex_mac("E0 91 F5 0C D5 C7 AB") is None  # 7 bytes

    def test_parse_hex_mac_empty(self):
        assert parse_hex_mac("") is None

    def test_parse_lldp_chassis_ids(self):
        rows = rows_from_snmpwalk_txt(
            "iso.0.8802.1.1.2.1.4.1.1.5.0.1.1 = Hex-STRING: E0 91 F5 0C D6 DB \n"
            "iso.0.8802.1.1.2.1.4.1.1.5.0.2.1 = Hex-STRING: DC A6 32 12 34 56 \n"
            "iso.0.8802.1.1.2.1.4.1.1.5.0.49.1 = Hex-STRING: 8C 3B AD 6B BB E3 \n"
        )
        result = parse_lldp_chassis_ids(rows)
        assert result[1] == "e0:91:f5:0c:d6:db"
        assert result[2] == "dc:a6:32:12:34:56"
        assert result[49] == "8c:3b:ad:6b:bb:e3"

    def test_parse_lldp_chassis_ids_filters_non_mac(self):
        rows = rows_from_snmpwalk_txt(
            "iso.0.8802.1.1.2.1.4.1.1.5.0.1.1 = Hex-STRING: E0 91 F5 0C D6 DB \n"
            # 7 bytes = not a MAC, should be filtered out
            "iso.0.8802.1.1.2.1.4.1.1.5.0.3.1 = Hex-STRING: 01 04 0A 01 05 0B 00 \n"
        )
        result = parse_lldp_chassis_ids(rows)
        assert result == {1: "e0:91:f5:0c:d6:db"}  # port 3 filtered out

    def test_fetch_lldp_neighbors(self):
        sysname_rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_gsm7252ps_lldp_sysname.txt").read_text()
        )
        portdesc_rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_gsm7252ps_lldp_portdesc.txt").read_text()
        )
        sw = _make_switch("test-gsm7252ps", "gsm7252ps")
        collector = collector_with(sw, walk_rows={
            ".9": sysname_rows,
            ".8": portdesc_rows,
        })
        neighbors = collector.fetch_lldp_neighbors(sw)
        # Port 1 should be "eth0.rpi5-pmod"
        assert "rpi5-pmod" in neighbors[1]
        assert "eth0" in neighbors[1]
        # Port 49 should be "trunk.gsm7252ps-s1.sw-netgear-m4300-24x"
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

    def test_poll_switch_success(self):
        sw = _make_switch("test-m4300", "m4300")
        collector = collector_with(sw, walk_rows={
            ".6.1.4": rows_from_snmpwalk_txt(
                (FIXTURES / "snmpwalk_m4300_fans.txt").read_text()
            ),
            ".15.1.3": rows_from_snmpwalk_txt(
                (FIXTURES / "snmpwalk_m4300_thermal.txt").read_text()
            ),
            ".8.1.5": rows_from_snmpwalk_txt(
                (FIXTURES / "snmpwalk_m4300_psu.txt").read_text()
            ),
        })
        assert collector.poll_switch(sw) == {
            "fan1_rpm": 5280, "fan2_rpm": 4560, "temp": 65, "psu_power": 65,
        }

    def test_poll_switch_all_fail(self):
        sw = _make_switch("test-m4300", "m4300")
        from sensors2mqtt.base import MqttConfig
        cfg = MqttConfig(host="test", port=1883, user="u", password="p")
        fake = FakeSnmpClient(
            walk_rows={},
            walk_error=(".6.1.4", ".15.1.3", ".8.1.5"),
        )
        collector = SnmpCollector(config=cfg, switches=[sw],
                                  client_factory=lambda s: fake)
        values = collector.poll_switch(sw)
        assert values is None

    def test_poll_switch_timeout(self):
        sw = _make_switch("test-m4300", "m4300")
        from sensors2mqtt.base import MqttConfig
        cfg = MqttConfig(host="test", port=1883, user="u", password="p")
        fake = FakeSnmpClient(
            walk_rows={},
            walk_error=(".6.1.4", ".15.1.3", ".8.1.5"),
        )
        collector = SnmpCollector(config=cfg, switches=[sw],
                                  client_factory=lambda s: fake)
        values = collector.poll_switch(sw)
        assert values is None

    def test_poll_switch_partial_failure(self):
        """A failed fan walk still yields temperature and PSU values."""
        thermal_rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_m4300_thermal.txt").read_text()
        )
        psu_rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_m4300_psu.txt").read_text()
        )
        sw = _make_switch("test-m4300", "m4300")
        from sensors2mqtt.base import MqttConfig
        cfg = MqttConfig(host="test", port=1883, user="u", password="p")
        fake = FakeSnmpClient(
            walk_rows={
                ".15.1.3": thermal_rows,
                ".8.1.5": psu_rows,
            },
            walk_error=(".6.1.4",),
        )
        collector = SnmpCollector(config=cfg, switches=[sw],
                                  client_factory=lambda s: fake)
        values = collector.poll_switch(sw)
        assert values == {"temp": 65, "psu_power": 65}

    def test_poll_walk_switch(self):
        poe_rows = rows_from_snmpwalk_txt(
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.1 = Gauge32: 3300\n"
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.2 = Gauge32: 2500\n"
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.5 = Gauge32: 5600\n"
        )
        sw = _make_switch("test-gsm7252ps", "gsm7252ps")
        collector = collector_with(sw, walk_rows={
            ".2.1": poe_rows,
        })
        values = collector.poll_switch(sw)
        assert values is not None
        assert "port01_poe_mw" in values
        assert values["port01_poe_mw"] == 3300.0
        assert values["port05_poe_mw"] == 5600.0

    def test_poll_box_sensors_m4300_layout(self):
        """Two-component instances (unit.fan) — suffix contract preserved."""
        sw = _box_test_switch()
        collector = collector_with(sw, walk_rows={
            ".6.1.4": rows_from_snmpwalk_txt(
                (FIXTURES / "snmpwalk_m4300_fans.txt").read_text()
            ),
            ".15.1.3": rows_from_snmpwalk_txt(
                (FIXTURES / "snmpwalk_m4300_thermal.txt").read_text()
            ),
            ".8.1.5": rows_from_snmpwalk_txt(
                (FIXTURES / "snmpwalk_m4300_psu.txt").read_text()
            ),
        })
        values = collector.poll_switch(sw)
        assert values == {
            "fan1_rpm": 5280, "fan2_rpm": 4560, "temp": 65, "psu_power": 65,
        }

    def test_poll_box_sensors_gsm7252ps_layout(self, caplog):
        """Single-component fan instances, Not Supported slot, 4 PSU rails."""
        sw = _box_test_switch()
        collector = collector_with(sw, walk_rows={
            ".6.1.4": rows_from_snmpwalk_txt(
                (FIXTURES / "snmpwalk_gsm7252ps_fans.txt").read_text()
            ),
            ".8.1.5": rows_from_snmpwalk_txt(
                (FIXTURES / "snmpwalk_gsm7252ps_psu.txt").read_text()
            ),
            # .15.1.3 (temp) falls through to empty rows — the GSM7252PS
            # exposes nothing readable there
        })
        with caplog.at_level(logging.WARNING):
            values = collector.poll_switch(sw)
        assert values == {
            "fan1_rpm": 3500, "fan2_rpm": 3450,
            "psu_power": 53, "psu_power2": 34,
            "psu_power3": 36, "psu_power4": 35,
        }
        # The "Not Supported" placeholder is expected hardware, not an error
        assert not any("non-integer" in r.getMessage() for r in caplog.records)

    def test_poll_box_sensors_s3300_layout(self):
        """Smart Managed Pro (4526.11) walks: 3 fans, temp, single PSU rail."""
        from sensors2mqtt.collector.snmp import _SMP_BOX
        sw = _box_test_switch(_SMP_BOX)
        collector = collector_with(sw, walk_rows={
            ".6.1.4": rows_from_snmpwalk_txt(
                (FIXTURES / "snmpwalk_s3300_fans.txt").read_text()
            ),
            ".15.1.3": rows_from_snmpwalk_txt(
                (FIXTURES / "snmpwalk_s3300_thermal.txt").read_text()
            ),
            ".8.1.5": rows_from_snmpwalk_txt(
                (FIXTURES / "snmpwalk_s3300_psu.txt").read_text()
            ),
        })
        values = collector.poll_switch(sw)
        assert values == {
            "fan1_rpm": 5018, "fan2_rpm": 5273, "fan3_rpm": 5357,
            "temp": 56, "psu_power": 56,
        }

    def test_poll_box_warns_on_unexpected_string(self, caplog):
        """Unknown non-integer values must be visible, not silently dropped."""
        fans_rows = rows_from_snmpwalk_txt(
            'iso.3.6.1.4.1.4526.10.43.1.6.1.4.0 = STRING: "3500"\n'
            'iso.3.6.1.4.1.4526.10.43.1.6.1.4.1 = STRING: "garbage"\n'
        )
        sw = _box_test_switch()
        collector = collector_with(sw, walk_rows={".6.1.4": fans_rows})
        with caplog.at_level(logging.WARNING):
            values = collector.poll_switch(sw)
        assert values == {"fan1_rpm": 3500}
        assert any("non-integer fan reading" in r.getMessage()
                   for r in caplog.records)

    def test_poll_box_walk_failure_skips_kind(self):
        """One failed walk doesn't lose the other kinds."""
        psu_rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_m4300_psu.txt").read_text()
        )
        sw = _box_test_switch()
        from sensors2mqtt.base import MqttConfig
        cfg = MqttConfig(host="test", port=1883, user="u", password="p")
        fake = FakeSnmpClient(
            walk_rows={".8.1.5": psu_rows},
            walk_error=(".6.1.4", ".15.1.3"),
        )
        collector = SnmpCollector(config=cfg, switches=[sw],
                                  client_factory=lambda s: fake)
        values = collector.poll_switch(sw)
        assert values == {"psu_power": 65}

    def test_get_sensors_for_switch_box(self):
        """Discovery defs are derived from the suffixes found by polling."""
        sw = _box_test_switch()
        collector = self.make_collector(switches=[sw])
        values = {"fan1_rpm": 5280, "fan2_rpm": 4560, "temp": 65,
                  "psu_power": 53, "psu_power2": 34}
        sensors = collector.get_sensors_for_switch(sw, values)
        by_suffix = {s.suffix: s for s in sensors}
        assert set(by_suffix) == set(values)
        assert by_suffix["fan1_rpm"].unit == "RPM"
        assert by_suffix["fan1_rpm"].icon == "mdi:fan"
        assert by_suffix["fan2_rpm"].name == "Fan 2"
        assert by_suffix["temp"].device_class == "temperature"
        assert by_suffix["psu_power"].name == "PSU Power"
        assert by_suffix["psu_power2"].name == "PSU Power 2"
        assert by_suffix["psu_power2"].device_class == "power"
        for s in sensors:
            assert s.state_class == "measurement"

    def test_new_sensor_defs_incremental(self):
        """Sensors first seen on a later poll still get discovery defs."""
        sw = _box_test_switch()
        collector = self.make_collector(switches=[sw])
        first = collector.new_sensor_defs(sw, {"fan1_rpm": 3500, "temp": 40})
        assert {s.suffix for s in first} == {"fan1_rpm", "temp"}
        # Same values again: nothing new to announce
        assert collector.new_sensor_defs(sw, {"fan1_rpm": 3500, "temp": 40}) == []
        # PSU walk recovers on a later poll: only the new suffixes returned
        later = collector.new_sensor_defs(
            sw,
            {"fan1_rpm": 3500, "temp": 40, "psu_power": 53, "psu_power2": 34},
        )
        assert {s.suffix for s in later} == {"psu_power", "psu_power2"}

    def test_get_sensors_for_switch_m4300(self):
        """MODELS wiring feeds walk-discovered values into discovery defs."""
        sw = _make_switch("test-m4300", "m4300")
        collector = self.make_collector(switches=[sw])
        values = {"fan1_rpm": 5280, "fan2_rpm": 4560, "temp": 65,
                  "psu_power": 65}
        sensors = collector.get_sensors_for_switch(sw, values)
        assert {s.suffix for s in sensors} == set(values)

    def test_get_sensors_excludes_walk_sensors(self):
        """Walk sensors (PoE) are NOT in hardware discovery — they're on per-port sub-devices."""
        sw = _make_switch("test-gsm7252ps", "gsm7252ps")
        collector = self.make_collector(switches=[sw])
        values = {"port01_poe_mw": 3300, "port05_poe_mw": 5600}
        sensors = collector.get_sensors_for_switch(sw, values)
        # Values contain only PoE keys — no box suffixes were discovered,
        # and PoE walk sensors are per-port sub-devices, not switch-level
        assert len(sensors) == 0

    def test_topics(self):
        sw = _make_switch("test-m4300", "m4300")
        collector = self.make_collector(switches=[sw])
        assert collector.state_topic(sw) == "sensors2mqtt/test_m4300/state"
        assert collector.avail_topic(sw) == "sensors2mqtt/test_m4300/status"

    def test_poll_port_status_m4300(self):
        """M4300 (no PoE) returns link/speed/vlan for all 24 ports."""
        oper_rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_m4300_ifoperstatus.txt").read_text()
        )
        speed_rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_m4300_ifhighspeed.txt").read_text()
        )
        pvid_rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_m4300_dot1qpvid.txt").read_text()
        )
        vlan_names_rows = rows_from_snmpwalk_txt(
            (FIXTURES / "snmpwalk_m4300_vlan_names.txt").read_text()
        )
        sw = _make_switch("test-m4300", "m4300")
        collector = collector_with(sw, walk_rows={
            "2.2.1.8": oper_rows,
            "31.1.1.1.15": speed_rows,
            "17.7.1.4.5.1.1": pvid_rows,
            "17.7.1.4.3.1.1": vlan_names_rows,
            # ifAlias and LLDP return empty (no suffix match → [])
        })
        ports = collector.poll_port_status(sw)

        # M4300 has 24 ports
        assert len(ports) == 24
        # Port 1 should be up with 10G
        assert ports[1]["link"] == "up"
        assert ports[1]["speed_mbps"] == 10000
        # No PoE fields on M4300
        assert "poe_admin" not in ports[1]
        assert "poe_status" not in ports[1]

    def test_poll_port_status_gsm7252ps(self):
        """GSM7252PS (PoE) returns link/speed/vlan + PoE fields."""
        fixture_map = {
            "2.2.1.8": "snmpwalk_gsm7252ps_ifoperstatus.txt",
            "31.1.1.1.15": "snmpwalk_gsm7252ps_ifhighspeed.txt",
            "17.7.1.4.5.1.1": "snmpwalk_gsm7252ps_dot1qpvid.txt",
            "17.7.1.4.3.1.1": "snmpwalk_gsm7252ps_vlan_names.txt",
            "105.1.1.1.3.1": "snmpwalk_gsm7252ps_poe_admin.txt",
            "105.1.1.1.6.1": "snmpwalk_gsm7252ps_poe_detect.txt",
        }
        walk_rows = {
            suffix: rows_from_snmpwalk_txt((FIXTURES / fname).read_text())
            for suffix, fname in fixture_map.items()
        }
        sw = _make_switch("test-gsm7252ps", "gsm7252ps")
        collector = collector_with(sw, walk_rows=walk_rows)
        ports = collector.poll_port_status(sw)

        # GSM7252PS has 52 ports
        assert len(ports) == 52
        # Port 1 has PoE delivering
        assert ports[1]["link"] == "up"
        assert ports[1]["poe_admin"] == "enabled"
        assert ports[1]["poe_status"] == "delivering"
        assert ports[1]["vlan_pvid"] == 90
        # Ports 49-52 are SFP+ uplinks (no PoE fields)
        assert "poe_admin" not in ports[49]
        assert "poe_status" not in ports[49]


def test_port_discovery_drops_bridge_and_has_expire_after():
    import json
    from unittest.mock import MagicMock

    from sensors2mqtt.collector.snmp import _publish_port_discovery
    from sensors2mqtt.discovery import EXPIRE_AFTER

    sw = _make_switch("test-m4300", "m4300")  # port_count=24 > 0
    client = MagicMock()
    _publish_port_discovery(client, sw, f"sensors2mqtt/{sw.node_id}/status")
    cfgs = [json.loads(c.args[1]) for c in client.publish.call_args_list]
    assert cfgs
    for c in cfgs:
        assert c["availability_topic"] == f"sensors2mqtt/{sw.node_id}/status"
        assert "availability" not in c  # no multi-topic list -> no bridge
        assert c["expire_after"] == EXPIRE_AFTER


def test_connection_status_topic_for_snmp(monkeypatch):
    import sensors2mqtt.base as base

    monkeypatch.setattr(base.socket, "gethostname", lambda: "ten64")
    from sensors2mqtt.base import connection_status_topic

    assert connection_status_topic("snmp") == "sensors2mqtt/ten64/snmp/status"
