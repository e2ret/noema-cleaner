# NOEMA Cleaner

Локальное приложение (add-on) для Home Assistant OS: находит и удаляет пересоздаваемые кэши, показывает, что занимает и что наращивает место на диске, публикует сущности в HA через MQTT. Интерфейс и все тексты — на русском.

## Структура

- `noema_cleaner/config.yaml` — манифест приложения. `version` здесь и `VERSION` в `noema_cleaner/app/main.py` меняются вместе.
- `noema_cleaner/build.yaml`, `noema_cleaner/Dockerfile` — образ на `ghcr.io/home-assistant/{arch}-base:3.20`, пакеты из apk: `python3 py3-aiohttp py3-docker-py py3-paho-mqtt` (без pip).
- `noema_cleaner/translations/en.yaml` — подписи настроек (на русском, файл en — потому что HA ищет его по умолчанию).
- `noema_cleaner/app/main.py` — весь бэкенд (aiohttp + docker SDK + paho-mqtt), разделы: helpers → scanners → history → orchestration → MQTT → web.
- `noema_cleaner/app/index.html` — ingress-панель, чистый HTML/CSS/JS без сборки.
- `dev/` — заглушка Docker и запуск для локальной проверки (в образ не попадает).

## Как устроено

- Сканеры заполняют `ITEMS` (id → пункт с `action`, `size`, `level`, `auto`, `exact`, `desc`).
  - `CONTAINER_RULES`: каталоги внутри контейнеров (по подстроке имени образа), чистятся `docker exec find -delete`.
  - `scan_dist`: старые версии ESPHome в dist server.
  - `scan_docker`: build cache, образы без тега, неиспользуемые образы с тегом.
  - `scan_config`: мусор в /config. `scan_recorder`: purge+repack базы.
  - Слой записи контейнеров только показывается в сводке; перезапуски приложений убраны намеренно.
- `level`: `safe` — пересоздаётся само; `caution` — есть последствия. Автоочистка и MQTT-кнопка трогают только `auto=True` (все `caution` — `auto=False`).
- `exact=False` — размер не равен освобождаемому месту (UI пишет «из …», в сумму не входит).
- История роста: снимок раз в 6 ч в `/data/history.json` (60 дней). Раздел HAOS `/mnt/data` обходится одноразовым контейнером из собственного образа (`host_sizes`), без скачивания образов.
- Все операции идут через `guarded()` (один `asyncio.Lock`), блокирующие вызовы — в `asyncio.to_thread`.

## Грабли, на которые уже наступили

- Protection mode должен быть выключен, иначе `docker_api: true` игнорируется и сокета нет. После смены прав в `noema_cleaner/config.yaml` HA может снова его включить.
- s6-overlay очищает окружение: `SUPERVISOR_TOKEN` читать из env или `/run/s6/container_environment/` (см. `supervisor_token()`).
- Обновлять через «Обновить», не «Пересобрать»: пересборка не применяет изменения `noema_cleaner/config.yaml`. Поэтому любое изменение — с повышением версии.
- Ingress: страница отдаётся с `<base href>` из заголовка `X-Ingress-Path`, все запросы относительные (`api/scan`). Иначе запросы уходят в API ядра HA → 401.
- Контейнеры приложений называются `app_<slug>` (раньше `addon_`), slug для API = имя без префикса.
- `du` только с `-x`: у `hassio_supervisor` в `/data` смонтированы сетевые хранилища (показывал 114 ГБ).
- Build cache удалять с `all=true` (`docker builder prune -a`), без него Docker не трогает «пригодный» кэш.
- Нельзя удалять базовые образы и образ сборщика (`*-base`, `hassio-addons/base`, `*builder*`, `docker:*`): они не используются контейнерами, но без них не соберётся ни одно локальное приложение, а `ghcr.io` из РФ бывает недоступен. См. `BUILD_IMAGE_PATTERNS`.
- Тулчейны ESP-IDF/PlatformIO (`xtensa-esp-elf`, `frameworks`) не трогать — нужны для каждой сборки.

## Проверка локально (macOS/Linux, без HA)

```
pip install aiohttp docker paho-mqtt
sh dev/run.sh         # поднимает приложение на http://localhost:8099 с заглушкой Docker
```

Для проверки MQTT: запустить mosquitto на 1883, `sh dev/run.sh` подставит его как брокер. Перед выдачей версии: `python3 -m py_compile noema_cleaner/app/main.py` и прогон через `sh dev/run.sh`.

## Установка на HA

Папка целиком кладётся в `/addons/noema_cleaner/` (Samba-шара `addons`), затем Магазин → ⋮ → «Проверить обновления» → «Обновить». Пользователь предпочитает получать готовые файлы или шаги в интерфейсе, а не скрипты для терминала.

## Стиль

- UI: русские тексты, типографские кавычки «», без капслока, светлая и тёмная темы через переменные.
- У каждого нового пункта очистки — `desc` (что это, что будет после удаления) и честный `level`.
