#!/bin/bash
set -e
systemctl daemon-reload
# Enable but don't start — user can start manually or reboot
systemctl enable sensors2mqtt-local
echo "sensors2mqtt-local service enabled (start with: systemctl start sensors2mqtt-local)"
