#!/bin/bash
set -e
systemctl stop sensors2mqtt-local || true
systemctl disable sensors2mqtt-local || true
systemctl daemon-reload
