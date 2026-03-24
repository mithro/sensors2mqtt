#!/bin/bash
# Only remove venv on purge, not on upgrade
if [ "$1" = "purge" ]; then
    rm -rf /opt/sensors2mqtt
    echo "Removed /opt/sensors2mqtt"
fi
