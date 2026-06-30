# Route 74 Monitor

[![CI](https://github.com/vtestah/route-74-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/vtestah/route-74-monitor/actions/workflows/ci.yml)
[![Release](https://github.com/vtestah/route-74-monitor/actions/workflows/release.yml/badge.svg)](https://github.com/vtestah/route-74-monitor/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)


Личное web-приложение для маршрутки 74 в Новосибирске. Основной сценарий в
браузере: `🎯 Поймать 74`. Приложение берёт live ETA из Яндекс.Карт, при
необходимости использует локальную историю Яндекса и, если данных нет, честно
показывает `no ETA`. Ранние и финальные сигналы могут приходить через
Pushover, но отсутствие настройки Pushover не ломает runtime.

## Коротко

- Runtime: Python 3.11+, FastAPI, SQLite.
- Source order: Yandex live -> Yandex history -> no ETA.
- Основной UX: одна кнопка `🎯 Поймать 74` в браузере.
- Безопасные буферы: утром `12` минут, вечером `17` минут.
- Live-источник: только `src/route74/sources/yandex/`.
- Pushover опционален: `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`.
- Данные: `data/route74.sqlite`, `data/web_watches.json`.
- Секреты: только `.env`, не в git.

## Быстрый Старт

```bash
cd /home/vladimir/work-projects/74
./bin/onboard
```

Открыть `.env`, при желании добавить Pushover-ключи, затем запустить:

```bash
route74-web
```

Если launcher не установлен:

```bash
./bin/web
```

Локальный smoke web-runtime:

```bash
./bin/smoke-web-local
```

Операторский dashboard со статистикой по сбору:

```bash
./bin/dashboard
```

CLI для быстрого предпросмотра:

```bash
.venv/bin/route74 commute morning
.venv/bin/route74 commute evening
```

## Пользовательский Сценарий

- Главный экран показывает кнопку `🎯 Поймать 74`.
- Над кнопкой/результатом есть короткая статус-лента: backend, Push,
  активные watch и время последнего обновления.
- Приложение само выбирает `morning` или `evening` по времени Новосибирска.
- Ответ остаётся catch-first: что делать сейчас, когда выйти, когда будет 74-й
  и сколько ждать у остановки.
- Источник, надёжность и статус Яндекса идут ниже действия.
- Настроенные в браузере утренний и вечерний буферы сохраняются локально в
  `localStorage`; в git и на сервер они не попадают.
- Решение ETA объясняется отдельными reason/action полями: почему выбран live,
  история, координата, поправка, запас риска или `no ETA`.
- Missed-сценарий обращается на `ты`: “на этот 74-й уже не успеешь”.
- После запроса создаётся watch на ограниченное время.
- Ранние сигналы и финальный `ВЫХОДИ СЕЙЧАС` уходят одиночными
  Pushover-уведомлениями, если notifier настроен.
- Если Pushover не настроен, watch и web UI продолжают работать без падения.

Профили:

| Профиль | Окно | Посадка | Цель | Буфер |
| --- | --- | --- | --- | --- |
| `morning` | `06:00-10:59` | Медицинский центр | Цветной проезд | `12` минут |
| `evening` | `17:00-22:59` | ВЦ | Ул. Твардовского | `17` минут |

## Архитектура

- `src/route74/domain/` — данные и правила предметной области.
- `src/route74/services/` — сбор snapshot и decision.
- `src/route74/presenters/` — человекочитаемый текст.
- `src/route74/web/` — FastAPI app, HTML UI, watch runtime.
- `src/route74/notifications/` — notifier interface и Pushover adapter.
- `src/route74/storage/` — SQLite schema, health, reporting.
- `src/route74/sources/yandex/` — live/history integration.
- `src/route74/cli/` — диагностические команды и smoke-friendly preview.

Краткая карта слоёв: [ARCHITECTURE.md](./ARCHITECTURE.md).

## Pushover

Минимальная настройка:

```text
PUSHOVER_APP_TOKEN=
PUSHOVER_USER_KEY=
```

Если один из ключей отсутствует, используется no-op notifier. Web app не
падает и просто не отправляет push-уведомления.

## Web Конфиг

Основные переменные:

```text
ROUTE74_WEB_HOST=127.0.0.1
ROUTE74_WEB_PORT=8074
ROUTE74_WEB_ALLOW_PUBLIC=0
ROUTE74_WEB_WATCH_STATE_PATH=data/web_watches.json
ROUTE74_DB_PATH=data/route74.sqlite
```

Для non-loopback bind нужно явно включить:

```text
ROUTE74_WEB_ALLOW_PUBLIC=1
```

Самый простой внешний доступ без домена и reverse proxy:

```text
ROUTE74_WEB_HOST=0.0.0.0
ROUTE74_WEB_ALLOW_PUBLIC=1
```

После этого web app открывается по адресу `http://<server-ip>:8074/`.
Это обычный HTTP без TLS и без защиты, поэтому подходит только для
закрытого личного использования.

## CLI

Полезные команды:

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

`commute` и `predict` печатают тот же пользовательский сценарий без web UI.

## ETA Решение

Алгоритм сохраняет порядок `Yandex live -> Yandex history -> no ETA`, но рядом
с выбранным ETA отдаёт машинно-читаемое объяснение:

- `live_eta` — прямой live ETA прошёл проверку;
- `corrected_live` — live ETA сдвинут по прошлым ошибкам;
- `vehicle_progress` — прогноз по координате машины, с дополнительным запасом;
- `history_fallback` — live ETA нет или он слабый, используется история;
- `risk_buffer` — добавлен запас из-за прошлых промахов источника;
- `weak_live_ignored` — live/координатный сигнал был слабым и не выбран;
- `storage_guardrail` — прошлые поправки недоступны, решение без них;
- `no_eta` — точного ETA нет.

Русский текст для этих причин формируется в `presenters/`; `domain/` хранит
только стабильные коды.

## Проверки

Базовая:

```bash
./bin/check
```

Профильные:

```bash
./bin/smoke-web-local
./bin/smoke-yandex
./bin/package-smoke
```

## Документация

- [docs/README.md](./docs/README.md) — индекс.
- [docs/QUALITY.md](./docs/QUALITY.md) — проверки.
- [docs/SECURITY.md](./docs/SECURITY.md) — `.env`, секреты, deploy hygiene.
- [docs/RUNBOOK.md](./docs/RUNBOOK.md) — диагностика.
- [docs/SERVER_DEPLOY.md](./docs/SERVER_DEPLOY.md) — серверный запуск.
- [docs/REPORTING.md](./docs/REPORTING.md) — forecast/reporting слой.
- [docs/DECISIONS.md](./docs/DECISIONS.md) — зафиксированные решения.
- [docs/AI_FIRST.md](./docs/AI_FIRST.md) — agent/harness контур.

## Инварианты

- Не возвращать official/gortrans fallback без нового решения.
- Не хранить `.env`, токены, user keys и реальные SQLite/JSON данные в git.
- Не писать точные личные адреса, этажи и рабочие локации в docs или код.
- Business-логика остаётся в `domain/services/presenters`, а не в web/notifier.
