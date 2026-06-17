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

### Debian packages (recommended for production)

| Package | Service | Notes |
|---------|---------|-------|
| `python3-sensors2mqtt` | *(library)* | Required by all service packages; seeds `/etc/sensors2mqtt/env` |
| `sensors2mqtt-local` | `sensors2mqtt-local` | Auto-detects hardware; installs **enabled and started** — no config needed |
| `sensors2mqtt-snmp` | `sensors2mqtt-snmp` | Installs **enabled, not started** — requires `/etc/sensors2mqtt/snmp.toml` |
| `sensors2mqtt-snmp-control` | `sensors2mqtt-snmp-control` | Installs **enabled, not started** — requires `/etc/sensors2mqtt/snmp.toml` |
| `sensors2mqtt-ipmi-sensors` | `sensors2mqtt-ipmi-sensors` | Installs **enabled, not started** — requires `BMC_*` vars in `/etc/sensors2mqtt/env` |

Packages are co-installable; a single host can run multiple collectors simultaneously.

**SNMP / SNMP-control bring-up:**
```bash
sudo apt install sensors2mqtt-snmp        # or sensors2mqtt-snmp-control
# Edit MQTT credentials (seeded from env.example by the python3-sensors2mqtt package):
sudo editor /etc/sensors2mqtt/env
# Create switch config from the provided example:
sudo cp /usr/share/sensors2mqtt/snmp.toml.example /etc/sensors2mqtt/snmp.toml
sudo chmod 0600 /etc/sensors2mqtt/snmp.toml   # holds SNMP community strings
sudo editor /etc/sensors2mqtt/snmp.toml
sudo systemctl start sensors2mqtt-snmp    # or sensors2mqtt-snmp-control
```

> The snmp collectors refuse to start if `/etc/sensors2mqtt/snmp.toml` is
> group- or world-readable (it contains SNMP community strings). The seeded
> `/etc/sensors2mqtt/env` is created `0600` automatically.

**IPMI bring-up:**
```bash
sudo apt install sensors2mqtt-ipmi-sensors
# Edit /etc/sensors2mqtt/env — add BMC_HOST, BMC_USER, BMC_PASS and MQTT creds:
sudo editor /etc/sensors2mqtt/env
sudo systemctl start sensors2mqtt-ipmi-sensors
```

### Development

```bash
make setup        # creates .venv, installs deps
make test         # run tests
make lint         # run ruff
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
