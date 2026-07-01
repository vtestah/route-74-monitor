# Data Sources

## Current Strategy

The runtime uses Yandex-family sources only:

1. Yandex Maps as the live ETA.
2. Local Yandex history from `route74 yandex-collect` as a fallback forecast.

The official schedule is no longer used in the runtime. If neither live nor
history yields an ETA, the app says honestly that there is no accurate signal.

## Yandex

The main internal masstransit methods:

- `getVehiclePredictionInfo`: the best live ETA when the response contains the
  boarding stop of the current profile.
- `getStopInfo`: a stop-level source; `Estimated` can be used as an ETA,
  `Scheduled`/`Frequencies` only as diagnostics.
- `getLine`: route topology, `threadId`, stop order, geometry. This is not an ETA.
- `getStopTimetable`: schedule and frequencies, not a live ETA without `Estimated`.
- `getVehiclesInfoWithRegion`: diagnostic coordinates and the nested
  `VehicleMetaData.Transport.threadId`.
  This `threadId` filters the profile direction and, when possible, is stitched
  onto `getVehiclePredictionInfo` by `vehicleId`.
  The route URL is opened with an explicit `threadId` and `openedBy[stopId]`,
  otherwise Yandex may return vehicles of the opposite direction as the main case.
  A raw ETA above 60 minutes for the profile is not used as a forecast sample:
  such values look like a full route loop, not the nearest boarding.

Commands:

```bash
route74 yandex-dump --profile morning
route74 yandex-line --dump path/to/dump.json
route74 yandex-collect --once --profile all
```

The detailed per-profile stop/thread contract lives in
[`YANDEX_CONTRACT.md`](./YANDEX_CONTRACT.md). It records which stop ids are used
for the stop-level page, which target stop ids are checked inside
`getVehiclePredictionInfo`, which `threadId`s confirm the direction, and why the
runtime must fail-closed on a mismatch.

## Statistics

`route74 yandex-collect` writes Yandex snapshots and vehicle observations into
SQLite. This is the base for a future Yandex-only forecast: accumulate pairs of
"vehicle position and threadId now -> how long it took to reach my stop".

For now it is a fact layer, not a separate forecast model.
