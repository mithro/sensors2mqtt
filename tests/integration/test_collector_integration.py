"""End-to-end: real ezsnmp + snmpsim through the collectors. Marked integration."""
import pytest

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.snmp import MODELS, SnmpCollector, SwitchConfig
from sensors2mqtt.collector.snmp_control import (
    POE_ADMIN_OID,
    PoeController,
)
from sensors2mqtt.snmp_client import SnmpClient

pytestmark = pytest.mark.integration


def _switch(name, model_name, host, community, write_community=None):
    m = MODELS[model_name]
    return SwitchConfig(
        node_id=name.replace("-", "_"), name=name, host=host, community=community,
        manufacturer=m.manufacturer, model=m.model, port_count=m.port_count,
        poe_port_count=m.poe_port_count, write_community=write_community,
        sensors=list(m.sensors), walk_sensors=list(m.walk_sensors),
        box_walks=list(m.box_walks),
    )


def test_poll_switch_m4300_end_to_end(snmpsim_agent):
    host, port = snmpsim_agent
    sw = _switch("m4300", "m4300", f"{host}:{port}", "m4300")
    cfg = MqttConfig(host="x", port=1883, user="u", password="p")
    collector = SnmpCollector(config=cfg, switches=[sw],
                              client_factory=lambda s: SnmpClient(
                                  s.host, s.community, timeout=2, retries=1))
    values = collector.poll_switch(sw)
    assert values, "expected box-sensor values from the m4300 fixture"
    # At least one fan + temp present (exact values depend on the captured fixture)
    assert any(k.startswith("fan") for k in values)


def test_poll_switch_m4300_fixture_values(snmpsim_agent):
    """Assert the exact sensor values authored in tests/fixtures/snmprec/m4300.snmprec.

    m4300.snmprec records:
      4526.10.43.1.6.1.4.1.0  = 5280   fan1_rpm
      4526.10.43.1.6.1.4.1.1  = 4560   fan2_rpm
      4526.10.43.1.8.1.5.1.0  = 65     psu_power
      4526.10.43.1.15.1.3.1   = 65     temp  (°C)
    """
    host, port = snmpsim_agent
    sw = _switch("m4300", "m4300", f"{host}:{port}", "m4300")
    cfg = MqttConfig(host="x", port=1883, user="u", password="p")
    collector = SnmpCollector(config=cfg, switches=[sw],
                              client_factory=lambda s: SnmpClient(
                                  s.host, s.community, timeout=2, retries=1))
    values = collector.poll_switch(sw)
    assert values is not None
    # Two fans in instance order (1.0 < 1.1 → ordinals 0, 1 → fan1, fan2)
    assert values["fan1_rpm"] == 5280
    assert values["fan2_rpm"] == 4560
    # PSU power and temperature from the fixture
    assert values["psu_power"] == 65
    assert values["temp"] == 65


def test_poll_switch_gsm7252ps_fixture_values(snmpsim_agent):
    """Assert sensor values from tests/fixtures/snmprec/gsm7252ps.snmprec.

    gsm7252ps.snmprec records:
      fans: index 0=3500, index 1="Not Supported" (skipped), index 2=3450
        → ordinals 0 and 1 → fan1_rpm=3500, fan2_rpm=3450
      psu_power rails: .1.5.1.{0,1,2,3} = 53, 34, 36, 35
        → ordinals 0-3 → psu_power=53, psu_power2=34, psu_power3=36, psu_power4=35
      temp: .15.1.3.1 = 45
      PoE walk (.15.1.1.1.2.1): port 1=15400, port 2=8200
    """
    host, port = snmpsim_agent
    sw = _switch("gsm7252ps", "gsm7252ps", f"{host}:{port}", "gsm7252ps")
    cfg = MqttConfig(host="x", port=1883, user="u", password="p")
    collector = SnmpCollector(config=cfg, switches=[sw],
                              client_factory=lambda s: SnmpClient(
                                  s.host, s.community, timeout=2, retries=1))
    values = collector.poll_switch(sw)
    assert values is not None
    # "Not Supported" placeholder at index 1 is skipped; indices 0 and 2 appear
    assert values["fan1_rpm"] == 3500
    assert values["fan2_rpm"] == 3450
    # Temperature
    assert values["temp"] == 45
    # Four PSU power rails (GSM7252PS-specific)
    assert values["psu_power"] == 53
    assert values["psu_power2"] == 34
    assert values["psu_power3"] == 36
    assert values["psu_power4"] == 35
    # PoE per-port walk (port01 and port02)
    assert values["port01_poe_mw"] == 15400.0
    assert values["port02_poe_mw"] == 8200.0


def test_poe_controller_poll_all_ports(snmpsim_agent):
    """PoeController.poll_all_ports reads PoE admin state from snmpsim.

    gsm7252ps.snmprec: pethPsePortAdminEnable ports 1.1=2, 1.2=2 (disabled).
    """
    host, port = snmpsim_agent
    sw = _switch("gsm7252ps", "gsm7252ps", f"{host}:{port}", "gsm7252ps",
                 write_community="gsm7252ps")
    cfg = MqttConfig(host="x", port=1883, user="u", password="p")
    ctrl = PoeController(
        mqtt_config=cfg,
        switches=[sw],
        client_factory=lambda s: SnmpClient(
            s.host, s.community, timeout=2, retries=1,
            write_community=s.write_community,
        ),
    )
    ctrl.poll_all_ports(sw)
    # Fixture has admin=2 (disabled) for ports 1 and 2
    state1 = ctrl._port_states[sw.node_id][1]
    state2 = ctrl._port_states[sw.node_id][2]
    assert state1.poe_admin == 2, "port 1 should be disabled (2)"
    assert state2.poe_admin == 2, "port 2 should be disabled (2)"


def test_poe_controller_toggle_and_readback(snmpsim_agent):
    """PoeController set_int round-trip: disable→enable, read back via _snmpget_int.

    gsm7252ps.snmprec records port 1.1 as writecache (snmpsim stores SET values
    in memory and returns them on subsequent GETs). We start at disabled (2),
    SET to enabled (1), then GET to verify the write landed.
    """
    host, port = snmpsim_agent
    sw = _switch("gsm7252ps", "gsm7252ps", f"{host}:{port}", "gsm7252ps",
                 write_community="gsm7252ps")
    cfg = MqttConfig(host="x", port=1883, user="u", password="p")
    client = SnmpClient(
        f"{host}:{port}", "gsm7252ps",
        timeout=2, retries=1, write_community="gsm7252ps",
    )
    ctrl = PoeController(
        mqtt_config=cfg,
        switches=[sw],
        client_factory=lambda s: client,
    )

    # Verify initial value is 2 (disabled) per the fixture
    initial = ctrl._snmpget_int(sw, POE_ADMIN_OID, 1)
    assert initial == 2, f"expected 2 (disabled) from fixture, got {initial}"

    # SET to enabled (1)
    ok = ctrl._snmpset_int(sw, POE_ADMIN_OID, 1, 1)
    assert ok, "set_int should succeed against the writecache OID"

    # Read back: snmpsim writecache stores the new value
    readback = ctrl._snmpget_int(sw, POE_ADMIN_OID, 1)
    assert readback == 1, f"expected 1 (enabled) after SET, got {readback}"
