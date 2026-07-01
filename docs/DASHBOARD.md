# Web and Dashboard

The main Route74 user interface is now browser-based.

## What Is on the Screen

- the `🎯 Поймать 74` button;
- a quick morning/evening switch;
- cards with the main action, departure time, ETA, wait, and source;
- a short explanation of the ETA decision: the chosen signal, the risk buffer, or
  the `no ETA` reason;
- the detailed commute answer in an expandable block;
- Pushover status;
- watch control with human-readable statuses;
- a collapsed diagnostics block with `stats` and `support`.

Request errors are shown separately and do not clear the last successful result.
On a mobile screen the main button is pinned to the bottom.

## Running

Local web UI:

```bash
./bin/web
```

Or through the launcher:

```bash
route74-web-open
```

Open:

```text
http://127.0.0.1:8074
```

Remote web UI over an SSH tunnel:

```bash
./bin/web-remote
```

## Local Smoke

```bash
./bin/smoke-web-local
```

## Operator Dashboard

The old dashboard with collection stats and a support overview lives separately
again:

```bash
./bin/dashboard
```

By default it starts locally from the current database on `127.0.0.1:8075`.

For a different port or database:

```bash
./bin/dashboard --port 8076
./bin/dashboard --db data/route74.sqlite
```

## How to Read the Statuses

At the top, the dashboard should quickly answer whether the data can be trusted.
The summary is split into four parts:

- `System health`: database, collector, canary, and watch state;
- `Yandex source quality`: whether there are fresh ETA measurements and how
  complete they are;
- `History readiness`: whether the history windows are ready and bucket coverage
  is enough;
- `Runtime quality`: whether there are live bot/runtime answers and the sample is
  not too small.

Key states read as follows:

- `live ETA`: an accurate ETA from Yandex, usable for a decision;
- `coordinates_only`: coordinates exist, ETA does not; a diagnostic signal, not an
  error;
- `unavailable`: the source is down; the reason should be shown when known;
- `insufficient data`: too little data for a hard conclusion, especially on a
  small sample;
- `integrity gaps`: there is a gap between the forecast, report, and runtime layers;
- `stale history`: history exists but is out of date or does not cover the needed
  window.

`recent samples`, `window series`, and `stats` should be read together: the top
summary says whether the data can be trusted, and the lower blocks show why.

## Invariants

- the web UI must not pull business logic into itself;
- a missing Pushover must not break the page;
- watch signals stay single notifications;
- the ETA source in the UI stays `Yandex live -> Yandex history -> no ETA`.

## ETA Reason Codes

`decision_ui` returns stable `eta_reason_code` and `eta_action_code` so the
browser does not guess meaning from warning strings. The main reason codes:

- `live_eta`: a direct live ETA was chosen;
- `corrected_live`: a corrected live ETA was chosen;
- `vehicle_progress`: a coordinate-based forecast was chosen;
- `history_fallback`: a Yandex history fallback was chosen;
- `risk_buffer`: a risk buffer was added to the decision;
- `weak_live_ignored`: a weak live or coordinate signal was not chosen;
- `storage_guardrail`: statistical corrections are unavailable;
- `no_eta`: there is no accurate ETA.
