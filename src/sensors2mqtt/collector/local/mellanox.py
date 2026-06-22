"""Mellanox SN2410 sensor collector.

All sensors come from the generic hwmon engine in the base collector via the
`mlxsw`/`jc42` registry overrides; this subclass only supplies device identity.
"""
from __future__ import annotations

import logging

from sensors2mqtt.collector.local.base import LocalCollector

log = logging.getLogger(__name__)


class MellanoxCollector(LocalCollector):
    """Mellanox SN2410: identity only; sensors via the base hwmon engine."""

    def _manufacturer(self) -> str:
        return "Mellanox"

    def _model(self) -> str:
        return "SN2410"

    def _mac_interfaces(self) -> tuple[str, ...]:
        return ("bmc", "eth0")

    def _log_summary(self, values: dict) -> None:
        log.info(
            "Published: ASIC=%s°C Board=%s°C CPU=%s°C",
            values.get("asic_temp", "?"),
            values.get("board_temp", "?"),
            values.get("cpu_temp", values.get("acpitz_temp", "?")),
        )
