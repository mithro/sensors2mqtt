# Walk-Discovered boxServices Sensors Implementation Plan

> **STATUS: READY (2026-06-12)** — the prerequisite `2026-06-12-snmp-control-dead-code-cleanup.md` is COMPLETE (commits 50fc6a5..acf3a26). All line-number anchors in this plan were re-verified against the post-cleanup files on 2026-06-12 and are unchanged (the cleanup's edits were same-size line replacements). Baseline: 247 tests passing, lint clean.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hardcoded per-instance boxServices snmpget OIDs (fans/temperature/PSU) with subtree walks that discover instances dynamically, closing the three gaps in `docs/gdoc2netcfg-snmp-cross-check.md` (GSM7252PS box sensors not collected; hardcoded instance indexing; only first PSU rail read).

**Architecture:** A new `BoxWalkDef` describes one boxServices value column to walk (`{base}.6.1.4` fans, `{base}.15.1.3` temperature, `{base}.8.1.5` PSU). A new parser `parse_box_walk()` extracts the *full* OID instance suffix relative to the base (e.g. `"1.0"`, `"0"`, `"1.3"`) — the existing `parse_snmpwalk()` keeps only the last component and would collide PSU rails. Discovered instances are sorted numerically and mapped to stable HA suffixes by `box_entity()` so existing entity IDs (`fan1_rpm`, `temp`, `psu_power`) are preserved. The literal `"Not Supported"` marker (a placeholder Netgear's FASTPATH firmware returns for absent sensors, e.g. the GSM7252PS middle fan slot) is skipped silently; any other non-integer value logs a warning instead of silently disappearing.

**Tech Stack:** Python 3.11+, subprocess `snmpwalk`, pytest with fixture files, paho-mqtt v2. All commands via `uv run`.

**Constraints:**
- `snmp_control.py` (the PoE control service) imports from `snmp.py`: `SwitchConfig` and `load_config` (shared snmp.toml), `parse_snmpget_value` (PoE state verification in `_snmpget_int()`), `parse_snmpwalk` (bulk PoE/link polling in `poll_all_ports()` — single-component port indices, so the last-component parser is correct there), and `_build_port_device` and `fetch_lldp_chassis_macs` (same per-port HA sub-device scheme). None of these change behaviour in this plan; `SwitchConfig` only gains an additive `box_walks` field that `snmp_control.py` ignores. Keep `parse_snmpget_value` and `parse_snmpwalk`. Keep `SnmpSensor` and `snmpget_value` too — those are used by `snmp.py`'s own static-sensor poll loop, which stays as the extension point for future single-OID sensors. Only `_box_sensors()` is deleted (Task 6). **No changes to `snmp_control.py` are required for box-sensor parity.** (The dead code formerly noted here as optional Task 9 was removed by the separate plan `2026-06-12-snmp-control-dead-code-cleanup.md`, completed 2026-06-12; `snmp_control.py` no longer imports `parse_lldp_walk`, and `parse_snmpwalk` remains used there via a lazy import in `poll_all_ports()`.)
- HA entity suffixes are `unique_id` components; renaming orphans entity history. The mapping below reproduces today's suffixes exactly on M4300/S3300.
- Every commit must leave `make test` and `make lint` green. Tasks are ordered so `MODELS` is only flipped (Task 6) after the walk machinery exists.

**Reference data (from docs/gdoc2netcfg-snmp-cross-check.md, live walks 2026-06-11):**

| Model | Fan instances | PSU instances | Temp instances |
|---|---|---|---|
| M4300 | `1.0`, `1.1` | `1.0` | `1` |
| S3300 | `1.0`, `1.1`, `1.2` | `1.0` | `1` |
| GSM7252PS | `0`, `2` (`1` = literal `"Not Supported"`) | `1.0`–`1.3` (rail `.0` ≈ sum of others) | none readable |

GSM7252PS live values (gsm7252ps-s1): fans 3500/3450 RPM, PSU rails 53/34/36/35 W.

---

### Task 1: `parse_box_walk()` parser

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp.py` (add function after `parse_snmpwalk`, ~line 391)
- Test: `tests/test_snmp.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_snmp.py` after class `TestParseSnmpwalk`. Also add `parse_box_walk` to the existing `from sensors2mqtt.collector.snmp import (...)` block at the top of the file (keep the list alphabetically sorted).

```python
class TestParseBoxWalk:
    BASE = "1.3.6.1.4.1.4526.10.43.1.6.1.4"

    def test_single_component_instance(self):
        output = 'iso.3.6.1.4.1.4526.10.43.1.6.1.4.0 = STRING: "3500"\n'
        assert parse_box_walk(output, self.BASE) == [("0", "3500")]

    def test_multi_component_instance(self):
        output = (
            'iso.3.6.1.4.1.4526.10.43.1.6.1.4.1.0 = STRING: "5280"\n'
            'iso.3.6.1.4.1.4526.10.43.1.6.1.4.1.1 = STRING: "4560"\n'
        )
        assert parse_box_walk(output, self.BASE) == [("1.0", "5280"), ("1.1", "4560")]

    def test_integer_values_unquoted(self):
        base = "1.3.6.1.4.1.4526.10.43.1.8.1.5"
        output = (
            "iso.3.6.1.4.1.4526.10.43.1.8.1.5.1.0 = INTEGER: 53\n"
            "iso.3.6.1.4.1.4526.10.43.1.8.1.5.1.3 = INTEGER: 35\n"
        )
        assert parse_box_walk(output, base) == [("1.0", "53"), ("1.3", "35")]

    def test_not_supported_passed_through(self):
        """The parser returns the raw marker; skipping is the poller's job."""
        output = 'iso.3.6.1.4.1.4526.10.43.1.6.1.4.1 = STRING: "Not Supported"\n'
        assert parse_box_walk(output, self.BASE) == [("1", "Not Supported")]

    def test_filters_lines_outside_base(self):
        """Feeding a full-table walk only yields rows under the value column."""
        text = (FIXTURES / "snmpwalk_m4300_fans.txt").read_text()
        result = parse_box_walk(text, self.BASE)
        # Fixture has columns 1-6; only column 4 (speed) rows are under BASE
        assert result == [("1.0", "5280"), ("1.1", "4560")]

    def test_no_false_prefix_match(self):
        """...6.1.4 must not match ...6.1.40 or bare ...6.1.4 itself."""
        output = (
            "iso.3.6.1.4.1.4526.10.43.1.6.1.40.1 = INTEGER: 7\n"
            "iso.3.6.1.4.1.4526.10.43.1.6.1.4 = INTEGER: 8\n"
        )
        assert parse_box_walk(output, self.BASE) == []

    def test_no_such_object_line_ignored(self):
        output = (
            "iso.3.6.1.4.1.4526.10.43.1.6.1.4 = "
            "No Such Object available on this agent at this OID\n"
        )
        assert parse_box_walk(output, self.BASE) == []

    def test_empty_output(self):
        assert parse_box_walk("", self.BASE) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_snmp.py::TestParseBoxWalk -v`
Expected: FAIL — `ImportError: cannot import name 'parse_box_walk'`

- [ ] **Step 3: Write the implementation**

Add to `src/sensors2mqtt/collector/snmp.py` directly after `parse_snmpwalk()` (after line 390):

```python
def parse_box_walk(output: str, base_oid: str) -> list[tuple[str, str]]:
    """Parse a boxServices value-column walk into [(instance, raw_value), ...].

    The instance is the full OID suffix relative to base_oid — e.g. "1.0"
    (unit 1, fan 0) on an M4300, or "0" on a GSM7252PS, whose fan table is
    indexed by a single component. Unlike parse_snmpwalk(), the suffix is
    kept whole: the GSM7252PS exposes PSU rails "1.0"-"1.3" that would
    collide if reduced to their last component.

    snmpwalk prints the leading OID arc as "iso" instead of "1", so the
    OID is normalised before the prefix comparison. Lines outside
    base_oid, with a non-numeric suffix, or without a "TYPE: value" form
    (e.g. "No Such Object ...") are ignored.
    """
    prefix = base_oid + "."
    results = []
    for line in output.strip().splitlines():
        m = re.match(r"(\S+)\s*=\s*\S+:\s*(.*)", line.strip())
        if not m:
            continue
        oid = m.group(1)
        if oid.startswith("iso."):
            oid = "1." + oid[len("iso."):]
        if not oid.startswith(prefix):
            continue
        instance = oid[len(prefix):]
        if not re.fullmatch(r"\d+(\.\d+)*", instance):
            continue
        val = m.group(2).strip().strip('"')
        results.append((instance, val))
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_snmp.py::TestParseBoxWalk -v`
Expected: 8 PASS

- [ ] **Step 5: Run full suite and lint**

Run: `uv run pytest && uv run ruff check src/ tests/`
Expected: all pass, no lint errors

- [ ] **Step 6: Commit**

```bash
git add src/sensors2mqtt/collector/snmp.py tests/test_snmp.py
git commit -m "feat(snmp): add parse_box_walk preserving full OID instance suffixes

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `box_entity()` suffix/name mapping

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp.py` (add after `parse_box_walk` from Task 1)
- Test: `tests/test_snmp.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_snmp.py` after `TestParseBoxWalk`. Add `box_entity` to the import block.

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_snmp.py::TestBoxEntity -v`
Expected: FAIL — `ImportError: cannot import name 'box_entity'`

- [ ] **Step 3: Write the implementation**

Add to `src/sensors2mqtt/collector/snmp.py` after `parse_box_walk()`:

```python
_BOX_KIND_LABELS = {"fan": "Fan", "temp": "Temperature", "psu_power": "PSU Power"}


def box_entity(kind: str, ordinal: int) -> tuple[str, str]:
    """Map a discovered box sensor (kind, ordinal) to its (suffix, name).

    ordinal is the 0-based position in instance-sorted order. Suffixes are
    HA unique_id components and must stay stable across releases — renaming
    one orphans the entity's recorded history. Fans have always been
    numbered (fan1_rpm); the first temperature/PSU sensor keeps its historic
    unnumbered suffix ("temp", "psu_power") and only extra instances (e.g.
    the GSM7252PS's additional PSU rails) get numbered ones.
    """
    if kind == "fan":
        return f"fan{ordinal + 1}_rpm", f"Fan {ordinal + 1}"
    label = _BOX_KIND_LABELS[kind]
    if ordinal == 0:
        return kind, label
    return f"{kind}{ordinal + 1}", f"{label} {ordinal + 1}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_snmp.py::TestBoxEntity -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/sensors2mqtt/collector/snmp.py tests/test_snmp.py
git commit -m "feat(snmp): add box_entity mapping preserving historic HA suffixes

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `BoxWalkDef` dataclass and plumbing

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp.py` (dataclass after `WalkSensorDef` ~line 113; `_box_walks()` after `_poe_walk()` ~line 205; fields on `SwitchModel` ~line 129 and `SwitchConfig` ~line 156; `load_config` ~line 297)
- Test: `tests/test_snmp.py`

No model uses `box_walks` yet — `MODELS` is flipped in Task 6. Everything stays green.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_snmp.py` after `TestBoxEntity`. Add `_box_walks` to the import block — ruff's isort rule (`I`) is enabled, so after Tasks 1-3 the complete block must read exactly:

```python
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
    parse_snmpget_value,
    parse_snmpwalk,
    snmpget_value,
)
```

If `uv run ruff check tests/` disagrees about the `_box_walks` position, run `uv run ruff check --fix tests/` and keep whatever ordering it settles on.

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_snmp.py::TestBoxWalks -v`
Expected: FAIL — `ImportError: cannot import name '_box_walks'`

- [ ] **Step 3: Write the implementation**

In `src/sensors2mqtt/collector/snmp.py`, add the dataclass after `WalkSensorDef` (before `SwitchModel`):

```python
@dataclass(frozen=True)
class BoxWalkDef:
    """A Netgear boxServices value column to walk.

    Instances are discovered from the walk rather than hardcoded because
    indexing differs by model: the M4300/S3300 index fans as unit.fan
    ("1.0"), the GSM7252PS as a bare fan number ("0"), and the GSM7252PS
    exposes four PSU rails ("1.0"-"1.3") where the others have one.

    Attributes:
        kind: Sensor kind — "fan", "temp", or "psu_power" (see box_entity).
        base_oid: The value column subtree to snmpwalk.
        unit: Unit of measurement.
        device_class: HA device class. None for RPM.
        icon: MDI icon override. None uses default.
    """

    kind: str
    base_oid: str
    unit: str
    device_class: str | None = None
    icon: str | None = None
```

Add `_box_walks()` after `_poe_walk()` (line ~205):

```python
def _box_walks(base: str) -> list[BoxWalkDef]:
    """Build boxServices walk definitions for a given enterprise OID base."""
    return [
        BoxWalkDef(kind="fan", base_oid=f"{base}.6.1.4", unit="RPM",
                   icon="mdi:fan"),
        BoxWalkDef(kind="temp", base_oid=f"{base}.15.1.3", unit="°C",
                   device_class="temperature"),
        BoxWalkDef(kind="psu_power", base_oid=f"{base}.8.1.5", unit="W",
                   device_class="power"),
    ]
```

Add the field to **both** `SwitchModel` and `SwitchConfig`, after their `walk_sensors` field:

```python
    box_walks: list[BoxWalkDef] = field(default_factory=list)
```

In `load_config()`, add to the `SwitchConfig(...)` construction after `walk_sensors=list(model.walk_sensors),`:

```python
            box_walks=list(model.box_walks),
```

In `tests/test_snmp.py`, add to `_make_switch()` after `walk_sensors=list(model.walk_sensors),`:

```python
        box_walks=list(model.box_walks),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_snmp.py -v`
Expected: all PASS (new TestBoxWalks plus all pre-existing tests)

- [ ] **Step 5: Commit**

```bash
git add src/sensors2mqtt/collector/snmp.py tests/test_snmp.py
git commit -m "feat(snmp): add BoxWalkDef and box_walks model/config plumbing

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Poll box sensors in `poll_switch()`

**Files:**
- Create: `tests/fixtures/snmpwalk_gsm7252ps_fans.txt`
- Create: `tests/fixtures/snmpwalk_gsm7252ps_psu.txt`
- Modify: `src/sensors2mqtt/collector/snmp.py:530-584` (`poll_switch`)
- Test: `tests/test_snmp.py`

- [ ] **Step 1: Create the GSM7252PS fixtures**

These are reconstructed from the live values recorded in `docs/gdoc2netcfg-snmp-cross-check.md` (gsm7252ps-s1, 2026-06-11), in the format snmpwalk prints.

`tests/fixtures/snmpwalk_gsm7252ps_fans.txt`:

```
iso.3.6.1.4.1.4526.10.43.1.6.1.4.0 = STRING: "3500"
iso.3.6.1.4.1.4526.10.43.1.6.1.4.1 = STRING: "Not Supported"
iso.3.6.1.4.1.4526.10.43.1.6.1.4.2 = STRING: "3450"
```

`tests/fixtures/snmpwalk_gsm7252ps_psu.txt`:

```
iso.3.6.1.4.1.4526.10.43.1.8.1.5.1.0 = INTEGER: 53
iso.3.6.1.4.1.4526.10.43.1.8.1.5.1.1 = INTEGER: 34
iso.3.6.1.4.1.4526.10.43.1.8.1.5.1.2 = INTEGER: 36
iso.3.6.1.4.1.4526.10.43.1.8.1.5.1.3 = INTEGER: 35
```

- [ ] **Step 2: Write the failing tests**

Add helpers to `tests/test_snmp.py` after `_make_switch()`, and `import logging` at the top of the file (with the other stdlib imports):

```python
def _box_test_switch() -> SwitchConfig:
    """A switch with only box walks (FM OID base), independent of MODELS."""
    from sensors2mqtt.collector.snmp import _FM_BOX
    return SwitchConfig(
        node_id="test_box",
        name="test-box",
        host="test-box.test",
        community="public",
        manufacturer="Netgear",
        model="TEST",
        box_walks=_box_walks(_FM_BOX),
    )


def _box_walk_side_effect(responses: dict[str, str]):
    """subprocess.run side effect: map walked-OID suffix -> stdout."""
    def side_effect(*args, **kwargs):
        oid = args[0][-1]
        for suffix, text in responses.items():
            if oid.endswith(suffix):
                return MagicMock(returncode=0, stdout=text, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")
    return side_effect
```

Add tests inside `class TestSnmpCollector` (after `test_poll_walk_switch`):

```python
    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_box_sensors_m4300_layout(self, mock_run):
        """Two-component instances (unit.fan) — suffix contract preserved."""
        mock_run.side_effect = _box_walk_side_effect({
            ".6.1.4": (FIXTURES / "snmpwalk_m4300_fans.txt").read_text(),
            ".15.1.3": (FIXTURES / "snmpwalk_m4300_thermal.txt").read_text(),
            ".8.1.5": (FIXTURES / "snmpwalk_m4300_psu.txt").read_text(),
        })
        sw = _box_test_switch()
        collector = self.make_collector(switches=[sw])
        values = collector.poll_switch(sw)
        assert values == {
            "fan1_rpm": 5280, "fan2_rpm": 4560, "temp": 65, "psu_power": 65,
        }

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_box_sensors_gsm7252ps_layout(self, mock_run, caplog):
        """Single-component fan instances, Not Supported slot, 4 PSU rails."""
        mock_run.side_effect = _box_walk_side_effect({
            ".6.1.4": (FIXTURES / "snmpwalk_gsm7252ps_fans.txt").read_text(),
            ".8.1.5": (FIXTURES / "snmpwalk_gsm7252ps_psu.txt").read_text(),
            # .15.1.3 (temp) falls through to empty output — the GSM7252PS
            # exposes nothing readable there
        })
        sw = _box_test_switch()
        collector = self.make_collector(switches=[sw])
        with caplog.at_level(logging.WARNING):
            values = collector.poll_switch(sw)
        assert values == {
            "fan1_rpm": 3500, "fan2_rpm": 3450,
            "psu_power": 53, "psu_power2": 34,
            "psu_power3": 36, "psu_power4": 35,
        }
        # The "Not Supported" placeholder is expected hardware, not an error
        assert not any("non-integer" in r.getMessage() for r in caplog.records)

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_box_warns_on_unexpected_string(self, mock_run, caplog):
        """Unknown non-integer values must be visible, not silently dropped."""
        fans = (
            'iso.3.6.1.4.1.4526.10.43.1.6.1.4.0 = STRING: "3500"\n'
            'iso.3.6.1.4.1.4526.10.43.1.6.1.4.1 = STRING: "garbage"\n'
        )
        mock_run.side_effect = _box_walk_side_effect({".6.1.4": fans})
        sw = _box_test_switch()
        collector = self.make_collector(switches=[sw])
        with caplog.at_level(logging.WARNING):
            values = collector.poll_switch(sw)
        assert values == {"fan1_rpm": 3500}
        assert any("non-integer fan reading" in r.getMessage()
                   for r in caplog.records)

    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_box_walk_failure_skips_kind(self, mock_run):
        """One failed walk doesn't lose the other kinds."""
        psu = (FIXTURES / "snmpwalk_m4300_psu.txt").read_text()

        def side_effect(*args, **kwargs):
            oid = args[0][-1]
            if oid.endswith(".8.1.5"):
                return MagicMock(returncode=0, stdout=psu, stderr="")
            return MagicMock(returncode=1, stdout="", stderr="Timeout")

        mock_run.side_effect = side_effect
        sw = _box_test_switch()
        collector = self.make_collector(switches=[sw])
        values = collector.poll_switch(sw)
        assert values == {"psu_power": 65}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_snmp.py -k "test_poll_box" -v`
Expected: 4 FAIL — `poll_switch` returns `None` (box walks not polled yet)

- [ ] **Step 4: Write the implementation**

In `poll_switch()` in `src/sensors2mqtt/collector/snmp.py`, insert between the static-sensor snmpget loop and the `# snmpwalk-based sensors` loop (after line 554):

```python
        # Walk-discovered boxServices sensors (fans, temperature, PSU).
        # Instances vary by model (see BoxWalkDef), so each value column is
        # walked and whatever rows exist become sensors, in instance order.
        for box in switch.box_walks:
            try:
                result = subprocess.run(
                    ["snmpwalk", "-v2c", "-c", switch.community, switch.host,
                     box.base_oid],
                    capture_output=True, text=True, timeout=self._timeout * 3,
                )
                if result.returncode != 0:
                    log.warning(
                        "%s: snmpwalk %s (%s) failed: %s",
                        switch.name, box.base_oid, box.kind,
                        result.stderr.strip(),
                    )
                    continue
                readings = []
                for instance, raw in parse_box_walk(result.stdout, box.base_oid):
                    if raw == "Not Supported":
                        # Netgear's literal placeholder for an absent sensor
                        # slot (e.g. the GSM7252PS middle fan) — not an error.
                        continue
                    try:
                        value = int(raw)
                    except ValueError:
                        log.warning(
                            "%s: non-integer %s reading %r at instance %s",
                            switch.name, box.kind, raw, instance,
                        )
                        continue
                    readings.append(
                        (tuple(int(c) for c in instance.split(".")), value)
                    )
                readings.sort()
                for ordinal, (_instance, value) in enumerate(readings):
                    suffix, _name = box_entity(box.kind, ordinal)
                    values[suffix] = value
            except subprocess.TimeoutExpired:
                log.warning("%s: snmpwalk %s timed out",
                            switch.name, box.base_oid)
            except Exception as e:
                log.warning("%s: snmpwalk %s error: %s",
                            switch.name, box.base_oid, e)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_snmp.py -v`
Expected: all PASS (pre-existing `test_poll_switch_*` tests still pass — `MODELS` is unchanged, so m4300 still polls via static sensors)

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/snmpwalk_gsm7252ps_fans.txt \
        tests/fixtures/snmpwalk_gsm7252ps_psu.txt \
        src/sensors2mqtt/collector/snmp.py tests/test_snmp.py
git commit -m "feat(snmp): poll boxServices sensors via subtree walks

Skips the literal 'Not Supported' placeholder; warns on any other
non-integer value instead of silently publishing nothing.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Discovery for walk-discovered sensors

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp.py:831-852` (`get_sensors_for_switch`)
- Test: `tests/test_snmp.py`

- [ ] **Step 1: Write the failing test**

Add inside `class TestSnmpCollector`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_snmp.py::TestSnmpCollector::test_get_sensors_for_switch_box -v`
Expected: FAIL — `assert set() == {...}` (no box sensors generated)

- [ ] **Step 3: Write the implementation**

In `get_sensors_for_switch()`, append before `return sensors` (after the static-sensor loop). Also update the method docstring's first line block: replace

```
        Walk sensors (PoE per-port power) are NOT included here — they are
```

with

```
        Box sensors (fans, temperature, PSU) are included based on which
        suffixes the poll discovered. Walk sensors (PoE per-port power) are
        NOT included here — they are
```

New code before `return sensors`:

```python
        # Walk-discovered box sensors: poll_switch() assigns contiguous
        # ordinals per kind, so probe values until the first missing suffix.
        for box in switch.box_walks:
            ordinal = 0
            while True:
                suffix, name = box_entity(box.kind, ordinal)
                if suffix not in values:
                    break
                sensors.append(SensorDef(
                    suffix=suffix,
                    name=name,
                    unit=box.unit,
                    device_class=box.device_class,
                    state_class="measurement",
                    icon=box.icon,
                ))
                ordinal += 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_snmp.py -v`
Expected: all PASS (`test_get_sensors_for_switch_static` and `test_get_sensors_excludes_walk_sensors` still pass — current `MODELS` entries have no `box_walks`)

- [ ] **Step 5: Commit**

```bash
git add src/sensors2mqtt/collector/snmp.py tests/test_snmp.py
git commit -m "feat(snmp): publish HA discovery for walk-discovered box sensors

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Switch `MODELS` to box walks, delete `_box_sensors()`

**Files:**
- Modify: `src/sensors2mqtt/collector/snmp.py:172-231` (`_box_sensors`, `MODELS`)
- Modify: `tests/test_snmp.py` (`TestModelDefinitions`, `TestConfigLoading`, `TestSnmpCollector`)

This task flips behaviour and updates tests in lockstep; the commit at the end is the only safe stopping point.

- [ ] **Step 1: Update the model definition tests**

In `tests/test_snmp.py`, replace the entire `TestModelDefinitions` class with:

```python
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
```

- [ ] **Step 2: Update the config-loading test**

Replace `test_config_sensors_populated` in `TestConfigLoading` with:

```python
    def test_config_box_walks_populated(self):
        """Config loading should populate box walks from model definitions."""
        switches = load_config(CONFIG_FILE)
        by_name = {s.name: s for s in switches}
        for name in ("test-m4300", "test-gsm7252ps", "test-s3300"):
            assert len(by_name[name].box_walks) == 3, name
        assert len(by_name["test-m4300"].walk_sensors) == 0
        assert len(by_name["test-s3300"].walk_sensors) >= 1
```

- [ ] **Step 3: Update the collector tests that relied on static sensors**

In `TestSnmpCollector`, replace `test_poll_switch_success` with a fixture-driven version (the old mock returned one snmpget line for every call, which no longer matches any poll the m4300 makes):

```python
    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_switch_success(self, mock_run):
        mock_run.side_effect = _box_walk_side_effect({
            ".6.1.4": (FIXTURES / "snmpwalk_m4300_fans.txt").read_text(),
            ".15.1.3": (FIXTURES / "snmpwalk_m4300_thermal.txt").read_text(),
            ".8.1.5": (FIXTURES / "snmpwalk_m4300_psu.txt").read_text(),
        })
        sw = _make_switch("test-m4300", "m4300")
        collector = self.make_collector(switches=[sw])
        values = collector.poll_switch(sw)
        assert values == {
            "fan1_rpm": 5280, "fan2_rpm": 4560, "temp": 65, "psu_power": 65,
        }
```

Replace `test_poll_switch_partial_failure` with:

```python
    @patch("sensors2mqtt.collector.snmp.subprocess.run")
    def test_poll_switch_partial_failure(self, mock_run):
        """A failed fan walk still yields temperature and PSU values."""
        thermal = (FIXTURES / "snmpwalk_m4300_thermal.txt").read_text()
        psu = (FIXTURES / "snmpwalk_m4300_psu.txt").read_text()

        def side_effect(*args, **kwargs):
            oid = args[0][-1]
            if oid.endswith(".6.1.4"):
                return MagicMock(returncode=1, stdout="", stderr="Timeout")
            if oid.endswith(".15.1.3"):
                return MagicMock(returncode=0, stdout=thermal, stderr="")
            if oid.endswith(".8.1.5"):
                return MagicMock(returncode=0, stdout=psu, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect
        sw = _make_switch("test-m4300", "m4300")
        collector = self.make_collector(switches=[sw])
        values = collector.poll_switch(sw)
        assert values == {"temp": 65, "psu_power": 65}
```

Replace `test_get_sensors_for_switch_static` with:

```python
    def test_get_sensors_for_switch_m4300(self):
        """MODELS wiring feeds walk-discovered values into discovery defs."""
        sw = _make_switch("test-m4300", "m4300")
        collector = self.make_collector(switches=[sw])
        values = {"fan1_rpm": 5280, "fan2_rpm": 4560, "temp": 65,
                  "psu_power": 65}
        sensors = collector.get_sensors_for_switch(sw, values)
        assert {s.suffix for s in sensors} == set(values)
```

In `test_get_sensors_excludes_walk_sensors`, update the trailing comment block from:

```python
        # GSM7252PS has no static snmpget sensors, only walk sensors
        # Walk sensors are excluded → empty list
        assert len(sensors) == 0
```

to:

```python
        # Values contain only PoE keys — no box suffixes were discovered,
        # and PoE walk sensors are per-port sub-devices, not switch-level
        assert len(sensors) == 0
```

Leave `test_poll_switch_all_fail`, `test_poll_switch_timeout`, and `test_poll_walk_switch` untouched — they remain correct: all-fail/timeout still yield `None` (three failed walks), and in `test_poll_walk_switch` the box walks receive PoE-OID output that `parse_box_walk`'s prefix filter rejects, so only the PoE assertions matter.

- [ ] **Step 4: Run tests to verify the new expectations fail**

Run: `uv run pytest tests/test_snmp.py -v`
Expected: FAIL — `test_m4300_model` (`m.sensors` is not empty / no `box_walks`), `test_config_box_walks_populated`, `test_poll_switch_success`, `test_get_sensors_for_switch_m4300`, and the gsm7252ps/s3300 model tests

- [ ] **Step 5: Flip the models**

In `src/sensors2mqtt/collector/snmp.py`:

Delete the entire `_box_sensors()` function (lines 172-190).

Replace `MODELS` with:

```python
# Known switch models — keyed by the name used in config files
MODELS: dict[str, SwitchModel] = {
    "m4300": SwitchModel(
        manufacturer="Netgear",
        model="M4300-24X",
        port_count=24,
        poe_port_count=0,
        box_walks=_box_walks(_FM_BOX),
    ),
    "gsm7252ps": SwitchModel(
        manufacturer="Netgear",
        model="GSM7252PS",
        port_count=52,
        poe_port_count=48,
        box_walks=_box_walks(_FM_BOX),
        walk_sensors=_poe_walk(_FM_POE),
    ),
    "s3300": SwitchModel(
        manufacturer="Netgear",
        model="GSM7228PS",
        port_count=52,
        poe_port_count=48,
        box_walks=_box_walks(_SMP_BOX),
        walk_sensors=_poe_walk(_SMP_POE),
    ),
}
```

Note on ordering: Task 3 placed `_box_walks()` directly after `_poe_walk()` (line ~193), which sits above `MODELS` (line ~208) — so `_box_walks` is already defined before this reference. No move needed; just confirm visually.

- [ ] **Step 6: Run full suite and lint**

Run: `uv run pytest && uv run ruff check src/ tests/`
Expected: all PASS, no lint errors. Do NOT remove `parse_snmpget_value` (used by `snmp_control.py`'s `_snmpget_int()`), `parse_snmpwalk` (used by `snmp_control.py`'s `poll_all_ports()` and `snmp.py`'s own table walks), or `SnmpSensor`/`snmpget_value` (used by `snmp.py`'s static-sensor poll loop, kept as the extension point for future single-OID sensors).

- [ ] **Step 7: Commit**

```bash
git add src/sensors2mqtt/collector/snmp.py tests/test_snmp.py
git commit -m "feat(snmp): switch all models to walk-discovered box sensors

Closes the three gaps from the gdoc2netcfg cross-check: GSM7252PS
fans/PSU now collected, instance indexing discovered per model, and
all four GSM7252PS PSU rails read (rail .0 stays 'psu_power' since
it reports the total).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Documentation updates

**Files:**
- Modify: `CLAUDE.md` (Supported Switch Models table + Key Design Decisions)
- Modify: `docs/gdoc2netcfg-snmp-cross-check.md` (status note)

- [ ] **Step 1: Update CLAUDE.md**

In the Supported Switch Models table, replace the GSM7252PS row:

```
| GSM7252PS | 4526.10 | FASTPATH PoE (.15.1.1.1.2 per-port mW) |
```

with:

```
| GSM7252PS | 4526.10 | boxServices (fans/PSU, walk-discovered) + FASTPATH PoE (.15.1.1.1.2 per-port mW) |
```

In Key Design Decisions, after the line `- Switch sensor definitions are Python constants, not config files`, add:

```
- boxServices sensors (fans/temp/PSU) are discovered by walking the value
  columns, not hardcoded per instance — indexing varies by model (M4300
  uses unit.fan like "1.0"; GSM7252PS uses bare "0"/"2" with a literal
  "Not Supported" placeholder, and has 4 PSU rails)
```

- [ ] **Step 2: Update the cross-check doc**

In `docs/gdoc2netcfg-snmp-cross-check.md`, immediately after the `## Gaps found in this repo's collection` heading and its "Verified against live walks..." paragraph, add:

```markdown
> **Update 2026-06-12:** all three gaps below are fixed. boxServices
> sensors are now walk-discovered (`_box_walks()` / `parse_box_walk()` in
> `snmp.py`), the literal `"Not Supported"` marker is skipped, other
> non-integer values log a warning, and all GSM7252PS PSU rails are
> published (`psu_power` = rail `.0`, `psu_power2`-`psu_power4` = the
> per-supply rails).
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/gdoc2netcfg-snmp-cross-check.md
git commit -m "docs: record walk-discovered box sensors closing cross-check gaps

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Final verification

- [ ] **Step 1: Full test suite**

Run: `uv run pytest -v`
Expected: all tests PASS, including the unchanged non-SNMP suites (local collectors, IPMI, discovery, snmp_control)

- [ ] **Step 2: Lint**

Run: `uv run ruff check src/ tests/`
Expected: no errors

- [ ] **Step 3: Confirm working tree is clean**

Run: `git status`
Expected: nothing to commit, working tree clean (all work committed in Tasks 1-7)

Optional live smoke test if a real switch is reachable (needs `snmp.toml` and MQTT env vars): `uv run python -m sensors2mqtt.collector.snmp --once` and check the log line `published N hw values` shows fan/temp/PSU counts for a GSM7252PS.

> **Note:** the optional snmp_control dead-code cleanup that used to be Task 9 here was promoted to its own plan, `2026-06-12-snmp-control-dead-code-cleanup.md`, executed and completed before this plan started.
