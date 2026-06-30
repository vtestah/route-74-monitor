# Решения

## 2026-06-08: Старый chat runtime удалён в пользу web app

Пользовательский transport переведён из чат-runtime в браузерное приложение на
FastAPI. Основной сценарий остался тем же: `🎯 Поймать 74`, catch-first ответ и
тот же commute/presenter слой.

## 2026-06-08: Уведомления вынесены в notifier interface

Pushover отправляется через отдельный adapter в `src/route74/notifications/`.
Web handlers не знают про Pushover HTTP API напрямую.

## 2026-06-08: Pushover остаётся опциональным

Если `PUSHOVER_APP_TOKEN` и `PUSHOVER_USER_KEY` не настроены, runtime
использует no-op notifier и не падает.

## 2026-06-08: Watch остаётся одиночными сигналами

Ранние сигналы и финальный `ВЫХОДИ СЕЙЧАС` отправляются одиночными
уведомлениями. Это сохраняет понятный alarm-path и не смешивает несколько
сигналов в одну доставку.

## 2026-06-08: Бизнес-логика commute сохранена

`domain/services/presenters`, Yandex live/history, утренний и вечерний буферы,
decision logic и CLI preview сохранены без transport compatibility facade.
