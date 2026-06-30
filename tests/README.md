# Tests (pytest layer)

Этот каталог — pytest-слой для локальной разработки. Он **дополняет**, а не
заменяет встроенный smoke-харнесс проекта.

## Что канон

- `./bin/check` — канонический gate проекта (smoke-модули, CLI, границы слоёв,
  окружение). Прод-деплой (`bin/server-update`) тоже гоняет smoke.
- `src/route74/smoke/*` — источник правды для проверок; pytest их не заменяет.

## Что здесь

- `conftest.py` — общие фикстуры (temp SQLite, фейковые источники, фикс времени),
  переиспользуют существующие `reporting_smoke_fixtures`.
- `test_dashboard_data.py` — модульные тесты агрегации дашборда
  (порог малой выборки, одинарное округление, p80/avg).
- `test_smoke_suite.py` — мост: автодискавер `route74.smoke.*_smoke` и прогон
  каждого `main()` как отдельного pytest-кейса.

## Запуск

```bash
pip install -e '.[test]'
pytest -q
```

pytest — dev-зависимость (`[project.optional-dependencies] test`). На сервер не
устанавливается и в прод-pipeline не входит.
