.PHONY: install test lint format typecheck run clean docker-build docker-up docker-down

VENV ?= .venv
PYTHON = $(VENV)/bin/python
PIP = $(VENV)/bin/pip

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev,test,yandex]"
	$(VENV)/bin/pre-commit install

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check src tests

format:
	$(PYTHON) -m ruff format src tests

typecheck:
	$(PYTHON) -m mypy src tests

run:
	$(PYTHON) -m route74.web.runtime

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .ruff_cache .mypy_cache *.egg-info .coverage

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down
