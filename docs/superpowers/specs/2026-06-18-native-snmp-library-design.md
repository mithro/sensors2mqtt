# Native Python SNMP Library Migration — Design

- **Task:** #33 (GitHub issue [#21](https://github.com/mithro/sensors2mqtt/issues/21))
- **Date:** 2026-06-18
- **Status:** Approved — proceeding to implementation plan

## 1. Goal & Scope

Replace the subprocess-based SNMP (`snmpget`/`snmpwalk`/`snmpset` via `subprocess.run`)
in `collector/snmp.py` and `collector/snmp_control.py` with a native, in-process
Python SNMP library (**ezsnmp**).

**In scope**

- SNMP **v2c only** — read community for GET/WALK, separate write community for SET.
- Behaviour **identical**: same OIDs, same published values, same MQTT topics, same
  per-switch isolation (one switch down never blocks others), same incremental HA
  discovery.
- Replace the regex CLI-text parsing with structured access to typed SNMP results.
- Drop the `snmp` (net-snmp CLI) Debian dependency; add `python3-ezsnmp`.
- CI exercises the **real** ezsnmp library; a real-library integration test suite.

**Non-goals (YAGNI)**

- No SNMP v3 (ezsnmp supports it; we have no need).
- No changes to switch models, OID tables, sensor definitions, topic structure, or
  HA discovery semantics.
