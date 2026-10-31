"""NOEMA Cleaner — поиск и удаление кэшей и временных файлов в Home Assistant OS."""

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import time
import urllib.request
from datetime import datetime, timezone

import docker
from aiohttp import web

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cleaner")

CONFIG = "/config"
MIN_SIZE = 1024 * 1024  # мельче 1 МБ не показываем
INGRESS_IPS = {"172.30.32.2", "127.0.0.1"}


def load_options():
    try:
        with open("/data/options.json") as f:
            return json.load(f)
    except Exception:
        return {}


OPTS = load_options()
AUTO = bool(OPTS.get("auto_clean", False))
INTERVAL_H = int(OPTS.get("interval_hours", 168))
LOG_DAYS = int(OPTS.get("log_max_age_days", 7))
KEEP_VERSIONS = max(1, int(OPTS.get("keep_esphome_versions", 1)))
MQTT_ENABLED = bool(OPTS.get("mqtt", True))
SCAN_MIN = max(5, int(OPTS.get("scan_interval_minutes", 60)))
VERSION = "0.4.1"

DOCKER_SOCKETS = ["/run/docker.sock", "/var/run/docker.sock"]
NO_DOCKER_MSG = ("Нет доступа к Docker. Отключите Protection mode на странице дополнения "
                 "и перезапустите его. Пока доступна только очистка /config.")
_client = None


def get_client():
    global _client
    if _client is None:
        sock = next((s for s in DOCKER_SOCKETS if os.path.exists(s)), None)
        if not sock:
            raise RuntimeError(NO_DOCKER_MSG)
        _client = docker.DockerClient(base_url=f"unix://{sock}", version="auto")
    return _client

_BUILD = ("Промежуточные файлы компиляции. Пересоздаются при следующей сборке, "
          "первая сборка каждого устройства после очистки займёт больше времени.")
_PACKAGES = ("Пакеты конфигураций (packages: в YAML), скачанные из git. "
             "Скачаются заново при компиляции, понадобится интернет.")
_EXTERNAL = ("Внешние компоненты (external_components: в YAML), скачанные из git. "
             "Скачаются заново при компиляции, понадобится интернет.")
_CCACHE = ("Кэш компилятора, ускоряет повторные сборки. "
           "После очистки следующая сборка будет медленнее, дальше снова быстро.")

# Каталоги внутри контейнеров, содержимое которых — пересоздаваемый кэш.
# match ищется в имени образа контейнера.
CONTAINER_RULES = [
    {
        "match": "esphome-hassio",
        "label": "ESPHome",
        "dirs": [
            ("/data/build", "Сборки прошивок", _BUILD),
            ("/data/packages", "Git-кэш packages", _PACKAGES),
            ("/data/external_components", "Git-кэш external_components", _EXTERNAL),
            ("/data/cache/idf/ccache", "ccache ESP-IDF", _CCACHE),
            ("/data/cache/platformio/cache", "Кэш загрузок PlatformIO",
             "Архивы, скачанные PlatformIO. Установленные платформы и тулчейны не затрагиваются."),
        ],
    },
    {
        "match": "esp_screen_manager",
        "label": "Tessera",
        "dirs": [
            ("/data/build", "Сборки прошивок экранов", _BUILD),
            ("/data/esphome/packages", "Git-кэш packages", _PACKAGES),
            ("/data/esphome/external_components", "Git-кэш external_components", _EXTERNAL),
            ("/data/idf/ccache", "ccache ESP-IDF", _CCACHE),
        ],
    },
]
DIST_MATCH = "esphome-dist-server"
DIST_DIR = "/data/esphome-versions"

ITEMS = {}
STATE = {"last_scan": None, "last_clean": None, "busy": False, "warning": None, "overview": []}
LOCK = asyncio.Lock()


# ---------- helpers ----------

def add(items, item_id, group, title, size, action, note="",
        auto=True, exact=True, warn="", force=False, desc="", level="safe"):
    """auto — можно удалять автоочисткой; exact — size равен освобождаемому месту;
    level — safe (пересоздаётся само) или caution (есть последствия)."""
    if size < MIN_SIZE and not force:
        return
    items[item_id] = {
        "id": item_id,
        "group": group,
        "title": title,
        "size": size,
        "note": note,
        "auto": auto,
        "exact": exact,
        "warn": warn,
        "desc": desc,
        "level": level,
        "action": action,
    }


