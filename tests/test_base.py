"""Tests for base module."""

import logging
import os
import signal
import threading
from unittest.mock import MagicMock, patch

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from sensors2mqtt.base import BasePublisher, MqttConfig, make_client
from sensors2mqtt.discovery import DeviceInfo, SensorDef


class TestMqttConfig:
    def test_defaults(self):
        config = MqttConfig()
        assert config.host == "localhost"
        assert config.port == 1883
        assert config.user == ""
        assert config.password == ""
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

    @patch("sensors2mqtt.base.mqtt.Client")
    def test_run_sets_will_to_avail_topic(self, MockClient):
        """run() registers a Last-Will on the availability topic before connect."""
        mock_instance = MagicMock()
        MockClient.return_value = mock_instance

        pub = StubPublisher(poll_values={"temp": 42.0}, config=MqttConfig(poll_interval=60))

        def stop_after_delay():
            while pub.poll_count < 1:
                pass
            pub._stop_event.set()

        t = threading.Thread(target=stop_after_delay)
        t.start()
        pub.run()
        t.join(timeout=5)

        mock_instance.will_set.assert_called_once_with(
            "sensors2mqtt/test/status", payload="offline", retain=True,
        )


class TestMakeClient:
    """make_client attaches connection-visibility callbacks.

    paho is silent about refused connections — a failed CONNACK means QoS-0
    publishes are silently dropped while the collector looks healthy (observed
    live when the broker started refusing anonymous connections).
    """

    def _client(self):
        return make_client(MqttConfig(user="u", password="p"), "test-client")

    def test_attaches_all_connection_callbacks(self):
        client = self._client()
        assert client.on_connect is not None
        assert client.on_connect_fail is not None
        assert client.on_disconnect is not None

    def test_connect_refused_logs_error(self, caplog):
        client = self._client()
        rc = ReasonCode(PacketTypes.CONNACK, "Not authorized")
        with caplog.at_level(logging.ERROR, logger="sensors2mqtt.base"):
            client.on_connect(client, None, {}, rc, None)
        assert any(
            "refused" in r.getMessage() and "Not authorized" in r.getMessage()
            for r in caplog.records
        )

    def test_connect_success_logs_info_not_warning(self, caplog):
        client = self._client()
        rc = ReasonCode(PacketTypes.CONNACK, "Success")
        with caplog.at_level(logging.INFO, logger="sensors2mqtt.base"):
            client.on_connect(client, None, {}, rc, None)
        assert any("MQTT connected" in r.getMessage() for r in caplog.records)
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_connect_fail_logs_error(self, caplog):
        client = self._client()
        with caplog.at_level(logging.ERROR, logger="sensors2mqtt.base"):
            client.on_connect_fail(client, None)
        assert any("connect" in r.getMessage().lower() for r in caplog.records)

    def test_unexpected_disconnect_logs_warning(self, caplog):
        client = self._client()
        rc = ReasonCode(PacketTypes.DISCONNECT, "Unspecified error")
        with caplog.at_level(logging.WARNING, logger="sensors2mqtt.base"):
            client.on_disconnect(client, None, None, rc, None)
        assert any(
            "disconnected" in r.getMessage().lower() for r in caplog.records
        )

    def test_normal_disconnect_logs_nothing(self, caplog):
        client = self._client()
        rc = ReasonCode(PacketTypes.DISCONNECT, "Normal disconnection")
        with caplog.at_level(logging.WARNING, logger="sensors2mqtt.base"):
            client.on_disconnect(client, None, None, rc, None)
        assert caplog.records == []

    def test_on_connected_invoked_on_successful_connect(self):
        """on_connected runs after a successful CONNACK (every reconnect) so
        subscriptions can be re-established."""
        seen = []
        client = make_client(
            MqttConfig(user="u", password="p"), "test-client",
            on_connected=seen.append,
        )
        client.on_connect(client, None, {}, ReasonCode(PacketTypes.CONNACK, "Success"), None)
        assert seen == [client]

    def test_on_connected_not_invoked_on_failed_connect(self):
        seen = []
        client = make_client(
            MqttConfig(user="u", password="p"), "test-client",
            on_connected=seen.append,
        )
        client.on_connect(
            client, None, {}, ReasonCode(PacketTypes.CONNACK, "Not authorized"), None,
        )
        assert seen == []

    @patch("sensors2mqtt.base.mqtt.Client")
    def test_will_set_when_will_topic_given(self, MockClient):
        """A Last-Will marks availability offline on ungraceful disconnect."""
        inst = MockClient.return_value
        make_client(
            MqttConfig(user="u", password="p"), "c",
            will_topic="sensors2mqtt/x/status",
        )
        inst.will_set.assert_called_once_with(
            "sensors2mqtt/x/status", payload="offline", retain=True,
        )

    @patch("sensors2mqtt.base.mqtt.Client")
    def test_no_will_without_will_topic(self, MockClient):
        inst = MockClient.return_value
        make_client(MqttConfig(user="u", password="p"), "c")
        inst.will_set.assert_not_called()
