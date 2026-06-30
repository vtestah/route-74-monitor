# Runbook Диагностики

Короткая карта инцидентов для web runtime и прогнозного слоя.

## Первый Осмотр

```bash
git status --short --untracked-files=all
./bin/check
route74 support-snapshot --profile morning
route74 support-report --profile morning
```

Фиксируй только symptom, профиль и статус источника. Секреты и персональные
данные в отчёты не копировать.

## Web Runtime

Если браузерный сценарий ведёт себя странно:

```bash
./bin/smoke-web-local
route74 watch-state
route74 stats morning
route74 stats evening
```

Что смотреть:

- `watch-state` — активные watch, просрочку, runtime errors файла.
- `stats` — Yandex live/history статус, readiness и next action.
- `support-report` — полный профильный snapshot.

## Pushover

Если уведомления не приходят:

1. Проверить, что заданы оба ключа: `PUSHOVER_APP_TOKEN`,
   `PUSHOVER_USER_KEY`.
2. Убедиться, что web app продолжает работать и без уведомлений.
3. Проверить локально `./bin/smoke-web-local`.
4. Проверить сетевую доступность `api.pushover.net` уже вне репозитория.

## Yandex

Если ETA выглядит ненадёжно:

```bash
./bin/smoke-yandex
route74 yandex-canary --profile all --strict
route74 forecast-health
route74 forecast-readiness --window weekday_morning_09_12
route74 forecast-coverage --window weekday_morning_09_12
```

Если `yandex-canary` даёт warning, не добавляй новый fallback-источник. Чини
contract, parser или readiness.

## SQLite И Отчёты

```bash
route74 db-health
route74 db-migrations
route74 report-stats --days 30
route74 yandex-stats --hours 24
```

## После Исправления

```bash
./bin/check
```
