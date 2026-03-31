# Plan: HA PoE Port Toggle via SNMP

## Goal

Enable Home Assistant to toggle PoE power (enable/disable) on individual
switch ports, allowing remote power cycling of PoE-powered devices (RPis,
access points, etc.) from the HA dashboard.

## Current State

- sensors2mqtt already publishes per-port PoE power readings as HA sensor entities
- Both PoE switches (GSM7252PS-S2, S3300-1) support the standard POWER-ETHERNET-MIB
- `pethPsePortAdminEnable` (OID `1.3.6.1.2.1.105.1.1.1.3.1.{port}`) is readable on both
- SNMP write community `private` is configured on both switches
- The M4300 has no PoE (10G copper switch) — not applicable

## Verified SNMP OIDs

| OID | Values | Description |
|-----|--------|-------------|
| `1.3.6.1.2.1.105.1.1.1.3.1.{port}` | 1=enabled, 2=disabled | pethPsePortAdminEnable |
| `1.3.6.1.2.1.105.1.1.1.6.1.{port}` | 1=disabled, 2=searching, 3=delivering, 4=fault | pethPsePortDetectionStatus (read-only) |

Both switches respond to the standard MIB (not Netgear proprietary OIDs).
Write community `private` confirmed working on GSM7252PS-S2.

## HA Integration Approach: MQTT Switch Entities

HA supports MQTT switch entities via auto-discovery. The SNMP collector would
publish both a **sensor** (current power reading) and a **switch** (PoE toggle)
for each port.

### MQTT Discovery for Switch Entities

```json
{
  "name": "(Port 01 PoE) rpi5-pmod",
  "unique_id": "sw_netgear_gsm7252ps_s2_port01_poe_enable",
  "command_topic": "sensors2mqtt/sw_netgear_gsm7252ps_s2/port01/poe/set",
  "state_topic": "sensors2mqtt/sw_netgear_gsm7252ps_s2/port01/poe/state",
  "payload_on": "ON",
  "payload_off": "OFF",
  "device": { ... same device as sensor entities ... },
  "icon": "mdi:ethernet",
  "availability_topic": "sensors2mqtt/sw_netgear_gsm7252ps_s2/status"
}
```

### Command Flow

1. User toggles switch in HA → MQTT publishes `OFF` to `.../port01/poe/set`
2. SNMP collector subscribes to `sensors2mqtt/+/+/poe/set` command topics
3. On receiving command: `snmpset -v2c -c private HOST OID i 2` (disable) or `i 1` (enable)
4. After SET: poll `pethPsePortDetectionStatus` to confirm state change
5. Publish confirmed state to `.../port01/poe/state`
6. On next poll cycle: power reading drops to 0 (confirming PoE is off)

### Power Cycle Sequence

For a "reboot via PoE" button (future enhancement):
1. SNMP SET disable → wait 5 seconds → SNMP SET enable
2. This is a separate MQTT button entity, not just a toggle

## Implementation Steps

### Step 1: Add write_community to config

```toml
[switches.sw-netgear-gsm7252ps-s2]
model = "gsm7252ps"
host = "sw-netgear-gsm7252ps-s2.example.com"
community = "public"
write_community = "private"
```

Switches without `write_community` only publish sensors (no toggle).

### Step 2: Add MQTT subscribe + SNMP SET handler

In the SNMP collector's main loop:
- Subscribe to `sensors2mqtt/+/+/poe/set` topics
- On message: parse switch name + port from topic, find matching SwitchConfig
- Execute `snmpset -v2c -c WRITE_COMMUNITY HOST pethPsePortAdminEnable.1.{port} i {1|2}`
- Verify with snmpget, publish state

### Step 3: Publish HA switch discovery

During discovery phase, for each PoE port on switches with write_community:
- Publish `homeassistant/switch/{node_id}/port{NN}_poe/config`
- Include command_topic and state_topic

### Step 4: Poll PoE admin state

Add `pethPsePortAdminEnable` to the regular poll cycle (alongside power readings).
Publish current enable/disable state to each port's state topic.

## Safety Considerations

1. **Trunk/uplink ports**: Ports 48-50 on GSM7252PS are trunk ports. Disabling PoE
   on these won't cause issues (they're SFP+ uplinks, not PoE), but they shouldn't
   show toggle switches in HA either. Filter to only PoE-capable ports (1-47).

2. **The switch's own management**: The switch itself isn't PoE-powered (it has AC
   power), so there's no risk of disabling the management interface.

3. **tweed ports on S3300**: Ports 41-42 are tweed's eth-local and BMC. The
   existing "CRITICAL: Tweed Reboot Safety" rule should be enforced — perhaps
   mark these ports as protected in config (no toggle entity published).

4. **Rate limiting**: Don't allow rapid toggling. The SNMP SET should have a
   per-port cooldown (e.g. 5 seconds between state changes).

5. **Confirmation in HA**: State only updates after SNMP GET confirms the change,
   not optimistically. This prevents the UI showing "off" when the SET failed.

## Config Addition for Protected Ports

```toml
[switches.sw-netgear-s3300-1]
model = "s3300"
host = "sw-netgear-s3300-1.example.com"
community = "public"
write_community = "pib"
protected_ports = [41, 42]  # tweed — never toggle PoE
```

## Dependencies

- paho-mqtt subscription handling (already in the library, just not used yet)
- `snmpset` binary (part of snmp-utils, same package as snmpget/snmpwalk)
- Write community strings added to snmp.toml

## Files to Create/Modify

| File | Change |
|------|--------|
| `snmp.toml` | Add `write_community` and `protected_ports` fields |
| `src/sensors2mqtt/collector/snmp.py` | Add `SwitchConfig.write_community`, MQTT subscribe handler, switch discovery, PoE state polling |
| `tests/test_snmp.py` | Tests for SET handler, protected ports, discovery |

## Estimated Scope

~150-200 lines of new code + tests. The main complexity is the MQTT subscribe
handler running alongside the existing poll loop (both need the MQTT client).
