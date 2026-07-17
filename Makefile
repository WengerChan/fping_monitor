.PHONY: help install test lint clean docker docker-up docker-down

PY     ?= python3
PIP    ?= $(PY) -m pip

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install:  ## Install Python dependencies
	$(PIP) install -r requirements.txt

test:  ## Run the test suite
	$(PY) -m pytest -q

lint:  ## Compile-check
	$(PY) -m compileall monitor.py scheduler.py detector.py notifier.py database.py models.py util.py

clean:  ## Remove caches and local state
	rm -rf __pycache__ */__pycache__ state.db state.db-* logs/*.log.* .pytest_cache

docker:  ## Build the Docker image
	docker build -t fping-monitor:latest .

docker-up:  ## Run daemon in docker compose
	docker compose up -d

docker-down:  ## Stop docker compose
	docker compose down
