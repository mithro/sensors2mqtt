# sensors2mqtt — Expanded Port Monitoring + PoE Control

## Context

The sensors2mqtt SNMP collector currently publishes only PoE power draw (mW) per port. Two enhancements are needed:

1. **Expand the sensor collector** to publish rich per-port data: link status, speed, PoE state, VLAN PVID + name, port description, and LLDP neighbor info.

2. **Add a separate PoE control service** that enables Home Assistant to toggle PoE on/off and power-cycle devices via SNMP SET.

These are two separate services sharing the same config and codebase:
- `sensors2mqtt-snmp` (existing, expanded) — read-only monitoring
- `sensors2mqtt-snmp-control` (new) — PoE toggle + power cycle commands

**Repository**: `~/github/mithro/sensors2mqtt`

## Work Process

- **Work on a dedicated branch** in its own git worktree (not main)
- **Frequent small commits** — commit immediately after each logical change, not in big batches
- **Track progress in-branch** — write both the plan and task list as files in the branch, commit and push after every update
- **Push the branch** after every commit so progress is visible remotely
- **Code review checkpoints** — after completing each Part (Part 1 sensor expansion, Part 2 control service), dispatch a code-review subagent to review all changes on the branch before proceeding. Also review after any step that touches discovery.py or base.py (shared code affecting all collectors)

## HA MQTT Discovery Conventions

Based on the authoritative reference at `/home/tim/github/mithro/gdoc2netcfg/docs/ha-mqtt-discovery-reference.md`:

### Entity Naming

- Entity `name` should identify **only the data point** (e.g. "Port 01 PoE Power", "Port 01 Link"), NOT the device name — HA prepends the device name automatically via `has_entity_name`
- `unique_id` is immutable: `{node_id}_{suffix}` (e.g. `sw_netgear_gsm7252ps_s2_port01_poe_mw`). No `sensors2mqtt_` prefix — matches existing format.
- Topic `object_id` should equal `unique_id`
- Use `default_entity_id` (not deprecated `object_id` JSON field) for predictable initial entity IDs: `sensor.{node_id}_port{nn}_{type}`

### State Topics

- **Discovery config topics**: ALWAYS retain
- **State topics**: Do NOT retain (current code retains — needs fixing). Let the publisher send fresh state on next cycle.
- **Availability topics**: Retain

### Origin Dict

Include in all discovery payloads:
```json
{
  "origin": {
    "name": "sensors2mqtt",
    "sw": "0.1.0",
    "url": "https://github.com/mithro/sensors2mqtt"
  }
}
```

### Entity Categories

| entity_category | Use |
|-----------------|-----|
| (not set) | Primary entities: PoE power, PoE toggle, power cycle button |
| `"diagnostic"` | Informational: link status, speed, VLAN, LLDP, port description, PoE admin/detection state |
| `"config"` | User-configurable: force override switch |

### expire_after

Use `expire_after` on sensor entities (supported on `sensor` and `binary_sensor` only). Set to `poll_interval * 5` (e.g. 150s for 30s polls) so entities go unavailable if the publisher stops.

### Multi-Component Discovery

With 9+ sensors per port × 48 ports, use multi-component device discovery (`homeassistant/device/{node_id}/config`) to publish all port entities in fewer messages. Each switch is one device with many components.

Note: switch-level hardware sensors (fans, temp, PSU) are separate components on the same device.

### via_device

Express switch topology: devices connected to a switch port can reference the switch as `via_device`. This is informational — shows connected devices on the switch's HA device page.

## Design Review Findings & Resolutions

Issues identified by code review subagent, with resolutions incorporated into the plan:

### Critical — addressed

1. **LLDP parsing needs dedicated parser**: `parse_snmpwalk()` only extracts the last OID component. LLDP uses a three-part index `{timeMark}.{localPortNum}.{remIndex}`. Resolution: Step 4 must implement a separate `parse_lldp_walk()` function (not reuse `parse_snmpwalk`), matching the pattern from `gsm7252ps_s1_inventory.py:get_lldp_neighbors()` lines 139-145.

