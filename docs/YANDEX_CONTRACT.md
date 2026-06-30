# Yandex Stop/Thread Contract

Этот документ фиксирует, как бот понимает остановки и направления Яндекса для
маршрута 74. Source of truth для runtime-значений остается в
[`src/route74/sources/yandex/constants.py`](../src/route74/sources/yandex/constants.py):
документ объясняет контракт, но не заменяет код.

## Назначение

Yandex masstransit API возвращает несколько похожих stop id для одной
посадочной зоны и разные `threadId` для направлений маршрута. Бот не угадывает
направление по названию остановки: live ETA считается пригодным только после
проверки stop/thread контракта текущего профиля.

Если контракт не подтвержден, runtime должен fail-closed: использовать историю
Яндекса или честно показать, что точного ETA нет.

## Профили

| Профиль | stopInfo stop id | prediction target stop ids | expected thread ids | Смысл |
| --- | --- | --- | --- | --- |
| `morning` | `stop__9982194` | `stop__9982194` | `2161326768` | посадка у Медицинского центра в сторону Цветного |
| `evening` | `stop__9982094` | `stop__9982094` | `2161326764` | посадка у ВЦ в сторону улицы Твардовского |

`stopInfo stop id` нужен для открытия stop-level страницы Яндекса. `prediction
target stop id` проверяется внутри ответа `getVehiclePredictionInfo`. Старые
альтернативные id не использовать: утро фиксируется на `stop__9982194`, вечер -
на `stop__9982094`.

Terminal stop ids из `getLine` для текущего направления профиля:
`morning` - `3174363647` (`Цветной проезд`), `evening` - `stop__9982203`
(`Улица Твардовского`).

## Правила Доверия

`getVehiclePredictionInfo` считается live ETA только если одновременно верно:

- `threadId` машины совпадает с `expected thread ids` профиля;
- в списке `stops` конкретной машины есть один из `prediction target stop ids`;
- ETA не выглядит как полный круг маршрута: raw ETA выше 60 минут не пишется как
  прогнозный sample.

Если `threadId` неизвестен, направление не совпало или целевой stop id не найден,
прогноз получает `NO_TARGET` и не используется как ETA.

## Методы Яндекса

- `getVehiclePredictionInfo` - главный live ETA после проверки stop/thread.
- `getStopInfo` - stop-level источник; `Estimated` можно использовать как ETA,
  `Scheduled`/`Frequencies` остаются диагностикой.
- `getVehiclesInfoWithRegion` - диагностические координаты, `vehicleId` и
  nested `VehicleMetaData.Transport.threadId`; когда возможно, этот `threadId`
  пришивается к `getVehiclePredictionInfo` по `vehicleId`.
- `getLine` - топология маршрута: thread ids, порядок остановок и геометрия. Это
  не ETA.
- `getStopTimetable` - расписание/частоты, не live ETA без `Estimated`.

Route URL открывается с явным `threadId` и `openedBy[stopId]`, чтобы Яндекс сразу
загружал нужное направление. Параметры строит `route_thread_params()`.

## Где Хранить Детали

- Runtime-значения профилей: только
  [`constants.py`](../src/route74/sources/yandex/constants.py).
- Минимальный контракт request/payload/response: этот документ.
- Исполняемые примеры shape: smoke-кейсы в
  [`sources/yandex/smoke/`](../src/route74/sources/yandex/smoke/).
- Raw dumps Яндекса: локально в `data/` или во временных файлах из
  `route74 yandex-dump`; в git их не хранить.

Полные ответы Яндекса шумные и могут содержать нестабильные поля. В git лучше
держать только минимальные shape, которые реально читает parser/runtime. Если
нужна регрессия по реальному ответу, добавлять sanitized fixture с минимальным
набором полей, без session/query/token деталей.

## Запросы

Яндекс-методы вызываются как HTTP endpoints или ловятся браузером из Network.
Для runtime важны не все query параметры, а только привязка к профилю:

| Метод | Как попадает в runtime | Что задает запрос |
| --- | --- | --- |
| `getStopInfo` | browser network capture | stop page URL со stop id из `STOP_ID_BY_PROFILE` |
| `getVehiclePredictionInfo` | browser network capture после клика по машине | vehicle `id` в query; `threadId` пришивается из соседнего `getVehiclesInfoWithRegion` |
| `getVehiclesInfoWithRegion` | HTTP или browser network capture | route map URL профиля с `threadId` и `openedBy[stopId]` |
| `getLine` | diagnostics/CLI dump | line/thread topology для проверки stop id, thread id и геометрии |

