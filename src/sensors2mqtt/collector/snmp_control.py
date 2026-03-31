"""PoE control service: toggle and power-cycle PoE ports via SNMP SET.

Separate service from the SNMP sensor collector. Reads the same snmp.toml
config, connects to MQTT independently, and subscribes to command topics.

Only manages switches that have write_community configured in snmp.toml.

Usage:
    python -m sensors2mqtt.collector.snmp_control
    python -m sensors2mqtt.collector.snmp_control --config /etc/sensors2mqtt/snmp.toml
    python -m sensors2mqtt.collector.snmp_control --once
"""

from __future__ import annotations

import json
import logging
import re
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import paho.mqtt.client as mqtt

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.snmp import (
    SwitchConfig,
    _build_port_device,
    fetch_lldp_chassis_macs,
    load_config,
    parse_lldp_walk,
    parse_snmpget_value,
)
from sensors2mqtt.discovery import ORIGIN, device_dict

log = logging.getLogger(__name__)

# SNMP OIDs
IF_ALIAS_OID = "1.3.6.1.2.1.31.1.1.1.18"          # ifAlias (port descriptions)
LLDP_REM_OID = "1.0.8802.1.1.2.1.4.1.1"            # LLDP remote table base

# SNMP OIDs for PoE control
POE_ADMIN_OID = "1.3.6.1.2.1.105.1.1.1.3.1"   # pethPsePortAdminEnable (R/W)
POE_DETECT_OID = "1.3.6.1.2.1.105.1.1.1.6.1"   # pethPsePortDetectionStatus (R)
IF_OPER_OID = "1.3.6.1.2.1.2.2.1.8"            # ifOperStatus (R)

# Value mappings
POE_ADMIN_MAP = {1: "enabled", 2: "disabled"}
POE_DETECT_MAP = {1: "unused", 2: "searching", 3: "delivering", 4: "fault"}
OPER_MAP = {1: "up", 2: "down"}


