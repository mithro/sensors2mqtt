# SNMP cross-check against gdoc2netcfg (2026-06-12)

On 2026-06-11/12 the SNMP collection in this repo was cross-checked against the
`bridge` supplement in [gdoc2netcfg](https://github.com/mithro/gdoc2netcfg)
(`src/gdoc2netcfg/supplements/bridge.py`). The check fixed bugs and added data
capture on the gdoc2netcfg side, and surfaced gaps on this side. This file
records both, with the live evidence.

## What this repo's code confirmed (fixes landed in gdoc2netcfg)

- **RFC 3621 PoE column bug.** gdoc2netcfg read `pethPsePortAdminEnable` from
  `pethPsePortTable` column 1 — a not-accessible index column that never
  appears in walks — so every PoE row was silently dropped and its PoE status
  table stayed empty forever. This repo's collector uses the correct columns
  (`snmp.py`: `POE_ADMIN = 1.3.6.1.2.1.105.1.1.1.3.1`,
  `POE_DETECT = 1.3.6.1.2.1.105.1.1.1.6.1`), which independently confirmed the
  fix: AdminEnable is column **3**, DetectionStatus is column **6**.
- **LLDP remote port description.** This repo parses `lldpRemPortDesc`
  (column 8 of `lldpRemTable`); gdoc2netcfg was discarding it. It now captures
  it — the description is usually far more readable than the port-ID field,
  which is often a bare MAC address.
- **ifAlias convention.** This repo's `extract_hostname` relies on the
  `interface.hostname` convention in `ifAlias`
  (`1.3.6.1.2.1.31.1.1.1.18`). gdoc2netcfg now walks `ifAlias` too and uses the
  same convention, so both tools see the same port→host mapping.

gdoc2netcfg additionally adopted this repo's Netgear vendor OIDs (per-port PoE
power draw `4526.{10,11}.15.1.1.1.2` and the boxServices fan/PSU/temperature
tables under `4526.{10,11}.43.1`) and stores all of it historically in its
`discovery.db` (tables `bridge_poe_power`, `bridge_box_sensors`, plus
`bridge_mac` from `dot1dBaseBridgeAddress` and LLDP `remote_port_desc`).

## Gaps found in this repo's collection

Verified against live walks of the production switches (welland,
2026-06-11):

### 1. GSM7252PS box sensors are not collected at all

The `gsm7252ps` entry in `MODELS` (`snmp.py`) has `walk_sensors` (PoE) but no
`sensors=` — no fans, no temperature, no PSU power. The hardware does expose
boxServices data under the fastpath enterprise (`4526.10.43.1`). Live values:

| Switch | Sensor | Instance | Value |
|---|---|---|---|
| gsm7252ps-s1 | fan | `0` | 3500 RPM |
| gsm7252ps-s1 | fan | `2` | 3450 RPM |
| gsm7252ps-s1 | psu_power | `1.0`–`1.3` | 53 / 34 / 36 / 35 W |
| gsm7252ps-s2 | fan | `0` | 4200 RPM |
| gsm7252ps-s2 | fan | `2` | 4150 RPM |
| gsm7252ps-s2 | psu_power | `1.0`–`1.3` | 55 / 35 / 38 / 36 W |

Temperature (`.15.1.3.*`) returns nothing readable on the GSM7252PS.

### 2. Hardcoded sensor instances don't fit the GSM7252PS indexing

`_box_sensors()` builds fan OIDs as `{base}.6.1.4.1.{i}` (unit.fan indexing,
as on the M4300) — but the GSM7252PS indexes fans with a **single** component:
`.6.1.4.0` and `.6.1.4.2`. Fan instance `.1` exists but returns the literal
string `"Not Supported"`. So simply adding `sensors=_box_sensors(...)` to the
`gsm7252ps` model would silently read nothing: `snmpget_value()` returns
`None` for non-numeric strings and the sensors would publish no data, with no
error.

**Recommendation:** walk the boxServices subtrees (`.6.1.4` fans, `.8.1.5`
PSU, `.15.1.3` temperature) and skip exactly the literal `"Not Supported"`
marker, instead of hardcoding per-model instance OIDs and fan counts. That is
what gdoc2netcfg's `parse_box_sensors()` now does; any other non-integer value
still raises so real parse problems stay visible.

### 3. Only the first PSU power rail is read

`_box_sensors()` reads a single PSU OID, `{base}.8.1.5.1.0`. The GSM7252PS
exposes **four** rails (`1.0`–`1.3`, see table above; the M4300 has only the
one, so nothing is lost there). The extra rails are individually meaningful —
on both s1 and s2 the `.0` rail (~53–55 W) is roughly the others combined,
which looks like total-plus-per-supply reporting.

## Where the recovered data lives

gdoc2netcfg's `discovery.db` (schema v7) now stores, per bridge scan, with
delta-based history: PoE admin/detection status (RFC 3621), per-port PoE power
draw in mW, fan/PSU/temperature box sensors, the switch's base bridge MAC,
port aliases (ifAlias), and LLDP neighbours including `remote_port_desc`.
Inspect with `gdoc2netcfg bridge show` or `gdoc2netcfg db history --type
bridge` from `/opt/gdoc2netcfg`.
