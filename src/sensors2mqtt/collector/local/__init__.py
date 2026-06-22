"""Local sensor collector: auto-detects hardware and publishes sensor data.

Runs directly on the target device (RPi, Mellanox switch, etc.).
Uses sysfs, /proc, and optional tools (vcgencmd, sensors) to collect data.

Usage:
    python -m sensors2mqtt.collector.local
"""

from sensors2mqtt.collector.local.base import LocalCollector, LocalSensor

__all__ = ["LocalCollector", "LocalSensor", "auto_detect"]


def auto_detect(sysfs_root: str = "/") -> type[LocalCollector]:
    """Detect hardware and return the appropriate collector class.

    Checks /proc/device-tree/model first (RPi, HiFive, etc.),
    then hwmon driver names (Mellanox), falls back to generic LocalCollector.
    """
    import logging
    from pathlib import Path

    log = logging.getLogger(__name__)
    root = Path(sysfs_root)

    # Check device-tree model (RPi, HiFive, etc.)
    model_path = root / "proc/device-tree/model"
    if model_path.exists():
        try:
            model = model_path.read_text().rstrip("\x00").strip()
            if "Raspberry Pi" in model:
                from sensors2mqtt.collector.local.rpi import RpiCollector

                log.info("Auto-detected Raspberry Pi: %s", model)
                return RpiCollector
        except OSError:
            pass

    # Check for Mellanox ASIC hwmon driver (name contains "mlxsw")
    from sensors2mqtt.collector.local.hwmon import iter_hwmon

    for hwmon in iter_hwmon(root / "sys/class/hwmon"):
        name_file = hwmon / "name"
        try:
            if name_file.exists() and "mlxsw" in name_file.read_text():
                from sensors2mqtt.collector.local.mellanox import MellanoxCollector
                log.info("Auto-detected Mellanox switch (driver: %s)",
                         name_file.read_text().strip())
                return MellanoxCollector
        except OSError:
            pass

    log.info("No specific hardware detected, using generic LocalCollector")
    return LocalCollector
