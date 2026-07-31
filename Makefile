.PHONY: help install test lint lint-fix typecheck format all apptainer-image goose goose-mcp-sdk goose-fastmcp

PYTHON ?= python3
VENV := local.venv

PY := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy
PYRIGHT := $(VENV)/bin/pyright

help:
	@echo "sandboxed-goose targets:"
	@echo ""
	@echo "  install      Create local.venv and install the package with dev dependencies"
	@echo "  test         Run contract, stdio, and local Goose integration tests"
	@echo "  lint         Run ruff checks"
	@echo "  lint-fix     Apply safe ruff fixes"
	@echo "  typecheck    Run mypy and pyright"
	@echo "  format       Format Python sources and tests"
	@echo "  all          Run lint, typecheck, and test"
	@echo "  apptainer-image  Build and check the rootless arm64 sandbox SIF"
	@echo "  goose        Run Goose with the official SDK adapter; pass ARGS='session ...'"
	@echo "  goose-mcp-sdk  Run Goose with the official SDK adapter"
	@echo "  goose-fastmcp  Run Goose with the standalone FastMCP adapter"

install:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e '.[dev]'

test:
	$(PYTEST) -q

lint:
	$(RUFF) check src/ tests/

lint-fix:
	$(RUFF) check --fix src/ tests/

typecheck:
	$(MYPY) src/
	$(PYRIGHT) src/

format:
	$(RUFF) format src/ tests/

all: lint typecheck test

apptainer-image:
	scripts/build-apptainer-image.sh

goose:
	SANDBOXED_GOOSE_MCP_IMPLEMENTATION=mcp-sdk scripts/goose.sh $(ARGS)

goose-mcp-sdk:
	SANDBOXED_GOOSE_MCP_IMPLEMENTATION=mcp-sdk scripts/goose.sh $(ARGS)

goose-fastmcp:
	SANDBOXED_GOOSE_MCP_IMPLEMENTATION=fastmcp scripts/goose.sh $(ARGS)