2. **Port index alignment**: `dot1qPvid` uses `dot1dBasePort` index which may differ from `ifIndex`. Resolution: Step 8 fixture capture must verify index alignment across all walks on all three switches before implementation.

3. **Port count must be in model definitions**: Discovery is published once per startup. Without knowing the port count, partial SNMP responses cause incomplete discovery. Resolution: Add `port_count` and `poe_port_count` to `SwitchModel` (M4300: 24/0, GSM7252PS: 52/48, S3300: 52/48). Publish discovery for all ports regardless of walk results.

4. **`unique_id` format change is a breaking migration**: Current format is `{node_id}_{suffix}` (e.g. `sw_netgear_gsm7252ps_s2_port01_poe_mw`). Changing this orphans existing entities. Resolution: keep existing `{node_id}_{suffix}` format. Do NOT add `sensors2mqtt_` prefix. Step 7 cleanup must clear old discovery messages before publishing new ones.

5. **`origin` is required for multi-component discovery**: Must be added before Step 6 (multi-component), not in parallel. Resolution: Step 2 is a hard prerequisite for Step 6, noted in dependencies.

### Important — addressed

6. **`default_entity_id` not set**: Resolution: add to discovery payloads. Format: `sensor.{node_id}_port{nn}_{type}`.

7. **Port description in entity name violates HA convention**: Entity `name` should identify only the data point. Resolution: entity names are just "Port 01 PoE Power", "Port 01 Link", etc. Port description (`ifAlias`) is published as a separate diagnostic sensor entity. Removed description embedding from entity names.

8. **`state_class` defaults to "measurement" for all sensors**: String/enum sensors would cause HA to log errors. Resolution: change `SensorDef.state_class` default to `None`. Set `state_class="measurement"` explicitly only on numeric sensors.

9. **Command handler blocks MQTT thread**: Power cycle polls in a loop for up to 90s. Resolution: dispatch commands to a worker thread (not the MQTT on_message callback).

10. **ON/OFF → SNMP value mapping needs explicit documentation**: "ON" → `i 1` (enable), "OFF" → `i 2` (disable). Resolution: noted in Step 5, with unit test required.

11. **Cleanup step underspecified**: Resolution: Step 7 must publish empty retained payloads to each old discovery topic BEFORE publishing new discovery. Enumerate old topics: `homeassistant/sensor/{node_id}/{suffix}/config` for every old suffix, plus old state blob `sensors2mqtt/{node_id}/state`.

12. **Force override lost on restart**: Resolution: publish force state with `retain=True`, read back on startup.

### Minor — noted

13. LLDP OID shorthand: full base is `1.0.8802.1.1.2.1.4.1.1` — noted in LLDP section.
14. `--once` on control service: publishes discovery + current state, then exits. No command processing.
15. HA birth message protocol (`homeassistant/status` subscription): deferred to future work.
16. Standard POWER-ETHERNET-MIB OIDs confirmed working on both switches (verified in brainstorming session).

## Part 1: Expand Sensor Collector Per-Port Data

### Per-Port Sensors to Publish

**All switches (M4300, GSM7252PS, S3300 — every port):**

| Sensor | OID | Type | entity_category | Example |
|--------|-----|------|-----------------|---------|
| Link status | `ifOperStatus` `.2.2.1.8.{port}` | binary_sensor, device_class: connectivity | (primary) | up |
| Link speed | `ifHighSpeed` `.31.1.1.1.15.{port}` | Mbps, device_class: data_rate | diagnostic | 1000 |
| VLAN PVID | `dot1qPvid` `.17.7.1.4.5.1.1.{port}` | integer | diagnostic | 90 |
| VLAN name | `dot1qVlanStaticName` `.17.7.1.4.3.1.1.{vlan}` | string | diagnostic | iot |
| Port description | `ifAlias` `.31.1.1.1.18.{port}` | string | diagnostic | eth0.rpi5-pmod |
| LLDP neighbor | `lldpRemEntry` `.9` + `.8` | string | diagnostic | rpi5-pmod / eth0 |

**PoE switches only (GSM7252PS, S3300 — PoE-capable ports):**

