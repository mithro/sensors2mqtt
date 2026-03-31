# Collectors

All collectors inherit from `BasePublisher` which provides MQTT connection
management, Home Assistant auto-discovery, and an interruptible poll loop.

## Local collector

Auto-detects hardware and runs the appropriate sub-collector.

```bash
uv run python -m sensors2mqtt.collector.local
uv run python -m sensors2mqtt.collector.local --hardware rpi      # force RPi mode
uv run python -m sensors2mqtt.collector.local --hardware mellanox  # force Mellanox mode
```

### Raspberry Pi

Reads CPU temperature, throttle flags, and under-voltage status from
`/sys/class/thermal` and the VideoCore mailbox interface.

### Mellanox SN2410

Reads ASIC temperature, CPU temperature, board temperature, and fan speeds
from the hwmon sysfs interface.

## SNMP collector

Polls Netgear managed switches via SNMP for hardware sensors (fans, thermal,
PSU) and per-port PoE power consumption.

```bash
uv run python -m sensors2mqtt.collector.snmp
```

Requires a configuration file at `snmp.toml` or `/etc/sensors2mqtt/snmp.toml`.
See `snmp.toml.example` for the format.

### Supported switch models

| Model | Sensors |
|-------|---------|
| M4300-24X | Fans, temperature, PSU status |
| GSM7252PS | Per-port PoE power (mW) |
| S3300-52X-PoE+ | Fans, temperature, PSU, per-port PoE power |

## IPMI sensor collector

Reads sensor data from a remote BMC via `ipmitool` and per-PSU PMBus data
via the BMC web API.

```bash
export BMC_HOST=your-bmc-host
export BMC_USER=admin
export BMC_PASS=password
uv run python -m sensors2mqtt.collector.ipmi_sensors
```

Sensors include CPU temperatures, board temperatures, VRM temperatures,
DIMM temperatures, fan speeds, voltages, and per-PSU input/output power.
