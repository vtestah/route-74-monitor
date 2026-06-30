# Route 74 Monitor

[![CI](https://github.com/vtestah/route-74-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/vtestah/route-74-monitor/actions/workflows/ci.yml)
[![Release](https://github.com/vtestah/route-74-monitor/actions/workflows/release.yml/badge.svg)](https://github.com/vtestah/route-74-monitor/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A small personal web app for one question: when do I leave to catch minibus route 74 in Novosibirsk? It reads a live ETA from Yandex Maps, falls back to local Yandex history, and shows "no ETA" honestly when there is no signal.

## Stack
- Python 3.11+, FastAPI, SQLite, Pydantic, Uvicorn
- Playwright for the Yandex browser source (optional `yandex` extra)
- pytest for unit and smoke tests

## How it decides
Source order is Yandex live, then Yandex history, then "no ETA". Safety buffers are 12 minutes in the morning and 17 in the evening. Pushover notifications are optional and the runtime works without them.

## Run
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[yandex]"
playwright install chromium
route74 --help
route74-web
```
Configuration is read from environment variables; no keys are committed.

## Tests
```bash
pip install -e ".[test,yandex]"
pytest
```