В URL карты обязательно должны быть `threadId` и `openedBy[stopId]`:

```text
threadId=<expected thread id>
openedBy[stopId]=<first prediction target stop id>
```

## Response Shape

Это не официальный полный контракт Яндекса. Это accepted shape: набор контейнеров
и полей, которые текущий parser умеет читать и от которых зависит решение бота.
Значения профилей не дублировать в новых местах: менять их только в
`constants.py`.

### `getVehiclePredictionInfo`

Browser capture сохраняет не весь response, а `data` конкретной машины. Если
в URL есть `id`, он переносится в `vehicleId`; если рядом уже пойман
`getVehiclesInfoWithRegion`, `threadId` пришивается по `vehicleId`.

Parser: [`vehicle_prediction.py`](../src/route74/sources/yandex/vehicle_prediction.py).
Smoke: `run_vehicle_prediction_smoke()` и `run_direction_smoke()`.

Минимальная форма:

```json
{
  "predictions": [
    {
      "vehicleId": "1651901|route74",
      "threadId": "2161326764",
      "coordinates": [83.11582825444825, 54.94095686809654],
      "stops": [
        {"stopId": "stop__9982094", "arrivalEstimation": "20:51"}
      ]
    }
  ]
}
```

Parser также принимает одиночную форму:

```json
{
  "data": {
    "threadId": "2161326764",
    "stops": [
      {"stopId": "stop__9982094", "arrivalEstimation": "20:51"}
    ]
  }
}
```

Читаемые поля:

- `threadId` - direction guard. Если совпал с `expected_thread_ids`, прогноз
  получает `HIGH` confidence. Если не совпал или отсутствует, но stop-level ETA
  найден именно для целевого stopId, прогноз принимается с `MEDIUM` confidence и
  `raw_status=vehicle_prediction_thread_fallback`.
- `stops[].stopId` - должен совпасть с одним из `prediction target stop ids`.
- `stops[].arrivalEstimation` - строка `HH:MM`, из нее считается ETA.
- `coordinates` - `[lng, lat]`, диагностическая позиция машины.
- `vehicleId` - идентификатор для диагностики и связи с `threadId`.

Fail-closed причины:

- без `predictions`/`data.stops`/`stops` -> `EMPTY`;
- без `threadId` при ожидаемом thread, но с целевым stopId ->
  `OK/MEDIUM/vehicle_prediction_thread_fallback`;
- другой `threadId`, но с целевым stopId ->
  `OK/MEDIUM/vehicle_prediction_thread_fallback`;
- нет target `stopId` -> `NO_TARGET`.

### `getStopInfo`

Parser: [`stop_info.py`](../src/route74/sources/yandex/stop_info.py).
Smoke: `run_stop_info_smoke()` и `run_stop_info_fallback_smoke()`.

Минимальная форма:

```json
{
  "data": {
    "transports": [
      {
        "lineId": "65_74_minibus_novosibirskgortrans",
        "name": "74",
        "type": "minibus",
        "threads": [
          {
            "EssentialStops": [
              {"name": "Цветной проезд", "info": {"firstStop": true}},
              {"name": "Улица Твардовского", "info": {"lastStop": true}}
            ],
            "BriefSchedule": {
              "Events": [
                {
                  "Estimated": {"value": "1780597080", "text": "20:18"},
                  "vehicleId": "novosib_obl1|route74"
                }
              ],
              "Frequencies": []
            }
          }
        ]
      }
    ]
  }
}
```

Читаемые поля:

- `data.transports` или top-level `transports` - контейнер маршрутов.
- `transports[].lineId` или пара `name=74`, `type=minibus` - выбор маршрута.
- `threads[].EssentialStops` - выбор направления по конечной остановке профиля.
- `BriefSchedule.Events[].Estimated` - единственный stop-level live ETA.
- `BriefSchedule.Events[].Scheduled` - только расписание, не live ETA.
- `BriefSchedule.Frequencies` - только интервальная диагностика.
- `Events[].vehicleId` - только id для диагностического `YandexVehicle`.

`Estimated.value` может быть Unix timestamp в секундах или миллисекундах.
Если `value` отсутствует, parser пробует `Estimated.text` в формате `HH:MM`.

