# NOEMA Cleaner

[![Translate](https://img.shields.io/badge/Translate-To_Your_Language-red)](https://translate.google.com/translate?sl=auto&u=https://github.com/e2ret/noema-cleaner)

Приложение (add-on) для Home Assistant OS: находит и удаляет пересоздаваемые кэши и временные файлы, показывает, что занимает и что наращивает место на диске, публикует сущности в HA через MQTT. Интерфейс на русском.

## Что ищет

- ESPHome: сборки, git-кэши packages/external_components, ccache, кэш загрузок PlatformIO
- Tessera (esp_screen_manager): сборки, git-кэши, ccache
- ESPHome dist server: старые версии ESPHome (оставляет последние N)
- Docker: кэш сборки образов, образы без тега, неиспользуемые образы с тегом
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

## Права и безопасность

Приложению нужны широкие права, поэтому Home Assistant ставит ему низкий рейтинг безопасности. Вот что запрашивается и зачем:

| Право | Зачем |
|---|---|
| `docker_api` (Protection mode выключен) | Размеры и очистка каталогов внутри контейнеров ESPHome и Tessera через `docker exec`, build cache и образы без тега, размеры образов. По сути это доступ к Docker хоста |
| `map: /config` (запись) | Удаление `callgrind.out.*`, старых `home-assistant.log.*`, старого `/config/esphome/.esphome` и логов Zigbee2MQTT. Больше ничего в `/config` не меняется |
| `homeassistant_api` | Вызов `recorder.purge` и уведомление в HA о результате очистки |
| `hassio_api`, `hassio_role: manager` | Данные MQTT-брокера. Роль осталась от прежней версии, где были перезапуски приложений. Сейчас она, вероятно, избыточна, понижение планируется после проверки |
| `services: mqtt` | Публикация сущностей |

Что удаляется: только каталоги из явного списка (кэши и сборки ESPHome и Tessera, старые версии ESPHome dist server), build cache Docker, образы без тега и перечисленные выше файлы `/config`. Данные приложений, базы, настройки, тулчейны ESP-IDF и PlatformIO и базовые образы для сборки не затрагиваются. Приложения Cleaner не перезапускает.

Автоочистка выключена по умолчанию и трогает только пункты уровня `safe`. Остальное (неиспользуемые образы с тегом, `recorder.purge` со сжатием) делается только вручную из панели. Код открыт: список правил лежит в `CONTAINER_RULES` и сканерах `noema_cleaner/app/main.py`.

## Установка

[![Добавить репозиторий в Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fe2ret%2Fnoema-cleaner)

1. Магазин приложений → ⋮ → «Репозитории» → добавьте `https://github.com/e2ret/noema-cleaner` (или нажмите кнопку выше).
2. Найдите в магазине «NOEMA Cleaner» и установите.
3. Отключите Protection mode на странице приложения: без него `docker_api` недоступен, и работает только очистка `/config`.

Обновления приходят через магазин: ⋮ → «Проверить обновления» → «Обновить». Нажимайте «Обновить», а не «Пересобрать», и каждое изменение выпускайте с новой версией в `noema_cleaner/config.yaml`.

HACS приложения (add-ons) не поддерживает, он работает только с интеграциями, картами и темами. Для приложений используется репозиторий магазина Supervisor, как описано выше.

## Разработка

Проверка без Home Assistant, с заглушкой Docker и Supervisor:

```
pip install aiohttp docker paho-mqtt
sh dev/run.sh    # http://localhost:8099
```

Для проверки MQTT запустите mosquitto на 1883 и выполните `MQTT_HOST=localhost sh dev/run.sh`.
Перед выпуском версии: `python3 -m py_compile noema_cleaner/app/main.py`. Версия задаётся в `noema_cleaner/config.yaml` и в `VERSION` в `noema_cleaner/app/main.py`.
