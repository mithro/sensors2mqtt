"""Tests for discovery module."""

import json

from sensors2mqtt.discovery import (
    DeviceInfo,
    SensorDef,
    discovery_payload,
    publish_discovery,
    publish_state,
)


def make_device():
    return DeviceInfo(
        node_id="test_device",
        name="test-device",
        manufacturer="TestCo",
        model="T-1000",
    )


def make_sensor(**kwargs):
    defaults = {
        "suffix": "cpu_temp",
        "name": "CPU Temperature",
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
    }
    defaults.update(kwargs)
    return SensorDef(**defaults)


class TestSensorDef:
    def test_defaults(self):
        s = SensorDef(suffix="x", name="x", unit="x")
        assert s.state_class is None  # Default is None — only numeric sensors set it
        assert s.icon is None
        assert s.entity_category is None

    def test_numeric_sensor(self):
        s = make_sensor()
        assert s.state_class == "measurement"

    def test_frozen(self):
        s = make_sensor()
        try:
            s.suffix = "other"
            assert False, "SensorDef should be frozen"
        except AttributeError:
            pass


class TestDiscoveryPayload:
    def test_basic_payload(self):
        sensor = make_sensor()
        device = make_device()
        payload = discovery_payload(
            sensor, device,
            state_topic="sensors2mqtt/test_device/state",
            avail_topic="sensors2mqtt/test_device/status",
        )

        assert payload["name"] == "CPU Temperature"
        assert payload["unique_id"] == "test_device_cpu_temp"
        assert payload["state_topic"] == "sensors2mqtt/test_device/state"
        assert payload["value_template"] == "{{ value_json.cpu_temp }}"
        assert payload["unit_of_measurement"] == "°C"
        assert payload["state_class"] == "measurement"
        assert payload["device_class"] == "temperature"
        assert payload["availability_topic"] == "sensors2mqtt/test_device/status"
        assert payload["payload_available"] == "online"
        assert payload["payload_not_available"] == "offline"
        assert payload["origin"]["name"] == "sensors2mqtt"
        assert "url" in payload["origin"]

    def test_no_state_class_when_none(self):
        sensor = make_sensor(state_class=None)
        payload = discovery_payload(
            sensor, make_device(),
            state_topic="t", avail_topic="a",
        )
        assert "state_class" not in payload

    def test_device_info(self):
        device = make_device()
        payload = discovery_payload(
            make_sensor(), device,
            state_topic="t", avail_topic="a",
        )
        dev = payload["device"]
        assert dev["identifiers"] == ["sensors2mqtt_test_device"]
        assert dev["name"] == "test-device"
        assert dev["manufacturer"] == "TestCo"
        assert dev["model"] == "T-1000"
        assert "configuration_url" not in dev

    def test_device_with_config_url(self):
        device = DeviceInfo(
            node_id="x", name="x", manufacturer="x", model="x",
            configuration_url="https://bmc.example.com",
        )
        payload = discovery_payload(make_sensor(), device, state_topic="t", avail_topic="a")
        assert payload["device"]["configuration_url"] == "https://bmc.example.com"

    def test_no_device_class(self):
        sensor = make_sensor(device_class=None)
        payload = discovery_payload(sensor, make_device(), state_topic="t", avail_topic="a")
        assert "device_class" not in payload

    def test_icon_included(self):
        sensor = make_sensor(icon="mdi:fan")
        payload = discovery_payload(sensor, make_device(), state_topic="t", avail_topic="a")
        assert payload["icon"] == "mdi:fan"

    def test_entity_category(self):
        sensor = make_sensor(entity_category="diagnostic")
        payload = discovery_payload(sensor, make_device(), state_topic="t", avail_topic="a")
        assert payload["entity_category"] == "diagnostic"


class TestPublishDiscovery:
    def test_publishes_all_sensors(self, mock_mqtt_client):
        sensors = [
            make_sensor(suffix="temp1", name="Temp 1"),
            make_sensor(suffix="temp2", name="Temp 2"),
            make_sensor(suffix="fan1", name="Fan 1", device_class=None, unit="RPM"),
        ]
        count = publish_discovery(
            mock_mqtt_client, sensors, make_device(),
            state_topic="sensors2mqtt/test_device/state",
            avail_topic="sensors2mqtt/test_device/status",
        )
        assert count == 3
        assert mock_mqtt_client.publish.call_count == 3

        # Check topic format
        topics = [call["topic"] for call in mock_mqtt_client.published]
        assert "homeassistant/sensor/test_device/temp1/config" in topics
        assert "homeassistant/sensor/test_device/temp2/config" in topics
        assert "homeassistant/sensor/test_device/fan1/config" in topics

        # All retained
        assert all(call["retain"] for call in mock_mqtt_client.published)

        # Payloads are valid JSON
        for call in mock_mqtt_client.published:
            parsed = json.loads(call["payload"])
            assert "name" in parsed
            assert "unique_id" in parsed


