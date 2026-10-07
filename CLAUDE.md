# CLAUDE.md

## Project Overview

sensors2mqtt publishes hardware sensor data to Home Assistant via MQTT
auto-discovery. Each collector runs as a systemd service on a target host.

## Architecture

```
BasePublisher (base.py)          — MQTT connection, poll loop, signals, discovery
├── SnmpCollector (collector/snmp.py)      — multi-switch SNMP polling
├── LocalCollector (collector/local/base.py) — shared sysfs/proc/hwmon infrastructure
│   ├── RpiCollector (collector/local/rpi.py)       — RPi sensors (all models)
│   └── MellanoxCollector (collector/local/mellanox.py) — Mellanox SN2410 switch sensors
├── IpmiSensorCollector (collector/ipmi_sensors.py) — ipmitool + BMC web API
└── StorageLvmCollector (collector/storage_lvm.py) — filesystems, md, LVM (host device)

StorageCollector (collector/storage/collector.py) — one HA device per drive + enclosure
```

### Storage collectors

`storage` publishes one HA device per **drive, keyed by serial** (`disk_<serial>`,
`via_device` = the host that has it now), so a drive that moves keeps its entities
and history. **smartd is the only process allowed to send SMART/log commands to
drives**: SMART data comes only from smartd's `--jsonstate` files
(`/var/lib/smartmontools/smartd-json.*.json`, mithro/smartmontools build). Never
add `smartctl`/`hdparm`/`nvme`/`drivetemp` reads to a collector: a SAMSUNG HD204UI
silently drops a pending write when IDENTIFY arrives (the 2026-03 md125 zero holes).
`storage_lvm` reads no disk either (LVM metadata backups + `dmsetup status`).
Design: `docs/superpowers/specs/2026-09-30-storage-collectors-design.md`.

`python -m sensors2mqtt.collector.local` auto-detects hardware and runs the right collector.

### Control services (command receivers, not BasePublisher subclasses)

Standalone daemons that *subscribe* to MQTT command topics and act, each the
control counterpart to a collector:

```
PoeController   (collector/snmp_control.py)   — toggle/cycle PoE ports via SNMP SET (counterpart to snmp)
PowerController (collector/local_control.py)  — graceful host shutdown/reboot via /sbin/shutdown (counterpart to local)
```

`local_control` runs **as root** on the host it controls, exposes HA Shutdown/Reboot
buttons under that host's device, subscribes `sensors2mqtt/{node_id}/power/{shutdown,reboot}/set`
(acts only on payload `PRESS`), and publishes a `power/state` ack. It only *triggers*
a clean halt — a consumer that cuts mains power must confirm "off" independently.

## MQTT Topic Convention

All publishers use the same topic structure:

```
sensors2mqtt/{node_id}/state    — JSON dict of sensor values (retained)
sensors2mqtt/{node_id}/status   — "online" or "offline" (retained)
homeassistant/sensor/{node_id}/{suffix}/config — HA auto-discovery (retained)
```

`node_id` is a Python-safe identifier like `m4300_24x`, `sw_bb_25g`, `big_storage`.

## Supported Switch Models (SNMP)

| Model | OID prefix | MIB |
|-------|------------|-----|
| M4300-24X | 4526.10 | boxServices (.43.1.6 fans, .43.1.15 thermal, .43.1.8 PSU) |
| GSM7252PS | 4526.10 | boxServices (fans/PSU, walk-discovered) + FASTPATH PoE (.15.1.1.1.2 per-port mW) |
| S3300-52X-PoE+ | 4526.11 | boxServices + PoE (same MIB structure, different prefix) |

The Netgear enterprise OID split: `4526.10` = Fully Managed (M4300, GSM7252PS),
`4526.11` = Smart Managed Pro (S3300). Same MIB structure within each subtree.

Switch connection details are configured in `snmp.toml` (see `snmp.toml.example`).

## Development

```bash
make setup    # uv sync --dev --all-extras
make test     # pytest
make lint     # ruff check
```

## Running Collectors

```bash
uv run python -m sensors2mqtt.collector.snmp
uv run python -m sensors2mqtt.collector.local          # auto-detects RPi/Mellanox
uv run python -m sensors2mqtt.collector.local --hardware rpi   # force RPi mode
uv run python -m sensors2mqtt.collector.ipmi_sensors
uv run python -m sensors2mqtt.collector.storage --once        # drives + enclosures
sudo uv run python -m sensors2mqtt.collector.storage_lvm --once   # prints values
```

## Key Design Decisions

- Switch sensor definitions are Python constants, not config files
- boxServices sensors (fans/temp/PSU) are discovered by walking the value
  columns, not hardcoded per instance — indexing varies by model (M4300
  uses unit.fan like "1.0"; GSM7252PS uses bare "0"/"2" with a literal
  "Not Supported" placeholder, and has 4 PSU rails)
- SNMP runs in-process via the `ezsnmp` net-snmp binding (v2c) behind a
  `SnmpClient` seam (`snmp_client.py`) — not subprocess CLI tools
  (`snmpget`/`snmpwalk`/`snmpset`) or pysnmp; deployed as Debian `python3-ezsnmp`
- Power readings are published raw for HA to integrate (`force_update`, no kWh
  accumulators in s2m). Units are declared per model, never converted:
  `pethMainPseConsumptionPower` is mW on the GSM7252PS and S3300 but W on the
  M4300-16X, despite the MIB saying watts. No model has a PoE energy counter
  (#43).
- Each collector is a `__main__.py`-style module runnable with `python -m`
- paho-mqtt v2 API (CallbackAPIVersion.VERSION2)
- Environment variables for MQTT connection (no config files)
