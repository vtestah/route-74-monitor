# Running on a Server

Instructions for a Linux server with `systemd`. The server usually runs two
processes:

- `route74-web`: the web app;
- `route74 yandex-collect`: Yandex history collection.

## Preparation

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip sqlite3 rsync sudo
sudo useradd --system --create-home --shell /bin/bash route74
sudo mkdir -p /opt/route74
sudo chown route74:route74 /opt/route74
```

Playwright/Chromium may need system Chrome and swap if the collector runs in
browser mode.

## Deploying Code

From the local machine:

```bash
./bin/server-sync
```

The script copies the code to `/opt/route74`, leaves `.env`, `.venv`, and `data/`
alone, and then runs `bin/server-update`. `server-update` installs the current
`systemd` unit files from `deploy/systemd/` and removes the legacy `route74-bot`
and `route74-dashboard`.

## `.env`

Minimal:

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

If Pushover is not configured, the web runtime should still start.

## Quick Check

```bash
cd /opt/route74
.venv/bin/route74 version
.venv/bin/route74 forecast-health
.venv/bin/route74 yandex-stats --hours 24
.venv/bin/route74-web --help
.venv/bin/python -m route74.smoke.web_runtime_smoke
```

## systemd

The current template is in `deploy/systemd/route74-web.service`.

The collector uses `deploy/systemd/route74-yandex-collect.service`.

If you deploy through `./bin/server-sync`, installing the unit by hand is usually
not needed: `bin/server-update` syncs them itself.

Example unit for the web app:

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

After installing:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now route74-web
sudo systemctl status route74-web --no-pager
```

The collector can run as a separate service or timer.

## Access

By default the web app listens on `127.0.0.1:8074`.

For the simplest access without a domain or a reverse proxy, you can open the app
on the server public IP:

```text
ROUTE74_WEB_HOST=0.0.0.0
ROUTE74_WEB_ALLOW_PUBLIC=1
```

The URL is then `http://<server-ip>:8074/`. This is plain HTTP with no TLS and no
protection, so it only fits closed personal use.

For cleaner external access, put a reverse proxy and HTTPS in front.