### `getVehiclesInfoWithRegion`

Этот метод не дает доверенный ETA. Он нужен для координат, количества машин и
direction diagnostics.

Parser: [`parser/forecast.py`](../src/route74/sources/yandex/parser/forecast.py),
[`parser/vehicle.py`](../src/route74/sources/yandex/parser/vehicle.py),
[`parser/time_fields.py`](../src/route74/sources/yandex/parser/time_fields.py).
Smoke: `run_vehicle_parser_smoke()` и `run_direction_smoke()`.

Минимальная форма:

```json
{
  "data": {
    "vehicles": [
      {
        "features": [
          {
            "geometry": {
              "type": "LineString",
              "coordinates": [[83.110656, 54.840692], [83.110924, 54.840811]]
            }
          }
        ],
        "properties": {
          "VehicleMetaData": {
            "id": "1651901|route74",
            "Transport": {"threadId": "2161326764"}
          }
        }
      }
    ]
  }
}
```

Читаемые поля:

- `data.vehicles`, top-level `vehicles` или первый вложенный ключ `vehicles` -
  контейнер машин.
- `id`, `vehicleId`, `uid`, `properties.VehicleMetaData.id` или
  `properties.VehicleMetaData.Transport.id` - vehicle id.
- `properties.VehicleMetaData.Transport.threadId` или top-level `threadId` -
  direction guard.
- `lat`/`lng`, `geometry.coordinates`, `features[].geometry.coordinates` или
  `position` - позиция машины.
- `age`, `ageSeconds`, `timestamp`, `updatedAt`, `timeNav` - свежесть позиции.
- `arrivalMinutes`/`eta` могут встретиться, но не становятся ETA: raw vehicle ETA
  считается недостаточно надежным и используется только как диагностический
  сигнал.

Для `HTTP`/`BROWSER` source parser всегда возвращает raw vehicles как
`COORDINATES_ONLY`: arrival-поля очищаются перед отдачей в decision layer.

### `getLine`

`getLine` фиксирует топологию маршрута, а не прогноз.

Parser: [`line.py`](../src/route74/sources/yandex/line.py).
Smoke: `run_line_smoke()`.

Минимальная форма:

```json
{
  "data": {
    "activeThread": {
      "properties": {
        "ThreadMetaData": {"id": "2161326764", "lineId": "65_74_minibus_novosibirskgortrans"}
      },
      "features": []
    },
    "features": [
      {
        "properties": {
          "ThreadMetaData": {
            "id": "2161326764",
            "lineId": "65_74_minibus_novosibirskgortrans",
            "EssentialStops": [
              {"id": "stop__9982094", "name": "ВЦ"},
              {"id": "stop__9982203", "name": "Улица Твардовского"}
            ]
          }
        },
        "features": [
          {"id": "stop__9982094", "name": "ВЦ", "coordinates": [83.10261213, 54.853318735]},
          {"points": [[83.10261213, 54.853318735], [83.110924, 54.840811]]}
        ]
      }
    ]
  }
}
```

Читаемые поля:

- `ThreadMetaData.id` - thread id.
- `ThreadMetaData.lineId` - line id маршрута 74.
- `ThreadMetaData.EssentialStops` - крайние остановки направления.
- stop features `id`, `name`, `coordinates` - наличие target stop и координаты.
- point features `points` - геометрия для prediction lab.

Collector сохраняет `route_geometry` только когда выбранный по `candidate
stopIds` thread совпадает с ожидаемым `threadId` профиля. Если target stop
найден только на другом thread, tick получает `route_geometry_status =
thread_drift`, а `route_geometry_reason` фиксирует `expected`, `selected`,
`stop`, `active`, `candidates` и первые thread id topology. Такой случай
считается contract risk и не маскируется координатным fallback.

## Как Перепроверять

После изменения схемы Яндекса, остановки, направления маршрута или констант
профиля:

```bash
route74 yandex-dump --profile morning
route74 yandex-dump --profile evening
route74 yandex-line --dump path/to/dump.json
./bin/smoke-yandex
```

Дополнительно для качества накопленного history-прогноза:

```bash
route74 forecast-health
route74 forecast-readiness --window weekday_morning_09_12
route74 forecast-coverage --window weekday_morning_09_12
```

`./bin/check` проверяет общий проект, но не заменяет `./bin/smoke-yandex` после
изменения stop id, thread id или парсинга masstransit.
