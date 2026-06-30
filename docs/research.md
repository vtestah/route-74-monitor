# Источники Данных

## Текущая Стратегия

Runtime использует только Yandex-family источники:

1. Яндекс.Карты как live ETA.
2. Локальная история Яндекса из `route74 yandex-collect` как резервный прогноз.

Официальное расписание больше не используется в runtime. Если live и история
не дают ETA, бот честно сообщает, что точного сигнала нет.

## Яндекс

Основные внутренние masstransit методы:

- `getVehiclePredictionInfo` - лучший live ETA, если response содержит
  посадочную остановку текущего профиля.
- `getStopInfo` - stop-level источник; `Estimated` можно использовать как ETA,
  `Scheduled`/`Frequencies` только как диагностику.
- `getLine` - топология маршрута: `threadId`, порядок остановок, геометрия.
  Это не ETA.
- `getStopTimetable` - расписание/частоты, не live ETA без `Estimated`.
- `getVehiclesInfoWithRegion` - диагностические координаты и nested
  `VehicleMetaData.Transport.threadId`.
  Этот `threadId` используется для фильтрации направления профиля и, когда
  возможно, пришивается к `getVehiclePredictionInfo` по `vehicleId`.
  Route URL открывается с явным `threadId` и `openedBy[stopId]`, иначе Yandex
  может отдать машины противоположного направления как основной сценарий.
  Raw ETA выше 60 минут для профиля не используется как прогнозный сэмпл:
  такие значения выглядят как полный круг маршрута, а не ближайшая посадка.

Команды:

```bash
route74 yandex-dump --profile morning
route74 yandex-line --dump path/to/dump.json
route74 yandex-collect --once --profile all
```

Подробный stop/thread контракт профилей вынесен в
[`YANDEX_CONTRACT.md`](./YANDEX_CONTRACT.md). Там зафиксировано, какие stop id
используются для stop-level страницы, какие target stop id проверяются внутри
`getVehiclePredictionInfo`, какие `threadId` подтверждают направление и почему
runtime должен fail-closed при несовпадении.

## Статистика

`route74 yandex-collect` пишет Yandex snapshots и vehicle observations в
SQLite. Это база для будущего Yandex-only прогноза заранее: накопить пары
“машина/позиция/threadId сейчас -> через сколько она дошла до моей остановки”.

Пока это слой фактов, а не отдельная модель прогноза.
