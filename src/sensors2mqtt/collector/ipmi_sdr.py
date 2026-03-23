"""IPMI sensor collector: publish big-storage sensor data to MQTT.

Runs locally on big-storage. Combines two data sources:
1. IPMI Sensor Data Record (ipmitool sdr) — board temps, fans, voltages, power
2. BMC web API — per-PSU PMBus data (AC/DC voltage, current, power, temps, fans)

Usage:
    python -m sensors2mqtt.collector.ipmi_sdr
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

import paho.mqtt.client as mqtt
import requests
import urllib3

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.discovery import ORIGIN, DeviceInfo, SensorDef, publish_discovery, publish_state

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NODE_ID = "big_storage"
DEVICE = DeviceInfo(
    node_id=NODE_ID,
    name="big-storage",
    manufacturer="Supermicro",
    model="X11DSC+",
    configuration_url=None,  # set dynamically from BMC_HOST
)

# IPMI sensor name -> (suffix, friendly_name, device_class, unit, icon)
# These map `ipmitool sdr list full` output names to HA discovery suffixes.
IPMI_SENSOR_MAP: dict[str, tuple[str, str, str | None, str, str | None]] = {
    "CPU1 Temp": ("cpu1_temp", "CPU1 Temperature", "temperature", "°C", None),
    "CPU2 Temp": ("cpu2_temp", "CPU2 Temperature", "temperature", "°C", None),
    "PCH Temp": ("pch_temp", "PCH Temperature", "temperature", "°C", None),
    "Inlet Temp": ("inlet_temp", "Inlet Temperature", "temperature", "°C", None),
    "System Temp": ("system_temp", "System Temperature", "temperature", "°C", None),
    "Peripheral Temp": ("peripheral_temp", "Peripheral Temperature", "temperature", "°C", None),
    "VRMCpu1IN Temp": ("vrm_cpu1_in_temp", "VRM CPU1 Input Temperature", "temperature", "°C", None),
    "VRMCpu1IO Temp": ("vrm_cpu1_io_temp", "VRM CPU1 I/O Temperature", "temperature", "°C", None),
    "VRMCpu2IN Temp": ("vrm_cpu2_in_temp", "VRM CPU2 Input Temperature", "temperature", "°C", None),
    "VRMCpu2IO Temp": ("vrm_cpu2_io_temp", "VRM CPU2 I/O Temperature", "temperature", "°C", None),
    "VRMP1ABC Temp": ("vrmp_cpu1_abc_temp", "VRMP CPU1 ABC Temperature", "temperature", "°C", None),
    "VRMP1DEF Temp": ("vrmp_cpu1_def_temp", "VRMP CPU1 DEF Temperature", "temperature", "°C", None),
    "VRMP2ABC Temp": ("vrmp_cpu2_abc_temp", "VRMP CPU2 ABC Temperature", "temperature", "°C", None),
    "VRMP2DEF Temp": ("vrmp_cpu2_def_temp", "VRMP CPU2 DEF Temperature", "temperature", "°C", None),
    "P1-DIMMA1 Temp": ("dimm_p1a1_temp", "DIMM P1-A1 Temperature", "temperature", "°C", None),
    "P1-DIMMB1 Temp": ("dimm_p1b1_temp", "DIMM P1-B1 Temperature", "temperature", "°C", None),
    "P1-DIMMD1 Temp": ("dimm_p1d1_temp", "DIMM P1-D1 Temperature", "temperature", "°C", None),
    "P1-DIMME1 Temp": ("dimm_p1e1_temp", "DIMM P1-E1 Temperature", "temperature", "°C", None),
    "P2-DIMMA1 Temp": ("dimm_p2a1_temp", "DIMM P2-A1 Temperature", "temperature", "°C", None),
    "P2-DIMMB1 Temp": ("dimm_p2b1_temp", "DIMM P2-B1 Temperature", "temperature", "°C", None),
    "P2-DIMMD1 Temp": ("dimm_p2d1_temp", "DIMM P2-D1 Temperature", "temperature", "°C", None),
    "P2-DIMME1 Temp": ("dimm_p2e1_temp", "DIMM P2-E1 Temperature", "temperature", "°C", None),
    "HDD Temp": ("hdd_temp", "HDD Temperature", "temperature", "°C", None),
    "BPN-1 Temp": ("bpn1_temp", "Backplane 1 Temperature", "temperature", "°C", None),
    "BPN-2 Temp": ("bpn2_temp", "Backplane 2 Temperature", "temperature", "°C", None),
    "Expander1 Temp": ("exp1_temp", "Expander 1 Temperature", "temperature", "°C", None),
    "Expander2 Temp": ("exp2_temp", "Expander 2 Temperature", "temperature", "°C", None),
    "AOC_NIC1_Temp": ("aoc_nic1_temp", "AOC NIC1 Temperature", "temperature", "°C", None),
    "AOC_SAS Temp": ("aoc_sas_temp", "AOC SAS Temperature", "temperature", "°C", None),
    "FAN1": ("fan1_rpm", "Fan 1", None, "RPM", "mdi:fan"),
    "FAN2": ("fan2_rpm", "Fan 2", None, "RPM", "mdi:fan"),
    "FAN3": ("fan3_rpm", "Fan 3", None, "RPM", "mdi:fan"),
    "FAN5": ("fan5_rpm", "Fan 5", None, "RPM", "mdi:fan"),
    "PW Consumption": ("power_consumption", "Board Power Consumption", "power", "W", None),
    # Voltage rails
    "12V": ("rail_12v", "12V Rail", "voltage", "V", None),
    "5VCC": ("rail_5v", "5V Rail", "voltage", "V", None),
    "3.3VCC": ("rail_3v3", "3.3V Rail", "voltage", "V", None),
    "Vcpu1": ("vcpu1", "CPU1 Voltage", "voltage", "V", None),
    "Vcpu2": ("vcpu2", "CPU2 Voltage", "voltage", "V", None),
    "VDimmP1ABC": ("vdimm_p1abc", "DIMM P1 ABC Voltage", "voltage", "V", None),
    "VDimmP1DEF": ("vdimm_p1def", "DIMM P1 DEF Voltage", "voltage", "V", None),
    "VDimmP2ABC": ("vdimm_p2abc", "DIMM P2 ABC Voltage", "voltage", "V", None),
    "VDimmP2DEF": ("vdimm_p2def", "DIMM P2 DEF Voltage", "voltage", "V", None),
    "5VSB": ("rail_5vsb", "5V Standby", "voltage", "V", None),
    "3.3VSB": ("rail_3v3sb", "3.3V Standby", "voltage", "V", None),
    "1.8V PCH": ("rail_1v8_pch", "1.8V PCH", "voltage", "V", None),
    "PVNN PCH": ("rail_pvnn_pch", "PVNN PCH Voltage", "voltage", "V", None),
    "1.05V PCH": ("rail_1v05_pch", "1.05V PCH", "voltage", "V", None),
}

# Per-PSU sensors (published on separate state topics)
PSU_SENSORS: list[tuple[str, str, str | None, str, str, str | None, str]] = [
    (
        "ac_input_voltage",
        "AC Input Voltage",
        "voltage",
        "V",
        "measurement",
        None,
        "ac_input_voltage_v",
    ),
    (
        "ac_input_current",
        "AC Input Current",
        "current",
        "A",
        "measurement",
        None,
        "ac_input_current_a",
    ),
    ("ac_input_power", "AC Input Power", "power", "W", "measurement", None, "ac_input_power_w"),
    (
        "dc_12v_voltage",
        "DC 12V Output Voltage",
        "voltage",
        "V",
        "measurement",
        None,
        "dc_12v_output_voltage_v",
    ),
    (
        "dc_12v_current",
        "DC 12V Output Current",
        "current",
        "A",
        "measurement",
        None,
        "dc_12v_output_current_a",
    ),
    (
        "dc_12v_power",
        "DC 12V Output Power",
        "power",
        "W",
        "measurement",
        None,
        "dc_12v_output_power_w",
    ),
    ("temp_1", "Temperature 1", "temperature", "°C", "measurement", None, "temperature_1_c"),
    ("temp_2", "Temperature 2", "temperature", "°C", "measurement", None, "temperature_2_c"),
    ("fan_1", "Fan 1", None, "RPM", "measurement", "mdi:fan", "fan_1_rpm"),
    ("fan_2", "Fan 2", None, "RPM", "measurement", "mdi:fan", "fan_2_rpm"),
]


# ---------------------------------------------------------------------------
# IPMI Sensor Data Record parsing
# ---------------------------------------------------------------------------


def parse_ipmi_sensors(output: str) -> dict:
    """Parse ipmitool sdr list full output into {suffix: value}."""
    values = {}
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        sensor_name = parts[0]
        value_str = parts[1]
        if sensor_name not in IPMI_SENSOR_MAP:
            continue
        if value_str == "no reading":
            continue
        suffix = IPMI_SENSOR_MAP[sensor_name][0]
        m = re.match(r"([\d.]+)", value_str)
        if m:
            values[suffix] = float(m.group(1))
    return values


# ---------------------------------------------------------------------------
# BMC PSU polling
# ---------------------------------------------------------------------------


def parse_bmc_psu_xml(xml_text: str) -> dict | None:
    """Parse BMC PSU XML response into a dict with PSU data."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        log.warning("Bad XML from BMC PSU endpoint")
        return None

    result = {"psus": []}
    ps_info = root.find("PSInfo")
    if ps_info is None:
        return result

    def pv(val_str):
        if not val_str:
            return None
        m = re.match(r"([\d.]+)", val_str)
        return float(m.group(1)) if m else None

    for i, item in enumerate(ps_info.findall("PSItem")):
        if item.get("IsPowerSupply") != "1":
            continue
        result["psus"].append(
            {
                "slot": i + 1,
                "status": "OK" if item.get("a_b_PS_Status_I2C") == "1" else "Error",
                "serial": item.get("PSname", ""),
                "ac_input_voltage_v": pv(item.get("acInVoltage")),
                "ac_input_current_a": pv(item.get("acInCurrent")),
                "ac_input_power_w": pv(item.get("acInPower")),
                "dc_12v_output_voltage_v": pv(item.get("dc12OutVoltage")),
                "dc_12v_output_current_a": pv(item.get("dc12OutCurrent")),
                "dc_12v_output_power_w": pv(item.get("dcOutPower")),
                "temperature_1_c": pv(item.get("temp1")),
                "temperature_2_c": pv(item.get("temp2")),
                "fan_1_rpm": pv(item.get("fan1")),
                "fan_2_rpm": pv(item.get("fan2")),
                "max_power_w": pv(item.get("maxPower")),
            }
        )

    return result


