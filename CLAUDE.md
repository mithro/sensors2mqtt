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
└── IpmiSensorCollector (collector/ipmi_sensors.py) — ipmitool + BMC web API
```

`python -m sensors2mqtt.collector.local` auto-detects hardware and runs the right collector.

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
| GSM7252PS | 4526.10 | FASTPATH PoE (.15.1.1.1.2 per-port mW) |
| S3300-52X-PoE+ | 4526.11 | boxServices + PoE (same MIB structure, different prefix) |

The Netgear enterprise OID split: `4526.10` = Fully Managed (M4300, GSM7252PS),
`4526.11` = Smart Managed Pro (S3300). Same MIB structure within each subtree.

Switch connection details are configured in `snmp.toml` (see `snmp.toml.example`).

## Development

```bash
make setup    # uv sync --dev
make test     # pytest
make lint     # ruff check
```

## Running Collectors

```bash
uv run python -m sensors2mqtt.collector.snmp
uv run python -m sensors2mqtt.collector.local          # auto-detects RPi/Mellanox
uv run python -m sensors2mqtt.collector.local --hardware rpi   # force RPi mode
uv run python -m sensors2mqtt.collector.ipmi_sensors
```

## Key Design Decisions

- Switch sensor definitions are Python constants, not config files
- SNMP uses subprocess `snmpget`/`snmpwalk` (not pysnmp) for simplicity
- Each collector is a `__main__.py`-style module runnable with `python -m`
- paho-mqtt v2 API (CallbackAPIVersion.VERSION2)
- Environment variables for MQTT connection (no config files)
