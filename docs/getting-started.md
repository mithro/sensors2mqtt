# Getting Started

## Installation

### Debian packages (recommended for production)

Each collector ships as its own Debian binary package. Install only what you need;
multiple packages can co-exist on the same host.

| Package | Installs service as | Notes |
|---------|---------------------|-------|
| `python3-sensors2mqtt` | *(library)* | Dependency of all service packages; seeds `/etc/sensors2mqtt/env` |
| `sensors2mqtt-local` | enabled + **started** | Auto-detects hardware; no extra config needed |
| `sensors2mqtt-snmp` | enabled, **not started** | Requires `/etc/sensors2mqtt/snmp.toml` |
| `sensors2mqtt-snmp-control` | enabled, **not started** | Requires `/etc/sensors2mqtt/snmp.toml` |
| `sensors2mqtt-ipmi-sensors` | enabled, **not started** | Requires `BMC_*` vars in `/etc/sensors2mqtt/env` |

**Local collector** (auto-detects Raspberry Pi / Mellanox):
```bash
sudo apt install sensors2mqtt-local
sudo editor /etc/sensors2mqtt/env    # set MQTT credentials
# Service is already running; it also starts automatically on every boot.
```

**SNMP / SNMP-control collector** (Netgear managed switches):
```bash
sudo apt install sensors2mqtt-snmp   # add sensors2mqtt-snmp-control if needed
sudo editor /etc/sensors2mqtt/env    # set MQTT credentials
sudo cp /usr/share/sensors2mqtt/snmp.toml.example /etc/sensors2mqtt/snmp.toml
sudo chmod 0600 /etc/sensors2mqtt/snmp.toml
sudo editor /etc/sensors2mqtt/snmp.toml
sudo systemctl start sensors2mqtt-snmp
```

**IPMI sensor collector** (remote BMC):
```bash
sudo apt install sensors2mqtt-ipmi-sensors
# Add BMC_HOST, BMC_USER, BMC_PASS and MQTT credentials:
sudo editor /etc/sensors2mqtt/env
sudo systemctl start sensors2mqtt-ipmi-sensors
```

### From PyPI

```bash
pip install sensors2mqtt
```

### From source

```bash
git clone https://github.com/mithro/sensors2mqtt
cd sensors2mqtt
make setup        # creates .venv, installs deps
```

## Quick start (development / manual)

1. Set the MQTT broker environment variables:

   ```bash
   export MQTT_HOST=your-mqtt-broker.local
   export MQTT_USER=your-user
   export MQTT_PASSWORD=your-password
   ```

2. Run a collector:

   ```bash
   # Local collector (auto-detects RPi/Mellanox)
   uv run python -m sensors2mqtt.collector.local

   # SNMP collector (polls remote switches)
   uv run python -m sensors2mqtt.collector.snmp

   # IPMI collector (remote BMC)
   export BMC_HOST=your-bmc-host
   export BMC_USER=admin
   export BMC_PASS=password
   uv run python -m sensors2mqtt.collector.ipmi_sensors
   ```

3. Sensors appear automatically in Home Assistant via MQTT discovery.

## A note on etckeeper

If a host runs etckeeper, files under `/etc/sensors2mqtt/` (including `env` and
`snmp.toml`) are committed into `/etc/.git`, so their credentials enter that
git history. If that is undesirable on a given host, exclude the directory from
etckeeper (e.g. add `/etc/sensors2mqtt` to `/etc/.gitignore`).