- No new sensors (SFP temperature etc. are separate tasks #40/#41).
- Not removing net-snmp from the system entirely (that would require pysnmp + async
  + trixie-backports — see §2).

## 2. Why ezsnmp (decision record)

**Hard constraint:** the collectors ship as **system `.deb`s** that depend on
`python3-*` Debian packages (e.g. `python3-paho-mqtt`); they cannot pip-install. The
SNMP library must therefore be **apt-installable across the fleet**.

**Fleet distros:** most hosts are **Debian trixie** (13, stable); ten64 (the only
current SNMP host) is **forky/sid**. The dependency must resolve on trixie *and*
forky.

**Availability (verified via `rmadison` against the canonical archive + `apt-cache`
on big-storage, a live trixie host):**

| Library | trixie main | sid/forky | API | Fleet-uniform |
|---|---|---|---|---|
| **ezsnmp** | `1.1.0-2` | `1.1.0-2.1` | **sync** | ✅ yes |
| easysnmp | `0.2.6` | `0.2.6` | sync | ✅ but upstream-unmaintained |
| pysnmp 7 (async) | ❌ (only `trixie-backports` `7.1.21~bpo13`) | `7.1.22` | asyncio | ❌ |
| pysnmp 4.4.12 (`python3-pysnmp4`) | ✅ trixie main | ❌ (name → 7.x on sid) | sync (legacy) | ❌ name clash |
| puresnmp | ❌ not in Debian | ❌ | — | ❌ |

The decisive point against pysnmp: on **trixie main** the only pysnmp is the legacy
**4.4.12** (sync, package `python3-pysnmp4`), while on sid that *same package name*
resolves to **7.x (async)** — a single dependency cannot target pysnmp uniformly
without forcing trixie-backports onto every SNMP host *and* taking an async rewrite.

**Decision: ezsnmp.** It is the only candidate that is uniform across the fleet
(trixie main + sid), **synchronous** (matches the synchronous poll loop and the PoE
control service's `ThreadPoolExecutor` model), actively maintained, and drops the
`snmp` CLI package. (ezsnmp is the maintained successor to easysnmp.)

## 3. Verified ezsnmp 1.1.0 API (read from the package source)

Confirmed by extracting `python3-ezsnmp_1.1.0-2.1_arm64.deb` and reading
`session.py` / `variables.py` / `exceptions.py` / `ez.py`:

- `Session(hostname, version=3, community="public", timeout=1, retries=3,
  remote_port=0, use_numeric=False, use_long_names=False,
  abort_on_nonexistent=False, ...)`:
  - **`version` defaults to 3** → we must pass `version=2` (== v2c).
  - `timeout` is **seconds** (per try); `retries` is retries before failure.
  - **`use_numeric=True` is required** or OIDs come back as MIB *names*; with it they
    are dotted-decimal and no MIBs need to be loaded. `use_long_names=True` is
    recommended alongside it.
  - `abort_on_nonexistent=False` (default) → a missing OID returns a row whose
    `snmp_type` is `NOSUCHOBJECT`/`NOSUCHINSTANCE` rather than raising.
- `SNMPVariable` exposes exactly `.oid`, `.oid_index`, `.value` (str — all values are
  string-coerced), `.snmp_type` (str, e.g. `INTEGER`, `GAUGE`, `OCTETSTR`,
  `NOSUCHOBJECT`).
- Methods: `get`, `get_next`, `get_bulk`, `walk`, `bulkwalk`,
  `set(oid, value, snmp_type) -> bool`, `set_multiple`.
- Exceptions: `EzSNMPError` ⊃ `EzSNMPConnectionError` ⊃ `EzSNMPTimeoutError`; plus
  `EzSNMPNoSuchObjectError` / `EzSNMPNoSuchInstanceError`.
- The package is a **C-extension** (`interface.*.so`) — importing `ezsnmp` requires
  net-snmp (libnetsnmp) present and a built extension matching the interpreter ABI.

## 4. Architecture — the `SnmpClient` seam

Introduce one new module, `src/sensors2mqtt/snmp_client.py`. It is the **only** place
that imports `ezsnmp`, and it imports it **lazily** (inside the constructor / first
use) so unit tests that inject a fake never trigger the C-extension import, and
local-only/RPi hosts never need libnetsnmp.

```python
@dataclass(frozen=True)
class SnmpRow:
    oid: str        # full numeric OID (reconstructed; see below)
    value: str      # raw value string as ezsnmp returns it
    snmp_type: str  # ezsnmp snmp_type, e.g. "INTEGER", "GAUGE", "OCTETSTR"

class SnmpError(Exception):
    """Wraps ezsnmp errors (timeout/connection/etc.) for callers to catch."""

class SnmpClient:
    def __init__(self, host: str, community: str, *,
                 timeout: int = 10, retries: int = 1,
                 write_community: str | None = None): ...
    def get(self, oid: str) -> SnmpRow | None: ...      # None on NOSUCH*/missing
    def walk(self, oid: str) -> list[SnmpRow]: ...       # NOSUCH*/ENDOFMIBVIEW filtered
    def set_int(self, oid: str, value: int) -> bool: ... # uses write_community
```

- Each call builds an `ezsnmp.Session(version=2, community=…, timeout=…, retries=…,
  use_numeric=True, use_long_names=True)`. (Per-call Session — see §8 concurrency.)
- **Full OID reconstruction** (MIB-independent): `full = row.oid` when `oid_index`
  is empty, else `f"{row.oid}.{row.oid_index}"`. This behaves identically for
  un-MIB'd Netgear enterprise OIDs and for MIB-2 OIDs, and preserves the existing
  "strip a known base OID prefix to get the instance" semantics.
- `walk()` filters out rows whose `snmp_type` is `NOSUCHOBJECT`, `NOSUCHINSTANCE`, or
  `ENDOFMIBVIEW`; `get()` returns `None` for those.
- ezsnmp exceptions are caught and re-raised as `SnmpError` (so callers have one
  exception type to catch).
- **Dependency injection:** `SnmpCollector` and `PoeController` accept an optional
  client factory (`Callable[[SwitchConfig], SnmpClient]`), defaulting to one that
  builds a real `SnmpClient` from the switch's host/community/write_community. Tests
  inject a fake factory.

## 5. Parser rewrite (data flow)

The regex text-parsers become trivial row-consumers; the *semantic* logic is
unchanged.

- `parse_snmpget_value` — **deleted** (the client returns `.value` directly).
- `parse_snmpwalk`, `parse_box_walk`, `parse_lldp_walk`, `parse_lldp_chassis_ids` —
  rewritten to take `list[SnmpRow]` (full numeric OIDs) and perform the same
  extraction they do today: strip the known base OID to get the instance
  (`parse_box_walk`), take the last component (`parse_snmpwalk`), pull the middle of
  the three-part LLDP index (`parse_lldp_walk`/`parse_lldp_chassis_ids`).
- **Kept as-is:** `box_entity` ordinal ordering and stable-suffix scheme, the literal
  `"Not Supported"` skip, FQDN→short-hostname stripping, `snmpget_value` numeric
  coercion + scaling, `parse_hex_mac`.
- **MAC handling:** Hex detection moves from `"Hex-STRING:" in line` to checking
  `snmp_type == "OCTETSTR"` plus the raw value; `parse_hex_mac` is adapted to whatever
  byte/hex representation ezsnmp yields. **Verified on real hardware** (M4300 returns
  a colon-`STRING`; GSM7252PS/S3300 return `Hex-STRING`).

## 6. Call-site migration map (exhaustive)

`collector/snmp.py`:
- `fetch_bridge_mac` → `client.get(BRIDGE_MAC_OID)` → format MAC from the row.
- `poll_switch`: static `sensors` → `client.get`; `box_walks` → `client.walk` +
  `parse_box_walk(rows)`; `walk_sensors` → `client.walk` + `parse_snmpwalk(rows)`.
- `_walk_int_table` → `client.walk` + `parse_snmpwalk(rows)`.
- `fetch_port_descriptions` (ifAlias) → `client.walk`; consume `OCTETSTR` rows.
- `fetch_vlan_names` → `client.walk` + `parse_snmpwalk(rows)`.
- `fetch_lldp_neighbors` → `client.walk` on `.9`/`.8` + `parse_lldp_walk(rows)`.
- `fetch_lldp_chassis_macs` → `client.walk` + `parse_lldp_chassis_ids(rows)`.

`collector/snmp_control.py`:
- `_snmpget_int` → `client.get`.
- `_snmpset_int` → `client.set_int` (write community).
- `poll_all_ports` → `client.walk` + `parse_snmpwalk(rows)`.

## 7. Error handling & per-switch isolation

`SnmpClient` catches `EzSNMPError` (incl. `EzSNMPTimeoutError`) and raises `SnmpError`.
Callers catch `SnmpError` exactly where they currently catch
`subprocess.TimeoutExpired` / `Exception`, emit the same warnings, and continue — so
per-switch isolation and the existing log surface are preserved. `NOSUCH*` /
`ENDOFMIBVIEW` rows are filtered by the client (mirroring today's ignoring of
"No Such Object" CLI lines).

## 8. Concurrency

The PoE control service dispatches SET/cycle operations across a
`ThreadPoolExecutor(max_workers=4)`. ezsnmp `Session` thread-safety is undocumented,
so `SnmpClient` builds a **fresh Session per call** (stateless wrapper) — thread-safe
by isolation, and still far cheaper than today's per-call `fork+exec`. The sensor
collector is single-threaded sequential and unaffected.

## 9. Packaging

- `pyproject.toml`: add an optional extra `snmp = ["ezsnmp~=1.1"]` (mirrors the
  existing `ipmi = ["requests"]`). Pin to the `1.1` line so PyPI ezsnmp used in CI
  matches the Debian-deployed `1.1.0` API.
- `debian/control`: in **both** `sensors2mqtt-snmp` and `sensors2mqtt-snmp-control`,
  replace `Depends: snmp` with `Depends: python3-ezsnmp`; add `python3-ezsnmp` to
  `Build-Depends`. net-snmp's shared library arrives transitively via
  `python3-ezsnmp`; the `snmp` CLI package is dropped.
- Verify the `ezsnmp` → `python3-ezsnmp` py3dist mapping resolves during the deb
  build (likely automatic; add a `py3dist-overrides` entry only if needed).

## 10. Testing

Two tiers, both first-class. TDD throughout (red → green).

**Tier 1 — fast unit tests** (no agent, no ezsnmp): the rewritten row-parsers,
`box_entity` ordinals, `snmpget_value` scaling, FQDN-strip, `parse_hex_mac`, plus
`SnmpCollector` / `PoeController` logic via a **fake injected `SnmpClient`** (canned
`SnmpRow` lists). Sub-second; run on every matrix Python; never import ezsnmp.

**Tier 2 — real-library integration tests** (`@pytest.mark.integration`): the real
`SnmpClient` → real ezsnmp → real libnetsnmp, against a local **snmpsim**
(`snmpsim-lextudio`) agent serving per-model `.snmprec` fixtures on
`127.0.0.1:1161`. A pytest fixture starts/stops snmpsim as a subprocess; the read
community selects which model's recording is served. Covers what only the real
library can prove:
- GET/WALK of every value type — INTEGER, Gauge32, OCTET STRING text, **Hex-STRING
  MAC**;
- `NOSUCHOBJECT`/`NOSUCHINSTANCE` handling and the literal `"Not Supported"` row;
- the `use_numeric` `.oid`/`.oid_index` reconstruction on un-MIB'd enterprise OIDs;
- a **SET round-trip** (snmpsim `writecache` variation) for the PoE toggle path;
- **end-to-end**: `poll_switch()` / `poll_port_status()` and the PoE controller's
  poll+toggle against the simulated switch, asserting the exact published dict.

Integration tests `skip` (never silently pass) when ezsnmp/snmpsim are absent on a
minimal dev box; CI always installs them, so they always run there.

**Fixtures:** capture real `.snmprec` snapshots from the live M4300 + GSM7252PS
(+ S3300 if reachable) via ten64 — authentic data including the real MAC encodings and
`"Not Supported"` placeholders — committed under `tests/fixtures/snmprec/`. Authored
fallback for any unreachable model.

## 11. CI / build

- `.github/workflows/ci.yml`: add a step `sudo apt-get update && sudo apt-get install
  -y libsnmp-dev` (builds ezsnmp's C extension); add `--extra snmp` to the `uv sync`;
  add `snmpsim-lextudio` to the dev group. `pytest -v` then runs **both tiers** across
  py3.11/3.12/3.13 — genuinely exercising the real library end-to-end.
- `.github/workflows/deb.yml`: add `python3-ezsnmp` to the build deps; keep the
  build's test phase to **Tier 1 only** (`pytest -m "not integration"`) so the package
  build stays hermetic (no agent spun up mid-build). Integration is CI's job.

## 12. Risks / must-verify-on-hardware (carried into the plan)

- **(a) OID split:** exactly how net-snmp populates `.oid`/`.oid_index` for un-MIB'd
  Netgear enterprise OIDs — handled by full-OID reconstruction (§4), but confirm
  against a real `walk` of a boxServices column.
- **(b) MAC representation:** ezsnmp's `.value`/`.snmp_type` for a MAC OCTET STRING on
  both switch families (M4300 colon-`STRING`; GSM7252PS/S3300 `Hex-STRING`); the
  `parse_hex_mac` adapter must handle whatever it yields.
- **(c) Timeout/retries:** tune `timeout`/`retries` to approximate today's 10s
  (GET) / 30s (WALK) wall-clock budgets.
- **(d) Version parity:** PyPI `ezsnmp` (CI) vs Debian `1.1.0` (deployed) — pinned to
  `~=1.1`; verify API parity at plan time.
- **(e) SET simulation:** confirm snmpsim's `writecache` variation is sufficient for
  the SET round-trip test; fall back to a minimal net-snmp `snmpd` for the SET-only
  test if not.

## 13. Out of scope / future

- SNMP v3 support.
- A Debian `autopkgtest` (`debian/tests/`) running the integration suite against the
  installed package (a possible future enhancement; CI covers integration for now).
- Fully removing net-snmp from the system (would require pysnmp + async +
  trixie-backports).

## 14. Rollout

Deploy to ten64 (the only current SNMP host) via the normal main → PyPI/apt release
pipeline; verify sensor output and PoE control parity live against the real switches
before considering the migration complete.
