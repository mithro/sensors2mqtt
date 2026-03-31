# VLAN PVID Control — Handoff Document

## Goal

Expand the sensors2mqtt PoE control service (`snmp_control.py`) to also support
setting the VLAN PVID (Port VLAN ID) on individual switch ports via Home
Assistant. This lets a user change which VLAN a port belongs to from the HA UI.

## Repository

`~/github/mithro/sensors2mqtt` — Python package installed at `/opt/sensors2mqtt/`
on each host. Services managed via systemd.

## What Already Exists

The control service (`src/sensors2mqtt/collector/snmp_control.py`) already:
- Connects to MQTT, subscribes to command topics per switch
- Dispatches commands to a `ThreadPoolExecutor` (4 workers)
- Executes SNMP SET via `_snmpset_int()` (subprocess `snmpset -v2c`)
- Publishes HA MQTT discovery (switch/button entities per port)
- Polls port state periodically and publishes state + availability
- Uses per-port sub-devices with `via_device` linking to parent switch
- Filters to switches with `write_community` configured in `snmp.toml`

The sensor collector (`src/sensors2mqtt/collector/snmp.py`) already:
- Reads VLAN PVID per port via `dot1qPvid` (OID `1.3.6.1.2.1.17.7.1.4.5.1.1`)
- Fetches VLAN ID → name mapping via `dot1qVlanStaticName` (OID `1.3.6.1.2.1.17.7.1.4.3.1.1`)
- Publishes both as per-port sensor state (`vlan_pvid` and `vlan_name` fields)

## SNMP OIDs

| OID | Name | Type | R/W | Purpose |
|-----|------|------|-----|---------|
| `1.3.6.1.2.1.17.7.1.4.5.1.1.{port}` | dot1qPvid | Gauge32 | R/W | Port's native VLAN ID |
| `1.3.6.1.2.1.17.7.1.4.3.1.1.{vlan}` | dot1qVlanStaticName | STRING | R | VLAN name |

**SNMP SET syntax**: `snmpset -v2c -c WRITE_COMMUNITY HOST OID u VLAN_ID`
(Use `u` for unsigned Gauge32, not `i` for integer.)

## Available VLANs Per Switch

**GSM7252PS-S2** (12 VLANs):
| ID | Name | Purpose |
|----|------|---------|
| 1 | default | Default VLAN |
| 5 | net | Network infrastructure |
| 6 | pwr | Power infrastructure |
| 7 | store | Storage (IOMs) |
| 10 | int | Internal LAN |
| 20 | roam | Roaming WiFi |
| 21 | fpgas | FPGA test network |
| 41 | sm | Supermicro network |
| 90 | iot | IoT devices |
| 99 | guest | Guest network |
| 121 | t-fpgas | Transit to fpgas |
| 141 | t-sm | Transit to SM |

**S3300** (5 VLANs): 1 (Default), 5 (net), 21 (fpgas), 121 (t-fpgas), 4089 (Auto-Video)

**M4300**: Has VLANs but **SNMP writes fail** — see caveat below.

## Critical Caveat: M4300 SNMP Write Failure

The M4300 switch (FASTPATH firmware 12.0.13.8) rejects Q-BRIDGE-MIB SNMP SET
operations with a `commitFailed` error. This includes `dot1qPvid` writes.

**The M4300 currently has no `write_community` in snmp.toml**, so the control
service already excludes it. If M4300 VLAN control is ever needed, it would
require SSH CLI commands instead of SNMP:

```
sshpass -p PASSWORD ssh -o PubkeyAuthentication=no -tt admin@SWITCH_IP
# Then: interface 0/N → switchport access vlan VLAN_ID
```

**For this implementation: only GSM7252PS-S2 and S3300 need VLAN PVID control.**
Both have `write_community = "private"` configured.

## Proposed HA Entity

**Entity type**: `select` (dropdown with VLAN options)

Each port on switches with `write_community` gets a VLAN select entity:
- **Name**: `Port {nn} VLAN` (HA prepends device name)
- **Options**: List of available VLAN IDs as strings, e.g. `["1", "5", "21", "90"]`
  - Fetch once at startup by walking `dot1qVlanStaticName`
  - Consider also showing the name: `"21 (fpgas)"` or just the ID
- **State topic**: `sensors2mqtt/{node_id}/port/{nn}/vlan/pvid/state` → current VLAN ID
- **Command topic**: `sensors2mqtt/{node_id}/port/{nn}/vlan/pvid/set` → desired VLAN ID
- **entity_category**: None (primary) — VLAN assignment is a core operational control
- **icon**: `mdi:lan`

**HA discovery topic**: `homeassistant/select/{switch_node_id}/port{nn}_vlan_pvid/config`

**Device**: Use the existing per-port sub-device (`sensors2mqtt_{switch_node_id}_port{nn}`)
so the VLAN select appears alongside the port's sensors and PoE controls.

## MQTT Topics

```
# Command (subscribed by control service)
sensors2mqtt/{node_id}/port/{nn}/vlan/pvid/set    → "21" (VLAN ID as string)

# State (published by control service, NOT retained — sensor collector handles state)
sensors2mqtt/{node_id}/port/{nn}/vlan/pvid/state  → "21"
```

**Important**: The sensor collector (`snmp.py`) already publishes `vlan_pvid` as
part of the per-port state JSON. The control service should publish the confirmed
VLAN ID to a separate state topic after a successful SET, and the sensor
collector will pick up the change on its next poll cycle.

## Implementation Approach

### Step 1: Add VLAN PVID to PortControlState

Extend the `PortControlState` dataclass in `snmp_control.py`:
```python
vlan_pvid: int = 0
```

