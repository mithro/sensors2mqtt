# Native Python SNMP Library Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the subprocess `snmpget`/`snmpwalk`/`snmpset` calls in the SNMP collectors with the in-process **ezsnmp** library, behind a small `SnmpClient` seam, with no change to published behaviour.

**Architecture:** A new `snmp_client.py` is the only module that imports ezsnmp (lazily). It returns structured `SnmpRow(oid, value, snmp_type)`. The regex CLI-text parsers in `collector/snmp.py` become row-consumers; the collectors receive an injectable client factory. A two-tier test suite (fast mocked unit tests + real-ezsnmp integration tests against a local `snmpsim` agent) runs in CI.

**Tech Stack:** Python ≥3.11, ezsnmp 1.1 (net-snmp C binding, synchronous), snmpsim-lextudio (test agent), pytest, uv, Debian packaging (dh-python).

**Spec:** `docs/superpowers/specs/2026-06-18-native-snmp-library-design.md`

## Global Constraints

Every task implicitly includes these (verbatim from the spec):

- **SNMP v2c only.** Every `ezsnmp.Session` is built with `version=2`. No v3.
- **Behaviour-identical.** Same OIDs, same published values, same MQTT topics, same per-switch isolation, same incremental discovery. Sensor suffixes are HA `unique_id` components and MUST NOT change.
- **ezsnmp import is localized + lazy.** Only `src/sensors2mqtt/snmp_client.py` imports `ezsnmp`, and only inside the session factory (not at module top level). No other module imports ezsnmp.
- **Session config:** `version=2, use_numeric=True, use_long_names=True`. A fresh `Session` is built per call (stateless → thread-safe for the control service's `ThreadPoolExecutor`).
- **Dependency pin:** the `snmp` extra is `ezsnmp~=1.1` (matches Debian's deployed `1.1.0`).
- **Python floor:** `requires-python = ">=3.11"`.
- **Integration tests** are marked `@pytest.mark.integration` and **skip** (never fail) when ezsnmp/snmpsim are unavailable. The deb build runs `pytest -m "not integration"`; CI runs the full suite.
- **Worktree:** `.claude/worktrees/native-snmp-library`, branch `worktree-native-snmp-library`. All paths below are relative to the worktree root. Commit after every task.

## File Structure

- **Create** `src/sensors2mqtt/snmp_client.py` — `SnmpRow`, `SnmpError`, `SnmpClient` (the ezsnmp seam). [Task 1]
- **Create** `tests/test_snmp_client.py` — unit tests for the client via a fake session. [Task 1]
- **Create** `tests/snmp_helpers.py` — `rows_from_snmpwalk_txt()` converter + `FakeSnmpClient` test double. [Task 3]
- **Create** `tests/integration/conftest.py` — `snmpsim` agent pytest fixture. [Task 2]
- **Create** `tests/integration/test_snmp_client_integration.py` — real-ezsnmp client tests. [Task 2]
- **Create** `tests/integration/test_collector_integration.py` — end-to-end poll/control against snmpsim. [Task 5]
- **Create** `tests/fixtures/snmprec/{m4300,gsm7252ps,s3300}.snmprec` — per-model recordings. [Task 2]
- **Modify** `src/sensors2mqtt/collector/snmp.py` — parsers → rows; call sites → `SnmpClient`; drop `subprocess`. [Task 3]
- **Modify** `tests/test_snmp.py` — drive the new code via `FakeSnmpClient`. [Task 3]
- **Modify** `src/sensors2mqtt/collector/snmp_control.py` — call sites → `SnmpClient`; drop `subprocess`. [Task 4]
- **Modify** `tests/test_snmp_control.py` — drive via `FakeSnmpClient`. [Task 4]
- **Modify** `pyproject.toml` — add `snmp` extra + `snmpsim-lextudio` dev dep + `integration` marker. [Task 2]
- **Modify** `debian/control` — `snmp` → `python3-ezsnmp` (both packages) + Build-Depends. [Task 6]
- **Modify** `.github/workflows/ci.yml`, `.github/workflows/deb.yml`. [Task 7]

---

### Task 1: `SnmpClient` seam

**Files:**
- Create: `src/sensors2mqtt/snmp_client.py`
- Test: `tests/test_snmp_client.py`

**Interfaces:**
- Produces:
  - `SnmpRow(oid: str, value: str, snmp_type: str)` — frozen dataclass; `oid` is the full numeric OID (no leading dot).
  - `SnmpError(Exception)`.
  - `SnmpClient(host: str, community: str, *, timeout: int = 10, retries: int = 1, write_community: str | None = None, session_factory: Callable = _default_session_factory)` with `get(oid: str) -> SnmpRow | None`, `walk(oid: str) -> list[SnmpRow]`, `set_int(oid: str, value: int) -> bool`.
  - `session_factory(host, community, version, timeout, retries) -> session` where the session has `.get(oid) -> var`, `.walk(oid) -> list[var]`, `.set(oid, value, snmp_type) -> bool`, and each `var` has `.oid`, `.oid_index`, `.value`, `.snmp_type`.

- [ ] **Step 1: Write failing tests** in `tests/test_snmp_client.py`:

```python
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
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest tests/test_snmp_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sensors2mqtt.snmp_client'`.

- [ ] **Step 3: Implement** `src/sensors2mqtt/snmp_client.py`:

```python
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
```

- [ ] **Step 4: Run — verify pass**

Run: `uv run pytest tests/test_snmp_client.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/sensors2mqtt/snmp_client.py tests/test_snmp_client.py
git add src/sensors2mqtt/snmp_client.py tests/test_snmp_client.py
git commit -m "feat(snmp): add SnmpClient ezsnmp seam with SnmpRow + SnmpError"
```

---

### Task 2: Integration harness, fixtures, and real-ezsnmp client tests

Verifies the real library's behaviour (the spec's §12 risks: OID split, MAC encoding, NOSUCH, SET) **before** migrating the call sites.

**Files:**
- Modify: `pyproject.toml` (add `snmp` extra, `snmpsim-lextudio` dev dep, `integration` marker)
- Create: `tests/integration/__init__.py` (empty), `tests/integration/conftest.py`
- Create: `tests/integration/test_snmp_client_integration.py`
- Create: `tests/fixtures/snmprec/m4300.snmprec`, `gsm7252ps.snmprec`, `s3300.snmprec`

**Interfaces:**
- Consumes: `SnmpClient` (Task 1).
- Produces: a `snmpsim_agent` pytest fixture yielding `(host, port)`; per-community snmprec fixtures named `<community>.snmprec`.

**Prerequisite (local run on ten64; CI installs these in Task 7):**
```bash
sudo apt-get update && sudo apt-get install -y libsnmp-dev build-essential
```

- [ ] **Step 1: Add deps + marker to `pyproject.toml`**

Add the optional extra (after the `ipmi` extra):
```toml
[project.optional-dependencies]
ipmi = ["requests"]
snmp = ["ezsnmp~=1.1"]
```
Add snmpsim to the dev group:
```toml
[dependency-groups]
dev = [
    "pytest",
    "ruff",
    "snmpsim-lextudio",
]
```
Register the marker under `[tool.pytest.ini_options]`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
markers = [
    "integration: real-library tests needing ezsnmp + a local snmpsim agent",
]
```

- [ ] **Step 2: Capture per-model snmprec fixtures from the live switches** (run on ten64).

snmprec format is `oid|tag|value` per line (tag = net-snmp type number; e.g. `2`=INTEGER, `4`=OCTET STRING, `66`=Gauge32, `67`=TimeTicks). Capture the subtrees the collectors read, using the read community from `/etc/sensors2mqtt/snmp.toml`. Use snmpsim's recorder:

```bash
mkdir -p tests/fixtures/snmprec
# Repeat per model+host; COMMUNITY/HOST from /etc/sensors2mqtt/snmp.toml.
# boxServices (FM 4526.10 or SMP 4526.11), PoE, ifTable, dot1q, LLDP, bridge MAC.
uv run snmpsim-record-commands \
  --agent-udpv4-endpoint=<switch-host>:161 --community=<read-community> \
  --start-oid=1.3.6.1.2.1 --stop-oid=1.3.6.1.2.1.105 \
  --output-file=tests/fixtures/snmprec/<model>.snmprec
uv run snmpsim-record-commands \
  --agent-udpv4-endpoint=<switch-host>:161 --community=<read-community> \
  --start-oid=1.3.6.1.4.1.4526 --stop-oid=1.3.6.1.4.1.4527 \
  --output-file=/tmp/<model>-ent.snmprec   # then append to <model>.snmprec
# LLDP lives under 1.0.8802 — capture and append too:
uv run snmpsim-record-commands \
  --agent-udpv4-endpoint=<switch-host>:161 --community=<read-community> \
  --start-oid=1.0.8802 --stop-oid=1.0.8803 \
  --output-file=/tmp/<model>-lldp.snmprec   # then append
```
Concatenate the captures into one `<model>.snmprec` per model, sorted by OID. **If a switch is unreachable**, hand-author a minimal `.snmprec` covering: one fan column row, one temp row, one PSU row (incl. a `Not Supported` OCTET STRING for gsm7252ps), a couple of PoE port rows, `ifOperStatus`/`ifHighSpeed`/`dot1qPvid` rows, an `ifAlias` string, a VLAN name, LLDP sysName/portDesc rows, an LLDP chassis-id MAC, and `dot1dBaseBridgeAddress` (1.3.6.1.2.1.17.1.1.0). Commit whatever is captured.

Verify each file is non-empty and parses:
```bash
wc -l tests/fixtures/snmprec/*.snmprec
head -3 tests/fixtures/snmprec/m4300.snmprec
```

- [ ] **Step 3: Write the snmpsim agent fixture** `tests/integration/conftest.py`:

```python
"""Integration test harness: a local snmpsim agent serving snmprec fixtures.

Skips the whole integration package when ezsnmp or snmpsim are unavailable
(e.g. a dev box without libsnmp-dev). CI installs both, so they always run there.
"""
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("ezsnmp", reason="ezsnmp (libnetsnmp) not installed")

SNMPREC_DIR = Path(__file__).parent.parent / "fixtures" / "snmprec"
_RESPONDER = "snmpsim-command-responder"


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def snmpsim_agent():
    """Start snmpsim serving tests/fixtures/snmprec, yield (host, port)."""
    if shutil.which(_RESPONDER) is None:
        pytest.skip("snmpsim-command-responder not on PATH")
    host, port = "127.0.0.1", _free_udp_port()
    proc = subprocess.Popen(
        [
            _RESPONDER,
            f"--data-dir={SNMPREC_DIR}",
            f"--agent-udpv4-endpoint={host}:{port}",
            "--logging-method=null",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Poll until the UDP responder answers (a GET of sysObjectID succeeds).
    deadline = time.monotonic() + 15
    import ezsnmp
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise RuntimeError(f"snmpsim exited early: {err.decode()[:500]}")
        try:
            ezsnmp.Session(
                hostname=host, remote_port=port, community="m4300", version=2,
                timeout=1, retries=0, use_numeric=True,
            ).get("1.3.6.1.2.1.1.2.0")
            ready = True
            break
        except Exception:
            time.sleep(0.3)
    if not ready:
        proc.terminate()
        pytest.skip("snmpsim agent did not become ready")
    yield host, port
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
```

Note: `SnmpClient` builds sessions with `hostname` only. To target the test port, the integration tests pass `host=f"{host}:{port}"` (ezsnmp parses `host:port` — verified in `session.py`).

- [ ] **Step 4: Write the real-ezsnmp client tests** `tests/integration/test_snmp_client_integration.py`:

```python
"""Real ezsnmp against a local snmpsim agent. Marked integration."""
import pytest

from sensors2mqtt.collector.snmp import _FM_BOX
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
    # Document the real MAC encoding so Task 3's format_mac handles it.
    assert row.snmp_type in ("OCTETSTR", "HEX-STRING", "STRING")


def test_get_missing_oid_returns_none(snmpsim_agent):
    assert client(snmpsim_agent).get("1.3.6.1.4.1.4526.99.99.0") is None
```

For the **SET round-trip**, add a writable OID to one fixture (e.g. `gsm7252ps.snmprec`) using snmpsim's `writecache` variation: a line `1.3.6.1.2.1.105.1.1.1.3.1.1|2:writecache|2` makes that PoE admin OID readable+writable. Then:
```python
def test_set_int_round_trips(snmpsim_agent):
    c = client(snmpsim_agent, community="gsm7252ps", write_community="gsm7252ps")
    oid = "1.3.6.1.2.1.105.1.1.1.3.1.1"
    assert c.set_int(oid, 1) is True
    assert c.get(oid).value == "1"
```

- [ ] **Step 5: Run integration tests** (on ten64, with libsnmp-dev installed)

Run: `uv sync --dev --extra ipmi --extra snmp && uv run pytest tests/integration -v -m integration`
Expected: PASS (or `skip` if ezsnmp/snmpsim genuinely unavailable). **Record the real MAC `snmp_type` and `value` form** observed in `test_get_bridge_mac_decodes` — Task 3's `format_mac` must handle it.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tests/integration tests/fixtures/snmprec
git commit -m "test(snmp): snmpsim integration harness + real-ezsnmp client tests + fixtures"
```

---

### Task 3: Migrate `collector/snmp.py` to `SnmpClient`

Cohesive task: the parsers and their call sites change together so the module stays green. Drives the rewritten Tier-1 tests via a fake client.

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp.py`
- Create: `tests/snmp_helpers.py`
- Modify: `tests/test_snmp.py`

**Interfaces:**
- Consumes: `SnmpClient`, `SnmpRow`, `SnmpError` (Task 1).
- Produces (new/changed signatures other code relies on):
  - `parse_snmpwalk(rows: list[SnmpRow]) -> list[tuple[int, str]]`
  - `parse_box_walk(rows: list[SnmpRow], base_oid: str) -> list[tuple[str, str]]`
  - `parse_lldp_walk(rows: list[SnmpRow], field_oid: str) -> dict[int, str]`
  - `parse_lldp_chassis_ids(rows: list[SnmpRow]) -> dict[int, str]`
  - `format_mac(row: SnmpRow) -> str | None` (new; replaces the two-format text branch in `fetch_bridge_mac`)
  - `fetch_bridge_mac(client: SnmpClient, name: str) -> str | None`
  - `fetch_lldp_chassis_macs(client: SnmpClient, name: str) -> dict[int, str]`
  - `SnmpCollector(config=None, switches=None, client_factory: Callable[[SwitchConfig], SnmpClient] | None = None)`; helper `SnmpCollector._client(switch) -> SnmpClient` (cached per node_id).
  - `parse_snmpget_value` is **removed**; `parse_hex_mac`, `snmpget_value`, `box_entity` are unchanged.

- [ ] **Step 1: Add the test helper** `tests/snmp_helpers.py`:

```python
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
```

- [ ] **Step 2: Rewrite the parsers** in `collector/snmp.py`. Remove `import subprocess`; add `from sensors2mqtt.snmp_client import SnmpClient, SnmpError, SnmpRow`. Replace the parser block with:

```python
def parse_snmpwalk(rows: list[SnmpRow]) -> list[tuple[int, str]]:
    """[(last_oid_component, value), ...] for rows whose final arc is numeric."""
    results = []
    for row in rows:
        last = row.oid.rsplit(".", 1)[-1]
        if last.isdigit():
            results.append((int(last), row.value))
    return results


def parse_box_walk(rows: list[SnmpRow], base_oid: str) -> list[tuple[str, str]]:
    """[(instance, value), ...] where instance is the OID suffix under base_oid.

    Keeps the whole suffix (e.g. "1.0" vs "0") so stacked-unit instances stay
    distinct, matching the previous behaviour.
    """
    prefix = base_oid + "."
    results = []
    for row in rows:
        if not row.oid.startswith(prefix):
            continue
        instance = row.oid[len(prefix):]
        if not re.fullmatch(r"\d+(\.\d+)*", instance):
            continue
        results.append((instance, row.value))
    return results


def parse_lldp_walk(rows: list[SnmpRow], field_oid: str) -> dict[int, str]:
    """{local_port: value} from an LLDP remote-table field walk.

    Index is {timeMark}.{localPortNum}.{remIndex}; localPortNum is the middle.
    """
    results: dict[int, str] = {}
    pattern = re.compile(rf"\.{field_oid}\.(\d+)\.(\d+)\.(\d+)$")
    for row in rows:
        m = pattern.search("." + row.oid)
        if not m:
            continue
        port = int(m.group(2))
        if row.value and port not in results:
            results[port] = row.value
    return results


def parse_lldp_chassis_ids(rows: list[SnmpRow]) -> dict[int, str]:
    """{local_port: mac} from lldpRemChassisId rows that are 6-byte MACs."""
    results: dict[int, str] = {}
    pattern = re.compile(r"\.5\.(\d+)\.(\d+)\.(\d+)$")
    for row in rows:
        m = pattern.search("." + row.oid)
        if not m:
            continue
        port = int(m.group(2))
        mac = parse_hex_mac(row.value)
        if mac and port not in results:
            results[port] = mac
    return results
```

Keep `parse_hex_mac`, `box_entity`, `_BOX_KIND_LABELS`, `snmpget_value` exactly as they are. **Delete** `parse_snmpget_value`.

- [ ] **Step 3: Add `format_mac` + rewrite `fetch_bridge_mac` / `fetch_lldp_chassis_macs`** to take a client. Use the MAC encoding observed in Task 2 Step 5. The tolerant adapter (handles colon-hex, space-hex, and 6 raw bytes):

```python
def format_mac(row: SnmpRow) -> str | None:
    """Format an ezsnmp MAC OCTET STRING row as 'aa:bb:cc:dd:ee:ff', or None."""
    v = row.value.strip()
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}[:\- ]){5}[0-9A-Fa-f]{2}", v):
        return v.replace("-", ":").replace(" ", ":").lower()
    if len(v) == 6:  # raw bytes
        return ":".join(f"{ord(c):02x}" for c in v)
    return parse_hex_mac(v)  # space-separated hex fallback


def fetch_bridge_mac(client: SnmpClient, name: str) -> str | None:
    """Fetch switch base MAC via dot1dBaseBridgeAddress, or None."""
    try:
        row = client.get(BRIDGE_MAC_OID)
    except SnmpError as e:
        log.warning("%s: bridge MAC fetch failed: %s", name, e)
        return None
    if row is None:
        return None
    mac = format_mac(row)
    if mac is None:
        log.debug("%s: unrecognised bridge MAC value %r", name, row.value)
    return mac


def fetch_lldp_chassis_macs(client: SnmpClient, name: str) -> dict[int, str]:
    """Fetch LLDP neighbour chassis MACs per local port."""
    try:
        rows = client.walk(LLDP_CHASSIS_OID)
    except SnmpError as e:
        log.warning("%s: LLDP chassis MAC walk failed: %s", name, e)
        return {}
    macs = parse_lldp_chassis_ids(rows)
    if macs:
        log.info("%s: fetched %d LLDP chassis MACs", name, len(macs))
    return macs
```

- [ ] **Step 4: Add DI + `_client` to `SnmpCollector`**, and rewrite every call site. In `__init__` add the factory:

```python
def _default_client_factory(switch: SwitchConfig) -> SnmpClient:
    return SnmpClient(switch.host, switch.community,
                      write_community=switch.write_community)
```
```python
    def __init__(self, config=None, switches=None, client_factory=None):
        self.config = config or MqttConfig.from_env()
        self.switches = switches if switches is not None else load_config()
        self._client_factory = client_factory or _default_client_factory
        self._clients: dict[str, SnmpClient] = {}
        # ... existing cache dicts unchanged ...

    def _client(self, switch: SwitchConfig) -> SnmpClient:
        c = self._clients.get(switch.node_id)
        if c is None:
            c = self._client_factory(switch)
            self._clients[switch.node_id] = c
        return c
```
Rewrite each call site to use `client = self._client(switch)` and catch `SnmpError`:
- **`poll_switch`** — replace the three `subprocess.run` blocks:
  - static sensors: `row = client.get(sensor.oid)`; `raw = row.value if row else None`; then `snmpget_value(raw, ...)` as before; wrap in `try/except SnmpError: log.warning(...)`.
  - box walks: `rows = client.walk(box.base_oid)`; `for instance, raw in parse_box_walk(rows, box.base_oid):` — body (Not-Supported skip, int parse, ordinal sort) unchanged.
  - walk sensors: `rows = client.walk(walk_def.base_oid)`; `for index, raw in parse_snmpwalk(rows):` — body unchanged.
- **`_walk_int_table`**: `rows = client.walk(oid)`; `for index, val in parse_snmpwalk(rows):` — body unchanged; `except SnmpError` returns `{}`.
- **`fetch_port_descriptions`**: `rows = client.walk(IF_ALIAS_OID)`; iterate `rows`: `port = int(row.oid.rsplit(".",1)[-1]); alias = row.value.strip()` (the row is an OCTETSTR; no quote stripping). `success = True` on no exception; `except SnmpError` leaves `success=False`.
- **`fetch_vlan_names`**: `rows = client.walk(VLAN_NAME_OID)`; `for index, val in parse_snmpwalk(rows): if val: names[index]=val`.
- **`fetch_lldp_neighbors`**: for each `field_oid` in `("9","8")`: `rows = client.walk(f"{LLDP_REM}.{field_oid}"); target.update(parse_lldp_walk(rows, field_oid))`; `except SnmpError: success=False`.
- **`get_device_info`**: `mac = fetch_bridge_mac(self._client(switch), switch.name)`.

In `main()`, `fetch_lldp_chassis_macs(switch)` → build/reuse a client: `fetch_lldp_chassis_macs(collector._client(switch), switch.name)`.

- [ ] **Step 5: Rewrite `tests/test_snmp.py`** to drive the fake client. Mechanical transformation rule applied throughout:
  1. Delete the `TestParseSnmpgetValue` class and the `parse_snmpget_value` import.
  2. Change parser tests to build rows: replace each text input with `rows_from_snmpwalk_txt(text)` (or literal `SnmpRow(...)` lists) and call the new signatures. Example:

```python
from tests.snmp_helpers import FakeSnmpClient, rows_from_snmpwalk_txt

class TestParseSnmpwalk:
    def test_parses_gauge32(self):
        rows = rows_from_snmpwalk_txt(
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.1 = Gauge32: 3300\n"
            "iso.3.6.1.4.1.4526.10.15.1.1.1.2.1.2 = Gauge32: 2500\n"
        )
        assert parse_snmpwalk(rows) == [(1, "3300"), (2, "2500")]

    def test_fixture_m4300_fans(self):
        rows = rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_m4300_fans.txt").read_text())
        result = parse_snmpwalk(rows)
        speeds = [(i, v) for i, v in result if v.isdigit() and int(v) > 100]
        assert len(speeds) >= 2
```
  `parse_box_walk` tests: pass `rows_from_snmpwalk_txt(output)` plus the same `BASE`; assertions unchanged.
  3. Replace `@patch("sensors2mqtt.collector.snmp.subprocess.run")` + `_box_walk_side_effect({suffix: text})` with a `FakeSnmpClient(walk_rows={suffix: rows_from_snmpwalk_txt(text)})` injected via `client_factory`. Helper for the collector tests:

```python
def collector_with(switch, *, walk_rows=None, get_rows=None):
    from sensors2mqtt.base import MqttConfig
    cfg = MqttConfig(host="test", port=1883, user="u", password="p")
    fake = FakeSnmpClient(walk_rows=walk_rows or {}, get_rows=get_rows or {})
    return SnmpCollector(config=cfg, switches=[switch],
                         client_factory=lambda sw: fake)
```
  Example rewrite of `test_poll_switch_success`:
```python
def test_poll_switch_success(self):
    sw = _make_switch("test-m4300", "m4300")
    collector = collector_with(sw, walk_rows={
        ".6.1.4": rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_m4300_fans.txt").read_text()),
        ".15.1.3": rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_m4300_thermal.txt").read_text()),
        ".8.1.5": rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_m4300_psu.txt").read_text()),
    })
    assert collector.poll_switch(sw) == {
        "fan1_rpm": 5280, "fan2_rpm": 4560, "temp": 65, "psu_power": 65,
    }
```
  4. The all-fail / timeout tests become a `FakeSnmpClient(walk_error=(".6.1.4", ".15.1.3", ".8.1.5"))` (raises `SnmpError`) and assert `poll_switch(sw) is None`.
  5. `fetch_vlan_names` / `fetch_lldp_neighbors` tests: inject `walk_rows` keyed by OID suffix (`"4.3.1.1"` for VLAN names; `".9"`/`".8"` for LLDP); assertions and cache checks unchanged (cache identity still holds — the collector caches results, not the client).
  6. `parse_lldp_chassis_ids` / `parse_hex_mac` tests: feed `rows_from_snmpwalk_txt(...)`; `parse_hex_mac` tests unchanged.
  7. `test_connection_status_topic_for_snmp` and `test_port_discovery_drops_bridge_and_has_expire_after` unchanged.

- [ ] **Step 6: Run + verify the module has no subprocess**

Run: `uv run pytest tests/test_snmp.py -q`
Expected: PASS.
Run: `grep -n subprocess src/sensors2mqtt/collector/snmp.py || echo "no subprocess"`
Expected: `no subprocess`.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check src/sensors2mqtt/collector/snmp.py tests/test_snmp.py tests/snmp_helpers.py
git add src/sensors2mqtt/collector/snmp.py tests/test_snmp.py tests/snmp_helpers.py
git commit -m "refactor(snmp): migrate sensor collector to SnmpClient (no subprocess)"
```

---

### Task 4: Migrate `collector/snmp_control.py` to `SnmpClient`

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp_control.py`
- Modify: `tests/test_snmp_control.py`

**Interfaces:**
- Consumes: `SnmpClient`, `SnmpError` (Task 1); `fetch_lldp_chassis_macs(client, name)`, `parse_snmpwalk(rows)` (Task 3).
- Produces: `PoeController(mqtt_config, switches, poll_interval=30, client_factory=None)`; `PoeController._client(switch) -> SnmpClient`.

- [ ] **Step 1: Write/adjust failing tests** in `tests/test_snmp_control.py`. Replace the `@patch(...subprocess.run)` mocks with an injected `FakeSnmpClient`. Add a controller builder:

```python
from tests.snmp_helpers import FakeSnmpClient, rows_from_snmpwalk_txt

def controller_with(switches, *, walk_rows=None, get_rows=None, set_ok=True, fakes=None):
    from sensors2mqtt.collector.snmp_control import PoeController
    from sensors2mqtt.base import MqttConfig
    cfg = MqttConfig(host="test", port=1883, user="u", password="p")
    # one shared fake unless per-switch fakes provided
    fake = FakeSnmpClient(walk_rows=walk_rows or {}, get_rows=get_rows or {}, set_ok=set_ok)
    ctrl = PoeController(mqtt_config=cfg, switches=switches,
                         client_factory=lambda sw: (fakes or {}).get(sw.node_id, fake))
    return ctrl, fake
```
Representative tests:
```python
def test_poll_all_ports_sets_state():
    sw = _make_switch("test-gsm7252ps", "gsm7252ps")  # has write_community
    admin = rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_gsm7252ps_poe_admin.txt").read_text())
    detect = rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_gsm7252ps_poe_detect.txt").read_text())
    oper = rows_from_snmpwalk_txt((FIXTURES / "snmpwalk_gsm7252ps_ifoperstatus.txt").read_text())
    ctrl, _ = controller_with([sw], walk_rows={
        "105.1.1.1.3.1": admin, "105.1.1.1.6.1": detect, "2.2.1.8": oper,
    })
    ctrl.poll_all_ports(sw)
    st = ctrl._port_states[sw.node_id][1]
    assert st.poe_admin in (1, 2)

def test_handle_toggle_sets_via_client():
    sw = _make_switch("test-gsm7252ps", "gsm7252ps")
    admin_row = SnmpRow("1.3.6.1.2.1.105.1.1.1.3.1.1", "1", "INTEGER")
    detect_row = SnmpRow("1.3.6.1.2.1.105.1.1.1.6.1.1", "3", "INTEGER")
    oper_row = SnmpRow("1.3.6.1.2.1.2.2.1.8.1", "1", "INTEGER")
    ctrl, fake = controller_with([sw], get_rows={
        "105.1.1.1.3.1.1": admin_row, "105.1.1.1.6.1.1": detect_row, "2.2.1.8.1": oper_row,
    })
    ctrl._client = lambda s: fake  # ensure shared fake
    ctrl._handle_toggle(sw, 1, "ON")
    assert (f"{ '1.3.6.1.2.1.105.1.1.1.3.1' }.1", 1) in fake.sets
```
(Import `SnmpRow` from `sensors2mqtt.snmp_client` at the top of the test module.)

- [ ] **Step 2: Run — verify failure** (`PoeController` has no `client_factory` yet)

Run: `uv run pytest tests/test_snmp_control.py -q`
Expected: FAIL (`TypeError: unexpected keyword 'client_factory'` / `AttributeError`).

- [ ] **Step 3: Implement.** In `snmp_control.py` remove `import subprocess` and `import re`-based parsing of CLI; add `from sensors2mqtt.snmp_client import SnmpClient, SnmpError`. Reuse the snmp module's default factory:
```python
from sensors2mqtt.collector.snmp import (
    SwitchConfig, _build_port_device, _default_client_factory,
    fetch_lldp_chassis_macs, load_config, parse_snmpwalk,
)
```
Add to `PoeController.__init__`:
```python
        self._client_factory = client_factory or _default_client_factory
        self._clients: dict[str, SnmpClient] = {}
```
```python
    def _client(self, switch: SwitchConfig) -> SnmpClient:
        c = self._clients.get(switch.node_id)
        if c is None:
            c = self._client_factory(switch)
            self._clients[switch.node_id] = c
        return c
```
Rewrite the three call sites:
- **`_snmpget_int`**:
```python
    def _snmpget_int(self, switch, oid, port):
        full_oid = f"{oid}.{port}"
        try:
            row = self._client(switch).get(full_oid)
        except SnmpError as e:
            log.warning("%s: get %s failed: %s", switch.name, full_oid, e)
            return None
        if row is None:
            return None
        m = re.match(r"(\d+)", row.value)
        return int(m.group(1)) if m else None
```
- **`_snmpset_int`**:
```python
    def _snmpset_int(self, switch, oid, port, value):
        full_oid = f"{oid}.{port}"
        try:
            ok = self._client(switch).set_int(full_oid, value)
        except SnmpError as e:
            log.error("%s: set %s=%d failed: %s", switch.name, full_oid, value, e)
            return False
        if ok:
            log.info("%s: set %s=%d ok", switch.name, full_oid, value)
        return ok
```
- **`poll_all_ports`**: replace each `subprocess.run` + `parse_snmpwalk(result.stdout)` with `rows = self._client(switch).walk(oid)` inside `try/except SnmpError`, then `for index, val in parse_snmpwalk(rows):` (body unchanged). Remove the local `from ...snmp import parse_snmpwalk` (now imported at top).
- **`run()`**: `fetch_lldp_chassis_macs(sw)` → `fetch_lldp_chassis_macs(self._client(sw), sw.name)`.

Keep `_snmpget_int`'s `re` use → keep `import re`.

- [ ] **Step 4: Run + verify no subprocess**

Run: `uv run pytest tests/test_snmp_control.py -q`
Expected: PASS.
Run: `grep -n subprocess src/sensors2mqtt/collector/snmp_control.py || echo "no subprocess"`
Expected: `no subprocess`.

- [ ] **Step 5: Full suite + lint + commit**

```bash
uv run pytest -q -m "not integration"
uv run ruff check
git add src/sensors2mqtt/collector/snmp_control.py tests/test_snmp_control.py
git commit -m "refactor(snmp): migrate PoE control service to SnmpClient (no subprocess)"
```
Expected: full unit suite green.

---

### Task 5: End-to-end integration tests (collector + control vs snmpsim)

**Files:**
- Create: `tests/integration/test_collector_integration.py`

**Interfaces:**
- Consumes: `snmpsim_agent` fixture (Task 2), `SnmpCollector`/`PoeController` (Tasks 3–4).

- [ ] **Step 1: Write end-to-end integration tests:**

```python
"""End-to-end: real ezsnmp + snmpsim through the collectors. Marked integration."""
import pytest

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.snmp import MODELS, SnmpCollector, SwitchConfig
from sensors2mqtt.snmp_client import SnmpClient

pytestmark = pytest.mark.integration


def _switch(name, model_name, host, community):
    m = MODELS[model_name]
    return SwitchConfig(
        node_id=name.replace("-", "_"), name=name, host=host, community=community,
        manufacturer=m.manufacturer, model=m.model, port_count=m.port_count,
        poe_port_count=m.poe_port_count, sensors=list(m.sensors),
        walk_sensors=list(m.walk_sensors), box_walks=list(m.box_walks),
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
```

Add an end-to-end PoE control test only if the `gsm7252ps.snmprec` fixture includes the writable PoE admin OID (Task 2 Step 4): build a `PoeController` pointed at the agent, call `poll_all_ports` then `_handle_toggle(sw, 1, "OFF")`, and assert the subsequent `_snmpget_int` reflects `2` (disabled).

- [ ] **Step 2: Run** (on ten64): `uv run pytest tests/integration -v -m integration` → PASS or skip.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_collector_integration.py
git commit -m "test(snmp): end-to-end collector integration tests against snmpsim"
```

---

### Task 6: Packaging — swap `snmp` → `python3-ezsnmp`

**Files:**
- Modify: `debian/control`

**Interfaces:** Consumes the `snmp` extra added in Task 2.

- [ ] **Step 1: Edit `debian/control`.** In **both** `sensors2mqtt-snmp` and `sensors2mqtt-snmp-control` stanzas, replace the `snmp,` dependency line with `python3-ezsnmp,`. Add `python3-ezsnmp,` to `Build-Depends` (after `python3-requests,`).

- [ ] **Step 2: Verify no stray `snmp` CLI dependency remains**

Run: `grep -nE '^\s*snmp,' debian/control || echo "no snmp CLI dep"`
Expected: `no snmp CLI dep`.
Run: `grep -c python3-ezsnmp debian/control`
Expected: `3` (one Build-Depends + two binary packages).

- [ ] **Step 3: Commit**

```bash
git add debian/control
git commit -m "build(deb): depend on python3-ezsnmp, drop net-snmp CLI (snmp)"
```

---

### Task 7: CI + deb workflow wiring

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/deb.yml`

- [ ] **Step 1: `ci.yml` — install net-snmp dev headers, the snmp extra, and run both tiers.** Replace the steps after `setup-uv` with:

```yaml
      - run: uv python install ${{ matrix.python-version }}
      - name: Install net-snmp build deps (for ezsnmp)
        run: sudo apt-get update && sudo apt-get install -y libsnmp-dev
      - run: uv sync --dev --extra ipmi --extra snmp --python ${{ matrix.python-version }}
      - run: uv run pytest -v
      - run: uv run ruff check
```
(`snmpsim-lextudio` is in the dev group, so `uv sync --dev` installs it; the integration tests start it themselves and run because ezsnmp + snmpsim are now present.)

- [ ] **Step 2: `deb.yml` — make ezsnmp importable at build time, keep the build test phase to unit tests.** In the `Install build dependencies` step, add `python3-ezsnmp` to the apt install list. The deb build's pybuild test phase must skip integration; add a `debian/rules` override if the build runs pytest. Confirm whether the build runs tests:

Run: `grep -n "PYBUILD\|pytest\|auto_test\|nocheck" debian/rules debian/control || echo "no explicit test config"`

If the build runs pytest (pybuild default), add to `debian/rules`:
```makefile
export PYBUILD_TEST_ARGS=-m "not integration"
```
(placed before the `%:` / `dh $@` line). If `debian/rules` doesn't exist or tests aren't run at build, no change is needed beyond the Build-Depends.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml .github/workflows/deb.yml debian/rules
git commit -m "ci: run real ezsnmp + snmpsim integration tests; keep deb build hermetic"
```

- [ ] **Step 4: Final full-suite verification**

Run (on ten64, libsnmp-dev installed): `uv sync --dev --extra ipmi --extra snmp && uv run pytest -v`
Expected: all unit tests pass; integration tests pass (or skip with a clear reason if the agent can't start).
Run: `uv run ruff check`
Expected: clean.

---

## Self-Review

**Spec coverage:**
- §1 scope / v2c / behaviour-identical → Global Constraints; Tasks 3–4 preserve OIDs/values/topics; suffix stability noted.
- §2 ezsnmp decision → recorded in spec; plan consumes it.
- §3 verified API → Task 1 (`version=2`, `use_numeric`, `SNMPVariable` attrs, exceptions, lazy C-ext import).
- §4 SnmpClient seam → Task 1.
- §5 parser rewrite → Task 3 Step 2 (+ `parse_snmpget_value` deleted; `parse_hex_mac`/`snmpget_value`/`box_entity` kept).
- §6 call-site map → Task 3 Step 4 + Task 4 Step 3 (every site listed).
- §7 error handling/isolation → `SnmpError` + per-site `except SnmpError` (Tasks 1/3/4).
- §8 concurrency → per-call Session (Task 1); per-switch client cache.
- §9 packaging → Task 2 Step 1 (extra) + Task 6 (debian/control).
- §10 testing two tiers → Tasks 2/3/4/5; `rows_from_snmpwalk_txt` reuses real fixtures; snmpsim integration.
- §11 CI/deb → Task 7.
- §12 risks → Task 2 verifies OID split (Step 4), MAC encoding (Step 5 records it; Task 3 `format_mac` handles it), SET (writecache), version pin (`ezsnmp~=1.1`).

**Placeholder scan:** the only `<...>` placeholders are the live switch host/community in Task 2 Step 2 (necessarily site-specific, sourced from `/etc/sensors2mqtt/snmp.toml`) and the deb-rules conditional in Task 7 Step 2 (guarded by a grep). No "TBD"/"handle errors"/"similar to" placeholders.

**Type consistency:** `SnmpRow(oid, value, snmp_type)` used uniformly; parser signatures `(rows[, base/field])` consistent between Task 3 definition and Task 4 reuse; `client_factory`/`_client` identical across `SnmpCollector` and `PoeController`; `fetch_bridge_mac(client, name)` / `fetch_lldp_chassis_macs(client, name)` consistent between definition (Task 3) and caller (Task 4).
