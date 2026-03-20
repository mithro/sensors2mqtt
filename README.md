# sensors2mqtt

Publish hardware sensor data to Home Assistant via MQTT auto-discovery.

## Overview

sensors2mqtt is a collection of sensor collectors that poll hardware monitoring
data and publish it to Home Assistant via MQTT. Each collector runs as a
standalone systemd service on the target host.

### Collectors

| Collector | Host | Data Source | Sensors |
|-----------|------|-------------|---------|
| **snmp** | ten64 (router) | SNMP polls to managed switches | M4300 fans/thermal/PSU, GSM7252PS PoE power |
| **hwmon** | sw-bb-25g (Mellanox SN2410) | `sensors -j` (local hwmon) | ASIC temp, CPU temp, board temp, 8 fans |
| **ipmi_sdr** | big-storage (Supermicro X11DSC+) | `ipmitool sdr` + BMC web API | CPU/board/VRM/DIMM temps, fans, per-PSU PMBus |

## Install

```bash
# Development
make setup        # creates .venv, installs deps
make test         # run tests
make lint         # run ruff

# Production (on target host)
sudo make install INSTALL_DIR=/opt/sensors2mqtt
```

## Usage

Each collector is runnable as a Python module:

```bash
# SNMP collector (on ten64)
uv run python -m sensors2mqtt.collector.snmp

# Hwmon collector (on sw-bb-25g)
uv run python -m sensors2mqtt.collector.hwmon

# IPMI SDR collector (on big-storage)
uv run python -m sensors2mqtt.collector.ipmi_sdr
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_HOST` | `ha.welland.mithis.com` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | `DVES_USER` | MQTT username |
| `MQTT_PASSWORD` | `DVES_USER` | MQTT password |
| `POLL_INTERVAL` | `30` | Seconds between polls |

The IPMI SDR collector also reads `BMC_HOST`, `BMC_USER`, `BMC_PASS`.

## Architecture

All collectors inherit from `BasePublisher` which provides:
- MQTT connection management (paho-mqtt v2 API)
- Home Assistant auto-discovery message publishing
- Interruptible poll loop with configurable interval
- Clean signal handling (SIGTERM/SIGINT)
- Per-device availability topics (`online`/`offline`)

Each collector implements a `poll()` method that returns sensor readings as a
dict. The base class handles discovery, state publishing, and availability.

## MQTT Topics

```
sensors2mqtt/{node_id}/state    # JSON sensor values (retained)
sensors2mqtt/{node_id}/status   # "online" or "offline" (retained)
homeassistant/sensor/{node_id}/{suffix}/config  # HA discovery (retained)
```

## License

Apache-2.0
