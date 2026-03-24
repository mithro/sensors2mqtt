"""Entry point for the local sensor collector.

Auto-detects hardware and runs the appropriate collector.

Usage:
    python -m sensors2mqtt.collector.local
    python -m sensors2mqtt.collector.local --once
    python -m sensors2mqtt.collector.local --hardware rpi
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Local sensor collector")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument("--config", type=Path, help="Path to local.toml config file")
    parser.add_argument(
        "--hardware",
        choices=["rpi", "mellanox", "auto"],
        default="auto",
        help="Hardware type (default: auto-detect)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # Select collector class
    if args.hardware == "auto":
        from sensors2mqtt.collector.local import auto_detect

        collector_cls = auto_detect()
    elif args.hardware == "rpi":
        from sensors2mqtt.collector.local.rpi import RpiCollector

        collector_cls = RpiCollector
    elif args.hardware == "mellanox":
        from sensors2mqtt.collector.local.mellanox import MellanoxCollector

        collector_cls = MellanoxCollector
    else:
        from sensors2mqtt.collector.local.base import LocalCollector

        collector_cls = LocalCollector

    collector = collector_cls(config_path=args.config)

    if args.once:
        import paho.mqtt.client as mqtt

        from sensors2mqtt.base import MqttConfig
        from sensors2mqtt.discovery import publish_discovery, publish_state

        config = MqttConfig.from_env()
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=collector.client_id
        )
        client.username_pw_set(config.user, config.password)
        client.connect(config.host, config.port, keepalive=120)
        client.loop_start()

        values = collector.poll()
        if values:
            publish_discovery(
                client,
                collector.sensors,
                collector.device,
                collector.state_topic,
                collector.avail_topic,
            )
            publish_state(client, collector.state_topic, values)
            client.publish(collector.avail_topic, "online", retain=True)
            collector._log_summary(values)
        else:
            logging.warning("No sensor data")

        client.disconnect()
        client.loop_stop()
    else:
        collector.run()


if __name__ == "__main__":
    main()
