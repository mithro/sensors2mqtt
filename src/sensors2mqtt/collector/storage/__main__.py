"""Storage device collector: drives (keyed by serial) and SES enclosures.

Usage:
    python -m sensors2mqtt.collector.storage
    python -m sensors2mqtt.collector.storage --once
"""

from __future__ import annotations

import argparse
import logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Storage device collector")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(message)s")

    from sensors2mqtt.collector.storage.collector import StorageCollector

    StorageCollector().run(once=args.once)


if __name__ == "__main__":
    main()
