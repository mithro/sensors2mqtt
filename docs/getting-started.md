# Getting Started

## Installation

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

## Quick start

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
