# NOEMA Cleaner

[![Translate](https://img.shields.io/badge/Translate-To_Your_Language-red)](https://translate.google.com/translate?sl=auto&u=https://github.com/e2ret/noema-cleaner)

Находит и удаляет пересоздаваемые кэши и временные файлы в Home Assistant OS, показывает, что занимает и что наращивает место на диске, публикует сущности в HA через MQTT.

- Кэши и сборки ESPHome и Tessera, старые версии ESPHome dist server
- Docker: кэш сборки, образы без тега, неиспользуемые образы
- Слой записи контейнеров, очистка базы recorder
- Мусор в `/config`: профили, старые логи HA и Zigbee2MQTT
- История роста занятого места по категориям

Тулчейны ESP-IDF и PlatformIO, а также базовые образы для сборки локальных приложений не трогает. Требуется отключённый Protection mode.
