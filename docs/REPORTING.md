# Reports and Forecast Layer

The reporting layer does not replace Yandex raw data. It prepares views for
diagnostics, history fallback, and decision quality.

## Main Tables

1. `yandex_snapshots`: the raw fact of each poll.
2. `yandex_vehicle_observations`: normalized vehicle rows.
3. `yandex_forecast_samples`: one forecast row per poll and profile.
4. `report_window_snapshots`: the weekday-window view.
5. `collector_runs`: the collector run log.
6. `prediction_events`: runtime decisions and their later evaluation against fact.

## Windows

- `weekday_morning_09_12`
- `weekday_evening_19_22`

Time is counted in `Asia/Novosibirsk`.

## Rules

- history fallback reads `yandex_forecast_samples`, not raw vehicle rows;
- one poll stays one sample, even if Yandex showed several vehicles;
- readiness and coverage are computed separately from the normal live runtime;
- runtime quality is stored alongside but does not change the source order by itself.

## Useful Commands

```bash
route74 report-stats --days 30
route74 forecast-health
route74 forecast-readiness --window weekday_morning_09_12
route74 forecast-coverage --window weekday_morning_09_12
route74 forecast-backtest --window weekday_morning_09_12
route74 prediction-calibration --window weekday_morning_09_12
route74 watch-state
```

## Invariants

- Source order: Yandex live -> Yandex history -> no ETA.
- History does not replace a strong live ETA.
- Operator reports must not pull transport-specific details into the domain logic.
