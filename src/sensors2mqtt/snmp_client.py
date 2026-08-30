"""In-process SNMP v2c client wrapping ezsnmp.

This is the ONLY module that imports ezsnmp, and the import is lazy (inside
the session factory) so unit tests that inject a fake session never load the
C extension, and local-only hosts never need libnetsnmp.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

# snmp_type values meaning "no value present here".
_ABSENT_TYPES = {"NOSUCHOBJECT", "NOSUCHINSTANCE", "ENDOFMIBVIEW"}


@dataclass(frozen=True)
class SnmpRow:
    """One SNMP varbind: full numeric OID, value string, ezsnmp snmp_type."""

    oid: str
    value: str
    snmp_type: str


class SnmpError(Exception):
    """Raised when an SNMP operation fails (timeout, connection, etc.)."""


def _full_oid(oid: str, oid_index: str) -> str:
    """Reconstruct the full numeric OID from ezsnmp's (oid, oid_index) split.

    ezsnmp may return the whole numeric OID in ``oid`` with an empty
    ``oid_index`` (no MIB loaded for the subtree), or split the instance into
    ``oid_index``. Joining both — and stripping any leading dot — is correct
    either way and yields the dotted-decimal form our parsers expect.
    """
    oid = oid.lstrip(".")
    return f"{oid}.{oid_index}" if oid_index else oid


def _default_session_factory(host, community, version, timeout, retries):
    """Build a real ezsnmp.Session. ezsnmp is imported here, lazily."""
    import ezsnmp  # noqa: PLC0415 — lazy import keeps the C-ext off the unit path

    return ezsnmp.Session(
        hostname=host,
        community=community,
        version=version,
        timeout=timeout,
        retries=retries,
        use_numeric=True,
        use_long_names=True,
    )


class SnmpClient:
    """Synchronous SNMP v2c client for a single switch.

    A fresh ezsnmp.Session is created per call (stateless), so instances are
    safe to use from the PoE control service's worker threads.
    """

    def __init__(
        self,
        host: str,
        community: str,
        *,
        timeout: int = 10,
        retries: int = 1,
        write_community: Optional[str] = None,
        session_factory: Callable = _default_session_factory,
    ):
        self.host = host
        self.community = community
        self.write_community = write_community
        self.timeout = timeout
        self.retries = retries
        self._session_factory = session_factory

    def _session(self, community: str):
        return self._session_factory(
            self.host, community, 2, self.timeout, self.retries
        )

    def get(self, oid: str) -> Optional[SnmpRow]:
        """SNMP GET. Returns None for a missing OID; raises SnmpError on failure."""
        try:
            v = self._session(self.community).get(oid)
        except Exception as e:  # ezsnmp.EzSNMPError and friends
            raise SnmpError(f"GET {oid} on {self.host} failed: {e}") from e
        if v.snmp_type in _ABSENT_TYPES:
            return None
        return SnmpRow(_full_oid(v.oid, v.oid_index), v.value, v.snmp_type)

    def walk(self, oid: str) -> list[SnmpRow]:
        """SNMP WALK. NOSUCH*/ENDOFMIBVIEW rows are filtered out."""
        try:
            variables = self._session(self.community).walk(oid)
        except Exception as e:
            raise SnmpError(f"WALK {oid} on {self.host} failed: {e}") from e
        return [
            SnmpRow(_full_oid(v.oid, v.oid_index), v.value, v.snmp_type)
            for v in variables
            if v.snmp_type not in _ABSENT_TYPES
        ]

    def set_int(self, oid: str, value: int) -> bool:
        """SNMP SET an INTEGER via the write community. Returns success."""
        if not self.write_community:
            raise SnmpError(f"SET {oid} on {self.host}: no write community configured")
        try:
            return bool(self._session(self.write_community).set(oid, value, "INTEGER"))
        except Exception as e:
            raise SnmpError(f"SET {oid} on {self.host} failed: {e}") from e
