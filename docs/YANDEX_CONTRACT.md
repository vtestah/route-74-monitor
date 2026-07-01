# Yandex Stop/Thread Contract

This document records how the app understands Yandex stops and directions for
route 74. The source of truth for runtime values stays in
[`src/route74/sources/yandex/constants.py`](../src/route74/sources/yandex/constants.py):
this document explains the contract but does not replace the code.

## Purpose

The Yandex masstransit API returns several similar stop ids for one boarding zone
and different `threadId`s for the route directions. The app does not guess the
direction from a stop name: a live ETA counts as usable only after the stop/thread
contract of the current profile is verified.

If the contract is not confirmed, the runtime must fail-closed: use Yandex history
or say honestly that there is no accurate ETA.

## Profiles

| Profile | stopInfo stop id | prediction target stop ids | expected thread ids | Meaning |
| --- | --- | --- | --- | --- |
| `morning` | `stop__9982194` | `stop__9982194` | `2161326768` | morning boarding stop toward the morning terminal |
| `evening` | `stop__9982094` | `stop__9982094` | `2161326764` | evening boarding stop toward the evening terminal |

The `stopInfo stop id` is used to open the Yandex stop-level page. The `prediction
target stop id` is checked inside the `getVehiclePredictionInfo` response. Do not
use old alternative ids: morning is fixed on `stop__9982194`, evening on
`stop__9982094`.

Terminal stop ids from `getLine` for the current profile direction: `morning` is
`3174363647`, `evening` is `stop__9982203`.

## Trust Rules

`getVehiclePredictionInfo` counts as a live ETA only if all of these hold:

- the vehicle `threadId` matches the profile `expected thread ids`;
- the `stops` list of that specific vehicle contains one of the `prediction target
  stop ids`;
- the ETA does not look like a full route loop: a raw ETA above 60 minutes is not
  written as a forecast sample.

If the `threadId` is unknown, the direction did not match, or the target stop id is
not found, the forecast gets `NO_TARGET` and is not used as an ETA.

## Yandex Methods

- `getVehiclePredictionInfo`: the main live ETA after the stop/thread check.
- `getStopInfo`: a stop-level source; `Estimated` can be used as an ETA,
  `Scheduled`/`Frequencies` stay diagnostics.
- `getVehiclesInfoWithRegion`: diagnostic coordinates, `vehicleId`, and the nested
  `VehicleMetaData.Transport.threadId`; when possible this `threadId` is stitched
  onto `getVehiclePredictionInfo` by `vehicleId`.
- `getLine`: route topology, thread ids, stop order, and geometry. This is not an
  ETA.
- `getStopTimetable`: schedule and frequencies, not a live ETA without `Estimated`.

The route URL is opened with an explicit `threadId` and `openedBy[stopId]` so that
Yandex loads the right direction immediately. `route_thread_params()` builds the
parameters.

## Where to Keep Details

- Runtime profile values: only
  [`constants.py`](../src/route74/sources/yandex/constants.py).
- The minimal request/payload/response contract: this document.
- Executable shape examples: the smoke cases in
  [`sources/yandex/smoke/`](../src/route74/sources/yandex/smoke/).
- Raw Yandex dumps: locally in `data/` or in temp files from
  `route74 yandex-dump`; do not keep them in git.

Full Yandex responses are noisy and can contain unstable fields. In git it is
better to keep only the minimal shapes the parser and runtime actually read. If a
regression against a real response is needed, add a sanitized fixture with a
minimal set of fields, without session/query/token details.

## Requests

Yandex methods are called as HTTP endpoints or captured by the browser from the
Network tab. Not all query parameters matter for the runtime, only the binding to
the profile:

| Method | How it reaches the runtime | What the request sets |
| --- | --- | --- |
| `getStopInfo` | browser network capture | stop page URL with the stop id from `STOP_ID_BY_PROFILE` |
| `getVehiclePredictionInfo` | browser network capture after a click on a vehicle | vehicle `id` in the query; `threadId` stitched from a nearby `getVehiclesInfoWithRegion` |
| `getVehiclesInfoWithRegion` | HTTP or browser network capture | route map URL of the profile with `threadId` and `openedBy[stopId]` |
| `getLine` | diagnostics/CLI dump | line/thread topology to check stop id, thread id, and geometry |

The map URL must include `threadId` and `openedBy[stopId]`:

```text
threadId=<expected thread id>
openedBy[stopId]=<first prediction target stop id>
```

## Response Shape

This is not the official full Yandex contract. It is the accepted shape: the set of
containers and fields the current parser can read and the bot decision depends on.
Do not duplicate profile values in new places: change them only in `constants.py`.

### `getVehiclePredictionInfo`

The browser capture stores not the whole response but the `data` of a specific
vehicle. If the URL has an `id`, it is carried into `vehicleId`; if a
`getVehiclesInfoWithRegion` was already captured nearby, the `threadId` is stitched
by `vehicleId`.

Parser: [`vehicle_prediction.py`](../src/route74/sources/yandex/vehicle_prediction.py).
Smoke: `run_vehicle_prediction_smoke()` and `run_direction_smoke()`.

