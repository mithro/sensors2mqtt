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
CONFIG_DIR := /etc/sensors2mqtt
SYSTEMD_DIR := /etc/systemd/system

# Shared install step: create venv + install package + create config dir
.PHONY: _install-base
_install-base:
	sudo uv venv $(INSTALL_DIR)
	sudo uv pip install --python $(INSTALL_DIR)/bin/python .
	sudo mkdir -p $(CONFIG_DIR)

# Helper: install + enable a systemd service
# Usage: $(call install-service,service-name)
define install-service
	sudo cp deploy/$(1).service $(SYSTEMD_DIR)/$(1).service
	sudo systemctl daemon-reload
	sudo systemctl enable $(1)
	sudo systemctl restart $(1)
	@echo "$(1) installed and started"
endef

.PHONY: install-snmp
install-snmp: _install-base ## Install SNMP collector + PoE control service (ten64)
	sudo cp snmp.toml $(CONFIG_DIR)/snmp.toml
	$(call install-service,sensors2mqtt-snmp)
	$(call install-service,sensors2mqtt-snmp-control)

.PHONY: install-hwmon
install-hwmon: _install-base ## Install hwmon collector (sw-bb-25g)
	$(call install-service,sensors2mqtt-hwmon)

.PHONY: install-ipmi
install-ipmi: _install-base ## Install IPMI SDR collector (big-storage)
	$(call install-service,sensors2mqtt-ipmi-sdr)

.PHONY: clean
clean: ## Remove virtualenv
	rm -rf $(VENV)
