"""Tests for the storage device collector's payloads and publishing."""

import json
from dataclasses import replace
from pathlib import Path

from sensors2mqtt.base import MqttConfig
from sensors2mqtt.collector.storage import collector as coll
from sensors2mqtt.collector.storage.drives import DiskStats, Drive, Enclosure, Slot
from sensors2mqtt.collector.storage.smartd import parse_smartd_json

FIXTURES = Path(__file__).parent / "fixtures/storage/smartd"


def drive(**kw):
    d = Drive(serial="ZHZ598DY", name="sdn", transport="SATA", vendor=None,
              model="ST12000NM0008-2H3101", firmware="SN03", capacity_bytes=12 * 10**12,
              logical_block_size=512, rotational=True, enclosure="0:0:29:0",
              enclosure_model="SAS3x48Front", slot=16, slot_status="OK",
              used_by="storage-big/space-3", phy={"link_rate": "12.0 Gbit",
                                                  "invalid_dwords": 1})
    return replace(d, **kw)


def test_drive_device_is_keyed_by_serial():
    pub = coll.build_drive(drive(), "big_storage", "big-storage", None, None, 0.0, None)
    dev = pub.device
    assert dev.node_id == "disk_zhz598dy"
    assert dev.via_device == "sensors2mqtt_big_storage"
    assert (dev.manufacturer, dev.sw_version, dev.serial_number) == (
        "Seagate", "SN03", "ZHZ598DY")
    v = pub.values
    assert v["location"] == "SAS3x48Front slot 16"
    assert v["host"] == "big-storage"
    assert v["link_rate"] == "12.0 Gbit" and v["link_invalid_dwords"] == 1
    assert v["smart_status"] == "not monitored"


def test_rates_between_polls():
    s0 = DiskStats(reads=100, sectors_read=2000, writes=10, sectors_written=0,
                   in_flight=0, io_ms=0)
    s1 = DiskStats(reads=200, sectors_read=2000 + 20000, writes=30, sectors_written=4000,
                   in_flight=2, io_ms=5000)
    pub = coll.build_drive(drive(), "h", "h", s1, (0.0, s0), 10.0, None)
    v = pub.values
    assert v["read_iops"] == 10.0 and v["write_iops"] == 2.0
    assert v["read_rate"] == round(20000 * 512 / 1e6 / 10, 3)
    assert v["utilization"] == 50.0
    assert v["io_in_flight"] == 2


def test_smart_values_are_merged():
    doc = json.loads(next(FIXTURES.glob("*SEAGATE-ST10000NM0226*")).read_text())
    smart = parse_smartd_json(doc)
    pub = coll.build_drive(drive(serial=smart.serial, transport="SAS", vendor="SEAGATE",
                                 model="ST10000NM0226", firmware="KTB5"),
                           "h", "h", None, None, 0.0, smart)
    assert pub.values["power_on_hours"] == smart.values["power_on_hours"]
    assert pub.values["smart_status"] == "PASSED"


def test_enclosure_slot_states():
    e = Enclosure(id="0:0:29:0", logical_id="0x500304801f06a3ff", vendor="LSI-F",
                  model="SAS3x48Front", slots=[
                      Slot(0, "not installed", False, False, None),
                      Slot(1, "OK", False, False, None),
                      Slot(2, "OK", True, False, "ZHZ598DY")])
    pub = coll.build_enclosure(e, "big_storage", "big-storage", {"ZHZ598DY"})
    v = pub.values
    assert pub.device.node_id == "encl_0x500304801f06a3ff"
    assert (v["slot_00_state"], v["slot_01_state"], v["slot_02_state"]) == (
        "empty", "phantom", "occupied")
    assert (v["slots_empty"], v["slots_phantom"], v["slots_occupied"], v["slots_fault"]) == (
        1, 1, 1, 1)


def configs(client):
    return {m["topic"]: m["payload"] for m in client.published
            if m["topic"].startswith("homeassistant/")}


def test_publish_discovery_once_then_on_change(mock_mqtt_client, monkeypatch):
    # connection_status_topic() (base.py) derives the host from the hostname too
    monkeypatch.setattr("socket.gethostname", lambda: "big-storage.welland.mithis.com")
    c = coll.StorageCollector(config=MqttConfig())
    pub = coll.build_drive(drive(), "big_storage", "big-storage", None, None, 0.0, None)

    c.publish(mock_mqtt_client, [pub])
    first = configs(mock_mqtt_client)
    cfg = json.loads(first["homeassistant/sensor/disk_zhz598dy/location/config"])
    assert cfg["unique_id"] == "disk_zhz598dy_location"
    assert cfg["state_topic"] == "sensors2mqtt/disk_zhz598dy/storage/state"
    # available only while the drive is here AND the collector is connected
    assert [a["topic"] for a in cfg["availability"]] == [
        "sensors2mqtt/disk_zhz598dy/storage/status", "sensors2mqtt/big_storage/storage/status"]
    assert cfg["device"]["serial_number"] == "ZHZ598DY"
    assert cfg["default_entity_id"] == "sensor.disk_zhz598dy_location"

    mock_mqtt_client.published.clear()
    c.publish(mock_mqtt_client, [pub])
    assert configs(mock_mqtt_client) == {}  # nothing new to discover

    # The drive moved host: its device changes, so discovery is re-sent
    moved = coll.build_drive(drive(), "other_host", "other-host", None, None, 0.0, None)
    mock_mqtt_client.published.clear()
    c.publish(mock_mqtt_client, [moved])
    cfg = json.loads(configs(mock_mqtt_client)[
        "homeassistant/sensor/disk_zhz598dy/location/config"])
    assert cfg["device"]["via_device"] == "sensors2mqtt_other_host"


def test_removed_drive_goes_offline(mock_mqtt_client, monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "h")
    c = coll.StorageCollector(config=MqttConfig())
    c.publish(mock_mqtt_client, [coll.build_drive(drive(), "h", "h", None, None, 0.0, None)])
    mock_mqtt_client.published.clear()
    c.publish(mock_mqtt_client, [])
    status = [m for m in mock_mqtt_client.published
              if m["topic"] == "sensors2mqtt/disk_zhz598dy/storage/status"]
    assert status == [{"topic": "sensors2mqtt/disk_zhz598dy/storage/status",
                       "payload": "offline", "retain": True}]


def test_vanished_value_is_published_as_null(mock_mqtt_client, monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "h")
    c = coll.StorageCollector(config=MqttConfig())
    c.publish(mock_mqtt_client, [coll.build_drive(drive(), "h", "h", None, None, 0.0, None)])
    gone = coll.build_drive(drive(phy={}), "h", "h", None, None, 0.0, None)
    mock_mqtt_client.published.clear()
    c.publish(mock_mqtt_client, [gone])
    (state,) = [json.loads(m["payload"]) for m in mock_mqtt_client.published
                if m["topic"] == "sensors2mqtt/disk_zhz598dy/storage/state"]
    assert state["link_rate"] is None and state["link_invalid_dwords"] is None
    assert state["location"] == "SAS3x48Front slot 16"


def test_device_info_ignores_smart_data():
    doc = json.loads(next(FIXTURES.glob("*SEAGATE-ST10000NM0226*")).read_text())
    smart = parse_smartd_json(doc)
    d = drive(serial=smart.serial, model="ST10000NM0226", firmware="KTB5")
    with_smart = coll.build_drive(d, "h", "h", None, None, 0.0, smart).device
    without = coll.build_drive(d, "h", "h", None, None, 0.0, None).device
    assert with_smart == without
