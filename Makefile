SHELL := /bin/sh

PYTHON ?= python
PIP ?= $(PYTHON) -m pip
BENCHMARK_OUTPUT_DIR ?= benchmarks/results
BENCHMARK_TRADE_COUNT ?= 1000

.PHONY: help install test demo lint benchmark docker-build docker-run

.DEFAULT_GOAL := help

help:
	@printf "%s\n" \
		"Common targets:" \
		"  make install       Install runtime and dev dependencies." \
		"  make test          Run the full pytest suite." \
		"  make demo          Run the deterministic demo." \
		"  make lint          Run Ruff when available, otherwise a syntax-only fallback." \
		"  make benchmark     Run the offline trading benchmark and write reports to $(BENCHMARK_OUTPUT_DIR)." \
		"  make docker-build  Build the Docker image with docker compose." \
		"  make docker-run    Start the Docker stack with docker compose."

install:
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	$(PIP) install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

demo:
	$(PYTHON) demo.py

lint:
	@if $(PYTHON) -m ruff --version >/dev/null 2>&1; then \
		$(PYTHON) -m ruff check .; \
	else \
		printf '%s\n' "ruff is not installed; running syntax-only fallback. Run 'make install' for full lint tooling."; \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m compileall src tests demo.py jupiter_sentinel_cli.py benchmarks; \
	fi

benchmark:
	$(PYTHON) benchmarks/benchmark_trading.py --trade-count $(BENCHMARK_TRADE_COUNT) --output-dir $(BENCHMARK_OUTPUT_DIR)

docker-build:
	docker compose build

docker-run:
	docker compose up --build