def cexec(c, cmd):
    r = c.exec_run(["sh", "-c", cmd])
    return r.exit_code, (r.output or b"").decode(errors="replace")


def cexec_ok(c, cmd):
    code, out = cexec(c, cmd)
    if code != 0:
        raise RuntimeError(out.strip() or f"exit code {code}")


def c_du(c, path):
    _, out = cexec(c, f"[ -e '{path}' ] && du -sxk '{path}' 2>/dev/null")
    try:
        return int(out.split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


def image_of(c):
    return (c.attrs.get("Config") or {}).get("Image") or ""


def path_size(p):
    try:
        if not os.path.isdir(p) or os.path.islink(p):
            return os.lstat(p).st_size
    except OSError:
        return 0
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def rm(p):
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    elif os.path.lexists(p):
        os.remove(p)


def vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def disk():
    u = shutil.disk_usage(CONFIG)
    return {"total": u.total, "used": u.used, "free": u.free}


def supervisor_token():
    """s6-overlay очищает окружение, исходные переменные лежат файлами в container_environment."""
    for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
        try:
            with open(f"/run/s6/container_environment/{name}") as f:
                token = f.read().strip()
            if token:
                return token
        except OSError:
            pass
    return None


def supervisor(method, path, body=None, timeout=300):
    token = supervisor_token()
    if not token:
        raise RuntimeError("Нет токена Supervisor (SUPERVISOR_TOKEN)")
    req = urllib.request.Request(
        f"http://supervisor{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# Пояснения для сводки: подстрока имени контейнера -> (что внутри, keep/ok)
KNOWN = [
    ("esphome_dist_server", "Распределённая сборка ESPHome: версии ESPHome для компиляции. Старые версии чистятся выше.", "ok"),
    ("esp_screen_manager", "Tessera: свои ESPHome и ESP-IDF для прошивок экранов. Тулчейн нужен для каждой сборки, кэши чистятся выше.", "ok"),
    ("esphome", "Тулчейны ESP-IDF и PlatformIO нужны для каждой сборки. Конфигурации лежат в /config/esphome. Кэши чистятся выше.", "ok"),
    ("mariadb", "База recorder: история состояний. Не удаляйте файлы вручную, используйте очистку базы выше.", "keep"),
    ("postgres", "База recorder: история состояний. Не удаляйте файлы вручную, используйте очистку базы выше.", "keep"),
    ("whisper", "Модели распознавания речи. Нужны голосовому ассистенту; если он не используется, удалите приложение целиком.", "ok"),
    ("piper", "Голоса синтеза речи. Нужны голосовому ассистенту; если он не используется, удалите приложение целиком.", "ok"),
    ("vosk", "Модели распознавания речи Vosk. Если не используются, удалите приложение целиком.", "ok"),
    ("openwakeword", "Модели слова активации для голосового ассистента.", "ok"),
    ("music_assistant", "Библиотека, обложки и кэш Music Assistant. Чистится в настройках самого приложения.", "ok"),
    ("zigbee2mqtt", "Сеть Zigbee: база устройств и резервная копия координатора. Не удалять, иначе придётся заново сопрягать устройства.", "keep"),
    ("mosquitto", "MQTT-брокер: пользователи и сохранённые сообщения. Не удалять.", "keep"),
    ("bitwarden", "Хранилище паролей. Не удалять.", "keep"),
    ("vaultwarden", "Хранилище паролей. Не удалять.", "keep"),
    ("homeassistant", "Ядро Home Assistant. Данные лежат в /config.", "keep"),
    ("hassio_supervisor", "Supervisor. Его /data — общий каталог со всеми приложениями и бэкапами, поэтому размер не показывается.", "keep"),
    ("hassio_", "Системный контейнер Supervisor. Не трогать.", "keep"),
]


def describe(name):
    for key, desc, level in KNOWN:
        if key in name:
            return desc, level
    return "", ""


# ---------- scanners ----------

def scan_containers(items):
    client = get_client()
    containers = client.containers.list()
    image_sizes = {i.id: i.attrs.get("Size", 0) for i in client.images.list()}
    rw_sizes = {}
    try:
        for x in client.api.containers(size=True):
            rw_sizes[x["Id"]] = x.get("SizeRw", 0) or 0
    except Exception as e:
        log.warning("container sizes: %s", e)

    overview = []
    for c in containers:
        rw = rw_sizes.get(c.id, 0)
        if c.name == "hassio_supervisor":
            data = 0  # его /data — весь каталог Supervisor, включая данные остальных и сетевые диски
        else:
            try:
                data = c_du(c, "/data")
            except Exception:
                data = 0
        overview.append({
            "name": c.name,
            "image": image_sizes.get(c.attrs.get("Image"), 0),
            "data": data,
            "rw": rw,
            "desc": describe(c.name)[0],
            "level": describe(c.name)[1],
        })
    overview.sort(key=lambda o: -(o["image"] + o["data"] + o["rw"]))
    STATE["overview"] = overview

    for c in containers:
        img = image_of(c)
        for rule in CONTAINER_RULES:
            if rule["match"] not in img:
                continue
            group = f"{rule['label']} · {c.name}"
            for path, title, desc in rule["dirs"]:
                add(
                    items, f"{c.name}:{path}", group, title, c_du(c, path),
                    lambda c=c, p=path: cexec_ok(
                        c, f"[ -d '{p}' ] && find '{p}' -mindepth 1 -maxdepth 1 -exec rm -rf {{}} + || true"
                    ),
                    path, desc=desc,
                )
        if DIST_MATCH in img:
            scan_dist(c, items)


def scan_dist(c, items):
    _, out = cexec(c, f"ls -1 '{DIST_DIR}' 2>/dev/null")
    versions = sorted((v for v in out.split() if re.match(r"^\d", v)), key=vkey)
    group = f"ESPHome dist server · {c.name}"
    for v in versions[:-KEEP_VERSIONS]:
        p = f"{DIST_DIR}/{v}"
        add(
            items, f"{c.name}:{p}", group, f"Старая версия ESPHome {v}", c_du(c, p),
            lambda c=c, p=p: cexec_ok(c, f"rm -rf '{p}'"),
            p,
            desc=(f"Версия ESPHome, которую dist server больше не использует. Последние {KEEP_VERSIONS} "
                  "остаются. Если какая-то сборка снова её запросит, она скачается заново."),
        )


def prune_builds_all(client):
    """docker builder prune -a: без all Docker удаляет только «висящий» кэш, а это обычно копейки."""
    api = client.api
    r = api._post(api._url("/build/prune"), params={"all": "true"})
    api._raise_for_status(r)


# Образы, которые не запущены, но нужны Supervisor для сборки локальных приложений.
BUILD_IMAGE_PATTERNS = (
    re.compile(r"(^|/)[a-z0-9]+-base(-[a-z0-9.]+)?:"),  # ghcr.io/home-assistant/amd64-base:3.20, -base-python
    re.compile(r"(^|/)base(-[a-z0-9.]+)?:"),             # ghcr.io/hassio-addons/base
    re.compile(r"builder"),                              # сборщик Supervisor
    re.compile(r"(^|/)docker:"),                         # docker CLI для сборки
    re.compile(r"(^|/)(alpine|debian|ubuntu|python):"),  # частые базовые образы
)


def is_build_image(tag):
    return any(p.search(tag) for p in BUILD_IMAGE_PATTERNS)


def remove_images(client, ids):
    errors = []
    for image_id in ids:
        try:
            client.images.remove(image_id)
        except Exception as e:
            errors.append(str(e))
    if errors:
        raise RuntimeError("; ".join(errors[:3]))


def scan_docker(items):
    client = get_client()
    try:
        df = client.df()
        size = sum(x.get("Size", 0) for x in (df.get("BuildCache") or []) if not x.get("InUse"))
        add(items, "docker:buildcache", "Docker", "Кэш сборки образов", size,
            lambda: prune_builds_all(client), "docker builder prune -a",
            desc=("Слои, оставшиеся от локальной сборки образов, например локальных дополнений. "
                  "Работающие приложения не затрагивает; следующая пересборка локального дополнения "
                  "будет дольше."))
    except Exception as e:
        log.warning("build cache: %s", e)
    try:
        dangling = client.images.list(filters={"dangling": True})
        size = sum(i.attrs.get("Size", 0) for i in dangling)
        add(items, "docker:dangling", "Docker", f"Образы без тега ({len(dangling)})", size,
            lambda: client.images.prune(filters={"dangling": True}), "docker image prune",
            desc="Старые образы без тега, оставшиеся после обновлений. Ни один контейнер их не использует.")
    except Exception as e:
        log.warning("dangling images: %s", e)
    try:
        used = {c.attrs.get("Image") for c in client.containers.list(all=True)}
        unused = [i for i in client.images.list()
                  if i.tags and i.id not in used and not any(is_build_image(t) for t in i.tags)]
        if unused:
            size = sum(i.attrs.get("Size", 0) for i in unused)
            tags = ", ".join(t for i in unused for t in i.tags[:1])
            add(items, "docker:unused", "Docker", f"Неиспользуемые образы ({len(unused)})", size,
                lambda ids=[i.id for i in unused]: remove_images(client, ids), tags,
                auto=False, exact=False, level="caution",
                desc=("Образы с тегом, которые не использует ни один контейнер, даже остановленный. "
                      "Обычно это старые версии локальных приложений после обновления. Базовые образы и "
                      "образы сборщика, нужные для сборки локальных приложений, сюда не попадают. Если образ "
                      "всё же понадобится, Supervisor скачает его заново. Часть слоёв может "
                      "быть общей с другими образами, поэтому освободится, возможно, меньше."))
    except Exception as e:
        log.warning("unused images: %s", e)


def scan_recorder(items):
    db_size = path_size(f"{CONFIG}/home-assistant_v2.db")
    for o in STATE["overview"]:
        if o["name"].endswith("mariadb") or o["name"].endswith("postgres"):
            db_size = max(db_size, o["data"])
    add(
        items, "ha:recorder", "База данных recorder", "Очистка и сжатие базы (purge + repack)", db_size,
        lambda: supervisor("POST", "/core/api/services/recorder/purge", {"repack": True}),
        "recorder.purge с repack: true",
        auto=False, exact=False, warn="Сжатие идёт в фоне и может занять несколько минут", level="caution",
        desc=("Удаляет историю состояний старше purge_keep_days (по умолчанию 10 дней) и сжимает таблицы. "
              "Удалённую историю не вернуть, долгосрочная статистика (графики энергии, средние значения) "
              "сохраняется. HA и так чистит базу каждую ночь, но без сжатия файлы базы не уменьшаются."),
    )


def scan_config(items):
    group = "Home Assistant · /config"
    for f in glob.glob(f"{CONFIG}/callgrind.out.*"):
        add(items, f"cfg:{f}", group, f"Профиль {os.path.basename(f)}", path_size(f), lambda f=f: rm(f), f,
            desc="Результат интеграции Profiler. Нужен только для анализа производительности, HA его не читает.")
    for f in glob.glob(f"{CONFIG}/home-assistant.log.*"):
        add(items, f"cfg:{f}", group, f"Старый лог {os.path.basename(f)}", path_size(f), lambda f=f: rm(f), f,
            desc="Лог предыдущего запуска HA. Пригодится, только если разбираете проблему, случившуюся до перезапуска.")

    legacy = f"{CONFIG}/esphome/.esphome"
    if os.path.isdir(legacy):
        add(items, "cfg:esphome-legacy", group, "Старый кэш ESPHome", path_size(legacy),
            lambda: rm(legacy), legacy,
            desc=("Кэш от старых версий ESPHome. Текущая версия хранит кэш в /data приложения, "
                  "а эту папку не использует. Конфигурации устройств лежат уровнем выше и не затрагиваются."))

    zlog = f"{CONFIG}/zigbee2mqtt/log"
    if os.path.isdir(zlog):
        cutoff = time.time() - LOG_DAYS * 86400
        old = []
        for name in os.listdir(zlog):
            p = os.path.join(zlog, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    old.append(p)
            except OSError:
                pass
        if old:
            add(items, "cfg:z2m-logs", "Zigbee2MQTT", f"Логи старше {LOG_DAYS} дн. ({len(old)})",
                sum(path_size(p) for p in old), lambda old=old: [rm(p) for p in old], zlog,
                desc=("Логи прошлых запусков Zigbee2MQTT. Текущий лог, сеть, устройства и настройки "
                      "не затрагиваются."))


# ---------- history ----------

HISTORY_FILE = "/data/history.json"
SNAP_EVERY = 6 * 3600
HISTORY_DAYS = 60
# Каталоги раздела HAOS (/mnt/data). docker и supervisor/addons не обходим:
# первый считается через Docker API, второй — через /data каждого приложения.
HOST_DU = (
    'for p in /d/* /d/supervisor/*; do '
    'case "$p" in /d/docker|/d/supervisor|/d/supervisor/addons) continue;; esac; '
    '[ -e "$p" ] && du -sxk "$p" 2>/dev/null; done'
)
HOST_LABELS = {
    "/mnt/data/supervisor/backup": "Бэкапы",
    "/mnt/data/supervisor/homeassistant": "/config",
    "/mnt/data/supervisor/media": "/media",
    "/mnt/data/supervisor/share": "/share",
    "/mnt/data/supervisor/tmp": "Временные файлы Supervisor",
    "/mnt/data/supervisor/apparmor": "Профили AppArmor",
    "/mnt/data/logs": "Системный журнал HAOS",
}


def load_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_history(hist):
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(hist, f)
    os.replace(tmp, HISTORY_FILE)


def host_sizes(client):
    """du по разделу HAOS через одноразовый контейнер из нашего же образа (ничего не скачивает)."""
    image = next((c.attrs.get("Image") for c in client.containers.list() if "noema_cleaner" in c.name), None)
    if not image:
        return {}
    out = client.containers.run(
        image, [HOST_DU], entrypoint=["sh", "-c"],
        volumes={"/mnt/data": {"bind": "/d", "mode": "ro"}},
        network_mode="none", remove=True, stderr=False,
    )
    sizes = {}
    for line in out.decode(errors="replace").splitlines():
        kb, _, path = line.partition("\t")
        if kb.isdigit() and path:
            sizes["/mnt/data" + path[2:]] = int(kb) * 1024
    return sizes


def take_snapshot(force=False):
    hist = load_history()
    now = time.time()
    if not force and hist and now - hist[-1]["t"] < SNAP_EVERY:
        return
    client = get_client()
    sizes = {"disk:used": disk()["used"]}
    try:
        df = client.df()
        sizes["docker:images"] = df.get("LayersSize") or 0
        sizes["docker:buildcache"] = sum(x.get("Size", 0) for x in (df.get("BuildCache") or []))
        sizes["docker:rw"] = sum((c.get("SizeRw") or 0) for c in (df.get("Containers") or []))
    except Exception as e:
        log.warning("snapshot docker: %s", e)
    for o in STATE["overview"]:
        if o["data"]:
            sizes[f"app:{o['name']}"] = o["data"]
    try:
        for path, size in host_sizes(client).items():
            sizes[f"host:{path}"] = size
    except Exception as e:
        log.warning("snapshot host: %s", e)
    hist.append({"t": now, "s": sizes})
    cutoff = now - HISTORY_DAYS * 86400
    save_history([h for h in hist if h["t"] >= cutoff])
    log.info("Снимок размеров сохранён (%d категорий)", len(sizes))


def label_for(key):
    kind, _, name = key.partition(":")
    if key == "disk:used":
        return "Весь диск, занято"
    if kind == "docker":
        return {"images": "Docker: образы", "buildcache": "Docker: кэш сборки",
                "rw": "Docker: слои записи контейнеров"}.get(name, key)
    if kind == "app":
        return f"{name}: /data"
    if kind == "host":
        return HOST_LABELS.get(name, name)
    return key


def growth():
    hist = load_history()
    if not hist:
        return {"first": None, "rows": [], "day_ref": None, "week_ref": None}
    last = hist[-1]

    def ref(age):
        older = [h for h in hist if h["t"] <= last["t"] - age]
        return older[-1] if older else None

    day = ref(20 * 3600)
    week = ref(6.5 * 86400) or (hist[0] if hist[0] is not last else None)
    rows = []
    for key, now in last["s"].items():
        d1 = now - day["s"][key] if day and key in day["s"] else None
        dw = now - week["s"][key] if week and key in week["s"] else None
        if now < 10 * 1048576 and abs(dw or 0) < 10 * 1048576:
            continue
        rows.append({"key": key, "label": label_for(key), "now": now, "day": d1, "week": dw})
    rows.sort(key=lambda r: (r["key"] != "disk:used", -(r["week"] if r["week"] is not None else r["now"])))
    return {
        "first": hist[0]["t"],
        "rows": rows,
        "day_ref": day["t"] if day else None,
        "week_ref": week["t"] if week else None,
    }


def do_scan():
    items = {}
    warning = None
    try:
        get_client().ping()
    except Exception as e:
        warning = str(e) if str(e) == NO_DOCKER_MSG else f"Docker недоступен: {e}"
        log.warning(warning)
    if warning:
        STATE["overview"] = []
    scanners = [scan_config, scan_recorder] if warning else [scan_containers, scan_docker, scan_config, scan_recorder]
    for fn in scanners:
        try:
            fn(items)
        except Exception:
            log.exception("%s failed", fn.__name__)
    STATE["warning"] = warning
    if not warning:
        try:
            take_snapshot()
        except Exception:
            log.exception("snapshot")
    return items


def do_clean(ids):
    errors = []
    for i in ids:
        it = ITEMS.get(i)
        if not it:
            continue
        try:
            log.info("Удаляю: %s — %s (%s)", it["group"], it["title"], it["note"])
            it["action"]()
        except Exception as e:
            log.error("Ошибка: %s: %s", it["title"], e)
            errors.append(f"{it['title']}: {e}")
    return errors


# ---------- orchestration ----------

async def run_scan():
    items = await asyncio.to_thread(do_scan)
    ITEMS.clear()
    ITEMS.update(items)
    STATE["last_scan"] = time.time()


async def run_clean(ids):
    before = disk()["free"]
    errors = await asyncio.to_thread(do_clean, ids)
    freed = max(0, disk()["free"] - before)
    STATE["last_clean"] = {"time": time.time(), "freed": freed}
    log.info("Освобождено %.1f МБ", freed / 1048576)
    await run_scan()
    return freed, errors


def public_state():
    items = sorted(ITEMS.values(), key=lambda x: (x["group"], -x["size"]))
    return {
        "items": [{k: v for k, v in it.items() if k != "action"} for it in items],
        "disk": disk(),
        "last_scan": STATE["last_scan"],
        "last_clean": STATE["last_clean"],
        "busy": STATE["busy"],
        "warning": STATE["warning"],
        "mqtt": bool(MQ and MQ.is_connected()),
        "overview": STATE["overview"],
        "growth": growth(),
        "auto": AUTO,
        "interval": INTERVAL_H,
    }


async def guarded(coro):
    if LOCK.locked():
        raise web.HTTPConflict(text="Уже выполняется другая операция")
    async with LOCK:
        STATE["busy"] = True
        mqtt_publish_state()
        try:
            return await coro
        finally:
            STATE["busy"] = False
            mqtt_publish_state()


def fmt_size(b):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if b < 1024 or unit == "ГБ":
            return f"{b:.1f} {unit}".replace(".", ",")
        b /= 1024


def notify(message):
    try:
        supervisor("POST", "/core/api/services/persistent_notification/create", {
            "title": "NOEMA Cleaner", "message": message, "notification_id": "noema_cleaner",
        }, timeout=15)
    except Exception as e:
        log.warning("уведомление: %s", e)


async def clean_safe(source):
    """Удаляет всё помеченное как безопасное (auto). Перезапуски и сжатие базы не трогает."""
    await run_scan()
    ids = [i for i, it in ITEMS.items() if it["auto"]]
    freed, errors = await run_clean(ids)
    msg = f"Очистка ({source}): освобождено {fmt_size(freed)}."
    if errors:
        msg += f" Ошибок: {len(errors)}. {errors[0]}"
    await asyncio.to_thread(notify, msg)
    return freed, errors


async def background():
    await asyncio.sleep(5)
    try:
        await guarded(run_scan())
    except Exception:
        log.exception("initial scan")

    next_scan = time.time() + SCAN_MIN * 60
    next_clean = time.time() + INTERVAL_H * 3600
    if AUTO:
        log.info("Автоочистка включена, каждые %d ч", INTERVAL_H)
    while True:
        await asyncio.sleep(60)
        now = time.time()
        try:
            if AUTO and now >= next_clean and not LOCK.locked():
                next_clean = now + INTERVAL_H * 3600
                next_scan = now + SCAN_MIN * 60
                await guarded(clean_safe("автоочистка"))
            elif now >= next_scan and not LOCK.locked():
                next_scan = now + SCAN_MIN * 60
                await guarded(run_scan())
            elif int(now) // 60 % 5 == 0:
                mqtt_publish_state()  # свободное место обновляется чаще, чем сканирование
        except Exception:
            log.exception("background")


# ---------- MQTT ----------

BASE_TOPIC = "noema_cleaner"
DISCOVERY = "homeassistant"
MQ = None
LOOP = None


def gib(b):
    return round(b / 1024 ** 3, 2)


def safe_total():
    return sum(it["size"] for it in ITEMS.values() if it["auto"] and it["exact"])


def mqtt_publish_state():
    if MQ is None or not MQ.is_connected() or not STATE["last_scan"]:
        return
    d = disk()
    lc = STATE["last_clean"] or {}
    state = {
        "free": gib(d["free"]),
        "used_pct": round(d["used"] / d["total"] * 100, 1),
        "reclaimable": gib(safe_total()),
        "last_freed": gib(lc.get("freed", 0)),
        "last_scan": datetime.fromtimestamp(STATE["last_scan"], timezone.utc).isoformat(),
        "busy": STATE["busy"],
        "growth_day": gib(next((r["day"] or 0 for r in growth()["rows"] if r["key"] == "disk:used"), 0)),
    }
    top = sorted(ITEMS.values(), key=lambda x: -x["size"])[:25]
    attrs = {
        "count": len(ITEMS),
        "items": [
            {"group": it["group"], "title": it["title"], "mb": round(it["size"] / 1048576),
             "safe": it["level"] == "safe"}
            for it in top
        ],
    }
    MQ.publish(f"{BASE_TOPIC}/state", json.dumps(state), retain=True)
    MQ.publish(f"{BASE_TOPIC}/items", json.dumps(attrs, ensure_ascii=False), retain=True)


def mqtt_discovery(client):
    device = {
        "identifiers": [BASE_TOPIC],
        "name": "NOEMA Cleaner",
        "manufacturer": "NOEMA",
        "model": "Cleaner",
        "sw_version": VERSION,
    }
    state = f"{BASE_TOPIC}/state"
    size = {"device_class": "data_size", "unit_of_measurement": "GiB", "suggested_display_precision": 2}
    entities = [
        ("sensor", "reclaimable", {**size, "name": "Можно освободить", "state_class": "measurement",
                                   "value_template": "{{ value_json.reclaimable }}",
                                   "json_attributes_topic": f"{BASE_TOPIC}/items", "icon": "mdi:broom"}),
        ("sensor", "free", {**size, "name": "Свободно на диске", "state_class": "measurement",
                            "value_template": "{{ value_json.free }}", "icon": "mdi:harddisk"}),
        ("sensor", "used", {"name": "Заполнение диска", "unit_of_measurement": "%", "state_class": "measurement",
                            "value_template": "{{ value_json.used_pct }}", "icon": "mdi:chart-donut"}),
        ("sensor", "last_freed", {**size, "name": "Освобождено при последней очистке",
                                  "value_template": "{{ value_json.last_freed }}", "icon": "mdi:delete-sweep"}),
        ("sensor", "last_scan", {"name": "Последнее сканирование", "device_class": "timestamp",
                                 "value_template": "{{ value_json.last_scan }}", "entity_category": "diagnostic"}),
        ("sensor", "growth_day", {**size, "name": "Рост занятого места за сутки",
                                  "value_template": "{{ value_json.growth_day }}", "icon": "mdi:trending-up"}),
        ("binary_sensor", "busy", {"name": "Выполняется", "device_class": "running",
                                   "value_template": "{{ 'ON' if value_json.busy else 'OFF' }}"}),
        ("button", "clean", {"name": "Очистить безопасное", "command_topic": f"{BASE_TOPIC}/cmd",
                             "payload_press": "clean_safe", "icon": "mdi:broom"}),
        ("button", "scan", {"name": "Пересканировать", "command_topic": f"{BASE_TOPIC}/cmd",
                            "payload_press": "scan", "icon": "mdi:magnify", "entity_category": "diagnostic"}),
    ]
    for component, key, cfg in entities:
        cfg = {**cfg, "unique_id": f"{BASE_TOPIC}_{key}", "device": device,
               "availability_topic": f"{BASE_TOPIC}/status"}
        if component != "button":
            cfg["state_topic"] = state
        client.publish(f"{DISCOVERY}/{component}/{BASE_TOPIC}/{key}/config",
                       json.dumps(cfg, ensure_ascii=False), retain=True)


def on_connect(client, userdata, flags, rc, properties=None):
    code = int(getattr(rc, "value", rc))
    if code != 0:
        log.warning("MQTT: отказ в подключении, код %s", code)
        return
    log.info("MQTT подключён")
    client.publish(f"{BASE_TOPIC}/status", "online", retain=True)
    mqtt_discovery(client)
    client.subscribe(f"{BASE_TOPIC}/cmd")
    client.subscribe(f"{DISCOVERY}/status")
    mqtt_publish_state()


def on_message(client, userdata, msg):
    payload = msg.payload.decode(errors="replace").strip()
    if msg.topic == f"{DISCOVERY}/status":
        if payload == "online":  # HA перезапустился — повторяем discovery
            mqtt_discovery(client)
            mqtt_publish_state()
        return
    if payload == "scan":
        coro = guarded(run_scan())
    elif payload == "clean_safe":
        coro = guarded(clean_safe("кнопка в HA"))
    else:
        return
    log.info("MQTT команда: %s", payload)
    fut = asyncio.run_coroutine_threadsafe(coro, LOOP)
    fut.add_done_callback(lambda f: f.exception() and log.warning("команда %s: %s", payload, f.exception()))


def mqtt_start():
    global MQ
    if mqtt is None:
        log.warning("paho-mqtt не установлен, MQTT отключён")
        return
    try:
        cfg = json.loads(supervisor("GET", "/services/mqtt", timeout=10))["data"]
    except Exception as e:
        log.warning("MQTT-брокер не найден (%s). Сущности в HA не появятся.", e)
        return
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=BASE_TOPIC)
    except AttributeError:
        client = mqtt.Client(client_id=BASE_TOPIC)
    if cfg.get("username"):
        client.username_pw_set(cfg["username"], cfg.get("password"))
    if cfg.get("ssl"):
        client.tls_set()
    client.will_set(f"{BASE_TOPIC}/status", "offline", retain=True)
    client.on_connect = on_connect
    client.on_message = on_message
    MQ = client
    client.connect_async(cfg["host"], int(cfg["port"]))
    client.loop_start()


# ---------- web ----------

@web.middleware
async def ingress_only(request, handler):
    if request.remote not in INGRESS_IPS:
        raise web.HTTPForbidden()
    return await handler(request)


INDEX_HTML = open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()


async def index(request):
    base = request.headers.get("X-Ingress-Path", "").rstrip("/") + "/"
    html = INDEX_HTML.replace("__BASE__", base.replace('"', ""))
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def api_state(_):
    return web.json_response(public_state())


async def api_scan(_):
    await guarded(run_scan())
    return web.json_response(public_state())


async def api_clean(request):
    data = await request.json()
    ids = [i for i in data.get("ids", []) if isinstance(i, str)]
    freed, errors = await guarded(run_clean(ids))
    s = public_state()
    s.update(freed=freed, errors=errors)
    return web.json_response(s)


async def on_startup(app):
    global LOOP
    LOOP = asyncio.get_running_loop()
    log.info("NOEMA Cleaner %s запущен", VERSION)
    if MQTT_ENABLED:
        await asyncio.to_thread(mqtt_start)
    app["bg"] = asyncio.create_task(background())


def main():
    app = web.Application(middlewares=[ingress_only])
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/scan", api_scan)
    app.router.add_post("/api/clean", api_clean)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=8099, access_log=None)


if __name__ == "__main__":
    main()