Minimal form:

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

The parser also accepts a single form:

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

Fields read:

- `threadId`: the direction guard. If it matches `expected_thread_ids`, the
  forecast gets `HIGH` confidence. If it is missing or does not match but a
  stop-level ETA is found for the target stopId, the forecast is accepted with
  `MEDIUM` confidence and `raw_status=vehicle_prediction_thread_fallback`.
- `stops[].stopId`: must match one of the `prediction target stop ids`.
- `stops[].arrivalEstimation`: an `HH:MM` string, the ETA is computed from it.
- `coordinates`: `[lng, lat]`, the diagnostic vehicle position.
- `vehicleId`: an id for diagnostics and for linking to `threadId`.

Fail-closed reasons:

- no `predictions`/`data.stops`/`stops` -> `EMPTY`;
- no `threadId` with an expected thread but with the target stopId ->
  `OK/MEDIUM/vehicle_prediction_thread_fallback`;
- a different `threadId` but with the target stopId ->
  `OK/MEDIUM/vehicle_prediction_thread_fallback`;
- no target `stopId` -> `NO_TARGET`.

### `getStopInfo`

Parser: [`stop_info.py`](../src/route74/sources/yandex/stop_info.py).
Smoke: `run_stop_info_smoke()` and `run_stop_info_fallback_smoke()`.

Minimal form:

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

Fields read:

- `data.transports` or top-level `transports`: the route container.
- `transports[].lineId`, or the pair `name=74`, `type=minibus`: route selection.
- `threads[].EssentialStops`: direction selection by the profile terminal stop.
- `BriefSchedule.Events[].Estimated`: the only stop-level live ETA.
- `BriefSchedule.Events[].Scheduled`: schedule only, not a live ETA.
- `BriefSchedule.Frequencies`: interval diagnostics only.
- `Events[].vehicleId`: id for the diagnostic `YandexVehicle` only.

`Estimated.value` can be a Unix timestamp in seconds or milliseconds. If `value` is
missing, the parser tries `Estimated.text` in `HH:MM` form.

### `getVehiclesInfoWithRegion`

This method does not give a trusted ETA. It is used for coordinates, vehicle count,
and direction diagnostics.

Parser: [`parser/forecast.py`](../src/route74/sources/yandex/parser/forecast.py),
[`parser/vehicle.py`](../src/route74/sources/yandex/parser/vehicle.py),
[`parser/time_fields.py`](../src/route74/sources/yandex/parser/time_fields.py).
Smoke: `run_vehicle_parser_smoke()` and `run_direction_smoke()`.

Minimal form:

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

Fields read:

- `data.vehicles`, top-level `vehicles`, or the first nested `vehicles` key: the
  vehicle container.
- `id`, `vehicleId`, `uid`, `properties.VehicleMetaData.id`, or
  `properties.VehicleMetaData.Transport.id`: the vehicle id.
- `properties.VehicleMetaData.Transport.threadId` or top-level `threadId`: the
  direction guard.
- `lat`/`lng`, `geometry.coordinates`, `features[].geometry.coordinates`, or
  `position`: the vehicle position.
- `age`, `ageSeconds`, `timestamp`, `updatedAt`, `timeNav`: position freshness.
- `arrivalMinutes`/`eta` may appear but do not become an ETA: the raw vehicle ETA
  is treated as not reliable enough and is used only as a diagnostic signal.

For an `HTTP`/`BROWSER` source the parser always returns raw vehicles as
`COORDINATES_ONLY`: arrival fields are cleared before handing off to the decision
layer.

### `getLine`

`getLine` records route topology, not a forecast.

Parser: [`line.py`](../src/route74/sources/yandex/line.py).
Smoke: `run_line_smoke()`.

Minimal form:

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

Fields read:

- `ThreadMetaData.id`: the thread id.
- `ThreadMetaData.lineId`: the line id of route 74.
- `ThreadMetaData.EssentialStops`: the terminal stops of the direction.
- stop features `id`, `name`, `coordinates`: presence of the target stop and its
  coordinates.
- point features `points`: geometry for the prediction lab.

The collector saves `route_geometry` only when the thread chosen by `candidate
stopIds` matches the expected profile `threadId`. If the target stop is found only
on another thread, the tick gets `route_geometry_status = thread_drift`, and
`route_geometry_reason` records `expected`, `selected`, `stop`, `active`,
`candidates`, and the first topology thread ids. This case is treated as a contract
risk and is not masked by a coordinate fallback.

## How to Re-check

After a change in the Yandex schema, the stop, the route direction, or the profile
constants:

```bash
route74 yandex-dump --profile morning
route74 yandex-dump --profile evening
route74 yandex-line --dump path/to/dump.json
./bin/smoke-yandex
```

Additionally, for the quality of the accumulated history forecast:

```bash
route74 forecast-health
route74 forecast-readiness --window weekday_morning_09_12
route74 forecast-coverage --window weekday_morning_09_12
```

`./bin/check` checks the overall project but does not replace `./bin/smoke-yandex`
after a change to stop id, thread id, or masstransit parsing.