### Step 2: Fetch available VLANs at startup

Import or duplicate `fetch_vlan_names()` from `snmp.py`. This walks
`dot1qVlanStaticName` and returns `{vlan_id: name}`. Store per switch.

### Step 3: Poll VLAN PVID per port

In `poll_all_ports()`, add a walk of `dot1qPvid` and store the result in
`PortControlState.vlan_pvid`. The existing `_walk_int_table()` pattern
(from `snmp.py`) can be reused via a similar helper in the control service.

### Step 4: Add HA select discovery

In `publish_discovery()`, add a `select` entity per port:
```python
vlan_config = {
    "name": f"Port {nn} VLAN{host_suffix}",
    "unique_id": f"{switch.node_id}_port{nn}_vlan_pvid",
    "command_topic": f"sensors2mqtt/{switch.node_id}/port/{nn}/vlan/pvid/set",
    "state_topic": f"sensors2mqtt/{switch.node_id}/port/{nn}/vlan/pvid/state",
    "options": [str(vid) for vid in sorted(available_vlans.keys())],
    "device": port_dev_dict,
    "availability": [...],  # same dual availability as PoE toggle
    "origin": ORIGIN,
    "icon": "mdi:lan",
}
client.publish(
    f"homeassistant/select/{switch.node_id}/port{nn}_vlan_pvid/config",
    json.dumps(vlan_config), retain=True,
)
```

### Step 5: Add command handler

```python
def _handle_vlan_set(self, switch, port, payload):
    """Set VLAN PVID on a port via SNMP SET."""
    vlan_id = int(payload)
    # Validate against available VLANs
    # snmpset with type "u" (unsigned Gauge32)
    # Verify with snmpget
    # Publish confirmed state
```

**SNMP SET for Gauge32**: Use `"u"` type flag, not `"i"`:
```python
subprocess.run([
    "snmpset", "-v2c", "-c", switch.write_community,
    switch.host, f"{VLAN_PVID_OID}.{port}", "u", str(vlan_id),
], ...)
```

This requires a new `_snmpset_unsigned()` helper (or modify `_snmpset_int` to
accept a type parameter).

### Step 6: Extend message router

In `_on_message()`, extend the regex to also match VLAN topics:
```python
# Current: r"sensors2mqtt/([^/]+)/port/(\d+)/poe/(set|cycle|force/set)$"
# New: also match vlan/pvid/set
m = re.match(
    r"sensors2mqtt/([^/]+)/port/(\d+)/(poe/(set|cycle|force/set)|vlan/pvid/set)$",
    topic,
)
```

And subscribe to the new topic pattern in `run()`:
```python
client.subscribe(f"sensors2mqtt/{sw.node_id}/port/+/vlan/pvid/set")
```

### Step 7: Publish VLAN state on each poll

In the poll loop, publish current PVID per port:
```python
for port in range(1, switch.poe_port_count + 1):
    nn = str(port).zfill(2)
    pvid = self._port_states[switch.node_id][port].vlan_pvid
    client.publish(
        f"sensors2mqtt/{switch.node_id}/port/{nn}/vlan/pvid/state",
        str(pvid), retain=False,
    )
```

### Step 8: Tests

Add tests to `tests/test_snmp_control.py`:
- VLAN set handler: valid ID, invalid ID, out-of-range
- VLAN discovery: select entity structure, options list
- Message routing: VLAN topic parsed correctly
- SNMP SET uses `u` type for Gauge32

## Key Design Decisions

1. **VLAN select applies to ALL ports on PoE switches** (1..port_count, not just
   1..poe_port_count) since VLAN assignment is independent of PoE capability.
   However, only switches with `write_community` get VLAN control.

2. **Options list is per-switch** (each switch has its own VLAN set). The
   GSM7252PS-S2 has 12 VLANs while S3300 has only 5.

3. **No "port type" control** (access/trunk/hybrid) in this iteration. The
   `dot1qPvid` OID only sets the native/untagged VLAN. Trunk configuration
   requires Q-BRIDGE bitmap writes which are more complex.

4. **State is not retained** — the sensor collector publishes fresh VLAN state
   every poll cycle. The control service publishes to a separate topic only
   immediately after a SET to give instant UI feedback.

5. **Safety**: Validate VLAN ID against the switch's available VLAN list before
   attempting SET. Log a warning and reject unknown VLAN IDs.

## Files to Modify

| File | Changes |
|------|---------|
| `src/sensors2mqtt/collector/snmp_control.py` | VLAN handler, discovery, state polling, message routing |
| `src/sensors2mqtt/collector/snmp.py` | Export `fetch_vlan_names` or make it importable |
| `tests/test_snmp_control.py` | VLAN control tests |
| `snmp.toml` | No changes needed (uses existing write_community) |

## Verification

1. `make test` — all tests pass
2. `make lint` — ruff clean
3. Deploy to ten64, restart `sensors2mqtt-snmp-control`
4. In HA: verify VLAN select entities appear on per-port sub-devices
5. In HA: change a port's VLAN via the select dropdown
6. Verify via SNMP: `snmpget -v2c -c public HOST dot1qPvid.{port}` shows new VLAN
7. Verify sensor collector picks up the change on next poll cycle

## References

- HA MQTT Select entity: https://www.home-assistant.io/integrations/select.mqtt/
- Q-BRIDGE-MIB (RFC 4363): dot1qPvid, dot1qVlanStaticName
- Existing PoE control implementation: `src/sensors2mqtt/collector/snmp_control.py`
- Project conventions: `CLAUDE.md` in repo root
