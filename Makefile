.PHONY: help install dev test test-cov lint fmt clean docker docker-up docker-down

PY     ?= python3
PIP    ?= $(PY) -m pip

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install:  ## Install production deps only
	$(PIP) install -r requirements.txt

dev:  ## Install dev + test deps
	$(PIP) install -r requirements-dev.txt

test:  ## Run the test suite
	$(PY) -m pytest -q

test-cov:  ## Run tests with coverage report
	$(PY) -m pytest --cov=. --cov-report=term-missing

lint:  ## Compile-check (no type checker configured)
	$(PY) -m compileall monitor.py scheduler.py detector.py notifier.py database.py models.py util.py

clean:  ## Remove caches and local state
	rm -rf __pycache__ */__pycache__ data/*.db data/*.db-* logs/*.log.* .pytest_cache .coverage

docker:  ## Build the Docker image
	docker build -t fping-monitor:latest .

docker-up:  ## Run daemon in docker compose
	docker compose up -d

docker-down:  ## Stop docker compose
	docker compose down
