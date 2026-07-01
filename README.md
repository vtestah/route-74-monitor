# Route 74 Monitor

[![CI](https://github.com/vtestah/route-74-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/vtestah/route-74-monitor/actions/workflows/ci.yml)
[![Release](https://github.com/vtestah/route-74-monitor/actions/workflows/release.yml/badge.svg)](https://github.com/vtestah/route-74-monitor/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)


A personal web app that answers one question: when do I leave to catch minibus
route 74 in Novosibirsk? The main flow is a single browser button,
`🎯 Поймать 74` ("Catch the 74"). It reads a live ETA from Yandex Maps, falls
back to local Yandex history when needed, and shows `no ETA` honestly when there
is no signal. Early and final alerts can go out over Pushover, and the runtime
works fine without it.

## At a glance

- Runtime: Python 3.11+, FastAPI, SQLite.
- Source order: Yandex live -> Yandex history -> no ETA.
- One-button UX: `🎯 Поймать 74` in the browser.
- Safety buffers: 12 minutes in the morning, 17 in the evening.
- The live source lives only in `src/route74/sources/yandex/`.
- Pushover is optional: `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`.
- Data: `data/route74.sqlite`, `data/web_watches.json`.
- Secrets stay in `.env` and never in git.

## Quick start

```bash
git clone https://github.com/vtestah/route-74-monitor
cd route-74-monitor
./bin/onboard
```

Open `.env`, add Pushover keys if you want them, then run:

```bash
route74-web
```

If the launcher is not installed:

```bash
./bin/web
```

Local web-runtime smoke:

```bash
./bin/smoke-web-local
```

Operator dashboard with collection stats:

```bash
./bin/dashboard
```

Quick CLI preview:

```bash
.venv/bin/route74 commute morning
.venv/bin/route74 commute evening
```

## User flow

- The main screen shows the `🎯 Поймать 74` button.
- A short status strip above the result covers backend, Push, active watches,
  and the last update time.
- The app picks `morning` or `evening` automatically from Novosibirsk time.
- The answer stays catch-first: what to do now, when to leave, when the 74
  arrives, and how long to wait at the stop.
- Source, reliability, and Yandex status sit below the action, not above it.
- Morning and evening buffers set in the browser live in `localStorage` only;
  they never reach git or the server.
- The ETA decision is explained through separate reason/action fields: why live,
  history, vehicle coordinate, correction, risk buffer, or `no ETA` was chosen.
- The missed case is blunt on purpose: it tells you that you will not make this 74.
- Each request opens a watch for a limited time.
- Early signals and the final "leave now" go out as single Pushover messages
  when the notifier is configured.
- Without Pushover, the watches and the web UI keep working.

Two profiles, chosen by local time:

| Profile | Window | Buffer |
| --- | --- | --- |
| `morning` | `06:00-10:59` | 12 min |
| `evening` | `17:00-22:59` | 17 min |

## Architecture

- `src/route74/domain/` holds domain data and rules.
- `src/route74/services/` does snapshot collection and the decision.
- `src/route74/presenters/` turns that into human-readable text.
- `src/route74/web/` is the FastAPI app, HTML UI, and watch runtime.
- `src/route74/notifications/` is the notifier interface plus the Pushover adapter.
- `src/route74/storage/` is the SQLite schema, health, and reporting.
- `src/route74/sources/yandex/` is the live and history integration.
- `src/route74/cli/` holds diagnostic commands and a smoke-friendly preview.


## Pushover

Minimal setup:

```text
PUSHOVER_APP_TOKEN=
PUSHOVER_USER_KEY=
```

If either key is missing, a no-op notifier is used. The web app does not crash,
it just skips push notifications.

## Web config

Main variables:

```text
ROUTE74_WEB_HOST=127.0.0.1
ROUTE74_WEB_PORT=8074
ROUTE74_WEB_ALLOW_PUBLIC=0
ROUTE74_WEB_WATCH_STATE_PATH=data/web_watches.json
ROUTE74_DB_PATH=data/route74.sqlite
```

A non-loopback bind has to be turned on explicitly:

```text
ROUTE74_WEB_ALLOW_PUBLIC=1
```

The simplest external access without a domain or reverse proxy:

```text
ROUTE74_WEB_HOST=0.0.0.0
ROUTE74_WEB_ALLOW_PUBLIC=1
```

The web app is then served at `http://<server-ip>:8074/`. This is plain HTTP
with no TLS and no auth, so it only fits closed personal use.

## CLI

Handy commands:

```bash
route74 commute morning
route74 commute evening
route74 predict morning
route74 stats morning
route74 support-report --profile morning
route74 watch-state
route74 forecast-health
route74 yandex-stats --hours 24
route74 runtime-latency --hours 24
route74 runtime-events --hours 24 --limit 8
route74 monitor-tick --fail-on warning
route74 prediction-lab --window weekday_morning_09_12
route74 prediction-evaluate --window weekday_morning_09_12
route74 prediction-backfill --profile all
route74 arrival-events --window weekday_morning_09_12
route74 db-backup --help
route74 version
route74 explain
```

`commute` and `predict` print the same user flow without the web UI.

## ETA decision

The algorithm keeps the `Yandex live -> Yandex history -> no ETA` order, and
ships a machine-readable explanation next to the chosen ETA:

- `live_eta`: a direct live ETA passed validation;
- `corrected_live`: live ETA shifted by past errors;
- `vehicle_progress`: forecast from the vehicle coordinate, with an extra margin;
- `history_fallback`: live ETA is missing or weak, so history is used;
- `risk_buffer`: extra margin added after past source misses;
- `weak_live_ignored`: the live or coordinate signal was weak and not chosen;
- `storage_guardrail`: past corrections are unavailable, decision made without them;
- `no_eta`: there is no accurate ETA.

The Russian wording for these reasons is built in `presenters/`; `domain/` keeps
only the stable codes.

## Checks

Base:

```bash
./bin/check
```

Focused:

```bash
./bin/smoke-web-local
./bin/smoke-yandex
./bin/package-smoke
```

## Docs

- [docs/README.md](./docs/README.md): index.
- [docs/QUALITY.md](./docs/QUALITY.md): checks.
- [docs/SECURITY.md](./docs/SECURITY.md): `.env`, secrets, deploy hygiene.
- [docs/RUNBOOK.md](./docs/RUNBOOK.md): diagnostics.
- [docs/SERVER_DEPLOY.md](./docs/SERVER_DEPLOY.md): server run.
- [docs/REPORTING.md](./docs/REPORTING.md): forecast and reporting layer.
- [docs/DECISIONS.md](./docs/DECISIONS.md): recorded decisions.

## Invariants

- No official/gortrans fallback without a fresh decision.
- No `.env`, tokens, user keys, or real SQLite/JSON data in git.
- No exact personal addresses, floors, or work locations in docs or code.
- Business logic stays in `domain/services/presenters`, not in web or notifier.
