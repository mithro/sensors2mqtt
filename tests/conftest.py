"""Shared test fixtures."""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_mqtt_client():
    """Mock paho-mqtt client that records published messages."""
    client = MagicMock()
    client.published = []

    def record_publish(topic, payload, retain=False):
        client.published.append({
            "topic": topic,
            "payload": payload,
            "retain": retain,
        })

    client.publish.side_effect = record_publish
    return client
