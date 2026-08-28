VENV := .venv
VENV_BIN := $(VENV)/bin

# Create venv and install project with dev dependencies and all extras.
# --all-extras is needed so the full test suite can import optional deps
# (e.g. the ipmi collector's `requests`, declared under the `ipmi` extra).
# Re-runs when pyproject.toml changes.
$(VENV)/.stamp: pyproject.toml
	uv sync --dev --all-extras
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

# Production installs are the Debian packages (see README "Install");
# the fleet is converged by the ansible `sensors2mqtt` role. The old
# `make install-*` targets that built a uv venv under /opt/sensors2mqtt
# were removed 2026-08-28 — that layout was orphaned by every python3.N
# removal and had been superseded by the debs since 0.3.
.PHONY: clean
clean: ## Remove virtualenv
	rm -rf $(VENV)
