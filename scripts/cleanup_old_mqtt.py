#!/usr/bin/env python3
"""Clean up old retained MQTT discovery messages from previous sensors2mqtt versions.

Removes:
- Old per-sensor discovery topics (homeassistant/sensor/{node_id}/{suffix}/config)
  for the flat port{NN}_poe_mw suffixes from the original PoE-only collector
- Old retained state blobs (sensors2mqtt/{node_id}/state) that contained port data
- Old hardware sensor discovery that used the original flat format

Run this ONCE after upgrading to the per-port state topic format.
"""

import os
import time

import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASSWORD", "")

# Switches and their old suffixes
OLD_SWITCHES = {
    "sw_netgear_m4300_24x": {
        "hw_suffixes": ["fan1_rpm", "fan2_rpm", "temp", "psu_power"],
        "poe_ports": 0,
    },
    "sw_netgear_gsm7252ps_s2": {
        "hw_suffixes": [],
        "poe_ports": 48,
    },
    "sw_netgear_s3300_1": {
        "hw_suffixes": ["fan1_rpm", "fan2_rpm", "fan3_rpm", "temp", "psu_power"],
        "poe_ports": 48,
    },
}


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cleanup-old-discovery")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()

    count = 0
    for node_id, info in OLD_SWITCHES.items():
        # Clear old hardware sensor discovery
        for suffix in info["hw_suffixes"]:
            topic = f"homeassistant/sensor/{node_id}/{suffix}/config"
            client.publish(topic, "", retain=True)
            count += 1

        # Clear old PoE port discovery (flat port{NN}_poe_mw format)
        for port in range(1, info["poe_ports"] + 1):
            nn = str(port).zfill(2)
            suffix = f"port{nn}_poe_mw"
            topic = f"homeassistant/sensor/{node_id}/{suffix}/config"
            client.publish(topic, "", retain=True)
            count += 1

        # Clear old retained state blob (contained port data in flat format)
        client.publish(f"sensors2mqtt/{node_id}/state", "", retain=True)
        count += 1

        # Clear old availability
        client.publish(f"sensors2mqtt/{node_id}/status", "", retain=True)
        count += 1

    time.sleep(2)
    client.disconnect()
    client.loop_stop()
    print(f"Cleared {count} old retained MQTT messages")


if __name__ == "__main__":
    main()
