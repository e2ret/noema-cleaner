"""Запуск NOEMA Cleaner локально: фальшивый /config и /data, заглушка Docker и Supervisor."""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "noema_cleaner", "app"))
sys.path.insert(0, HERE)

import main  # noqa: E402
from fake_docker import MB, FakeDocker  # noqa: E402

root = tempfile.mkdtemp(prefix="noema_dev_")
config = os.path.join(root, "config")
data = os.path.join(root, "data")
for d in (config, os.path.join(config, "zigbee2mqtt", "log"), data):
    os.makedirs(d)


def blob(path, mb, age_days=0):
    with open(path, "wb") as f:
        f.truncate(mb * MB)
    if age_days:
        t = os.path.getmtime(path) - age_days * 86400
        os.utime(path, (t, t))


blob(os.path.join(config, "home-assistant.log.1"), 12)
blob(os.path.join(config, "callgrind.out.123"), 30)
blob(os.path.join(config, "zigbee2mqtt", "log", "old.log"), 5, age_days=30)
blob(os.path.join(config, "home-assistant_v2.db"), 200)

main.CONFIG = config
main.HISTORY_FILE = os.path.join(data, "history.json")
main.MQTT_ENABLED = os.environ.get("MQTT_HOST") is not None

docker = FakeDocker()
main.get_client = lambda: docker


def fake_supervisor(method, path, body=None, timeout=300):
    print(f"[supervisor] {method} {path} {body or ''}")
    if path == "/services/mqtt":
        host = os.environ.get("MQTT_HOST", "localhost")
        return json.dumps({"data": {"host": host, "port": int(os.environ.get("MQTT_PORT", 1883))}}).encode()
    return b"{}"


main.supervisor = fake_supervisor

print(f"Данные заглушки: {root}")
print("Открыть: http://localhost:8099")
main.main()
