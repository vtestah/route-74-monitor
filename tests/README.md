# Tests (pytest layer)

This directory is the pytest layer for local development. It complements the
project smoke harness, it does not replace it.

## What Is Canonical

- `./bin/check` is the canonical project gate (smoke modules, CLI, layer
  boundaries, environment). The production deploy (`bin/server-update`) runs smoke
  too.
- `src/route74/smoke/*` is the source of truth for checks; pytest does not replace
  it.

## What Is Here

- `conftest.py`: shared fixtures (temp SQLite, fake sources, fixed time), reusing
  the existing `reporting_smoke_fixtures`.
- `test_dashboard_data.py`: unit tests for dashboard aggregation (small-sample
  threshold, single rounding, p80/avg).
- `test_smoke_suite.py`: the bridge that auto-discovers `route74.smoke.*_smoke` and
  runs each `main()` as a separate pytest case.

## Running

```bash
pip install -e '.[test]'
pytest -q
```

pytest is a dev dependency (`[project.optional-dependencies] test`). It is not
installed on the server and is not part of the production pipeline.
