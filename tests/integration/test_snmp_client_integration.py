"""Real ezsnmp against a local snmpsim agent. Marked integration."""
import pytest

from sensors2mqtt.collector.snmp import (
    _FM_BOX,
    LLDP_CHASSIS_OID,
    format_mac,
    parse_lldp_chassis_ids,
)
from sensors2mqtt.snmp_client import SnmpClient

pytestmark = pytest.mark.integration


def client(agent, community="m4300", write_community=None):
    host, port = agent
    return SnmpClient(f"{host}:{port}", community, timeout=2, retries=1,
                      write_community=write_community)


def test_walk_returns_full_numeric_oids(snmpsim_agent):
    rows = client(snmpsim_agent).walk(f"{_FM_BOX}.6.1.4")  # fan speed column
    assert rows, "expected at least one fan row from the m4300 fixture"
    for r in rows:
        assert r.oid.startswith(_FM_BOX + ".6.1.4."), r.oid
        assert not r.oid.startswith("."), "OID must be normalised (no leading dot)"


def test_get_bridge_mac_decodes(snmpsim_agent):
    row = client(snmpsim_agent).get("1.3.6.1.2.1.17.1.1.0")  # dot1dBaseBridgeAddress
    assert row is not None
    # m4300.snmprec: 1.3.6.1.2.1.17.1.1.0|4x|0011223344aa → "00:11:22:33:44:aa"
    assert format_mac(row) == "00:11:22:33:44:aa"


def test_get_lldp_chassis_mac(snmpsim_agent):
    """parse_lldp_chassis_ids returns expected {port: mac} from m4300 fixture.

    m4300.snmprec LLDP chassis rows:
      1.0.8802.1.1.2.1.4.1.1.5.117.1.3    |4x|001122334401  → port 1
      1.0.8802.1.1.2.1.4.1.1.5.124134.21.43|4x|001122334402  → port 21
    """
    rows = client(snmpsim_agent).walk(LLDP_CHASSIS_OID)
    macs = parse_lldp_chassis_ids(rows)
    assert macs.get(1) == "00:11:22:33:44:01"
    assert macs.get(21) == "00:11:22:33:44:02"


def test_get_missing_oid_returns_none(snmpsim_agent):
    assert client(snmpsim_agent).get("1.3.6.1.4.1.4526.99.99.0") is None


def test_set_int_round_trips(snmpsim_agent):
    c = client(snmpsim_agent, community="gsm7252ps", write_community="gsm7252ps")
    oid = "1.3.6.1.2.1.105.1.1.1.3.1.1"
    assert c.set_int(oid, 1) is True
    assert c.get(oid).value == "1"
