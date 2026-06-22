"""Host power-control service: graceful shutdown / reboot via MQTT.

The control counterpart to the ``local`` sensor collector, mirroring the way
``snmp_control`` is the control counterpart to ``snmp``. It runs *on the host it
controls*, as root, subscribes to per-host power command topics, and calls
``/sbin/shutdown`` directly — no SSH, no external agent reaching in.

It exposes two Home Assistant ``button`` entities per host — **Shutdown** and
**Reboot** — attached to the host's existing sensors2mqtt device.

Safety: this daemon only *triggers* a clean halt. It can never report "I am off"
(it is dead by then), so a consumer that cuts mains power MUST confirm the host
is off via an independent observer (e.g. HA pinging the host) — never by trusting
this daemon. The ``power/state`` ack topic (``shutting_down``/``rebooting``) is an
acknowledgement that the command was received, not a confirmation of power state.

Usage:
    python -m sensors2mqtt.collector.local_control
    python -m sensors2mqtt.collector.local_control --once
"""

from __future__ import annotations

import json
import logging
import re
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import paho.mqtt.client as mqtt

from sensors2mqtt.base import (
    MqttConfig,
    client_id_for,
    host_id,
    host_name,
    make_client,
)
from sensors2mqtt.discovery import (
    DISCOVERY_PREFIX,
    ORIGIN,
    availability_config,
    publish_connection_diagnostic,
)

log = logging.getLogger(__name__)

# Module token: appears in the client-id, the connection status topic, and the
# connectivity diagnostic entity. Matches the Python module name.
MODULE = "local_control"

# Supported power actions. ``argv`` is executed verbatim (no shell). Reboot uses
# ``shutdown -r`` rather than ``reboot`` so both paths go through the same
# graceful systemd shutdown sequence.
ACTIONS: dict[str, dict] = {
    "shutdown": {
        "argv": ["/sbin/shutdown", "-h", "now"],
        "state": "shutting_down",
        "name": "Shutdown",
        "icon": "mdi:power",
    },
    "reboot": {
        "argv": ["/sbin/shutdown", "-r", "now"],
        "state": "rebooting",
        "name": "Reboot",
        "icon": "mdi:restart",
    },
}

# Only this exact payload triggers an action, and only when delivered live (not
# retained — see _on_message). Any other payload, and any retained message, is
# ignored so a stray message can never halt a host.
TRIGGER_PAYLOAD = "PRESS"

# sensors2mqtt/{node_id}/power/{action}/set
_COMMAND_RE = re.compile(r"sensors2mqtt/([^/]+)/power/([^/]+)/set$")


