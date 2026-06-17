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
    def module(self):
        return "stub"

    def poll(self):
        self.poll_count += 1
        return self._poll_values


class TestBasePublisher:
    @patch("sensors2mqtt.base.socket.gethostname", return_value="ten64")
    def test_topics_include_module(self, _gh):
        pub = StubPublisher(config=MqttConfig())
        assert pub.state_topic == "sensors2mqtt/test/stub/state"
        assert pub.avail_topic == "sensors2mqtt/test/stub/status"

    @patch("sensors2mqtt.base.socket.gethostname", return_value="ten64")
    def test_client_id_from_module(self, _gh):
        pub = StubPublisher(config=MqttConfig())
        assert pub.client_id == "sensors2mqtt-ten64-stub"

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
            "sensors2mqtt/test/stub/status", "offline", retain=True,
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
            "sensors2mqtt/test/stub/status", "offline", retain=True,
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
            "sensors2mqtt/test/stub/status", payload="offline", retain=True,
        )

    @patch("sensors2mqtt.base.publish_connection_diagnostic")
    @patch("sensors2mqtt.base.mqtt.Client")
    def test_run_clears_legacy_topics_and_publishes_diagnostic(self, MockClient, mock_diag):
        """run() clears the pre-module retained topics and publishes the per-host
        connection diagnostic on startup."""
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

        # Legacy (pre-module) retained topics cleared with empty payloads.
        mock_instance.publish.assert_any_call("sensors2mqtt/test/state", "", retain=True)
        mock_instance.publish.assert_any_call("sensors2mqtt/test/status", "", retain=True)
        # Per-host connection diagnostic published once with node_id/module/name.
        mock_diag.assert_called_once_with(mock_instance, "test", "stub", "test")


class TestConnectionStatusTopic:
    @patch("sensors2mqtt.base.socket.gethostname")
    def test_topic(self, gethost):
        from sensors2mqtt.base import connection_status_topic
        gethost.return_value = "ten64.welland.mithis.com"
        assert connection_status_topic("snmp") == "sensors2mqtt/ten64/snmp/status"


class TestHostId:
    """host_id() namespaces client-ids and connection status topics per host, so
    multiple daemons of the same kind on different hosts don't collide on a shared
    broker.
    """

    @patch("sensors2mqtt.base.socket.gethostname")
    def test_strips_domain(self, gethost):
        from sensors2mqtt.base import host_id
        gethost.return_value = "ten64.welland.mithis.com"
        assert host_id() == "ten64"

    @patch("sensors2mqtt.base.socket.gethostname")
    def test_dashes_to_underscores(self, gethost):
        from sensors2mqtt.base import host_id
        gethost.return_value = "rpi-sdr-kraken"
        assert host_id() == "rpi_sdr_kraken"


class TestHostName:
    """host_name() is the HA device name; HA derives entity ids from it, so it
    must be the short hostname (domain stripped) to match host_id()."""

    @patch("sensors2mqtt.base.socket.gethostname")
    def test_strips_domain(self, gethost):
        from sensors2mqtt.base import host_name
        gethost.return_value = "ten64.welland.mithis.com"
        assert host_name() == "ten64"

    @patch("sensors2mqtt.base.socket.gethostname")
    def test_keeps_dashes(self, gethost):
        from sensors2mqtt.base import host_name
        gethost.return_value = "rpi5-pmod"
        assert host_name() == "rpi5-pmod"


class TestClientIdFor:
    """client_id_for builds the one consistent connection identity for every
    collector: ``sensors2mqtt-{host}-{module}`` where {host} is host_id(). Two
    daemons of the same kind on different hosts get distinct client-ids, so the
    broker never takes one over for the other.
    """

    @patch("sensors2mqtt.base.socket.gethostname")
    def test_format_uses_short_host_and_module(self, gethost):
        from sensors2mqtt.base import client_id_for
        gethost.return_value = "ten64.welland.mithis.com"
        assert client_id_for("snmp") == "sensors2mqtt-ten64-snmp"

    @patch("sensors2mqtt.base.socket.gethostname")
    def test_compound_module_token(self, gethost):
        from sensors2mqtt.base import client_id_for
        gethost.return_value = "ten64"
        assert client_id_for("snmp_control") == "sensors2mqtt-ten64-snmp_control"


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
