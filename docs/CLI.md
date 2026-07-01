# CLI Reference

All commands run as `route74 <command>` once the package is installed.

## Commute and Status

```bash
route74 commute morning
route74 commute evening
route74 predict morning
route74 stats morning
route74 support-report --profile morning
route74 watch-state
route74 version
route74 explain
```

`commute` and `predict` print the same user flow as the web UI, without a
browser.

## Yandex and Forecast Health

```bash
route74 forecast-health
route74 yandex-stats --hours 24
route74 runtime-latency --hours 24
route74 runtime-events --hours 24 --limit 8
route74 monitor-tick --fail-on warning
```

## Prediction Lab

```bash
route74 prediction-lab --window weekday_morning_09_12
route74 prediction-evaluate --window weekday_morning_09_12
route74 prediction-backfill --profile all
route74 arrival-events --window weekday_morning_09_12
```

## Maintenance

```bash
route74 db-backup --help
```

## ETA Decision Reason Codes

The algorithm keeps the `Yandex live -> Yandex history -> no ETA` order, and
ships a machine-readable explanation next to the chosen ETA:

- `live_eta`: a direct live ETA passed validation.
- `corrected_live`: live ETA shifted by past errors.
- `vehicle_progress`: forecast from the vehicle coordinate, with an extra margin.
- `history_fallback`: live ETA is missing or weak, so history is used.
- `risk_buffer`: extra margin added after past source misses.
- `weak_live_ignored`: the live or coordinate signal was weak and not chosen.
- `storage_guardrail`: past corrections are unavailable, decision made without
  them.
- `no_eta`: there is no accurate ETA.

The Russian wording for these reasons is built in `presenters/`; `domain/` keeps
only the stable codes.
