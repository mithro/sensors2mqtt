"""Tests for the local collector entry point (collector/local/__main__.py).

The --once path must build its MQTT client via base.make_client so it gets a
Last-Will and connect-failure logging, matching the daemon path (BasePublisher.run)
and the other collectors' --once paths (snmp, ipmi_sensors). It previously
hand-rolled a bare paho client with raw username_pw_set, no will, and no logging
callbacks.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.local.__main__ import main

ONCE_ARGV = ["sensors2mqtt-local", "--once", "--hardware", "rpi"]


def _mock_collector() -> MagicMock:
    collector = MagicMock()
    collector.client_id = "sensors2mqtt-testhost-local"
    collector.avail_topic = "sensors2mqtt/testhost/local/status"
    collector.state_topic = "sensors2mqtt/testhost/local/state"
    collector.sensors = []
    collector.device = MagicMock()
    collector.poll.return_value = {"cpu_temp": 42.0}
    return collector


class TestLocalOnce:
    """`--once` routes through make_client for LWT + connect-failure parity."""

    @patch("sensors2mqtt.discovery.publish_state")
    @patch("sensors2mqtt.discovery.publish_discovery")
    @patch("paho.mqtt.client.Client")
    @patch("sensors2mqtt.base.make_client")
    @patch("sensors2mqtt.collector.local.rpi.RpiCollector")
    def test_once_builds_client_via_make_client_with_will(
        self, MockRpi, mock_make_client, MockPaho, mock_pub_disc, mock_pub_state
    ):
        MockRpi.return_value = _mock_collector()

        with patch.object(sys, "argv", ONCE_ARGV):
            main()

        mock_make_client.assert_called_once()
        # A bare paho client must NOT be constructed directly.
        MockPaho.assert_not_called()

        call = mock_make_client.call_args
        config_arg, client_id_arg = call.args[0], call.args[1]
        assert isinstance(config_arg, MqttConfig)
        assert client_id_arg == "sensors2mqtt-testhost-local"
        assert call.kwargs["will_topic"] == "sensors2mqtt/testhost/local/status"

    @patch("sensors2mqtt.discovery.publish_state")
    @patch("sensors2mqtt.discovery.publish_discovery")
    @patch("paho.mqtt.client.Client")
    @patch("sensors2mqtt.base.make_client")
    @patch("sensors2mqtt.collector.local.rpi.RpiCollector")
    def test_once_polls_publishes_and_marks_online_on_make_client_client(
        self, MockRpi, mock_make_client, MockPaho, mock_pub_disc, mock_pub_state
    ):
        collector = _mock_collector()
        MockRpi.return_value = collector
        client = mock_make_client.return_value

        with patch.object(sys, "argv", ONCE_ARGV):
            main()

        # Discovery + state published, online flag set — all on the make_client
        # client (the lifecycle must use the returned client, not a bare one).
        mock_pub_disc.assert_called_once()
        mock_pub_state.assert_called_once_with(
            client, collector.state_topic, {"cpu_temp": 42.0}
        )
        client.publish.assert_any_call(collector.avail_topic, "online", retain=True)
        client.connect.assert_called_once()
        client.loop_start.assert_called_once()
        client.disconnect.assert_called_once()
        client.loop_stop.assert_called_once()
