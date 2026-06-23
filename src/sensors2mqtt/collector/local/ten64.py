"""Traverse Ten64 collector: device identity only.

All board sensors come from the generic hwmon engine in the base collector via
the ten64 registry overrides (pac1934/emc1704/emc1813/emc2301). This subclass
only supplies the manufacturer/model the engine cannot infer.
"""
from __future__ import annotations

import logging

from sensors2mqtt.collector.local.base import LocalCollector

log = logging.getLogger(__name__)


class Ten64Collector(LocalCollector):
    """Traverse Ten64 (NXP LS1088A): identity only; sensors via the base engine."""

    def _manufacturer(self) -> str:
        return "Traverse Technologies"

    def _model(self) -> str:
        return "Ten64"

    def _log_summary(self, values: dict) -> None:
        log.info(
            "Published: CPU=%s°C Board=%s°C 12V=%sV Fan=%sRPM",
            values.get("cpu_temp", "?"), values.get("board_temp", "?"),
            values.get("supply_voltage", "?"), values.get("fan_rpm", "?"),
        )