def fetch_port_descriptions(switch: SwitchConfig, timeout: int = 30) -> dict[int, str]:
    """Fetch port descriptions (ifAlias) from switch via SNMP walk.

    Returns {port_number: description_string}.
    """
    descriptions: dict[int, str] = {}
    try:
        result = subprocess.run(
            ["snmpwalk", "-v2c", "-c", switch.community, switch.host, IF_ALIAS_OID],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            log.warning("%s: ifAlias walk failed: %s", switch.name, result.stderr.strip())
            return descriptions
        for line in result.stdout.strip().splitlines():
            m = re.match(r'.*\.(\d+)\s*=\s*STRING:\s*"(.+)"', line)
            if not m:
                continue
            port = int(m.group(1))
            alias = m.group(2).strip()
            if alias:
                descriptions[port] = alias
    except subprocess.TimeoutExpired:
        log.warning("%s: ifAlias walk timed out", switch.name)
    except Exception as e:
        log.warning("%s: ifAlias walk error: %s", switch.name, e)

    if descriptions:
        log.info("%s: fetched %d port descriptions", switch.name, len(descriptions))
    return descriptions


def extract_hostname(description: str) -> str:
    """Extract the hostname portion from an ifAlias or LLDP description.

    Convention: "interface.hostname" (e.g. "eth0.rpi5-pmod" → "rpi5-pmod").
    If no dot separator, returns the whole description.
    """
    dot = description.find(".")
    if dot >= 0:
        return description[dot + 1:]
    return description


def fetch_lldp_neighbors(switch: SwitchConfig, timeout: int = 30) -> dict[int, str]:
    """Fetch LLDP neighbor sysName per port from switch.

    Returns {port_number: sys_name} with domain suffixes stripped.
    """
    sys_names: dict[int, str] = {}
    try:
        result = subprocess.run(
            ["snmpwalk", "-v2c", "-c", switch.community, switch.host,
             f"{LLDP_REM_OID}.9"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            log.warning("%s: LLDP sysName walk failed: %s",
                        switch.name, result.stderr.strip())
            return sys_names
        sys_names = parse_lldp_walk(result.stdout, "9")
    except subprocess.TimeoutExpired:
        log.warning("%s: LLDP sysName walk timed out", switch.name)
    except Exception as e:
        log.warning("%s: LLDP sysName walk error: %s", switch.name, e)

    # Strip FQDN to short hostname
    for port in sys_names:
        sn = sys_names[port]
        if "." in sn:
            sys_names[port] = sn.split(".")[0]

    if sys_names:
        log.info("%s: fetched %d LLDP neighbors", switch.name, len(sys_names))
    return sys_names



@dataclass
class PortControlState:
    """Tracked state for a single PoE port."""
    poe_admin: int = 0       # 1=enabled, 2=disabled
    poe_detect: int = 0      # 1=unused, 2=searching, 3=delivering, 4=fault
    link: int = 0            # 1=up, 2=down
    force_override: bool = False
    busy: bool = False       # True while a command is in progress
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def is_available(self) -> bool:
        """Whether PoE control is available for this port.

        Available when:
        - Link is down (any PoE state)
        - Link is up AND PoE is delivering/searching/fault
        - Force override is ON

        Disabled (greyed out in HA) when:
        - Link is up AND PoE detection is "unused" (not negotiated)
          AND force override is OFF
        """
        if self.force_override:
            return True
        if self.link != 1:  # not up
            return True
        # Link is up — only available if PoE is actively negotiated
        return self.poe_detect in (2, 3, 4)  # searching, delivering, fault

    @property
    def poe_is_on(self) -> bool:
        """Whether PoE admin state is enabled."""
        return self.poe_admin == 1


class PoeController:
    """Manages PoE control for all configured switches."""

    def __init__(
        self,
        mqtt_config: MqttConfig,
        switches: list[SwitchConfig],
        poll_interval: int = 30,
    ):
        self.mqtt_config = mqtt_config
        # Only keep switches with write_community and PoE ports
        self.switches = [
            s for s in switches
            if s.write_community and s.poe_port_count > 0
        ]
        self.poll_interval = poll_interval
        self._snmp_timeout = 10

        # {node_id: {port: PortControlState}}
        self._port_states: dict[str, dict[int, PortControlState]] = {}
        for sw in self.switches:
            self._port_states[sw.node_id] = {
                port: PortControlState()
                for port in range(1, sw.poe_port_count + 1)
            }

        # Lookup: node_id → SwitchConfig
        self._switch_by_id: dict[str, SwitchConfig] = {
            s.node_id: s for s in self.switches
        }

        self._client: mqtt.Client | None = None
        self._stop_event = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=4)

    def _snmpget_int(self, switch: SwitchConfig, oid: str, port: int) -> int | None:
        """SNMP GET a single integer value for a port."""
        full_oid = f"{oid}.{port}"
        try:
            result = subprocess.run(
                ["snmpget", "-v2c", "-c", switch.community, switch.host, full_oid],
                capture_output=True, text=True, timeout=self._snmp_timeout,
            )
            if result.returncode != 0:
                log.warning("%s: snmpget %s failed: %s",
                            switch.name, full_oid, result.stderr.strip())
                return None
            raw = parse_snmpget_value(result.stdout)
            if raw is None:
                return None
            m = re.match(r"(\d+)", raw)
            return int(m.group(1)) if m else None
        except subprocess.TimeoutExpired:
            log.warning("%s: snmpget %s timed out", switch.name, full_oid)
            return None

    def _snmpset_int(self, switch: SwitchConfig, oid: str, port: int, value: int) -> bool:
        """SNMP SET an integer value for a port. Returns True on success."""
        full_oid = f"{oid}.{port}"
        try:
            result = subprocess.run(
                [
                    "snmpset", "-v2c", "-c", switch.write_community,
                    switch.host, full_oid, "i", str(value),
                ],
                capture_output=True, text=True, timeout=self._snmp_timeout,
            )
            if result.returncode != 0:
                log.error("%s: snmpset %s=%d failed: %s",
                          switch.name, full_oid, value, result.stderr.strip())
                return False
            log.info("%s: snmpset %s=%d ok", switch.name, full_oid, value)
            return True
        except subprocess.TimeoutExpired:
            log.error("%s: snmpset %s timed out", switch.name, full_oid)
            return False

    def poll_port_state(self, switch: SwitchConfig, port: int) -> PortControlState | None:
        """Poll current state of a single port."""
        admin = self._snmpget_int(switch, POE_ADMIN_OID, port)
        detect = self._snmpget_int(switch, POE_DETECT_OID, port)
        oper = self._snmpget_int(switch, IF_OPER_OID, port)
        if admin is None or detect is None or oper is None:
            return None
        state = self._port_states[switch.node_id][port]
        state.poe_admin = admin
        state.poe_detect = detect
        state.link = oper
        return state

    def poll_all_ports(self, switch: SwitchConfig) -> None:
        """Poll all PoE port states on a switch via walk."""
        from sensors2mqtt.collector.snmp import parse_snmpwalk
        for oid, attr in [
            (POE_ADMIN_OID, "poe_admin"),
            (POE_DETECT_OID, "poe_detect"),
            (IF_OPER_OID, "link"),
        ]:
            try:
                result = subprocess.run(
                    ["snmpwalk", "-v2c", "-c", switch.community, switch.host, oid],
                    capture_output=True, text=True, timeout=self._snmp_timeout * 3,
                )
                if result.returncode != 0:
                    log.warning("%s: walk %s failed: %s",
                                switch.name, oid, result.stderr.strip())
                    continue
                for index, val in parse_snmpwalk(result.stdout):
                    if 1 <= index <= switch.poe_port_count:
                        try:
                            setattr(self._port_states[switch.node_id][index], attr, int(val))
                        except (ValueError, KeyError):
                            pass
            except subprocess.TimeoutExpired:
                log.warning("%s: walk %s timed out", switch.name, oid)

    def publish_availability(self, switch: SwitchConfig) -> None:
        """Publish per-port PoE control availability."""
        if not self._client:
            return
        for port in range(1, switch.poe_port_count + 1):
            nn = str(port).zfill(2)
            state = self._port_states[switch.node_id][port]
            avail = "online" if state.is_available else "offline"
            topic = f"sensors2mqtt/{switch.node_id}/port/{nn}/poe/available"
            self._client.publish(topic, avail, retain=True)

    def publish_poe_state(self, switch: SwitchConfig, port: int) -> None:
        """Publish PoE ON/OFF state for a single port."""
        if not self._client:
            return
        nn = str(port).zfill(2)
        state = self._port_states[switch.node_id][port]
        payload = "ON" if state.poe_is_on else "OFF"
        topic = f"sensors2mqtt/{switch.node_id}/port/{nn}/poe/state"
        self._client.publish(topic, payload, retain=True)

    def publish_all_poe_states(self, switch: SwitchConfig) -> None:
        """Publish PoE state for all ports on a switch."""
        for port in range(1, switch.poe_port_count + 1):
            self.publish_poe_state(switch, port)

    def _handle_toggle(self, switch: SwitchConfig, port: int, payload: str) -> None:
        """Handle PoE toggle command. Runs in worker thread."""
        state = self._port_states[switch.node_id].get(port)
        if not state:
            log.warning("%s: toggle port %d — invalid port", switch.name, port)
            return

        # Atomic check-and-set of busy flag to prevent concurrent commands
        with state.lock:
            if state.busy:
                log.warning("%s: port %d busy, ignoring toggle", switch.name, port)
                return
            state.busy = True

        try:
            # ON → enable (1), OFF → disable (2)
            if payload == "ON":
                snmp_val = 1
            elif payload == "OFF":
                snmp_val = 2
            else:
                log.warning("%s: port %d invalid payload: %r", switch.name, port, payload)
                return

            log.info("%s: port %d toggle → %s (snmpset i %d)",
                     switch.name, port, payload, snmp_val)

            if not self._snmpset_int(switch, POE_ADMIN_OID, port, snmp_val):
                return

            # Verify with snmpget
            self.poll_port_state(switch, port)
            self.publish_poe_state(switch, port)
            self.publish_availability(switch)
        finally:
            state.busy = False

    def _handle_cycle(self, switch: SwitchConfig, port: int) -> None:
        """Handle power cycle command. Runs in worker thread.

        Sequence:
        1. Pre-check current state
        2. Disable PoE
        3. Poll until detection=unused + link=down (30s timeout)
        4. Enable PoE
        5. Poll until detection=delivering (60s timeout)
        6. Publish final state
        """
        state = self._port_states[switch.node_id].get(port)
        if not state:
            log.warning("%s: cycle port %d — invalid port", switch.name, port)
            return

        # Atomic check-and-set of busy flag
        with state.lock:
            if state.busy:
                log.warning("%s: port %d busy, ignoring cycle", switch.name, port)
                return
            state.busy = True

        try:
            log.info("%s: port %d power cycle starting", switch.name, port)

            # 1. Pre-check
            self.poll_port_state(switch, port)
            log.info("%s: port %d pre-check: admin=%s detect=%s link=%s",
                     switch.name, port,
                     POE_ADMIN_MAP.get(state.poe_admin, "?"),
                     POE_DETECT_MAP.get(state.poe_detect, "?"),
                     OPER_MAP.get(state.link, "?"))

            # 2. Disable PoE
            if not self._snmpset_int(switch, POE_ADMIN_OID, port, 2):
                log.error("%s: port %d cycle failed — couldn't disable", switch.name, port)
                return

            # 3. Poll until off (30s timeout)
            off_confirmed = False
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                self.poll_port_state(switch, port)
                if state.poe_detect == 1 and state.link == 2:  # unused + down
                    off_confirmed = True
                    break
                if self._stop_event.wait(timeout=2):
                    # Shutting down — publish actual state before exit
                    self.poll_port_state(switch, port)
                    self.publish_poe_state(switch, port)
                    self.publish_availability(switch)
                    return

            if not off_confirmed:
                log.warning("%s: port %d cycle — off timeout (detect=%s link=%s)",
                            switch.name, port,
                            POE_DETECT_MAP.get(state.poe_detect, "?"),
                            OPER_MAP.get(state.link, "?"))

            log.info("%s: port %d PoE disabled, re-enabling", switch.name, port)

            # 4. Enable PoE
            if not self._snmpset_int(switch, POE_ADMIN_OID, port, 1):
                log.error("%s: port %d cycle failed — couldn't re-enable", switch.name, port)
                self.poll_port_state(switch, port)
                self.publish_poe_state(switch, port)
                self.publish_availability(switch)
                return

            # 5. Poll until delivering (60s timeout)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                self.poll_port_state(switch, port)
                if state.poe_detect == 3:  # delivering
                    break
                if self._stop_event.wait(timeout=2):
                    # Shutting down — publish actual state before exit
                    self.poll_port_state(switch, port)
                    self.publish_poe_state(switch, port)
                    self.publish_availability(switch)
                    return
            else:
                log.warning("%s: port %d cycle — delivering timeout (detect=%s)",
                            switch.name, port,
                            POE_DETECT_MAP.get(state.poe_detect, "?"))

            # 6. Publish final state
            self.poll_port_state(switch, port)
            self.publish_poe_state(switch, port)
            self.publish_availability(switch)
            log.info("%s: port %d power cycle complete (detect=%s link=%s)",
                     switch.name, port,
                     POE_DETECT_MAP.get(state.poe_detect, "?"),
                     OPER_MAP.get(state.link, "?"))
        finally:
            state.busy = False

    def _handle_force(self, switch: SwitchConfig, port: int, payload: str) -> None:
        """Handle force override command."""
        state = self._port_states[switch.node_id].get(port)
        if not state:
            return

        state.force_override = payload == "ON"
        nn = str(port).zfill(2)
        log.info("%s: port %d force override → %s", switch.name, port, payload)

        if self._client:
            # Publish force state (retained — survives restart)
            self._client.publish(
                f"sensors2mqtt/{switch.node_id}/port/{nn}/poe/force/state",
                payload, retain=True,
            )
        self.publish_availability(switch)

    def _on_message(self, client: mqtt.Client, userdata, message: mqtt.MQTTMessage) -> None:
        """MQTT message callback — dispatches commands to worker threads."""
        topic = message.topic
        payload = message.payload.decode("utf-8", errors="replace").strip()

        # Parse topic: sensors2mqtt/{node_id}/port/{nn}/poe/{action}
        m = re.match(
            r"sensors2mqtt/([^/]+)/port/(\d+)/poe/(set|cycle|force/set)$",
            topic,
        )
        if not m:
            return

        node_id = m.group(1)
        port = int(m.group(2))
        action = m.group(3)

        switch = self._switch_by_id.get(node_id)
        if not switch:
            log.warning("Command for unknown switch %s, ignoring", node_id)
            return

        if port < 1 or port > switch.poe_port_count:
            log.warning("%s: command for invalid port %d, ignoring", switch.name, port)
            return

        if action == "set":
            self._executor.submit(self._handle_toggle, switch, port, payload)
        elif action == "cycle":
            self._executor.submit(self._handle_cycle, switch, port)
        elif action == "force/set":
            self._executor.submit(self._handle_force, switch, port, payload)

    def publish_discovery(
        self,
        switch: SwitchConfig,
        chassis_macs: dict[int, str] | None = None,
    ) -> int:
        """Publish HA switch/button entity discovery for all PoE ports.

        Each port gets a per-port sub-device (via_device → parent switch),
        matching the sensor collector's per-port device scheme.
        """
        if not self._client:
            return 0

        avail_topic = f"sensors2mqtt/{switch.node_id}/status"

        count = 0
        for port in range(1, switch.poe_port_count + 1):
            nn = str(port).zfill(2)
            port_avail = f"sensors2mqtt/{switch.node_id}/port/{nn}/poe/available"

            # Build per-port sub-device (same scheme as snmp.py)
            port_device = _build_port_device(switch, port, chassis_macs)
            port_dev_dict = device_dict(port_device)

            # Build host suffix for entity names
            # PoE Toggle (switch entity)
            # Short names — device name already identifies the port.
            toggle_config = {
                "name": "PoE",
                "unique_id": f"{switch.node_id}_{nn}_poe_toggle",
                "command_topic": f"sensors2mqtt/{switch.node_id}/port/{nn}/poe/set",
                "state_topic": f"sensors2mqtt/{switch.node_id}/port/{nn}/poe/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "device": port_dev_dict,
                "availability": [
                    {"topic": avail_topic, "payload_available": "online",
                     "payload_not_available": "offline"},
                    {"topic": port_avail, "payload_available": "online",
                     "payload_not_available": "offline"},
                ],
                "availability_mode": "all",
                "origin": ORIGIN,
                "icon": "mdi:lightning-bolt",
            }
            self._client.publish(
                f"homeassistant/switch/{switch.node_id}/port{nn}_poe_toggle/config",
                json.dumps(toggle_config), retain=True,
            )
            count += 1

            # Power Cycle (button entity)
            cycle_config = {
                "name": "PoE Cycle",
                "unique_id": f"{switch.node_id}_{nn}_poe_cycle",
                "command_topic": f"sensors2mqtt/{switch.node_id}/port/{nn}/poe/cycle",
                "payload_press": "PRESS",
                "device": port_dev_dict,
                "availability": [
                    {"topic": avail_topic, "payload_available": "online",
                     "payload_not_available": "offline"},
                    {"topic": port_avail, "payload_available": "online",
                     "payload_not_available": "offline"},
                ],
                "availability_mode": "all",
                "origin": ORIGIN,
                "icon": "mdi:restart",
            }
            self._client.publish(
                f"homeassistant/button/{switch.node_id}/port{nn}_poe_cycle/config",
                json.dumps(cycle_config), retain=True,
            )
            count += 1

            # Force Override (switch entity, hidden config category)
            force_config = {
                "name": "PoE Force",
                "unique_id": f"{switch.node_id}_{nn}_poe_force",
                "command_topic": f"sensors2mqtt/{switch.node_id}/port/{nn}/poe/force/set",
                "state_topic": f"sensors2mqtt/{switch.node_id}/port/{nn}/poe/force/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
                "device": port_dev_dict,
                "availability_topic": avail_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "entity_category": "config",
                "origin": ORIGIN,
                "icon": "mdi:shield-key",
            }
            self._client.publish(
                f"homeassistant/switch/{switch.node_id}/port{nn}_poe_force/config",
                json.dumps(force_config), retain=True,
            )
            count += 1

        return count

    def _read_force_overrides(self, switch: SwitchConfig) -> None:
        """Read back retained force override states from MQTT on startup.

        Subscribes to force/state topics, waits briefly, then unsubscribes.
        This ensures force_override flags survive restart.
        """
        if not self._client:
            return

        def on_force_msg(client, userdata, message):
            m = re.match(
                rf"sensors2mqtt/{re.escape(switch.node_id)}/port/(\d+)/poe/force/state$",
                message.topic,
            )
            if not m:
                return
            port = int(m.group(1))
            payload = message.payload.decode("utf-8", errors="replace").strip()
            state = self._port_states[switch.node_id].get(port)
            if state:
                state.force_override = payload == "ON"
                if payload == "ON":
                    log.info("%s: port %d force override restored from retained state",
                             switch.name, port)

        topic = f"sensors2mqtt/{switch.node_id}/port/+/poe/force/state"
        self._client.message_callback_add(topic, on_force_msg)
        self._client.subscribe(topic)

        # Wait briefly for retained messages to arrive
        time.sleep(1)

        self._client.unsubscribe(topic)
        self._client.message_callback_remove(topic)

    def run(self, once: bool = False) -> None:
        """Main entry point: connect MQTT, poll, handle commands."""
        if not self.switches:
            log.warning("No switches with write_community configured — nothing to control")
            return

        log.info("Managing %d PoE switches: %s",
                 len(self.switches),
                 ", ".join(s.name for s in self.switches))

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="sensors2mqtt-snmp-control",
        )
        client.username_pw_set(self.mqtt_config.user, self.mqtt_config.password)
        client.on_message = self._on_message
        self._client = client

        log.info("Connecting to MQTT %s:%d",
                 self.mqtt_config.host, self.mqtt_config.port)
        client.connect(self.mqtt_config.host, self.mqtt_config.port, keepalive=120)
        client.loop_start()

        try:
            # Read back retained force overrides
            for sw in self.switches:
                self._read_force_overrides(sw)

            # Subscribe to command topics
            if not once:
                for sw in self.switches:
                    client.subscribe(f"sensors2mqtt/{sw.node_id}/port/+/poe/set")
                    client.subscribe(f"sensors2mqtt/{sw.node_id}/port/+/poe/cycle")
                    client.subscribe(f"sensors2mqtt/{sw.node_id}/port/+/poe/force/set")
                    log.info("%s: subscribed to command topics", sw.name)

            # Fetch LLDP chassis MACs for per-port device connections
            port_chassis_macs: dict[str, dict[int, str]] = {}
            for sw in self.switches:
                port_chassis_macs[sw.node_id] = fetch_lldp_chassis_macs(sw)

            # Initial poll + discovery + state publish
            for sw in self.switches:
                self.poll_all_ports(sw)
                disc_count = self.publish_discovery(
                    sw,
                    chassis_macs=port_chassis_macs.get(sw.node_id),
                )
                self.publish_all_poe_states(sw)
                self.publish_availability(sw)
                client.publish(f"sensors2mqtt/{sw.node_id}/status", "online", retain=True)
                log.info("%s: published %d control entities, %d ports polled",
                         sw.name, disc_count, sw.poe_port_count)

            if once:
                return

            # Poll loop
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=self.poll_interval)
                if self._stop_event.is_set():
                    break
                for sw in self.switches:
                    self.poll_all_ports(sw)
                    self.publish_all_poe_states(sw)
                    self.publish_availability(sw)

        finally:
            for sw in self.switches:
                client.publish(f"sensors2mqtt/{sw.node_id}/status", "offline", retain=True)
            self._executor.shutdown(wait=False)
            client.disconnect()
            client.loop_stop()
            self._client = None
            log.info("Disconnected from MQTT")


def main():
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="PoE control service")
    parser.add_argument("--config", type=Path, help="Path to TOML config file")
    parser.add_argument("--once", action="store_true",
                        help="Poll once, publish discovery + state, then exit")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level")
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    mqtt_config = MqttConfig.from_env()
    switches = load_config(args.config)

    controller = PoeController(mqtt_config=mqtt_config, switches=switches)

    stop_event = controller._stop_event

    def shutdown(signum, frame):
        log.info("Shutting down (signal %d)", signum)
        stop_event.set()

    if not args.once:
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

    controller.run(once=args.once)


if __name__ == "__main__":
    main()
