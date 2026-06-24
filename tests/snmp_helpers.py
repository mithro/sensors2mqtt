"""Test helpers for the row-based SNMP code: convert legacy CLI-text walk
fixtures into SnmpRow lists, and a FakeSnmpClient test double."""
import re

from sensors2mqtt.snmp_client import SnmpRow

# net-snmp display type -> ezsnmp snmp_type
_TYPE_MAP = {
    "INTEGER": "INTEGER", "Gauge32": "GAUGE", "Counter32": "COUNTER",
    "STRING": "OCTETSTR", "Hex-STRING": "OCTETSTR", "Timeticks": "TICKS",
    "IpAddress": "IPADDR",
}


def rows_from_snmpwalk_txt(text: str) -> list[SnmpRow]:
    """Parse legacy net-snmp walk text into SnmpRow objects (full numeric OIDs)."""
    rows = []
    for line in text.strip().splitlines():
        m = re.match(r"(\S+)\s*=\s*(\S+):\s*(.*)", line.strip())
        if not m:
            continue
        oid, typ, val = m.group(1), m.group(2), m.group(3).strip()
        if oid.startswith("iso."):
            oid = "1." + oid[len("iso."):]
        oid = oid.lstrip(".")
        if typ == "STRING":
            val = val.strip('"')
        rows.append(SnmpRow(oid=oid, value=val, snmp_type=_TYPE_MAP.get(typ, typ.upper())))
    return rows


class FakeSnmpClient:
    """In-memory SnmpClient stand-in.

    walk_rows / get_rows map an OID *suffix* -> rows / row (matched by
    ``endswith``). set_int records calls and returns ``set_ok``.
    """

    def __init__(self, *, walk_rows=None, get_rows=None, set_ok=True, walk_error=()):
        self._walk = walk_rows or {}
        self._get = get_rows or {}
        self.set_ok = set_ok
        self.sets = []
        self._walk_error = tuple(walk_error)  # OID suffixes that raise SnmpError

    def walk(self, oid):
        from sensors2mqtt.snmp_client import SnmpError
        if any(oid.endswith(s) for s in self._walk_error):
            raise SnmpError(f"walk {oid} failed")
        for suffix, rows in self._walk.items():
            if oid.endswith(suffix):
                return rows
        return []

    def get(self, oid):
        for suffix, row in self._get.items():
            if oid.endswith(suffix):
                return row
        return None

    def set_int(self, oid, value):
        self.sets.append((oid, value))
        return self.set_ok
