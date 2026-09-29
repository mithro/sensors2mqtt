"""Read smartd's JSON state files (smartd -j/--jsonstate).

smartd is the only process that sends SMART commands to the drives; this module
only reads the files it writes after each check cycle, e.g.
``/var/lib/smartmontools/smartd-json.HGST_HUH721010ALE600-4DGZ4LBZ.ata.json``.
The schema is smartctl -j's. The file name's model and serial are sanitised
('-' and other characters become '_'), so the serial comes from the file's
``device_info`` string instead.

Every numeric lifetime value becomes a sensor, so Home Assistant's long-term
statistics keep it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sensors2mqtt.discovery import SensorDef

log = logging.getLogger(__name__)

DEFAULT_JSONSTATE_GLOB = "/var/lib/smartmontools/smartd-json.*.json"

# NVMe data units are 1000 512-byte units.
NVME_DATA_UNIT_BYTES = 512 * 1000
GB = 1e9

_SERIAL_RE = re.compile(r"S/N:\s*([^,]+)")
_FW_RE = re.compile(r"FW:\s*([^,]+)")
_LEADING_INT_RE = re.compile(r"^\s*(-?\d+)")


def slug(text: str) -> str:
    """Python/HA-safe identifier fragment: lower case, runs of non-alnum -> '_'."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


@dataclass
class SmartData:
    """What one smartd JSON state file says about one drive."""

    serial: str
    protocol: str  # "ATA", "SCSI", "NVMe" (and "ATA+SCSI" for SAT bridges)
    model: str
    firmware: str | None
    updated: datetime | None
    sensors: list[SensorDef] = field(default_factory=list)
    values: dict = field(default_factory=dict)

    def add(self, sensor: SensorDef, value) -> None:
        if value is None:
            return
        self.sensors.append(sensor)
        self.values[sensor.suffix] = value


def parse_device_info(info: str) -> tuple[str | None, str, str | None]:
    """(serial, model, firmware) from smartd's device_info string.

    ATA/NVMe: ``"HGST HUH721010ALE600, S/N:4DGZ4LBZ, WWN:..., FW:LHGNTB01, 10.0 TB"``
    SCSI:     ``"[SEAGATE  ST10000NM0226    KTB5], lu id: 0x..., S/N: ZA29..., 9.93 TB"``
    """
    m = _SERIAL_RE.search(info)
    serial = m.group(1).strip() if m else None
    if info.startswith("["):
        inner = info[1:info.index("]")] if "]" in info else info[1:]
        parts = inner.split()
        firmware = parts[-1] if len(parts) >= 2 else None
        model = " ".join(parts[:-1]) if len(parts) >= 2 else inner.strip()
    else:
        model = info.split(",", 1)[0].strip()
        fm = _FW_RE.search(info)
        firmware = fm.group(1).strip() if fm else None
    return serial, model, firmware


