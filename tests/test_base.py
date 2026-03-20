"""Tests for base module."""

import os
import signal
import threading
from unittest.mock import MagicMock, patch

from sensors2mqtt.base import BasePublisher, MqttConfig
from sensors2mqtt.discovery import DeviceInfo, SensorDef


class TestMqttConfig:
    def test_defaults(self):
        config = MqttConfig()
        assert config.host == "ha.welland.mithis.com"
        assert config.port == 1883
        assert config.user == "DVES_USER"
        assert config.password == "DVES_USER"
        assert config.poll_interval == 30

    def test_from_env(self):
        env = {
            "MQTT_HOST": "broker.test",
            "MQTT_PORT": "8883",
            "MQTT_USER": "testuser",
            "MQTT_PASSWORD": "testpass",
            "POLL_INTERVAL": "10",
        }
        with patch.dict(os.environ, env):
            config = MqttConfig.from_env()
        assert config.host == "broker.test"
        assert config.port == 8883
        assert config.user == "testuser"
        assert config.password == "testpass"
        assert config.poll_interval == 10

    def test_from_env_partial(self):
        """Missing env vars use defaults."""
        with patch.dict(os.environ, {"MQTT_HOST": "custom.host"}, clear=False):
            config = MqttConfig.from_env()
        assert config.host == "custom.host"
        assert config.port == 1883


# Concrete subclass for testing BasePublisher
class StubPublisher(BasePublisher):
    def __init__(self, poll_values=None, **kwargs):
        super().__init__(**kwargs)
        self._poll_values = poll_values
        self.poll_count = 0

    @property
    def sensors(self):
        return [
            SensorDef(suffix="temp", name="Temperature", unit="°C", device_class="temperature"),
        ]

    @property
    def device(self):
        return DeviceInfo(node_id="test", name="test", manufacturer="Test", model="T1")

    @property
    def client_id(self):
        return "test-publisher"

    def poll(self):
        self.poll_count += 1
        return self._poll_values


class TestBasePublisher:
    def test_topics(self):
        pub = StubPublisher(config=MqttConfig())
        assert pub.state_topic == "sensors2mqtt/test/state"
        assert pub.avail_topic == "sensors2mqtt/test/status"

    @patch("sensors2mqtt.base.mqtt.Client")
    def test_poll_once_success(self, MockClient):
        mock_client = MagicMock()
        pub = StubPublisher(poll_values={"temp": 42.0}, config=MqttConfig())

        pub._poll_once(mock_client)

        assert pub.poll_count == 1
        assert pub._discovery_published is True
        # Discovery + state + availability
        assert mock_client.publish.call_count >= 2

    @patch("sensors2mqtt.base.mqtt.Client")
    def test_poll_once_failure(self, MockClient):
        mock_client = MagicMock()
        pub = StubPublisher(poll_values=None, config=MqttConfig())

        pub._poll_once(mock_client)

        assert pub.poll_count == 1
        assert pub._discovery_published is False
        # Only offline availability published
        mock_client.publish.assert_called_once_with(
            "sensors2mqtt/test/status", "offline", retain=True,
        )

    @patch("sensors2mqtt.base.mqtt.Client")
    def test_discovery_published_once(self, MockClient):
        mock_client = MagicMock()
        pub = StubPublisher(poll_values={"temp": 42.0}, config=MqttConfig())

        pub._poll_once(mock_client)
        first_count = mock_client.publish.call_count

        pub._poll_once(mock_client)
        # Second poll should have fewer publishes (no discovery)
        second_count = mock_client.publish.call_count - first_count
        assert second_count < first_count

    def test_signal_handler_sets_stop(self):
        pub = StubPublisher(config=MqttConfig())
        assert not pub._stop_event.is_set()
        pub._signal_handler(signal.SIGTERM, None)
        assert pub._stop_event.is_set()

    @patch("sensors2mqtt.base.mqtt.Client")
    def test_run_stops_on_signal(self, MockClient):
        """run() exits when stop event is set."""
        mock_instance = MagicMock()
        MockClient.return_value = mock_instance

        pub = StubPublisher(poll_values={"temp": 42.0}, config=MqttConfig(poll_interval=60))

        # Set stop event after a short delay to let run() start
        def stop_after_delay():
            # Wait for at least one poll
            while pub.poll_count < 1:
                pass
            pub._stop_event.set()

        t = threading.Thread(target=stop_after_delay)
        t.start()
        pub.run()
        t.join(timeout=5)

        assert pub.poll_count >= 1
        # Verify cleanup: offline + disconnect
        mock_instance.publish.assert_any_call(
            "sensors2mqtt/test/status", "offline", retain=True,
        )
        mock_instance.disconnect.assert_called_once()
        mock_instance.loop_stop.assert_called_once()
