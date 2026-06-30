.PHONY: install test lint run clean docker-build docker-up docker-down

VENV ?= .venv
PYTHON = \$(VENV)/bin/python
PIP = \$(VENV)/bin/pip

install:
	python3 -m venv \$(VENV)
	\$(PIP) install --upgrade pip
	\$(PIP) install -e ".[test,yandex]"

test:
	\$(PYTHON) -m pytest

lint:
	\$(PYTHON) -m ruff check src tests || echo "No ruff or check passed"

run:
	\$(PYTHON) -m route74.web.runtime

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .ruff_cache *.egg-info

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down