class TestPublishState:
    def test_publishes_json(self, mock_mqtt_client):
        values = {"cpu_temp": 42.5, "fan1_rpm": 3200}
        publish_state(
            mock_mqtt_client,
            state_topic="sensors2mqtt/test_device/state",
            values=values,
        )
        assert mock_mqtt_client.publish.call_count == 1
        call = mock_mqtt_client.published[0]
        assert call["topic"] == "sensors2mqtt/test_device/state"
        assert call["retain"] is True
        assert json.loads(call["payload"]) == {"cpu_temp": 42.5, "fan1_rpm": 3200}


class TestAvailabilityConfig:
    """availability_config builds HA availability from one or more topics.

    Multi-device collectors (one MQTT connection, many switches) add a per-
    collector bridge topic so the bridge Last-Will marks every entity
    unavailable if the collector dies, while a single unreachable device still
    marks only its own entities unavailable.
    """

    def test_single_topic_uses_availability_topic(self):
        from sensors2mqtt.discovery import availability_config
        assert availability_config("sensors2mqtt/x/status") == {
            "availability_topic": "sensors2mqtt/x/status",
            "payload_available": "online",
            "payload_not_available": "offline",
        }

    def test_multiple_topics_use_list_mode_all(self):
        from sensors2mqtt.discovery import availability_config
        cfg = availability_config("sensors2mqtt/x/status", "sensors2mqtt/snmp_bridge/status")
        assert cfg["availability_mode"] == "all"
        assert [a["topic"] for a in cfg["availability"]] == [
            "sensors2mqtt/x/status", "sensors2mqtt/snmp_bridge/status",
        ]
        assert "availability_topic" not in cfg

    def test_none_topics_filtered(self):
        from sensors2mqtt.discovery import availability_config
        cfg = availability_config("sensors2mqtt/x/status", None)
        assert cfg["availability_topic"] == "sensors2mqtt/x/status"
        assert "availability" not in cfg



def test_discovery_payload_emits_expire_after():
    from sensors2mqtt.discovery import EXPIRE_AFTER, DeviceInfo, SensorDef, discovery_payload
    sensor = SensorDef("temp", "Temp", "°C", device_class="temperature")
    device = DeviceInfo(node_id="x", name="x", manufacturer="x", model="x")
    cfg = discovery_payload(sensor, device, "s2m/x/state", "s2m/x/status")
    assert cfg["expire_after"] == EXPIRE_AFTER == 300


def test_publish_connection_diagnostic_shape():
    import json
    from unittest.mock import MagicMock

    from sensors2mqtt.discovery import EXPIRE_AFTER, publish_connection_diagnostic
    client = MagicMock()
    publish_connection_diagnostic(client, "ten64", "snmp", "ten64")
    topic, payload = client.publish.call_args[0][0], client.publish.call_args[0][1]
    assert topic == "homeassistant/binary_sensor/ten64/snmp_connection/config"
    cfg = json.loads(payload)
    assert cfg["state_topic"] == "sensors2mqtt/ten64/snmp/status"
    assert cfg["device_class"] == "connectivity"
    assert cfg["entity_category"] == "diagnostic"
    assert cfg["unique_id"] == "ten64_snmp_connection"
    assert cfg["payload_on"] == "online" and cfg["payload_off"] == "offline"
    assert cfg["expire_after"] == EXPIRE_AFTER
    assert cfg["device"]["identifiers"] == ["sensors2mqtt_ten64"]
    assert cfg["device"]["name"] == "ten64"
    assert "manufacturer" not in cfg["device"]  # identifiers+name only, no clobber


def test_device_dict_omits_unknown_metadata():
    from sensors2mqtt.discovery import DeviceInfo, device_dict
    d = device_dict(DeviceInfo(node_id="x", name="x", manufacturer="Unknown", model="Unknown"))
    assert "manufacturer" not in d and "model" not in d  # generic collector won't clobber
    d2 = device_dict(DeviceInfo(node_id="y", name="y", manufacturer="Supermicro", model="X11DSC+"))
    assert d2["manufacturer"] == "Supermicro" and d2["model"] == "X11DSC+"