class PowerController:
    """Receives power commands for one host and runs them as graceful halts."""

    def __init__(
        self,
        mqtt_config: MqttConfig,
        node_id: str | None = None,
        name: str | None = None,
        command_timeout: int = 30,
    ):
        self.mqtt_config = mqtt_config
        self.node_id = node_id or host_id()
        self.host_name = name or host_name()
        self._command_timeout = command_timeout

        self._client: mqtt.Client | None = None
        self._stop_event = threading.Event()
        self._connected = threading.Event()
        self._once = False
        self._executor = ThreadPoolExecutor(max_workers=2)

        # Once a halt is scheduled the host is going down; further commands are
        # ignored so a flood of presses spawns one action, not many. A failed
        # command (e.g. not root) clears this so a fixed deployment can retry.
        self._busy = False
        self._busy_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------

    @property
    def status_topic(self) -> str:
        """Daemon connection status (Last-Will + heartbeat)."""
        return f"sensors2mqtt/{self.node_id}/{MODULE}/status"

    @property
    def state_topic(self) -> str:
        """Command acknowledgement: idle / shutting_down / rebooting."""
        return f"sensors2mqtt/{self.node_id}/power/state"

    def command_topic(self, action: str) -> str:
        return f"sensors2mqtt/{self.node_id}/power/{action}/set"

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def _run_command(self, argv: list[str]) -> subprocess.CompletedProcess:
        """Execute a power command. Isolated so tests mock exactly this call.

        Never invoked for real in the test suite — ``subprocess.run`` is patched.
        """
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=self._command_timeout
        )

    def _publish_state(self, state: str) -> None:
        if self._client:
            self._client.publish(self.state_topic, state, retain=True)

    def _handle_command(self, action: str) -> None:
        """Worker: ack, then invoke the power command. Runs in a thread."""
        spec = ACTIONS.get(action)
        if not spec:
            log.warning("power: unknown action %r — ignoring", action)
            return

        # Atomic check-and-set of the busy flag.
        with self._busy_lock:
            if self._busy:
                log.warning("power: %s ignored — a power action is already scheduled",
                            action)
                return
            self._busy = True

        # Publish the ack BEFORE invoking the command: the process is about to be
        # SIGTERM'd by systemd during the shutdown it just triggered.
        log.warning("power: %s requested — invoking %s",
                    action, " ".join(spec["argv"]))
        self._publish_state(spec["state"])

        try:
            result = self._run_command(spec["argv"])
        except subprocess.TimeoutExpired:
            log.error("power: %s timed out", action)
            self._reset_after_failure()
            return
        except Exception as exc:  # noqa: BLE001 — log and stay alive
            log.error("power: %s errored: %s", action, exc)
            self._reset_after_failure()
            return

        if result.returncode != 0:
            # The host is NOT going down (commonly: not root). Make that visible
            # and reset so a consumer never assumes the host is off.
            log.error("power: %s failed (rc=%d): %s",
                      action, result.returncode, (result.stderr or "").strip())
            self._reset_after_failure()
            return

        log.info("power: %s issued ok — host going down", action)
        # Leave _busy set and state at the in-progress value; on next boot the
        # daemon republishes idle (see _announce).

    def _reset_after_failure(self) -> None:
        """A command did not take effect: clear busy and republish idle.

        Clear ``_busy`` *before* publishing ``idle`` so the two are never
        observed inconsistent: once a consumer sees ``idle`` (button available
        again) a retry press is accepted, not rejected as still-busy.
        """
        with self._busy_lock:
            self._busy = False
        self._publish_state("idle")

    # ------------------------------------------------------------------
    # MQTT message handling
    # ------------------------------------------------------------------

    def _on_message(self, client: mqtt.Client, userdata, message: mqtt.MQTTMessage) -> None:
        m = _COMMAND_RE.match(message.topic)
        if not m:
            return
        node_id, action = m.group(1), m.group(2)
        if node_id != self.node_id:
            return
        if action not in ACTIONS:
            log.warning("power: unknown action %r in %s — ignoring", action, message.topic)
            return
        if message.retain:
            # A retained command is stale by definition: the broker re-delivers it
            # on every (re)subscribe and after every reboot. Acting on a retained
            # PRESS would turn one stray message (a manual ``mosquitto_pub -r`` or
            # a buggy automation) into a shutdown/boot loop. Only live presses act.
            log.warning("power: %s ignored — retained (stale) message", action)
            return
        payload = message.payload.decode("utf-8", errors="replace").strip()
        if payload != TRIGGER_PAYLOAD:
            log.warning("power: %s ignored — payload %r != %r",
                        action, payload, TRIGGER_PAYLOAD)
            return
        self._executor.submit(self._handle_command, action)

    def _subscribe_commands(self, client: mqtt.Client) -> None:
        """(Re)subscribe to this host's power command topics on every connect."""
        for action in ACTIONS:
            client.subscribe(self.command_topic(action))
        log.info("%s: subscribed to power command topics", self.node_id)

    def _on_mqtt_connected(self, client: mqtt.Client) -> None:
        """Called on each successful MQTT (re)connect (from make_client)."""
        self._connected.set()
        client.publish(self.status_topic, "online", retain=True)
        publish_connection_diagnostic(client, self.node_id, MODULE, self.host_name)
        if not self._once:
            self._subscribe_commands(client)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def publish_discovery(self) -> int:
        """Publish HA button discovery for each power action. Returns count."""
        if not self._client:
            return 0
        # Attach to the host's existing device by identifiers + name only — never
        # clobbers the manufacturer/model the ``local`` collector set (same safe
        # attach as publish_connection_diagnostic).
        device = {"identifiers": [f"sensors2mqtt_{self.node_id}"], "name": self.host_name}
        count = 0
        for action, spec in ACTIONS.items():
            config = {
                "name": spec["name"],
                "unique_id": f"{self.node_id}_power_{action}",
                "command_topic": self.command_topic(action),
                "payload_press": TRIGGER_PAYLOAD,
                "device": device,
                **availability_config(self.status_topic),
                "entity_category": "config",
                "origin": ORIGIN,
                "icon": spec["icon"],
            }
            self._client.publish(
                f"{DISCOVERY_PREFIX}/button/{self.node_id}/power_{action}/config",
                json.dumps(config), retain=True,
            )
            count += 1
        return count

    def _announce(self) -> None:
        """Startup publish: reset stale state to idle, discovery, online."""
        # A retained shutting_down/rebooting from before a reboot is now stale —
        # we are clearly back up, so reset it.
        self._publish_state("idle")
        self.publish_discovery()
        if self._client:
            self._client.publish(self.status_topic, "online", retain=True)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, once: bool = False) -> None:
        self._once = once
        self._connected.clear()
        client = make_client(
            self.mqtt_config, client_id_for(MODULE),
            on_connected=self._on_mqtt_connected,
            will_topic=self.status_topic,
        )
        client.on_message = self._on_message
        self._client = client

        log.info("Connecting to MQTT %s:%d", self.mqtt_config.host, self.mqtt_config.port)
        client.connect(self.mqtt_config.host, self.mqtt_config.port, keepalive=120)
        client.loop_start()

        try:
            # Wait for the first connect so the announce below actually reaches a
            # subscribed broker (a publish issued before CONNACK is dropped).
            if not self._connected.wait(timeout=10):
                log.warning("MQTT not connected within 10s; startup may be incomplete")

            self._announce()
            log.info("%s: local-control ready (%d power buttons)",
                     self.node_id, len(ACTIONS))

            if once:
                return

            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=self.mqtt_config.poll_interval)
                if self._stop_event.is_set():
                    break
                client.publish(self.status_topic, "online", retain=True)  # heartbeat
        finally:
            client.publish(self.status_topic, "offline", retain=True)
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

    parser = argparse.ArgumentParser(description="Host power-control service")
    parser.add_argument("--once", action="store_true",
                        help="Publish discovery + idle state, then exit")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level")
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    controller = PowerController(mqtt_config=MqttConfig.from_env())

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
