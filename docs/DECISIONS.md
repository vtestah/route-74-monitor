# Decisions

## 2026-06-08: Old chat runtime dropped in favor of the web app

The user-facing transport moved from a chat runtime to a browser app on FastAPI.
The main flow stayed the same: `🎯 Поймать 74`, a catch-first answer, and the same
commute and presenter layer.

## 2026-06-08: Notifications moved behind a notifier interface

Pushover is sent through a separate adapter in `src/route74/notifications/`. Web
handlers do not talk to the Pushover HTTP API directly.

## 2026-06-08: Pushover stays optional

If `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY` are not set, the runtime uses a
no-op notifier and does not crash.

## 2026-06-08: Watch stays single signals

Early signals and the final `ВЫХОДИ СЕЙЧАС` go out as single notifications. This
keeps a clear alarm path and does not merge several signals into one delivery.

## 2026-06-08: Commute business logic kept

`domain/services/presenters`, Yandex live and history, the morning and evening
buffers, the decision logic, and the CLI preview were kept without a transport
compatibility facade.
