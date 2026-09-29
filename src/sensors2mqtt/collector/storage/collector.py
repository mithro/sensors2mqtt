"""Storage device collector: one Home Assistant device per drive and enclosure.

A drive's device is keyed by its serial number (``disk_<serial>``), not by host,
slot or /dev name, so its entities and long-term statistics follow the drive
when it moves. The host that has it now is the device's ``via_device``.

SMART data comes only from smartd's JSON state files (see smartd.py); the rest
is passive (see drives.py). This collector sends no command to any drive.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt

from sensors2mqtt.base import (
    MqttConfig,
    client_id_for,
    connection_status_topic,
    host_id,
    host_name,
    make_client,
)
from sensors2mqtt.collector.storage.drives import (
    DiskStats,
    Drive,
    Enclosure,
    discover_drives,
    discover_enclosures,
    read_diskstats,
)
from sensors2mqtt.collector.storage.smartd import (
    DEFAULT_JSONSTATE_GLOB,
    GB,
    SmartData,
    load_smartd_states,
    slug,
)
from sensors2mqtt.discovery import (
    DeviceInfo,
    SensorDef,
    publish_connection_diagnostic,
    publish_discovery,
    publish_state,
)

log = logging.getLogger(__name__)

MODULE = "storage"

_MANUFACTURERS = (
    ("HGST", "HGST"), ("HUH", "HGST"), ("WDC", "Western Digital"), ("WD", "Western Digital"),
    ("SEAGATE", "Seagate"), ("ST", "Seagate"), ("OOS", "Seagate (recertified)"),
    ("TOSHIBA", "Toshiba"), ("SAMSUNG", "Samsung"), ("INTEL", "Intel"),
    ("CRUCIAL", "Crucial"), ("CT", "Crucial"), ("MTFD", "Micron"), ("MICRON", "Micron"),
    ("KINGSTON", "Kingston"), ("HITACHI", "Hitachi"),
)


def manufacturer_of(model: str, vendor: str | None) -> str:
    m = model.upper()
    for prefix, name in _MANUFACTURERS:
        if m.startswith(prefix):
            return name
    return vendor or "Unknown"


def drive_node_id(serial: str) -> str:
    return f"disk_{slug(serial)}"


@dataclass
class Published:
    """One HA device this collector publishes: its info, sensors and values."""

    device: DeviceInfo
    sensors: list[SensorDef] = field(default_factory=list)
    values: dict = field(default_factory=dict)

    def add(self, sensor: SensorDef, value) -> None:
        if value is None:
            return
        if sensor.suffix not in self.values:
            self.sensors.append(sensor)
        self.values[sensor.suffix] = value


def _text(suffix, name, icon=None, diagnostic=True) -> SensorDef:
    return SensorDef(suffix, name, "", icon=icon,
                     entity_category="diagnostic" if diagnostic else None)


def _total(suffix, name, unit="", icon=None, device_class=None, diagnostic=False) -> SensorDef:
    return SensorDef(suffix, name, unit, device_class=device_class,
                     state_class="total_increasing", icon=icon,
                     entity_category="diagnostic" if diagnostic else None)


def _gauge(suffix, name, unit="", icon=None, device_class=None, diagnostic=False) -> SensorDef:
    return SensorDef(suffix, name, unit, device_class=device_class,
                     state_class="measurement", icon=icon,
                     entity_category="diagnostic" if diagnostic else None)


def drive_location(drive: Drive) -> str:
    if drive.enclosure is not None:
        return f"{drive.enclosure_model or drive.enclosure} slot {drive.slot}"
    return drive.transport


def build_drive(drive: Drive, host: str, host_display: str, stats: DiskStats | None,
                prev: tuple[float, DiskStats] | None, now: float,
                smart: SmartData | None) -> Published:
    """Everything published for one drive."""
    model = smart.model if smart and smart.model else drive.model
    firmware = drive.firmware or (smart.firmware if smart else None)
    pub = Published(DeviceInfo(
        node_id=drive_node_id(drive.serial),
        name=f"{model} {drive.serial}",
        manufacturer=manufacturer_of(model, drive.vendor),
        model=model,
        via_device=f"sensors2mqtt_{host}",
        sw_version=firmware,
        serial_number=drive.serial,
    ))

    # Where the drive is and what uses it
    pub.add(_text("host", "Host", "mdi:server", diagnostic=False), host_display)
    pub.add(_text("device_name", "Device Name", "mdi:harddisk"), f"/dev/{drive.name}")
    pub.add(_text("location", "Location", "mdi:map-marker", diagnostic=False),
            drive_location(drive))
    pub.add(_text("transport", "Transport", "mdi:connection"), drive.transport)
    pub.add(_text("firmware", "Firmware", "mdi:chip"), firmware)
    pub.add(_text("used_by", "Used By", "mdi:database", diagnostic=False),
            drive.used_by or "unused")
    pub.add(_gauge("capacity", "Capacity", "GB", "mdi:harddisk", "data_size", diagnostic=True),
            round(drive.capacity_bytes / GB, 1))
    if drive.enclosure is not None:
        pub.add(_text("enclosure", "Enclosure", "mdi:server-network"),
                f"{drive.enclosure_model or ''} {drive.enclosure}".strip())
        pub.add(_gauge("slot", "Slot", icon="mdi:numeric", diagnostic=True), drive.slot)
        pub.add(_text("slot_status", "Slot Status", "mdi:list-status"), drive.slot_status)

    # The link as the expander (or HBA) sees it: no command to the drive
    if drive.phy:
        pub.add(_text("link_rate", "Link Rate", "mdi:speedometer", diagnostic=False),
                drive.phy.get("link_rate"))
        for key, label in (("invalid_dwords", "Link Invalid DWORDs"),
                           ("disparity_errors", "Link Disparity Errors"),
                           ("loss_of_dword_sync", "Link Loss of DWORD Sync"),
                           ("phy_reset_problems", "Link Phy Reset Problems")):
            v = drive.phy.get(key)
            if v is not None and v >= 0:
                pub.add(_total(f"link_{key}", label, icon="mdi:cable-data", diagnostic=True), v)

    # I/O since boot (lifetime counters) and rates since the last poll.
    # /proc/diskstats sectors are always 512 bytes.
    if stats is not None:
        pub.add(_total("io_read_ops", "Reads Since Boot", icon="mdi:counter", diagnostic=True),
                stats.reads)
        pub.add(_total("io_write_ops", "Writes Since Boot", icon="mdi:counter",
                       diagnostic=True), stats.writes)
        pub.add(_total("io_read", "Read Since Boot", "GB", device_class="data_size",
                       diagnostic=True), round(stats.sectors_read * 512 / GB, 3))
        pub.add(_total("io_written", "Written Since Boot", "GB", device_class="data_size",
                       diagnostic=True), round(stats.sectors_written * 512 / GB, 3))
        pub.add(_gauge("io_in_flight", "I/O In Flight", icon="mdi:tray-full"),
                stats.in_flight)
        if prev is not None:
            t0, s0 = prev
            dt = now - t0
            if dt > 0 and stats.reads >= s0.reads and stats.writes >= s0.writes:
                mb = 512 / 1e6
                pub.add(_gauge("read_rate", "Read Rate", "MB/s", device_class="data_rate"),
                        round((stats.sectors_read - s0.sectors_read) * mb / dt, 3))
                pub.add(_gauge("write_rate", "Write Rate", "MB/s", device_class="data_rate"),
                        round((stats.sectors_written - s0.sectors_written) * mb / dt, 3))
                pub.add(_gauge("read_iops", "Read IOPS", "IOPS", icon="mdi:speedometer"),
                        round((stats.reads - s0.reads) / dt, 2))
                pub.add(_gauge("write_iops", "Write IOPS", "IOPS", icon="mdi:speedometer"),
                        round((stats.writes - s0.writes) / dt, 2))
                pub.add(_gauge("utilization", "Utilization", "%", icon="mdi:gauge"),
                        round(min(100.0, (stats.io_ms - s0.io_ms) / (dt * 10)), 1))

    if smart is not None:
        for sensor in smart.sensors:
            pub.add(sensor, smart.values[sensor.suffix])
    else:
        pub.add(_text("smart_status", "SMART Status", "mdi:heart-pulse", diagnostic=False),
                "not monitored")
    return pub


def enclosure_node_id(encl: Enclosure) -> str:
    return f"encl_{slug(encl.logical_id)}"


def build_enclosure(encl: Enclosure, host: str, host_display: str,
                    present_serials: set[str]) -> Published:
    """An SES enclosure: each slot's status and the drive in it."""
    model = encl.model or encl.id
    pub = Published(DeviceInfo(
        node_id=enclosure_node_id(encl),
        name=f"{host_display} {model}",
        manufacturer=encl.vendor or "Unknown",
        model=model,
        via_device=f"sensors2mqtt_{host}",
        serial_number=encl.logical_id,
    ))
    counts = {"occupied": 0, "empty": 0, "phantom": 0, "fault": 0}
    for s in encl.slots:
        n = f"{s.number:02d}"
        if s.status == "not installed":
            state, counts["empty"] = "empty", counts["empty"] + 1
        elif not s.serial:
            # The enclosure reports a drive but the host has no device for it.
            state, counts["phantom"] = "phantom", counts["phantom"] + 1
        else:
            state, counts["occupied"] = "occupied", counts["occupied"] + 1
        if s.fault:
            counts["fault"] += 1
        pub.add(_text(f"slot_{n}_state", f"Slot {n} State", "mdi:harddisk", diagnostic=False),
                state)
        pub.add(_text(f"slot_{n}_status", f"Slot {n} SES Status", "mdi:list-status"),
                s.status or "unknown")
        pub.add(_text(f"slot_{n}_drive", f"Slot {n} Drive", "mdi:identifier"),
                s.serial or "none")
        pub.add(_text(f"slot_{n}_fault", f"Slot {n} Fault LED", "mdi:led-on"),
                "on" if s.fault else "off")
    pub.add(_gauge("slots", "Slots", icon="mdi:tray", diagnostic=True), len(encl.slots))
    for key, label in (("occupied", "Occupied Slots"), ("empty", "Empty Slots"),
                       ("phantom", "Phantom Slots"), ("fault", "Faulted Slots")):
        pub.add(_gauge(f"slots_{key}", label, icon="mdi:tray"), counts[key])
    return pub