def poll_bmc_psu(bmc_host: str, bmc_user: str, bmc_pass: str) -> dict | None:
    """Poll the BMC web API for per-PSU PMBus data."""
    base = f"https://{bmc_host}"
    s = requests.Session()
    s.verify = False
    s.timeout = 15

    try:
        s.post(f"{base}/cgi/login.cgi", data={"name": bmc_user, "pwd": bmc_pass})
        r = s.get(f"{base}/cgi/url_redirect.cgi?url_name=servh_power")
        csrf_match = re.search(r'CSRF_TOKEN.*?"([0-9a-f]+)"', r.text)
        if not csrf_match:
            log.warning("Failed to get CSRF token from BMC (login failed?)")
            return None
        csrf = csrf_match.group(1)
        headers = {"Content-Type": "application/x-www-form-urlencoded", "CSRF_TOKEN": csrf}

        ts = int(time.time() * 1000)
        r = s.post(
            f"{base}/cgi/ipmi.cgi",
            data=f"?Get_PSInfoReadings.XML=(0,0)&time_stamp={ts}".encode(),
            headers=headers,
        )
        s.post(f"{base}/cgi/logout.cgi")
    except Exception as e:
        log.warning("BMC web API error: %s", e)
        return None

    if r.status_code != 200 or not r.text.strip():
        log.warning("BMC PSU response: status=%d, empty=%s", r.status_code, not r.text.strip())
        return None

    return parse_bmc_psu_xml(r.text)


