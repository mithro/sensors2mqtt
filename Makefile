VENV := .venv
VENV_BIN := $(VENV)/bin

# Create venv and install project with dev dependencies.
# Re-runs when pyproject.toml changes.
$(VENV)/.stamp: pyproject.toml
	uv sync --dev
	touch $@

.PHONY: help
help: ## Show this help message
	@grep -E '^[a-zA-Z_.-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

.PHONY: setup
setup: $(VENV)/.stamp ## Create local development virtualenv

.PHONY: test
test: $(VENV)/.stamp ## Run tests
	$(VENV_BIN)/pytest

.PHONY: lint
lint: $(VENV)/.stamp ## Run linter
	$(VENV_BIN)/ruff check src/ tests/

.PHONY: fmt
fmt: $(VENV)/.stamp ## Auto-format code
	$(VENV_BIN)/ruff format src/ tests/
	$(VENV_BIN)/ruff check --fix src/ tests/

INSTALL_DIR := /opt/sensors2mqtt

.PHONY: install
install: ## Install into INSTALL_DIR (default /opt/sensors2mqtt)
	uv venv $(INSTALL_DIR)
	uv pip install --python $(INSTALL_DIR)/bin/python .

.PHONY: clean
clean: ## Remove virtualenv
	rm -rf $(VENV)