class StorageCollector:
    """Poll drives and enclosures; publish them to MQTT."""

    def __init__(self, config: MqttConfig | None = None, sysfs_root: str = "/",
                 proc_root: str = "/", jsonstate_glob: str | None = None):
        self.config = config or MqttConfig.from_env()
        self.sysfs_root = sysfs_root
        self.proc_root = proc_root
        self.jsonstate_glob = jsonstate_glob or os.environ.get(
            "SMARTD_JSONSTATE", DEFAULT_JSONSTATE_GLOB)
        self.host = host_id()
        self.host_display = host_name()
        self.connection_topic = connection_status_topic(MODULE)
        self._prev_stats: dict[str, tuple[float, DiskStats]] = {}
        # node_id -> (device info, discovered suffixes) as last published
        self._published: dict[str, tuple[DeviceInfo, set[str]]] = {}
        self._stop_event = threading.Event()

    @staticmethod
    def state_topic(node_id: str) -> str:
        return f"sensors2mqtt/{node_id}/{MODULE}/state"

    @staticmethod
    def status_topic(node_id: str) -> str:
        return f"sensors2mqtt/{node_id}/{MODULE}/status"

    def collect(self) -> list[Published]:
        """One poll: every drive and enclosure, ready to publish."""
        now = time.monotonic()
        drives = discover_drives(self.sysfs_root, self.proc_root)
        stats = read_diskstats(self.proc_root)
        smart = load_smartd_states(
            self.jsonstate_glob, {d.serial: d.logical_block_size for d in drives})
        out = []
        for d in drives:
            s = stats.get(d.name)
            out.append(build_drive(d, self.host, self.host_display, s,
                                   self._prev_stats.get(d.serial), now, smart.get(d.serial)))
            if s is not None:
                self._prev_stats[d.serial] = (now, s)
        present = {d.serial for d in drives}
        for e in discover_enclosures(self.sysfs_root):
            out.append(build_enclosure(e, self.host, self.host_display, present))
        return out

    def publish(self, client: mqtt.Client, items: list[Published]) -> None:
        seen = set()
        for pub in items:
            node = pub.device.node_id
            seen.add(node)
            prev = self._published.get(node)
            if prev is None or prev[0] != pub.device:
                # New, or moved / renamed / new firmware: (re)publish everything
                todo = pub.sensors
                done: set[str] = set()
            else:
                done = prev[1]
                todo = [s for s in pub.sensors if s.suffix not in done]
            if todo:
                publish_discovery(client, todo, pub.device, self.state_topic(node),
                                  self.status_topic(node), self.connection_topic)
            self._published[node] = (pub.device, done | {s.suffix for s in todo})
            publish_state(client, self.state_topic(node), pub.values)
            client.publish(self.status_topic(node), "online", retain=True)
        for node in list(self._published):
            if node not in seen:
                # The drive left this host: its entities go unavailable until
                # whichever host has it now publishes it.
                log.info("%s is no longer present", node)
                client.publish(self.status_topic(node), "offline", retain=True)
                del self._published[node]
        client.publish(self.connection_topic, "online", retain=True)
        drives = sum(1 for p in items if p.device.node_id.startswith("disk_"))
        log.info("Published %d drives and %d enclosures (%d values)", drives,
                 len(items) - drives, sum(len(p.values) for p in items))

    def poll_once(self, client: mqtt.Client) -> None:
        try:
            items = self.collect()
        except Exception:
            log.exception("Storage poll failed")
            return
        self.publish(client, items)

    def run(self, once: bool = False) -> None:
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        client = make_client(self.config, client_id_for(MODULE),
                             will_topic=self.connection_topic)
        log.info("Connecting to MQTT %s:%d", self.config.host, self.config.port)
        client.connect(self.config.host, self.config.port, keepalive=120)
        client.loop_start()
        publish_connection_diagnostic(client, self.host, MODULE, self.host_display)
        try:
            while not self._stop_event.is_set():
                self.poll_once(client)
                if once:
                    break
                self._stop_event.wait(timeout=self.config.poll_interval)
        finally:
            if not once:
                for node in self._published:
                    client.publish(self.status_topic(node), "offline", retain=True)
                client.publish(self.connection_topic, "offline", retain=True)
            client.disconnect()
            client.loop_stop()
            log.info("Disconnected from MQTT")

    def _signal_handler(self, signum, frame):
        log.info("Shutting down (signal %d)", signum)
        self._stop_event.set()