| Sensor | OID | Type | entity_category | Example |
|--------|-----|------|-----------------|---------|
| PoE power | Netgear `4526.1x.15.1.1.1.2.1.{port}` | mW, device_class: power | (primary) | 3300 |
| PoE admin state | `pethPsePortAdminEnable` `.105.1.1.1.3.1.{port}` | enum: enabled/disabled | diagnostic | enabled |
| PoE detection status | `pethPsePortDetectionStatus` `.105.1.1.1.6.1.{port}` | enum: unused/searching/delivering/fault | diagnostic | delivering |

Notes:
- PoE detection status: 1=unused, 2=searching, 3=delivering, 4=fault. Uses "unused" (not "disabled") to distinguish from admin-disabled.
- Link status as `binary_sensor` with `device_class: connectivity` gives native Connected/Disconnected display in HA.
- Link speed uses `device_class: data_rate` with `unit_of_measurement: Mbit/s`.
- The M4300 has 24 ports (20×10GBASE-T + 4×combo). All get link/speed/VLAN/LLDP sensors but no PoE sensors.

### Data Collection Strategy

**Walks per poll cycle (ALL switches — link/VLAN data applies to every port):**
- `ifOperStatus` (link up/down)
- `ifHighSpeed` (speed)
- `dot1qPvid` (VLAN PVID)

**Walks per poll cycle (PoE switches only):**
- `pethPsePortAdminEnable` (PoE admin state)
- `pethPsePortDetectionStatus` (PoE detection state)
- PoE power walk (already implemented)

**Walks at startup (cached, refreshed every 10 minutes, ALL switches):**
- `ifAlias` (port descriptions) — already implemented
- `dot1qVlanStaticName` (VLAN ID → name mapping)
- `lldpRemEntry.9` + `.8` (LLDP neighbor)

### State Topic Structure

Switch to **per-port state topics** (NOT retained):
```
sensors2mqtt/{node_id}/port/{port_nn}/state → {
  "poe_mw": 3300,
  "poe_admin": "enabled",
  "poe_status": "delivering",
  "link": "up",
  "speed_mbps": 1000,
  "vlan_pvid": 90,
  "vlan_name": "iot",
  "description": "eth0.rpi5-pmod",
  "lldp_neighbor": "rpi5-pmod / eth0"
}
```

Switch-level hardware sensors (fans, temp, PSU) stay on `sensors2mqtt/{node_id}/state` (also NOT retained).

Switch-level availability stays on `sensors2mqtt/{node_id}/status` (retained).

### HA Discovery

Use multi-component device discovery per switch. Entity names identify only the data point — HA prepends the device name automatically:

```
Device: "sw-netgear-gsm7252ps-s2"
  → "Port 01 PoE Power"        (HA shows: "sw-netgear-gsm7252ps-s2 Port 01 PoE Power")
  → "Port 01 Link"             (HA shows: "sw-netgear-gsm7252ps-s2 Port 01 Link")
  → "Port 01 VLAN"             (HA shows: "sw-netgear-gsm7252ps-s2 Port 01 VLAN")
  → "Port 01 Description"      (diagnostic — shows "eth0.rpi5-pmod")
  → "Port 01 LLDP Neighbor"    (diagnostic — shows "rpi5-pmod / eth0")
```

Entity names do NOT embed ifAlias descriptions. Port descriptions are separate diagnostic sensors. This follows the HA convention that `name` identifies only the data point.

`unique_id` format: `{node_id}_port{nn}_{type}` (e.g. `sw_netgear_gsm7252ps_s2_port01_poe_mw`). No `sensors2mqtt_` prefix — matches existing format.

`default_entity_id` format: `sensor.{node_id}_port{nn}_{type}` (set on first discovery only).

`SensorDef.state_class`: default `None`. Only set to `"measurement"` on numeric sensors (PoE power, speed, PVID). String/enum sensors omit it.

### LLDP Parsing

LLDP remote table base OID: `1.0.8802.1.1.2.1.4.1.1` (IEEE 802.1AB LLDP-MIB). Fields: `.9` = system name, `.8` = port description. Index format: `{timeMark}.{localPortNum}.{remIndex}` — three components, not one.

