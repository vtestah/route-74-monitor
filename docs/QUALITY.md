# Quality

## Base Check

```bash
./bin/check
```

What the base run covers:

- shell syntax for `bin/*`;
- `compileall` and bytecode cleanup;
- smoke package modules;
- web runtime smoke;
- package smoke;
- CLI help/version/explain and basic reject cases;
- no drift between docs, env, and runtime.

## Focused Checks

Web runtime:

```bash
./bin/smoke-web-local
```

Yandex contract:

```bash
./bin/smoke-yandex
```

Packaging:

```bash
./bin/package-smoke
```

Commute layer:

```bash
.venv/bin/python -m route74.smoke.commute_smoke
```

## Clean Code Rules

- `domain/`: no web, notifier, SQLite, or HTTP.
- `services/`: orchestration, no FastAPI/HTML/Pushover.
- `presenters/`: text only.
- `web/`: transport and UI, no business rules.
- `notifications/`: sending through an interface and adapter.
- `storage/`: schema, migrations, reports, health.

## After Changes

- Changed web/UI/notifier: `./bin/check`, `./bin/smoke-web-local`.
- Changed Yandex integration: `./bin/check`, `./bin/smoke-yandex`.
