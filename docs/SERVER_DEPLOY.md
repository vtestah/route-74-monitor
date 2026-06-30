# Запуск На Сервере

Инструкция для Linux-сервера с `systemd`. На сервере обычно живут два процесса:

- `route74-web` — web приложение;
- `route74 yandex-collect` — сбор истории Яндекса.

## Подготовка

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip sqlite3 rsync sudo
sudo useradd --system --create-home --shell /bin/bash route74
sudo mkdir -p /opt/route74
sudo chown route74:route74 /opt/route74
```

Для Playwright/Chromium может понадобиться системный Chrome и swap, если
collector работает через browser mode.

## Деплой Кода

С локальной машины:

```bash
./bin/server-sync
```

Скрипт переносит код в `/opt/route74`, не трогает `.env`, `.venv` и `data/`,
а затем запускает `bin/server-update`. `server-update` сам ставит актуальные
`systemd` unit-файлы из `deploy/systemd/` и убирает legacy `route74-bot` /
`route74-dashboard`.

## `.env`

Минимально:

```text
ROUTE74_DB_PATH=/opt/route74/data/route74.sqlite
ROUTE74_WEB_HOST=127.0.0.1
ROUTE74_WEB_PORT=8074
ROUTE74_WEB_ALLOW_PUBLIC=0
ROUTE74_WEB_WATCH_STATE_PATH=/opt/route74/data/web_watches.json
ROUTE74_MORNING_WALK_MINUTES=12
ROUTE74_EVENING_WALK_MINUTES=17
ROUTE74_YANDEX_ENABLED=1
ROUTE74_YANDEX_MODE=auto
PUSHOVER_APP_TOKEN=
PUSHOVER_USER_KEY=
```

Если Pushover не настроен, web runtime всё равно должен подняться.

## Быстрая Проверка

```bash
cd /opt/route74
.venv/bin/route74 version
.venv/bin/route74 forecast-health
.venv/bin/route74 yandex-stats --hours 24
.venv/bin/route74-web --help
.venv/bin/python -m route74.smoke.web_runtime_smoke
```

## systemd

Актуальный шаблон лежит в `deploy/systemd/route74-web.service`.

Для collector используется `deploy/systemd/route74-yandex-collect.service`.

Если запуск идёт через `./bin/server-sync`, вручную ставить unit обычно не
нужно: `bin/server-update` синхронизирует их сам.

Пример unit для web app:

```ini
[Unit]
Description=Route74 web app
After=network.target

[Service]
User=route74
WorkingDirectory=/opt/route74
Environment=ROUTE74_ENV_FILE=/opt/route74/.env
ExecStart=/opt/route74/.venv/bin/route74-web
Restart=always

[Install]
WantedBy=multi-user.target
```

После установки:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now route74-web
sudo systemctl status route74-web --no-pager
```

Collector можно запускать отдельным service/timer.

## Доступ

По умолчанию web app слушает `127.0.0.1:8074`.

Если нужен самый простой доступ без домена и без reverse proxy, можно
открыть приложение по публичному IP сервера:

```text
ROUTE74_WEB_HOST=0.0.0.0
ROUTE74_WEB_ALLOW_PUBLIC=1
```

Тогда URL будет вида `http://<server-ip>:8074/`. Это обычный HTTP без TLS и
без защиты, поэтому подходит только для личного закрытого использования.

Если нужен более аккуратный внешний доступ, ставьте reverse proxy и HTTPS.
