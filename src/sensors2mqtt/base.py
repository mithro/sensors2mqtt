"""Base publisher: MQTT connection, poll loop, signal handling.

All collectors inherit from BasePublisher and implement poll().
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from sensors2mqtt.discovery import DeviceInfo, SensorDef, publish_discovery, publish_state

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


class BasePublisher(ABC):
    """Base class for all sensor collectors.

    Subclasses must implement:
        poll() -> dict | None
            Returns a dict of {sensor_suffix: value} or None on failure.
        sensors -> list[SensorDef]
            Property returning sensor definitions.
        device -> DeviceInfo
            Property returning device info for HA discovery.
        client_id -> str
            Property returning MQTT client ID.
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
    def client_id(self) -> str:
        """MQTT client ID."""

    @abstractmethod
    def poll(self) -> dict | None:
        """Poll sensors. Return {suffix: value} dict, or None on failure."""

    @property
    def state_topic(self) -> str:
        return f"sensors2mqtt/{self.device.node_id}/state"

    @property
    def avail_topic(self) -> str:
        return f"sensors2mqtt/{self.device.node_id}/status"

    def run(self) -> None:
        """Main entry point: connect MQTT, poll in a loop, handle signals."""
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        client.username_pw_set(self.config.user, self.config.password)

        log.info("Connecting to MQTT %s:%d", self.config.host, self.config.port)
        client.connect(self.config.host, self.config.port, keepalive=120)
        client.loop_start()

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
