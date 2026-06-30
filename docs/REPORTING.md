# Отчёты И Прогнозный Слой

Отчётный слой не заменяет raw-данные Яндекса. Он готовит витрины для
диагностики, history fallback и качества решения.

## Основные Таблицы

1. `yandex_snapshots` — raw факт каждого опроса.
2. `yandex_vehicle_observations` — нормализованные vehicle rows.
3. `yandex_forecast_samples` — одна строка прогноза на один poll и профиль.
4. `report_window_snapshots` — витрина будних окон.
5. `collector_runs` — журнал запусков сборщика.
6. `prediction_events` — runtime решения и последующая оценка фактом.

## Окна

- `weekday_morning_09_12`
- `weekday_evening_19_22`

Время считается в `Asia/Novosibirsk`.

## Правила

- history fallback читает `yandex_forecast_samples`, а не raw vehicle rows;
- один poll остаётся одним sample, даже если Яндекс показал несколько машин;
- readiness и coverage считаются отдельно от обычного live runtime;
- runtime quality хранится рядом, но не меняет source order сам по себе.

## Полезные Команды

```bash
route74 report-stats --days 30
route74 forecast-health
route74 forecast-readiness --window weekday_morning_09_12
route74 forecast-coverage --window weekday_morning_09_12
route74 forecast-backtest --window weekday_morning_09_12
route74 prediction-calibration --window weekday_morning_09_12
route74 watch-state
```

## Инварианты

- Source order: Yandex live -> Yandex history -> no ETA.
- History не подменяет сильный live ETA.
- Операторские отчёты не должны тянуть transport-specific детали в доменную
  логику.