def _num(value):
    """A JSON number as int/float, or None (smartd writes some as strings)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return None
    return None


def _ata_raw(attr: dict):
    """The attribute's decoded raw value.

    ``raw.string`` is formatted by smartd's drive database (e.g. temperature
    ``"45 (Min/Max 20/59)"`` or power-on time ``"8349h+12m"``), so its leading
    integer is the meaningful value; ``raw.value`` is the packed 48-bit field.
    """
    raw = attr.get("raw") or {}
    m = _LEADING_INT_RE.match(str(raw.get("string", "")))
    if m:
        return int(m.group(1))
    return _num(raw.get("value"))


def _counter(suffix, name, unit="", icon=None, device_class=None, diagnostic=False,
             enabled=True) -> SensorDef:
    """A lifetime counter: only ever grows, so HA sums it correctly."""
    return SensorDef(suffix, name, unit, device_class=device_class,
                     state_class="total_increasing", icon=icon,
                     entity_category="diagnostic" if diagnostic else None,
                     enabled_by_default=enabled)


def _gauge(suffix, name, unit="", icon=None, device_class=None, diagnostic=False,
           enabled=True) -> SensorDef:
    return SensorDef(suffix, name, unit, device_class=device_class,
                     state_class="measurement", icon=icon,
                     entity_category="diagnostic" if diagnostic else None,
                     enabled_by_default=enabled)


def _text(suffix, name, icon=None, diagnostic=True, device_class=None) -> SensorDef:
    return SensorDef(suffix, name, "", device_class=device_class, icon=icon,
                     entity_category="diagnostic" if diagnostic else None)


def _hours(suffix, name) -> SensorDef:
    return _counter(suffix, name, "h", icon="mdi:clock-outline", device_class="duration")


def _data(suffix, name, diagnostic=False) -> SensorDef:
    return _counter(suffix, name, "GB", device_class="data_size", diagnostic=diagnostic)


def _temp(suffix, name, diagnostic=False) -> SensorDef:
    return _gauge(suffix, name, "°C", device_class="temperature", diagnostic=diagnostic)


# ATA attributes that only ever grow: summed as counters. Everything else
# (temperatures, error rates, vendor packed values, and counts that fall when
# sectors are remapped or rewritten, like 197 pending and 198 offline
# uncorrectable) is a measurement: HA would read a fall in a total_increasing
# sensor as a meter reset and add the new value to its long-term sum.
_ATA_COUNTER_IDS = {4, 5, 9, 10, 12, 183, 184, 187, 188, 192, 193, 196, 199,
                    225, 240, 241, 242, 246, 247, 248}
# Device Statistics entries that can fall.
_DEVSTAT_GAUGES = {"Number of Realloc. Candidate Logical Sectors", "Pending Error Count"}
# ATA attribute ids whose raw value is a temperature.
_ATA_TEMP_IDS = {190, 194}


def _parse_ata(d: dict, sd: SmartData, logical_sector_bytes: int) -> None:
    attrs = (d.get("ata_smart_attributes") or {}).get("table") or []
    failing = []
    by_id: dict = {}
    names: dict = {}
    for a in attrs:
        aid, name = a.get("id"), a.get("name", "Unknown_Attribute")
        if aid is None:
            continue
        raw = _ata_raw(a)
        by_id[aid] = raw
        names[aid] = name
        base = f"ata_{aid}_{slug(name)}"
        label = f"SMART {aid} {name}"
        if aid in _ATA_TEMP_IDS:
            sensor = _temp(base, label, diagnostic=True)
        elif aid in _ATA_COUNTER_IDS:
            sensor = _counter(base, label, icon="mdi:counter")
        else:
            sensor = _gauge(base, label, icon="mdi:harddisk")
        sd.add(sensor, raw)
        if "value" in a:
            sd.add(_gauge(f"{base}_value", f"{label} (normalised)", diagnostic=True,
                          enabled=False), a["value"])
        if a.get("when_failed") in ("now", "past"):
            failing.append(f"{name} ({a['when_failed']})")
    sd.add(_text("smart_failing_attributes", "SMART Failing Attributes",
                 icon="mdi:alert", diagnostic=False), ", ".join(failing) or "none")

    devstat = {}
    for page in (d.get("ata_device_statistics") or {}).get("pages") or []:
        for e in page.get("table") or []:
            if "value" not in e or not (e.get("flags") or {}).get("valid", True):
                continue
            name = e.get("name", "Unknown")
            devstat[name] = e["value"]
            s = f"devstat_{slug(name)}"
            if "Temperature" in name and "Time in" not in name:
                sensor = _temp(s, name, diagnostic=True)
            elif "Hours" in name:
                sensor = _hours(s, name)
            elif name in _DEVSTAT_GAUGES:
                sensor = _gauge(s, name, icon="mdi:alert-circle")
            elif page.get("number") == 5 or name.startswith("Date and Time") \
                    or "Workload" in name or "Utilization" in name \
                    or "Percentage" in name or "Resource" in name:
                sensor = _gauge(s, name, "%" if "Percentage" in name else "",
                                icon="mdi:chart-line", diagnostic=True)
            else:
                sensor = _counter(s, name, icon="mdi:counter")
            sd.add(sensor, e["value"])

    # smartd only writes "temperature" when it tracks it (-W, attribute log);
    # otherwise take the drive's own current temperature.
    if "temperature" not in sd.values:
        current = devstat.get("Current Temperature",
                              by_id.get(194, by_id.get(190)))
        sd.add(_temp("temperature", "Temperature"), current)

    # Common cross-protocol lifetime values
    poh = devstat.get("Power-on Hours", by_id.get(9))
    sd.add(_hours("power_on_hours", "Power-on Hours"), poh)
    cycles = devstat.get("Lifetime Power-On Resets", by_id.get(12))
    sd.add(_counter("power_cycles", "Power Cycles", icon="mdi:power-cycle"), cycles)
    # 241/242 count LBAs on most drives, but e.g. Intel SSDs count 32 MiB or GiB
    # units (Host_Writes_32MiB): only trust them when named in LBAs.
    def lbas(aid):
        return by_id.get(aid) if "LBA" in names.get(aid, "") else None

    written = devstat.get("Logical Sectors Written", lbas(241))
    read = devstat.get("Logical Sectors Read", lbas(242))
    if written is not None:
        sd.add(_data("lifetime_written", "Lifetime Written"),
               round(written * logical_sector_bytes / GB, 3))
    if read is not None:
        sd.add(_data("lifetime_read", "Lifetime Read"),
               round(read * logical_sector_bytes / GB, 3))
    sd.add(_counter("reallocated_sectors", "Reallocated Sectors", icon="mdi:alert-circle"),
           by_id.get(5))
    sd.add(_gauge("pending_sectors", "Pending Sectors", icon="mdi:alert-circle"),
           by_id.get(197))
    sd.add(_gauge("offline_uncorrectable", "Offline Uncorrectable Sectors",
                  icon="mdi:alert-circle"), by_id.get(198))
    sd.add(_counter("interface_crc_errors", "Interface CRC Errors", icon="mdi:cable-data"),
           devstat.get("Number of Interface CRC Errors", by_id.get(199)))

    for key in ("summary", "extended"):
        count = ((d.get("ata_smart_error_log") or {}).get(key) or {}).get("count")
        sd.add(_counter("ata_error_log_count", "ATA Error Log Entries",
                        icon="mdi:alert"), count)


_SCSI_ERROR_FIELDS = {
    "errors_corrected_by_eccfast": "Corrected by ECC (fast)",
    "errors_corrected_by_eccdelayed": "Corrected by ECC (delayed)",
    "errors_corrected_by_rereads_rewrites": "Corrected by Retries",
    "total_errors_corrected": "Total Errors Corrected",
    "correction_algorithm_invocations": "Correction Algorithm Invocations",
    "total_uncorrected_errors": "Total Uncorrected Errors",
}

_SAS_PHY_FIELDS = {
    "invalid_dword_count": "Invalid DWORDs",
    "running_disparity_error_count": "Running Disparity Errors",
    "loss_of_dword_synchronization_count": "Loss of DWORD Sync",
    "phy_reset_problem_count": "Phy Reset Problems",
}


def _parse_scsi(d: dict, sd: SmartData) -> None:
    ecl = d.get("scsi_error_counter_log") or {}
    for op in ("read", "write", "verify"):
        page = ecl.get(op) or {}
        for key, label in _SCSI_ERROR_FIELDS.items():
            important = key == "total_uncorrected_errors"
            sd.add(_counter(f"scsi_{op}_{key}", f"{op.title()} {label}",
                            icon="mdi:alert-circle" if important else "mdi:counter",
                            diagnostic=not important), _num(page.get(key)))
        gb = _num(page.get("gigabytes_processed"))
        if gb is not None:
            sd.add(_data(f"scsi_{op}_gigabytes_processed", f"{op.title()} Data Processed",
                         diagnostic=True), gb)
    sd.add(_counter("scsi_non_medium_errors", "Non-medium Errors", icon="mdi:alert-circle"),
           _num((ecl.get("non_medium_error") or {}).get("count")))
    if "read" in ecl:
        sd.add(_data("lifetime_read", "Lifetime Read"),
               _num(ecl["read"].get("gigabytes_processed")))
    if "write" in ecl:
        sd.add(_data("lifetime_written", "Lifetime Written"),
               _num(ecl["write"].get("gigabytes_processed")))

    ss = d.get("scsi_start_stop_cycle_counter") or {}
    if ss.get("year_of_manufacture"):
        sd.add(_text("manufactured", "Manufactured", icon="mdi:factory"),
               f"{ss['year_of_manufacture']}-W{ss.get('week_of_manufacture', '??')}")
    sd.add(_counter("power_cycles", "Power Cycles", icon="mdi:power-cycle"),
           ss.get("accumulated_start_stop_cycles"))
    sd.add(_counter("scsi_start_stop_cycles", "Start-Stop Cycles", icon="mdi:power-cycle"),
           ss.get("accumulated_start_stop_cycles"))
    sd.add(_counter("scsi_load_unload_cycles", "Load-Unload Cycles", icon="mdi:counter"),
           ss.get("accumulated_load_unload_cycles"))
    sd.add(_gauge("scsi_specified_start_stop_cycles", "Rated Start-Stop Cycles",
                  diagnostic=True), ss.get("specified_cycle_count_over_device_lifetime"))
    sd.add(_gauge("scsi_specified_load_unload_cycles", "Rated Load-Unload Cycles",
                  diagnostic=True),
           ss.get("specified_load_unload_count_over_device_lifetime"))

    pot = d.get("power_on_time") or {}
    if "hours" in pot:
        sd.add(_hours("power_on_hours", "Power-on Hours"),
               round(pot["hours"] + pot.get("minutes", 0) / 60, 2))
    sd.add(_counter("scsi_grown_defects", "Grown Defects", icon="mdi:alert-circle"),
           _num(d.get("scsi_grown_defect_list")))
    sd.add(_gauge("scsi_pending_defects", "Pending Defects", icon="mdi:alert-circle"),
           (d.get("scsi_pending_defects") or {}).get("count"))
    bms = (d.get("scsi_background_scan") or {}).get("status") or {}
    sd.add(_counter("scsi_background_scans", "Background Scans", icon="mdi:magnify-scan",
                    diagnostic=True), bms.get("number_scans_performed"))
    sd.add(_counter("scsi_background_medium_scans", "Background Medium Scans",
                    icon="mdi:magnify-scan", diagnostic=True),
           bms.get("number_medium_scans_performed"))

    for key, port in d.items():
        m = re.fullmatch(r"scsi_sas_port_(\d+)", key)
        if not m or not isinstance(port, dict):
            continue
        for phy_key, phy in port.items():
            pm = re.fullmatch(r"phy_(\d+)", phy_key)
            if not pm or not isinstance(phy, dict):
                continue
            base = f"sas_port{m.group(1)}_phy{pm.group(1)}"
            label = f"Drive Port {m.group(1)} Phy {pm.group(1)}"
            sd.add(_text(f"{base}_link_rate", f"{label} Link Rate", icon="mdi:speedometer"),
                   phy.get("negotiated_logical_link_rate"))
            for field_key, field_label in _SAS_PHY_FIELDS.items():
                sd.add(_counter(f"{base}_{slug(field_key)}", f"{label} {field_label}",
                                icon="mdi:cable-data", diagnostic=True),
                       phy.get(field_key))


_NVME_FIELDS = {
    "critical_warning": ("Critical Warning", _gauge, {"icon": "mdi:alert"}),
    "available_spare": ("Available Spare", _gauge, {"unit": "%"}),
    "available_spare_threshold": ("Available Spare Threshold", _gauge,
                                  {"unit": "%", "diagnostic": True}),
    "percentage_used": ("Percentage Used", _gauge, {"unit": "%", "icon": "mdi:gauge"}),
    "host_reads": ("Host Read Commands", _counter, {"icon": "mdi:counter"}),
    "host_writes": ("Host Write Commands", _counter, {"icon": "mdi:counter"}),
    "controller_busy_time": ("Controller Busy Time", _counter,
                             {"unit": "min", "device_class": "duration"}),
    "power_cycles": ("NVMe Power Cycles", _counter, {"diagnostic": True}),
    "unsafe_shutdowns": ("Unsafe Shutdowns", _counter, {"icon": "mdi:power-plug-off"}),
    "media_errors": ("Media Errors", _counter, {"icon": "mdi:alert-circle"}),
    "num_err_log_entries": ("Error Log Entries", _counter, {"icon": "mdi:alert"}),
    "warning_temp_time": ("Time Above Warning Temperature", _counter,
                          {"unit": "min", "device_class": "duration"}),
    "critical_comp_time": ("Time Above Critical Temperature", _counter,
                           {"unit": "min", "device_class": "duration"}),
}


def _parse_nvme(d: dict, sd: SmartData) -> None:
    h = d.get("nvme_smart_health_information_log") or {}
    # smartd writes the top-level "temperature" only when it tracks it (-W).
    if "temperature" not in sd.values:
        sd.add(_temp("temperature", "Temperature"), _num(h.get("temperature")))
    for key, (label, kind, kw) in _NVME_FIELDS.items():
        sd.add(kind(f"nvme_{key}", label, **kw), _num(h.get(key)))
    for key, suffix, label in (("data_units_read", "lifetime_read", "Lifetime Read"),
                               ("data_units_written", "lifetime_written", "Lifetime Written")):
        units = _num(h.get(key))
        if units is not None:
            sd.add(_data(suffix, label), round(units * NVME_DATA_UNIT_BYTES / GB, 3))
    sd.add(_hours("power_on_hours", "Power-on Hours"), _num(h.get("power_on_hours")))
    sd.add(_counter("power_cycles", "Power Cycles", icon="mdi:power-cycle"),
           _num(h.get("power_cycles")))
    for i, t in enumerate(h.get("temperature_sensors") or []):
        if t is not None:
            sd.add(_temp(f"nvme_temperature_sensor_{i + 1}",
                         f"Temperature Sensor {i + 1}", diagnostic=True), t)


def parse_smartd_json(d: dict, logical_sector_bytes: int = 512) -> SmartData | None:
    """Turn one smartd JSON state document into sensors and values."""
    serial, model, firmware = parse_device_info(str(d.get("device_info", "")))
    if not serial:
        return None
    protocol = str((d.get("device") or {}).get("protocol", ""))
    t = (d.get("local_time") or {}).get("time_t")
    updated = datetime.fromtimestamp(t, tz=timezone.utc) if isinstance(t, int) else None
    sd = SmartData(serial=serial, protocol=protocol, model=model, firmware=firmware,
                   updated=updated)

    passed = (d.get("smart_status") or {}).get("passed")
    if passed is not None:
        sd.add(_text("smart_status", "SMART Status", icon="mdi:heart-pulse",
                     diagnostic=False), "PASSED" if passed else "FAILED")
    if updated is not None:
        sd.add(_text("smart_updated", "SMART Updated", device_class="timestamp",
                     icon="mdi:update"), updated.isoformat())
    temp = d.get("temperature") or {}
    sd.add(_temp("temperature", "Temperature"), _num(temp.get("current")))
    sd.add(_temp("temperature_lifetime_max", "Lifetime Maximum Temperature", diagnostic=True),
           _num(temp.get("lifetime_max")))
    sd.add(_temp("temperature_lifetime_min", "Lifetime Minimum Temperature", diagnostic=True),
           _num(temp.get("lifetime_min")))
    sd.add(_counter("self_test_errors", "Self-test Errors", icon="mdi:alert"),
           (d.get("smartd_self_test_errors") or {}).get("count"))

    if "ATA" in protocol or "ata_smart_attributes" in d:
        _parse_ata(d, sd, logical_sector_bytes)
    elif protocol == "NVMe" or "nvme_smart_health_information_log" in d:
        _parse_nvme(d, sd)
    else:
        _parse_scsi(d, sd)

    # A key added twice (e.g. two sources for one value) keeps the first sensor
    # definition and the last value; drop the duplicate definitions.
    seen: set[str] = set()
    sd.sensors = [s for s in sd.sensors if not (s.suffix in seen or seen.add(s.suffix))]
    return sd


def load_smartd_states(pattern: str = DEFAULT_JSONSTATE_GLOB,
                       sector_bytes: dict[str, int] | None = None) -> dict[str, SmartData]:
    """Read every smartd JSON state file; return {serial: SmartData}.

    ``sector_bytes`` maps serial -> logical sector size (for ATA sector counts).
    A file that can't be read or parsed is logged and skipped; if two files name
    the same serial, the most recently updated one wins.
    """
    sector_bytes = sector_bytes or {}
    out: dict[str, SmartData] = {}
    base = Path(pattern).parent
    for path in sorted(base.glob(Path(pattern).name)):
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            log.warning("Cannot read smartd JSON state %s: %s", path, e)
            continue
        serial, _, _ = parse_device_info(str(doc.get("device_info", "")))
        sd = parse_smartd_json(doc, sector_bytes.get(serial or "", 512))
        if sd is None:
            log.warning("No serial number in %s", path)
            continue
        prev = out.get(sd.serial)
        if prev is None or (sd.updated and prev.updated and sd.updated > prev.updated):
            out[sd.serial] = sd
    return out
