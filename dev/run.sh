#!/bin/sh
# Локальный запуск без Home Assistant: http://localhost:8099
# Для проверки MQTT: MQTT_HOST=localhost sh dev/run.sh (брокер на порту 1883)
cd "$(dirname "$0")" && exec python3 -u run.py
