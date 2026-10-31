# NOEMA Cleaner

Приложение (add-on) для Home Assistant OS: находит и удаляет пересоздаваемые кэши и временные файлы, показывает, что занимает и что наращивает место на диске, публикует сущности в HA через MQTT. Интерфейс на русском.

## Что ищет

- ESPHome: сборки, git-кэши packages/external_components, ccache, кэш загрузок PlatformIO
- Tessera (esp_screen_manager): сборки, git-кэши, ccache
- ESPHome dist server: старые версии ESPHome (оставляет последние N)
- Docker: кэш сборки образов, образы без тега, неиспользуемые образы с тегом
- Слой записи контейнеров приложений (сброс перезапуском)
- База recorder: purge + repack
- /config: `callgrind.out.*`, `home-assistant.log.*`, старый `/config/esphome/.esphome`, логи Zigbee2MQTT старше N дней

Тулчейны (`xtensa-esp-elf`, ESP-IDF frameworks) и базовые образы для сборки локальных приложений не трогает: они нужны для каждой сборки.

Каждый пункт помечен: `safe` — пересоздаётся само, `caution` — есть последствия. Автоочистка и кнопка в HA удаляют только `safe`.

## Настройки

| Параметр | По умолчанию | Описание |
|---|---|---|
| `auto_clean` | `false` | Автоматически чистить безопасное |
| `interval_hours` | `168` | Период автоочистки |
| `log_max_age_days` | `7` | Возраст логов Zigbee2MQTT для удаления |
| `keep_esphome_versions` | `1` | Сколько последних версий ESPHome оставлять |
| `mqtt` | `true` | Публиковать сущности в HA через MQTT |
| `scan_interval_minutes` | `60` | Период сканирования |

## Установка

1. Скопируйте папку целиком в `/addons/noema_cleaner/` (Samba-шара `addons`).
2. Магазин приложений → ⋮ → «Проверить обновления» → установить или «Обновить».
3. Отключите Protection mode на странице приложения: без него `docker_api` недоступен, и работает только очистка `/config`.

Обновлять нужно кнопкой «Обновить», а не «Пересобрать», и каждое изменение выпускать с новой версией в `config.yaml`.

## Разработка

Проверка без Home Assistant, с заглушкой Docker и Supervisor:

```
pip install aiohttp docker paho-mqtt
sh dev/run.sh    # http://localhost:8099
```

Для проверки MQTT запустите mosquitto на 1883 и выполните `MQTT_HOST=localhost sh dev/run.sh`.
Перед выпуском версии: `python3 -m py_compile app/main.py`. Версия задаётся в `config.yaml` и в `VERSION` в `app/main.py`.
