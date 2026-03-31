# sensors2mqtt

Publish hardware sensor data to Home Assistant via MQTT auto-discovery.

## Overview

sensors2mqtt is a collection of sensor collectors that poll hardware monitoring
data and publish it to Home Assistant via MQTT. Each collector runs as a
standalone systemd service on the target host.

### Collectors

| Collector | Data Source | Example Sensors |
|-----------|-------------|---------|
| **snmp** | SNMP polls to managed switches | Netgear M4300 fans/thermal/PSU, GSM7252PS PoE power |
| **local** | sysfs/hwmon on local host | RPi CPU temp/throttle, Mellanox SN2410 ASIC/fans |
| **ipmi_sensors** | IPMI SDR + BMC web API | CPU/board/VRM/DIMM temps, fans, voltages, per-PSU PMBus |

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
# SNMP collector (polls remote switches)
uv run python -m sensors2mqtt.collector.snmp

# Local collector (auto-detects RPi/Mellanox)
uv run python -m sensors2mqtt.collector.local

# IPMI sensor collector (remote BMC)
uv run python -m sensors2mqtt.collector.ipmi_sensors
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_HOST` | `localhost` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | *(empty)* | MQTT username |
| `MQTT_PASSWORD` | *(empty)* | MQTT password |
| `POLL_INTERVAL` | `30` | Seconds between polls |
| `BMC_HOST` | *(required)* | BMC hostname (IPMI collector only) |
| `BMC_USER` | *(required)* | BMC username (IPMI collector only) |
| `BMC_PASS` | *(required)* | BMC password (IPMI collector only) |

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