**Critical**: `parse_snmpwalk()` cannot parse this — it only extracts the last OID component. Must implement a dedicated `parse_lldp_walk()` matching the three-group regex from `gsm7252ps_s1_inventory.py:get_lldp_neighbors()` (lines 139-145).

Format: `{sys_name} / {port_desc}` (e.g. `rpi5-pmod / eth0`). Empty string if no LLDP neighbor on that port.

### Implementation Steps (Part 1)

**Step 1: Fix state topic retention**
- Change all `client.publish(state_topic, ..., retain=True)` to `retain=False` for state topics
- Keep `retain=True` for discovery config and availability topics
- Applies to all collectors (snmp, hwmon, ipmi_sdr)

**Step 2: Add origin dict to discovery payloads + fix SensorDef.state_class default**
- Update `discovery.py` to include `origin` in all payloads
- Add `origin` parameter to `discovery_payload()` and `publish_discovery()`
- Change `SensorDef.state_class` default from `"measurement"` to `None`. Only numeric sensors should set `state_class="measurement"` explicitly. String/enum sensors must omit it (HA logs errors if `state_class` is set on non-numeric sensors).
- **This is a prerequisite for Step 6** (multi-component discovery requires origin)

**Step 3: Add VLAN name lookup**
- Walk `dot1qVlanStaticName`, cache as `{vlan_id: name}` dict
- Refresh every 10 minutes
- File: `src/sensors2mqtt/collector/snmp.py`

**Step 4: Add LLDP neighbor lookup**
- Walk `lldpRemEntry.9` and `.8`, parse three-part OID index
- Cache as `{port: "sysname / portdesc"}` dict
- Refresh every 10 minutes
- File: `src/sensors2mqtt/collector/snmp.py`

**Step 5: Add port_count to SwitchModel + per-port state walks**
- Add `port_count: int` and `poe_port_count: int` to `SwitchModel`: M4300 (24, 0), GSM7252PS (52, 48), S3300 (52, 48)
- New method `poll_port_status(switch)` walking ifOperStatus, ifHighSpeed, dot1qPvid (all switches) + pethPsePortAdminEnable, pethPsePortDetectionStatus (PoE switches only, ports 1..poe_port_count)
- Returns `{port: {field: value}}` dict for ports 1..port_count
- Discovery publishes for all port_count ports regardless of walk results (stable entity set)
- **Verify in fixtures**: `dot1qPvid` port indices align with `ifIndex` on all three switches
- File: `src/sensors2mqtt/collector/snmp.py`

**Step 6: Switch to per-port state topics + multi-component discovery**
- Publish per-port JSON to `sensors2mqtt/{node_id}/port/{port_nn}/state` (not retained)
- Publish multi-component device discovery to `homeassistant/device/{node_id}/config` (retained)
- Keep switch-level sensors on `sensors2mqtt/{node_id}/state`
- File: `src/sensors2mqtt/collector/snmp.py`, `src/sensors2mqtt/discovery.py`

**Step 7: Clean up old retained MQTT messages (BEFORE publishing new discovery)**
- Must run before Step 6 publishes new discovery, to avoid duplicate entities
- For each switch node_id, publish empty (`""`) retained payloads to:
  - Old state blob: `sensors2mqtt/{node_id}/state` (clear the retained port data blob)
  - Old per-sensor discovery topics: `homeassistant/sensor/{node_id}/{suffix}/config` for every old suffix (e.g. `port01_poe_mw` through `port48_poe_mw`, plus `fan1_rpm`, `temp`, `psu_power`, etc.)
- Can be a one-off cleanup script run manually, or inline in collector startup (detect and clear old topics on first run)

**Step 8: Capture fixture data + tests**
- Save snmpwalk output for all new OIDs from both PoE switches
- Tests for VLAN name lookup, LLDP parsing, port status polling, discovery payloads
- Files: `tests/fixtures/snmpwalk_*`, `tests/test_snmp.py`

## Part 2: PoE Control Service

### Architecture

A **separate service** (`python -m sensors2mqtt.collector.snmp_control`) that:
- Reads the same `snmp.toml` config file
- Connects to MQTT independently
- Subscribes to command topics for PoE toggle/cycle
- Executes SNMP SET commands via subprocess `snmpset`
- Publishes HA switch + button entity discovery
- Polls port state to determine entity availability

