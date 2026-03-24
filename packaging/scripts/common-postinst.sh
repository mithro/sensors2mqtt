#!/bin/bash
set -e

INSTALL_DIR=/opt/sensors2mqtt
WHEELS_DIR=/usr/share/sensors2mqtt/wheels
WHL=/usr/share/sensors2mqtt/sensors2mqtt-*.whl

# Create or update venv
if [ ! -d "$INSTALL_DIR" ]; then
    python3 -m venv "$INSTALL_DIR"
fi

# Install sensors2mqtt + dependencies from bundled wheels (offline)
"$INSTALL_DIR/bin/pip" install --no-index \
    --find-links "$WHEELS_DIR" \
    --find-links /usr/share/sensors2mqtt/ \
    --force-reinstall \
    $WHL

echo "sensors2mqtt installed to $INSTALL_DIR"
