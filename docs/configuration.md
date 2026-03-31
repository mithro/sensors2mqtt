# Configuration

## Environment variables

All collectors read MQTT connection settings from environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_HOST` | `localhost` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | *(empty)* | MQTT username |
| `MQTT_PASSWORD` | *(empty)* | MQTT password |
| `POLL_INTERVAL` | `30` | Seconds between polls |

### IPMI collector

| Variable | Default | Description |
|----------|---------|-------------|
| `BMC_HOST` | *(required)* | BMC hostname or IP |
| `BMC_USER` | *(required)* | BMC username |
| `BMC_PASS` | *(required)* | BMC password |

## SNMP configuration file

The SNMP collector reads switch definitions from a TOML config file.
It searches for `snmp.toml` in the current directory and
`/etc/sensors2mqtt/snmp.toml`.

See `snmp.toml.example` for the format:

```toml
[switches.my-switch]
model = "m4300"
host = "my-switch.example.com"
community = "public"
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `model` | Yes | Switch model: `m4300`, `gsm7252ps`, or `s3300` |
| `host` | No | Hostname (defaults to the switch name) |
| `community` | Yes | SNMP v2c read community string |
| `write_community` | No | SNMP v2c write community (for PoE control) |

## Systemd service files

Example service files are provided in the `deploy/` directory for running
collectors as systemd services. Copy them to `/etc/systemd/system/` and
create an environment file at `/etc/sensors2mqtt/env` with your settings:

```ini
MQTT_HOST=your-mqtt-broker.local
MQTT_USER=your-user
MQTT_PASSWORD=your-password
```

## MQTT topics

All collectors publish to the same topic structure:

```
sensors2mqtt/{node_id}/state    # JSON sensor values (retained)
sensors2mqtt/{node_id}/status   # "online" or "offline" (retained)
homeassistant/sensor/{node_id}/{suffix}/config  # HA discovery (retained)
```
