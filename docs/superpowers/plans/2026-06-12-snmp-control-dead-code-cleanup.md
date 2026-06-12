# snmp_control.py Dead Code Cleanup Implementation Plan

> **STATUS: COMPLETE (2026-06-12)** — executed as commits 50fc6a5, 1f66317, e74137d, 939c396 on main. Final review: READY.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the three orphaned hostname-helper functions from `snmp_control.py` (plus their constants, imports, tests, and stale comments) and fix the related stale docstring in `snmp.py`.

**Architecture:** Pure deletion plus two comment/docstring corrections — no behaviour changes. Commit `64e16be` added `fetch_port_descriptions()`/`extract_hostname()` (and `10aa558` added `fetch_lldp_neighbors()`) to embed connected-device hostnames in PoE entity names; commit `fb327b5` (25 March 2026) removed that feature's combinator `fetch_port_hostnames()` but left the three building blocks behind. They have no production callers anywhere in the repo — only their own unit tests keep them alive.

**Tech Stack:** Python 3.11+, pytest, ruff. All commands via `uv run`.

**Verified facts (grep evidence, 2026-06-12):**
- `fetch_port_descriptions`, `extract_hostname`, `fetch_lldp_neighbors` (in `snmp_control.py`) are referenced only by `tests/test_snmp_control.py` (their own unit tests). `snmp.py`'s identically-named *methods* on `SnmpCollector` are separate, live code and are NOT touched.
- `IF_ALIAS_OID` and `LLDP_REM_OID` module constants in `snmp_control.py` are used only by the dead functions. (`snmp.py` has its own local `IF_ALIAS_OID` variable inside a method — unrelated.)
- `parse_lldp_walk` is imported by `snmp_control.py` solely for the dead `fetch_lldp_neighbors()`. The function itself lives in `snmp.py`, is used there by `SnmpCollector.fetch_lldp_neighbors()`, and is tested in `tests/test_snmp.py` — only the `snmp_control.py` import line is removed.
- 9 tests will be deleted: 5 in `TestExtractHostname`, 2 in `TestFetchPortDescriptions`, 2 in `TestFetchLldpNeighbors`.
- `TestDiscoveryShortNames` (tests/test_snmp_control.py:634) sits BETWEEN two of the deleted classes and MUST be kept.

**Related plan:** `2026-06-12-box-sensor-walks.md` is ON HOLD until this plan completes. This cleanup does not shift any line numbers that plan references (its `snmp.py` anchors are all below line 666 only in the docstring task, which replaces 3 lines with 3 lines).

---

### Task 1: Delete the dead functions, constants, imports, and their tests

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp_control.py`
- Modify: `tests/test_snmp_control.py`

- [x] **Step 1: Pre-verify the dead-code claim**

Run:

```bash
grep -rn "extract_hostname\|fetch_port_descriptions\|fetch_lldp_neighbors" \
    src/ tests/ --include="*.py" | grep -v "def \|self\.fetch\|collector\.fetch"
```

Expected: hits only in `tests/test_snmp_control.py` (imports and test bodies). The filter excludes the *separate, live* `SnmpCollector` methods of the same names: their `def` lines, `self.fetch_*` calls in `snmp.py`, and `collector.fetch_*` calls in `tests/test_snmp.py`. If anything else appears, STOP and reassess.

- [x] **Step 2: Delete from `snmp_control.py`**

Delete these, top to bottom (line numbers from the current file; deleting top-down shifts later ones, so work bottom-up or re-grep):

1. The stale comment line inside `publish_discovery()` (line 523). The block currently reads:

```python
            # Build host suffix for entity names
            # PoE Toggle (switch entity)
            # Short names — device name already identifies the port.
```

Remove only the `# Build host suffix for entity names` line ("host suffix" was deleted in `fb327b5`; the two following lines still describe the code below them).

2. The three functions and the blank lines between them — everything from `def fetch_port_descriptions(` (line 57) through the end of `fetch_lldp_neighbors()` (line 131, `return sys_names`) plus the extra blank lines before the `@dataclass` at line 135. Leave exactly two blank lines (PEP 8) between the `OPER_MAP = ...` line and `@dataclass`.

3. The two now-unused constants (lines 43-44):

```python
IF_ALIAS_OID = "1.3.6.1.2.1.31.1.1.1.18"          # ifAlias (port descriptions)
LLDP_REM_OID = "1.0.8802.1.1.2.1.4.1.1"            # LLDP remote table base
```

Also remove the now-orphaned `# SNMP OIDs` comment above them (keep the `# SNMP OIDs for PoE control` comment that heads the POE constants).

4. `parse_lldp_walk,` from the import block, leaving:

```python
from sensors2mqtt.collector.snmp import (
    SwitchConfig,
    _build_port_device,
    fetch_lldp_chassis_macs,
    load_config,
    parse_snmpget_value,
)
```

5. `re` is still used (`_snmpget_int`, `_on_message`, `_read_force_overrides`) and `subprocess` is still used (`_snmpget_int`, `_snmpset_int`, `poll_all_ports`) — do NOT remove those imports. `ruff check` in Step 5 confirms nothing else became unused.

- [x] **Step 3: Delete from `tests/test_snmp_control.py`**

1. Remove the three names from the import block (lines 9-18), leaving:

```python
from sensors2mqtt.collector.snmp_control import (
    IF_OPER_OID,
    POE_ADMIN_OID,
    POE_DETECT_OID,
    PoeController,
    PortControlState,
)
```

