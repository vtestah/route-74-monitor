# Качество

## Базовая Проверка

```bash
./bin/check
```

Что входит в базовый прогон:

- agent-harness и AI-first audit;
- shell syntax для `bin/*`;
- `compileall` и cleanup bytecode;
- smoke package-модули;
- web runtime smoke;
- package smoke;
- CLI help/version/explain и базовые reject-cases;
- отсутствие drift между docs, env и runtime.

## Профильные Проверки

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

Commute слой:

```bash
.venv/bin/python -m route74.smoke.commute_smoke
```

## Правила Чистого Кода

- `domain/` — без web, notifier, SQLite и HTTP.
- `services/` — orchestration, без FastAPI/HTML/Pushover.
- `presenters/` — только текст.
- `web/` — transport и UI, без бизнес-правил.
- `notifications/` — отправка уведомлений через interface/adapter.
- `storage/` — schema, migrations, reports, health.

## После Изменений

- Менял web/UI/notifier: `./bin/check`, `./bin/smoke-web-local`.
- Менял Yandex integration: `./bin/check`, `./bin/smoke-yandex`.
- Менял docs/rules/skills: `./bin/ai-first-audit`, затем `./bin/check`.