### HA Entities Per PoE Port

| Entity | HA Type | entity_category | Purpose |
|--------|---------|-----------------|---------|
| PoE Toggle | `switch` | (primary) | Enable/disable PoE admin state |
| Power Cycle | `button` | (primary) | Disable → verify off → re-enable → verify on |
| Force Override | `switch` | `config` (hidden) | Unlock control when port is in disabled state |

Entity names (HA prepends device name automatically):
- "Port 01 PoE" — toggle
- "Port 01 PoE Cycle" — button
- "Port 01 PoE Force" — override (hidden, entity_category: config)

Entity names do NOT embed ifAlias — consistent with sensor collector convention.

### Port Control State Machine

| Link | PoE Detection | Control State | Can toggle? |
|------|--------------|--------------|-------------|
| down | any | **available** | Yes |
| up | delivering/searching/fault | **available** | Yes |
| up | unused (not negotiated) | **disabled** | No (greyed out) |
| up | unused + Force Override ON | **available** | Yes (override) |

Disabled means the entities exist but have their per-port availability set to `offline`. The entities are greyed out in HA, not removed.

### MQTT Topics

```
# Command topics (subscribed by control service)
sensors2mqtt/{node_id}/port/{port_nn}/poe/set        → "ON" or "OFF"
sensors2mqtt/{node_id}/port/{port_nn}/poe/cycle       → "PRESS"

# State topics (published by control service, NOT retained)
sensors2mqtt/{node_id}/port/{port_nn}/poe/state       → "ON" or "OFF"

# Per-port availability (retained)
sensors2mqtt/{node_id}/port/{port_nn}/poe/available    → "online" or "offline"

# Force override
sensors2mqtt/{node_id}/port/{port_nn}/poe/force/set    → "ON" or "OFF"
sensors2mqtt/{node_id}/port/{port_nn}/poe/force/state   → "ON" or "OFF"
```

### Power Cycle Sequence (poll-based, no sleep)

```
1. Pre-check: snmpget pethPsePortAdminEnable + pethPsePortDetectionStatus + ifOperStatus
   → Confirm current state matches expectation
2. SNMP SET pethPsePortAdminEnable = 2 (disable)
3. Poll loop (timeout 30s):
   → snmpget pethPsePortDetectionStatus until "unused" (1)
   → snmpget ifOperStatus until "down" (2)
4. Port confirmed off
5. SNMP SET pethPsePortAdminEnable = 1 (enable)
6. Poll loop (timeout 60s):
   → snmpget pethPsePortDetectionStatus until "delivering" (3)
7. Publish updated state to MQTT
```

