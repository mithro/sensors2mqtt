"""Base publisher: MQTT connection, poll loop, signal handling.

All collectors inherit from BasePublisher and implement poll().
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from sensors2mqtt.discovery import (
    DeviceInfo,
    SensorDef,
    publish_connection_diagnostic,
    publish_discovery,
    publish_state,
)

log = logging.getLogger(__name__)


@dataclass
class MqttConfig:
    """MQTT broker connection settings."""

    host: str = "localhost"
    port: int = 1883
    user: str = ""
    password: str = ""
    poll_interval: int = 30

    @classmethod
    def from_env(cls) -> MqttConfig:
        """Create config from environment variables."""
        return cls(
            host=os.environ.get("MQTT_HOST", cls.host),
            port=int(os.environ.get("MQTT_PORT", str(cls.port))),
            user=os.environ.get("MQTT_USER", cls.user),
            password=os.environ.get("MQTT_PASSWORD", cls.password),
            poll_interval=int(os.environ.get("POLL_INTERVAL", str(cls.poll_interval))),
        )


def host_id() -> str:
    """The host's node_id: short hostname, dashes -> underscores (e.g. ``ten64``).

    This is the single, consistent host identifier used everywhere: it is the
    device node_id for the host-local collectors (local, ipmi, hwmon) and the
    ``{host}`` segment of every collector's client-id (see ``client_id_for``).

    It is deliberately the *short* hostname for now. Two machines that share a
    short hostname (e.g. a ``ten64`` at two sites) would therefore collide if
    both ever connect to the same broker; making this globally unique is a
    separate, later decision.
    """
    return socket.gethostname().split(".", 1)[0].replace("-", "_")


def client_id_for(module: str) -> str:
    """MQTT client-id for a collector: ``sensors2mqtt-{host}-{module}``.

    ``{host}`` is :func:`host_id`, so every daemon on a host gets a distinct,
    stable connection identity (e.g. ``sensors2mqtt-ten64-snmp``). Two daemons
    of the same kind on different hosts never present the same client-id, which
    is what stops the broker from kicking one connection to take over the other
    in a reconnect loop. ``module`` names the collector, spelled with underscores
    to match the Python module (``local``, ``snmp``, ``snmp_control``,
    ``ipmi_sensors``, ``hwmon``) — the same token used in its topics.
    """
    return f"sensors2mqtt-{host_id()}-{module}"


def connection_status_topic(module: str) -> str:
    """Per-host, per-daemon connection status topic (Last-Will + heartbeat).

    ``sensors2mqtt/{host}/{module}/status`` — grouped under the host so all of a
    machine's topics live together, and namespaced by module so two collectors on
    one host never collide on a shared status topic.
    """
    return f"sensors2mqtt/{host_id()}/{module}/status"


def make_client(
    config: MqttConfig,
    client_id: str,
    on_connected: Callable[[mqtt.Client], None] | None = None,
    will_topic: str | None = None,
) -> mqtt.Client:
    """Create an MQTT client with credentials and connection logging attached.

    paho is silent about refused connections: after a failed CONNACK the
    client just never connects and every QoS-0 publish is silently dropped,
    so a collector can log "published N values" while delivering nothing
    (observed live when the broker stopped accepting anonymous connections).
    The attached callbacks make connect results, network-level connect
    failures, and unexpected disconnects visible in the logs.

    ``on_connected``, if given, is called with the client after each
    *successful* connect (CONNACK ok), including reconnects. Use it to
    (re)establish subscriptions: a broker drops them on disconnect with a
    clean session, and paho does not resubscribe automatically.

    ``will_topic``, if given, registers a Last-Will: when the client dies
    ungracefully (crash, power loss, network drop), the broker publishes
    ``offline`` (retained) to that topic so Home Assistant marks the device
    unavailable. A clean shutdown still publishes ``offline`` itself.
    """
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.username_pw_set(config.user, config.password)
    if will_topic is not None:
        client.will_set(will_topic, payload="offline", retain=True)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect refused: %s", reason_code)
            return
        log.info("MQTT connected")
        if on_connected is not None:
            on_connected(client)

    def on_connect_fail(client, userdata):
        log.error("MQTT connect attempt failed (network); paho will retry")

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("MQTT disconnected unexpectedly: %s", reason_code)

    client.on_connect = on_connect
    client.on_connect_fail = on_connect_fail
    client.on_disconnect = on_disconnect
    return client


class BasePublisher(ABC):
    """Base class for all sensor collectors.

    Subclasses must implement:
        poll() -> dict | None
            Returns a dict of {sensor_suffix: value} or None on failure.
        sensors -> list[SensorDef]
            Property returning sensor definitions.
        device -> DeviceInfo
            Property returning device info for HA discovery.
        module -> str
            Property returning the collector's module token (e.g. 'local', 'hwmon').
    """

    def __init__(self, config: MqttConfig | None = None):
        self.config = config or MqttConfig.from_env()
        self._stop_event = threading.Event()
        self._discovery_published = False

    @property
    @abstractmethod
    def sensors(self) -> list[SensorDef]:
        """Sensor definitions for HA auto-discovery."""

    @property
    @abstractmethod
    def device(self) -> DeviceInfo:
        """Device info for HA device registry."""

    @property
    @abstractmethod
    def module(self) -> str:
        """Module token (e.g. 'local', 'hwmon'); identifies this daemon."""

    @abstractmethod
    def poll(self) -> dict | None:
        """Poll sensors. Return {suffix: value} dict, or None on failure."""

    @property
    def client_id(self) -> str:
        return client_id_for(self.module)

    @property
    def state_topic(self) -> str:
        return f"sensors2mqtt/{self.device.node_id}/{self.module}/state"

    @property
    def avail_topic(self) -> str:
        return f"sensors2mqtt/{self.device.node_id}/{self.module}/status"

    def run(self) -> None:
        """Main entry point: connect MQTT, poll in a loop, handle signals."""
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # avail_topic == connection_status_topic(self.module) for host-local
        # collectors (node_id == host_id()), so the Last-Will and the connection
        # diagnostic published below share one topic.
        client = make_client(self.config, self.client_id, will_topic=self.avail_topic)

        log.info("Connecting to MQTT %s:%d", self.config.host, self.config.port)
        client.connect(self.config.host, self.config.port, keepalive=120)
        client.loop_start()

        # One-time migration: clear legacy non-module-scoped retained topics.
        client.publish(f"sensors2mqtt/{self.device.node_id}/state", "", retain=True)
        client.publish(f"sensors2mqtt/{self.device.node_id}/status", "", retain=True)
        # Per-daemon connection diagnostic on the host device.
        publish_connection_diagnostic(
            client, self.device.node_id, self.module, self.device.name
        )

        try:
            while not self._stop_event.is_set():
                self._poll_once(client)
                self._stop_event.wait(timeout=self.config.poll_interval)
        finally:
            client.publish(self.avail_topic, "offline", retain=True)
            client.disconnect()
            client.loop_stop()
            log.info("Disconnected from MQTT")

    def _poll_once(self, client: mqtt.Client) -> None:
        """Execute one poll cycle."""
        log.info("Polling sensors")
        values = self.poll()

        if values is None:
            client.publish(self.avail_topic, "offline", retain=True)
            log.warning("No sensor data")
            return

        if not self._discovery_published:
            count = publish_discovery(
                client, self.sensors, self.device,
                self.state_topic, self.avail_topic,
            )
            self._discovery_published = True
            log.info("Published MQTT discovery for %d sensors", count)

        publish_state(client, self.state_topic, values)
        client.publish(self.avail_topic, "online", retain=True)
        self._log_summary(values)

    def _log_summary(self, values: dict) -> None:
        """Log a summary of polled values. Override for custom summaries."""
        log.info("Published %d sensor values", len(values))

    def _signal_handler(self, signum, frame):
        log.info("Shutting down (signal %d)", signum)
        self._stop_event.set()
