"""Tests for auto-detection logic."""

from pathlib import Path

from sensors2mqtt.collector.local import auto_detect
from sensors2mqtt.collector.local.base import LocalCollector
from sensors2mqtt.collector.local.mellanox import MellanoxCollector
from sensors2mqtt.collector.local.rpi import RpiCollector

FIXTURES = Path(__file__).parent / "fixtures"


class TestAutoDetect:
    def test_detects_rpi5(self):
        cls = auto_detect(sysfs_root=str(FIXTURES / "rpi5_sysfs"))
        assert cls is RpiCollector

    def test_detects_rpi4(self):
        cls = auto_detect(sysfs_root=str(FIXTURES / "rpi4_sysfs"))
        assert cls is RpiCollector

    def test_detects_rpi3(self):
        cls = auto_detect(sysfs_root=str(FIXTURES / "rpi3_sysfs"))
        assert cls is RpiCollector

    def test_detects_rpizero(self):
        cls = auto_detect(sysfs_root=str(FIXTURES / "rpizero_sysfs"))
        assert cls is RpiCollector

    def test_detects_mellanox(self):
        cls = auto_detect(sysfs_root=str(FIXTURES / "mellanox_sysfs"))
        assert cls is MellanoxCollector

    def test_fallback_to_generic(self, tmp_path):
        """No device-tree, no mlxsw hwmon → generic LocalCollector."""
        cls = auto_detect(sysfs_root=str(tmp_path))
        assert cls is LocalCollector

    def test_empty_device_tree_model(self, tmp_path):
        """device-tree/model exists but is empty → not RPi."""
        model_path = tmp_path / "proc/device-tree/model"
        model_path.parent.mkdir(parents=True)
        model_path.write_text("")
        cls = auto_detect(sysfs_root=str(tmp_path))
        assert cls is LocalCollector

    def test_unknown_device_tree_model(self, tmp_path):
        """device-tree/model is something unknown → generic."""
        model_path = tmp_path / "proc/device-tree/model"
        model_path.parent.mkdir(parents=True)
        model_path.write_text("SiFive HiFive Unmatched A00\x00")
        cls = auto_detect(sysfs_root=str(tmp_path))
        # HiFive is not yet implemented, falls through to hwmon check
        assert cls is LocalCollector
