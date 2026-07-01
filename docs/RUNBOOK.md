# Diagnostics Runbook

A short incident map for the web runtime and the forecast layer.

## First Look

```bash
git status --short --untracked-files=all
./bin/check
route74 support-snapshot --profile morning
route74 support-report --profile morning
```

Record only the symptom, the profile, and the source status. Do not copy secrets
or personal data into reports.

## Web Runtime

If the browser flow behaves oddly:

```bash
./bin/smoke-web-local
route74 watch-state
route74 stats morning
route74 stats evening
```

What to look at:

- `watch-state`: active watches, expiry, runtime errors in the file.
- `stats`: Yandex live/history status, readiness, and next action.
- `support-report`: the full per-profile snapshot.

## Pushover

If notifications do not arrive:

1. Check that both keys are set: `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`.
2. Confirm the web app keeps working without notifications.
3. Check locally with `./bin/smoke-web-local`.
4. Check network access to `api.pushover.net` outside the repo.

## Yandex

If the ETA looks unreliable:

```bash
./bin/smoke-yandex
route74 yandex-canary --profile all --strict
route74 forecast-health
route74 forecast-readiness --window weekday_morning_09_12
route74 forecast-coverage --window weekday_morning_09_12
```

If `yandex-canary` warns, do not add a new fallback source. Fix the contract, the
parser, or readiness.

## SQLite and Reports

```bash
route74 db-health
route74 db-migrations
route74 report-stats --days 30
route74 yandex-stats --hours 24
```

## After a Fix

```bash
./bin/check
```
