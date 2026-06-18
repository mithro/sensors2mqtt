"""Unit tests for the SnmpClient seam (fake session — no real ezsnmp)."""
from types import SimpleNamespace

import pytest

from sensors2mqtt.snmp_client import SnmpClient, SnmpError, SnmpRow, _full_oid


def var(oid, oid_index, value, snmp_type):
    return SimpleNamespace(oid=oid, oid_index=oid_index, value=value, snmp_type=snmp_type)


class FakeSession:
    def __init__(self, *, get_var=None, walk_vars=None, set_ret=True, raises=None):
        self._get_var, self._walk_vars = get_var, walk_vars or []
        self._set_ret, self._raises = set_ret, raises
        self.set_calls = []

    def get(self, oid):
        if self._raises:
            raise self._raises
        return self._get_var

    def walk(self, oid):
        if self._raises:
            raise self._raises
        return list(self._walk_vars)

    def set(self, oid, value, snmp_type):
        self.set_calls.append((oid, value, snmp_type))
        if self._raises:
            raise self._raises
        return self._set_ret


def factory_for(session):
    return lambda host, community, version, timeout, retries: session


def test_full_oid_joins_index():
    assert _full_oid(".1.3.6.1.2.1.2.2.1.8", "5") == "1.3.6.1.2.1.2.2.1.8.5"


def test_full_oid_no_index_strips_leading_dot():
    assert _full_oid(".1.3.6.1.4.1.4526.10.43.1.6.1.4.1.0", "") == \
        "1.3.6.1.4.1.4526.10.43.1.6.1.4.1.0"


def test_get_returns_row():
    s = FakeSession(get_var=var(".1.3.6.1.2.1.1.3.0", "", "12345", "TIMETICKS"))
    c = SnmpClient("h", "public", session_factory=factory_for(s))
    assert c.get("1.3.6.1.2.1.1.3.0") == SnmpRow("1.3.6.1.2.1.1.3.0", "12345", "TIMETICKS")


def test_get_missing_returns_none():
    s = FakeSession(get_var=var(".1.3.6", "", "NOSUCHOBJECT", "NOSUCHOBJECT"))
    c = SnmpClient("h", "public", session_factory=factory_for(s))
    assert c.get("1.3.6") is None


def test_walk_filters_absent_rows():
    s = FakeSession(walk_vars=[
        var(".1.3.6.1.4.1.4526.10.43.1.6.1.4", "1.0", "5280", "OCTETSTR"),
        var(".1.3.6.1.4.1.4526.10.43.1.6.1.4", "1.1", "ENDOFMIBVIEW", "ENDOFMIBVIEW"),
    ])
    c = SnmpClient("h", "public", session_factory=factory_for(s))
    rows = c.walk("1.3.6.1.4.1.4526.10.43.1.6.1.4")
    assert rows == [SnmpRow("1.3.6.1.4.1.4526.10.43.1.6.1.4.1.0", "5280", "OCTETSTR")]


def test_set_int_uses_write_community_and_type():
    s = FakeSession(set_ret=True)
    seen = {}

    def factory(host, community, version, timeout, retries):
        seen["community"] = community
        return s

    c = SnmpClient("h", "public", write_community="private", session_factory=factory)
    assert c.set_int("1.3.6.1.2.1.105.1.1.1.3.1.5", 1) is True
    assert seen["community"] == "private"
    assert s.set_calls == [("1.3.6.1.2.1.105.1.1.1.3.1.5", 1, "INTEGER")]


def test_set_int_without_write_community_raises():
    c = SnmpClient("h", "public", session_factory=factory_for(FakeSession()))
    with pytest.raises(SnmpError):
        c.set_int("1.3.6", 1)


def test_session_error_becomes_snmp_error():
    s = FakeSession(raises=RuntimeError("timeout"))
    c = SnmpClient("h", "public", session_factory=factory_for(s))
    with pytest.raises(SnmpError):
        c.get("1.3.6")
    with pytest.raises(SnmpError):
        c.walk("1.3.6")