# ---------------------------------------------------------------------------
# IPMI sensor polling
# ---------------------------------------------------------------------------


def poll_ipmi_sensors(bmc_host: str, bmc_user: str, bmc_pass: str) -> dict | None:
    """Query IPMI Sensor Data Records via network and parse output."""
    try:
        result = subprocess.run(
            [
                "ipmitool",
                "-I",
                "lanplus",
                "-H",
                bmc_host,
                "-U",
                bmc_user,
                "-P",
                bmc_pass,
                "sdr",
                "list",
                "full",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("ipmitool failed (rc=%d): %s", result.returncode, result.stderr.strip())
            return None
        return parse_ipmi_sensors(result.stdout)
    except subprocess.TimeoutExpired:
        log.warning("ipmitool timed out")
        return None


# ---------------------------------------------------------------------------
# MQTT discovery for IPMI sensors + PSU
# ---------------------------------------------------------------------------


def get_ipmi_sensors() -> list[SensorDef]:
    """Build SensorDef list from IPMI_SENSOR_MAP."""
    sensors = []
    for suffix, name, dev_class, unit, icon in IPMI_SENSOR_MAP.values():
        sensors.append(
            SensorDef(
                suffix=suffix,
                name=name,
                unit=unit,
                device_class=dev_class,
                state_class="measurement",
                icon=icon,
            )
        )
    return sensors


def publish_psu_discovery(
    client: mqtt.Client,
    device_dict: dict,
    psu_data: dict,
    avail_topic: str,
) -> int:
    """Publish HA discovery for per-PSU sensors. Returns count."""
    count = 0
    for psu in psu_data.get("psus", []):
        slot = psu["slot"]
        psu_state_topic = f"sensors2mqtt/{NODE_ID}/psu{slot}/state"

        for suffix, name, dev_class, unit, state_class, icon, value_key in PSU_SENSORS:
            object_id = f"{NODE_ID}_psu{slot}_{suffix}"
            config_topic = f"homeassistant/sensor/{NODE_ID}/psu{slot}_{suffix}/config"
            config = {
                "name": f"PSU{slot} {name}",
                "unique_id": object_id,
                "state_topic": psu_state_topic,
                "value_template": f"{{{{ value_json.{value_key} }}}}",
                "unit_of_measurement": unit,
                "state_class": state_class,
                "device": device_dict,
                "availability_topic": avail_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "origin": ORIGIN,
            }
            if dev_class:
                config["device_class"] = dev_class
            if icon:
                config["icon"] = icon
            client.publish(config_topic, json.dumps(config), retain=True)
            count += 1

        # PSU status + serial (non-measurement entities)
        for extra_suffix, extra_name, extra_key, extra_icon, extra_cat in [
            ("status", f"PSU{slot} Status", "status", "mdi:power-plug", None),
            ("serial", f"PSU{slot} Serial Number", "serial", "mdi:identifier", "diagnostic"),
        ]:
            object_id = f"{NODE_ID}_psu{slot}_{extra_suffix}"
            config_topic = f"homeassistant/sensor/{NODE_ID}/psu{slot}_{extra_suffix}/config"
            config = {
                "name": extra_name,
                "unique_id": object_id,
                "state_topic": psu_state_topic,
                "value_template": f"{{{{ value_json.{extra_key} }}}}",
                "device": device_dict,
                "icon": extra_icon,
                "availability_topic": avail_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "origin": ORIGIN,
            }
            if extra_cat:
                config["entity_category"] = extra_cat
            client.publish(config_topic, json.dumps(config), retain=True)
            count += 1

    return count


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main():
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="IPMI sensor + BMC PSU collector")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level")
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    config = MqttConfig.from_env()
    bmc_host = os.environ.get("BMC_HOST", "10.1.5.150")
    bmc_user = os.environ.get("BMC_USER", "ADMIN")
    bmc_pass = os.environ.get("BMC_PASS", "ADMIN")

    stop_event = threading.Event()
    discovery_published = False

    state_topic = f"sensors2mqtt/{NODE_ID}/state"
    avail_topic = f"sensors2mqtt/{NODE_ID}/status"

    def shutdown(signum, frame):
        log.info("Shutting down (signal %d)", signum)
        stop_event.set()

    if not args.once:
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sensors2mqtt-ipmi-sdr")
    client.username_pw_set(config.user, config.password)

    log.info("Connecting to MQTT %s:%d", config.host, config.port)
    client.connect(config.host, config.port, keepalive=120)
    client.loop_start()

    device_info = DeviceInfo(
        node_id=NODE_ID,
        name="big-storage",
        manufacturer="Supermicro",
        model="X11DSC+",
        configuration_url=f"https://{bmc_host}",
    )
    device_dict = {
        "identifiers": [f"sensors2mqtt_{NODE_ID}"],
        "name": device_info.name,
        "manufacturer": device_info.manufacturer,
        "model": device_info.model,
        "configuration_url": device_info.configuration_url,
    }

    try:
        while not stop_event.is_set():
            log.info("Polling IPMI sensors + BMC PSU")
            ipmi_values = poll_ipmi_sensors(bmc_host, bmc_user, bmc_pass)
            psu_data = poll_bmc_psu(bmc_host, bmc_user, bmc_pass)

            if ipmi_values is None and psu_data is None:
                client.publish(avail_topic, "offline", retain=True)
                log.warning("No sensor data from either source")
            else:
                if not discovery_published:
                    ipmi_sensors = get_ipmi_sensors()
                    ipmi_count = publish_discovery(
                        client,
                        ipmi_sensors,
                        device_info,
                        state_topic,
                        avail_topic,
                    )
                    psu_count = 0
                    if psu_data:
                        psu_count = publish_psu_discovery(
                            client,
                            device_dict,
                            psu_data,
                            avail_topic,
                        )
                    discovery_published = True
                    log.info("Published discovery: %d IPMI + %d PSU sensors", ipmi_count, psu_count)

                if ipmi_values is None:
                    ipmi_values = {}
                publish_state(client, state_topic, ipmi_values)
                client.publish(avail_topic, "online", retain=True)

                if psu_data:
                    for psu in psu_data.get("psus", []):
                        slot = psu["slot"]
                        psu_topic = f"sensors2mqtt/{NODE_ID}/psu{slot}/state"
                        client.publish(psu_topic, json.dumps(psu), retain=False)

                psu_power = sum(
                    p.get("dc_12v_output_power_w", 0) or 0 for p in (psu_data or {}).get("psus", [])
                )
                log.info(
                    "Published: IPMI=%d sensors, PSU=%d units (%.0fW DC total)",
                    len(ipmi_values),
                    len((psu_data or {}).get("psus", [])),
                    psu_power,
                )

            if args.once:
                break
            stop_event.wait(timeout=config.poll_interval)

    finally:
        client.publish(avail_topic, "offline", retain=True)
        client.disconnect()
        client.loop_stop()
        log.info("Disconnected from MQTT")


if __name__ == "__main__":
    main()