If timeout: publish the actual state (don't revert). If link stays up after PoE disable (device has dual power), port enters disabled control state (link UP + PoE not negotiated).

### Config Additions

```toml
[switches.sw-netgear-gsm7252ps-s2]
model = "gsm7252ps"
host = "sw-netgear-gsm7252ps-s2.example.com"
community = "public"
write_community = "private"

[switches.sw-netgear-s3300-1]
model = "s3300"
host = "sw-netgear-s3300-1.example.com"
community = "public"
write_community = "private"
```

Switches without `write_community` are ignored by the control service. M4300 has no PoE — never gets `write_community`.

### SNMP OIDs for Control

| OID | R/W | Values | Purpose |
|-----|-----|--------|---------|
| `pethPsePortAdminEnable` `.105.1.1.1.3.1.{port}` | R/W | 1=enabled, 2=disabled | Toggle PoE |
| `pethPsePortDetectionStatus` `.105.1.1.1.6.1.{port}` | R | 1=unused, 2=searching, 3=delivering, 4=fault | Verify state |
| `ifOperStatus` `.2.2.1.8.{port}` | R | 1=up, 2=down | Verify link |

Write community `private` confirmed working on both PoE switches. SNMP SET not yet tested (requires toggling a real port).

### Implementation Steps (Part 2)

**Step 1: Add write_community to SwitchConfig + config loading**
- New field: `write_community: str | None = None`
- Load from TOML
- File: `src/sensors2mqtt/collector/snmp.py`, `snmp.toml`

**Step 2: Create snmp_control module skeleton**
- New file: `src/sensors2mqtt/collector/snmp_control.py`
- Imports shared code from `snmp.py`
- Own `main()` with argparse, signal handling, MQTT connection
- Filters to switches with `write_community`

**Step 3: Port state polling + control availability**
- Poll pethPsePortAdminEnable, pethPsePortDetectionStatus, ifOperStatus
- Apply state machine → publish per-port `poe/available`
- Reuse `SnmpCollector`'s SNMP helpers

**Step 4: HA switch/button entity discovery**
- Publish discovery for toggle, cycle button, force override per port
- Use multi-component discovery
- Include `origin` dict
- Use ifAlias for entity names (fetched via shared `fetch_port_descriptions()`)

**Step 5: Toggle command handler**
- Subscribe to `sensors2mqtt/+/port/+/poe/set`
- Parse node_id + port from topic; ignore commands for unmanaged switches (log warning)
- **Value mapping**: "ON" → `snmpset ... i 1` (enable), "OFF" → `snmpset ... i 2` (disable). Unit test required for this mapping.
- `snmpset -v2c -c WRITE_COMMUNITY HOST 1.3.6.1.2.1.105.1.1.1.3.1.{port} i {1|2}`
- Verify with snmpget, publish confirmed state
- **Threading**: command handler must NOT block the MQTT on_message callback. Dispatch to a worker thread or thread pool executor.

**Step 6: Power cycle handler**
- Subscribe to `sensors2mqtt/+/port/+/poe/cycle`
- Poll-based sequence: pre-check → disable → poll off → enable → poll delivering
- Runs in worker thread (same pool as Step 5) — blocking poll loop is fine in the worker
- Timeout → publish actual state

**Step 7: Force override handler**
- Subscribe to `sensors2mqtt/+/port/+/poe/force/set`
- Publish force state to `poe/force/state` with `retain=True` (survives restart)
- On startup: read back retained force states via MQTT subscription before publishing availability
- Override availability for disabled ports when force is ON

**Step 8: Systemd service + tests**
- `deploy/sensors2mqtt-snmp-control.service`
- `tests/test_snmp_control.py` — state machine, power cycle, force override

## File Changes Summary

| File | Change |
|------|--------|
| `src/sensors2mqtt/collector/snmp.py` | Per-port status walks, VLAN name cache, LLDP cache, per-port state topics, write_community field, fix state retention |
| `src/sensors2mqtt/collector/snmp_control.py` | **New** — PoE toggle/cycle control service |
| `src/sensors2mqtt/discovery.py` | Add origin dict, switch/button discovery helpers, multi-component discovery support |
| `src/sensors2mqtt/base.py` | Fix state topic retention (retain=False) |
| `src/sensors2mqtt/collector/hwmon.py` | Fix state topic retention |
| `src/sensors2mqtt/collector/ipmi_sdr.py` | Fix state topic retention |
| `snmp.toml` | Add `write_community` to PoE switches |
| `deploy/sensors2mqtt-snmp-control.service` | **New** |
| `tests/test_snmp.py` | Per-port polling, VLAN names, LLDP, discovery payloads |
| `tests/test_snmp_control.py` | **New** — control service tests |
| `tests/fixtures/` | New fixture files for port status, VLAN names, LLDP walks |

## One-Shot Mode

All collectors must support a `--once` flag that polls once and exits (no loop). This is essential for debugging:

```bash
# Poll all switches once and exit
uv run python -m sensors2mqtt.collector.snmp --config snmp.toml --once

# Poll once with debug logging
uv run python -m sensors2mqtt.collector.snmp --config snmp.toml --once --log-level DEBUG

# Control service: one-shot state check (no MQTT subscribe loop)
uv run python -m sensors2mqtt.collector.snmp_control --config snmp.toml --once
```

The `--once` flag should:
- Run one full poll cycle
- Publish discovery + state + availability
- Print a summary to stdout
- Exit cleanly (no signal handling needed)

## Verification

### Unit tests and lint
1. `make test` — all tests pass
2. `make lint` — ruff clean

### Existing collector regression

Verify the existing hwmon and ipmi_sdr collectors still work after the state retention fix:
```bash
# hwmon can't run on ten64 (no hwmon chips) — verified via tests only
# IPMI SDR: one-shot test against big-storage BMC
uv run python -m sensors2mqtt.collector.ipmi_sdr  # (brief run, ctrl-C after first poll)
```

### Sensor collector — per-switch verification

Run the sensor collector in one-shot mode against each switch individually and verify output:

**sw-netgear-m4300-24x** (switch-ip) — 24 ports, no PoE:
```bash
# Verify: link status + speed for all 24 ports, VLAN PVID, LLDP for connected ports
# Expected: ports 1-2 (trunks, 10G), port 3 (big-storage BMC, 1G), ports 19-20 (big-storage 10G), ports 21-24 (LAG to sw-bb-25g, 10G)
# No PoE sensors should appear
```

**sw-netgear-gsm7252ps-s2** (switch-ip) — 48 PoE + 4 SFP+, PoE switch:
```bash
# Verify: link + speed + VLAN + LLDP for all ports
# Verify: PoE power + admin + detection for ports 1-48
# Expected active PoE: ports 1, 2, 5, 41, 47 (known RPi/IoT devices)
# Expected LLDP: rpi5-pmod, rpi4-pmod, sw-netgear-m4300-24x, sw-netgear-s3300-1, etc.
```

**sw-netgear-s3300-1** (switch-ip) — 48 PoE + 4 SFP+, PoE switch:
```bash
# Verify: link + speed + VLAN + LLDP for all ports
# Verify: PoE power + admin + detection for ports 1-48
# Expected active PoE: ~23 FPGA RPi ports (ports 1-33 odd + some even)
# Expected VLAN: most ports PVID=21, uplinks PVID=1
# Verify fans (3), temp, PSU sensors also present
```

### MQTT verification

```bash
# Verify per-port state topics are published (not retained)
mosquitto_sub -h ha.example.com -u $MQTT_USER -P $MQTT_USER \
  -t 'sensors2mqtt/+/port/+/state' -v -C 10

# Verify state topics are NOT retained (start sub after publisher exits — should get nothing)
mosquitto_sub -h ha.example.com -u $MQTT_USER -P $MQTT_USER \
  -t 'sensors2mqtt/+/port/+/state' -v -W 5

# Verify discovery topics ARE retained
mosquitto_sub -h ha.example.com -u $MQTT_USER -P $MQTT_USER \
  -t 'homeassistant/device/+/config' -v -C 3
```

### HA entity verification

```bash
# Query HA API for all sensors2mqtt entities, verify:
# - Entity names don't duplicate device name (HA prepends it)
# - entity_category: diagnostic on informational sensors
# - device_class: connectivity on link status binary_sensors
# - Correct device grouping (all port entities under one switch device)
curl -s -H "Authorization: Bearer <token>" "http://ha.example.com:8123/api/states" | \
  uv run python -c "import json,sys; [print(f'{s[\"entity_id\"]:60s} {s[\"state\"]:>12s}  {s[\"attributes\"].get(\"friendly_name\",\"\")}') for s in sorted(json.load(sys.stdin), key=lambda x: x['entity_id']) if 'sensors2mqtt' in json.dumps(s)]"
```

### Control service verification (Part 2)

```bash
# One-shot: verify switch + button entities published for PoE switches only
uv run python -m sensors2mqtt.collector.snmp_control --config snmp.toml --once

# Verify M4300 does NOT get any control entities (no write_community, no PoE)

# Toggle test on a safe port (one with no device or a spare RPi):
# 1. Note current PoE state in HA
# 2. Toggle OFF from HA UI
# 3. Verify snmpget confirms disabled
# 4. Toggle ON from HA UI
# 5. Verify snmpget confirms re-enabled + delivering

# Power cycle test on a non-critical device
```

## Future Work (task #14)

Expand the control daemon to support setting VLAN PVID via SNMP SET or SSH CLI, exposed as HA select/number entities.