2. Delete `TestExtractHostname` (lines 575-593) together with its section banner (lines 571-573, `# Hostname extraction tests`).

3. Delete `TestFetchPortDescriptions` (lines 600-627) together with its section banner (lines 596-598, `# Port description fetch tests`).

4. **Keep `TestDiscoveryShortNames` (lines 634-662)** — it tests live `publish_discovery()` behaviour. Update its stale section banner (lines 630-632) from `# Discovery with port descriptions tests` to `# Discovery short-name tests`.

5. Delete `TestFetchLldpNeighbors` (lines 669-690) together with its section banner (lines 665-667, `# LLDP fetch + combined hostname tests`). This class runs to the end of the file.

- [x] **Step 4: Verify deletion is complete**

Run:

```bash
grep -rn "extract_hostname\|fetch_port_descriptions\|fetch_lldp_neighbors\|IF_ALIAS_OID\|LLDP_REM_OID\|parse_lldp_walk" \
    src/sensors2mqtt/collector/snmp_control.py tests/test_snmp_control.py
```

Expected: no output.

- [x] **Step 5: Run full suite and lint**

Run: `uv run pytest && uv run ruff check src/ tests/`
Expected: all tests PASS with exactly 9 fewer tests collected than before (5 + 2 + 2 deleted); no lint errors. The dead functions had no other coverage, so nothing else can fail — if anything does, STOP: the dead-code claim was wrong somewhere.

- [x] **Step 6: Commit**

```bash
git add src/sensors2mqtt/collector/snmp_control.py tests/test_snmp_control.py
git commit -m "refactor: remove dead hostname helpers from snmp_control

fetch_port_descriptions, extract_hostname and fetch_lldp_neighbors
were orphaned by fb327b5 when hostname embedding was removed from
PoE entity names; only their own unit tests still called them. Also
drops their now-unused IF_ALIAS_OID/LLDP_REM_OID constants and the
parse_lldp_walk import.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Fix the stale docstring in snmp.py and note the removal in the cross-check doc

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp.py:666-668`
- Modify: `docs/gdoc2netcfg-snmp-cross-check.md`

- [x] **Step 1: Fix the `fetch_port_descriptions` docstring**

In `SnmpCollector.fetch_port_descriptions()` the docstring claims the interface prefix is stripped, but the code stores the full alias (the per-port `description` sensor publishes e.g. `eth0.rpi5-pmod`, deliberately matching the combined LLDP-neighbor format built in `fetch_lldp_neighbors()`). Replace:

```python
        Returns {port_number: device_name}. The ifAlias convention is
        "{interface}.{hostname}" (e.g. "eth0.rpi5-pmod") — we strip the
        interface prefix to get just the device name.
```

with:

```python
        Returns {port_number: alias}. The ifAlias convention is
        "{interface}.{hostname}" (e.g. "eth0.rpi5-pmod"); the full alias
        is kept, matching the combined LLDP neighbor format.
```

- [x] **Step 2: Update the cross-check doc's reference to extract_hostname**

`docs/gdoc2netcfg-snmp-cross-check.md` credits "This repo's `extract_hostname`" for the ifAlias convention — that function is now deleted. Replace the first sentence of the bullet starting `- **ifAlias convention.**`:

```markdown
- **ifAlias convention.** This repo's `extract_hostname` relies on the
  `interface.hostname` convention in `ifAlias`
  (`1.3.6.1.2.1.31.1.1.1.18`). gdoc2netcfg now walks `ifAlias` too and uses the
```

with:

```markdown
- **ifAlias convention.** This repo relies on the `interface.hostname`
  convention in `ifAlias` (`1.3.6.1.2.1.31.1.1.1.18`); the
  `extract_hostname` helper originally named here has since been removed
  as dead code. gdoc2netcfg now walks `ifAlias` too and uses the
```

Keep the rest of the bullet unchanged.

- [x] **Step 3: Run tests and lint**

Run: `uv run pytest && uv run ruff check src/ tests/`
Expected: all PASS (docstring/doc-only changes), no lint errors

- [x] **Step 4: Commit**

```bash
git add src/sensors2mqtt/collector/snmp.py docs/gdoc2netcfg-snmp-cross-check.md
git commit -m "docs: fix stale ifAlias docstring and extract_hostname reference

The fetch_port_descriptions docstring claimed the interface prefix is
stripped; the full alias has been published since fb327b5. The
cross-check doc pointed at the now-removed extract_hostname.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Final verification

- [x] **Step 1: Full test suite**

Run: `uv run pytest -v`
Expected: all tests PASS; `tests/test_snmp_control.py` no longer contains TestExtractHostname/TestFetchPortDescriptions/TestFetchLldpNeighbors but still contains TestDiscoveryShortNames

- [x] **Step 2: Lint**

Run: `uv run ruff check src/ tests/`
Expected: no errors

- [x] **Step 3: Smoke-import the control module**

Run: `uv run python -c "from sensors2mqtt.collector import snmp_control; print('ok')"`
Expected: `ok` (catches any import-level breakage the tests might not exercise)

- [x] **Step 4: Confirm working tree is clean**

Run: `git status`
Expected: nothing to commit, working tree clean

After this plan completes, resume `docs/superpowers/plans/2026-06-12-box-sensor-walks.md` (remove its ON HOLD marker when starting).
