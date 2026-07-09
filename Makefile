.EXPORT_ALL_VARIABLES:
.ONESHELL:
.SILENT:

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
MAKEFLAGS += --no-builtin-rules --no-builtin-variables
export PATH := $(abspath .venv)/bin:$(PATH)

default: .venv help

.PHONY: help check e2e lint test py-format py-lint py-types py-update py-reset \
	install build release major minor patch

###############################################################################
# Python dev (lint / format / types)
###############################################################################

check: lint test ## Run all checks (lint + offline tests; live verification runs from acumatica-baseline)

lint: py-format py-lint py-types ## Lint Python code

test: ## Run the test suite (pytest, offline — no live instance needed)
	$(call header,Running pytest)
	uv run pytest

e2e: ## Live E2E vs the data-repo instance: provision scratch tenant, diff, destroy (needs tailnet + decrypted .env)
	test -e acu.yaml || { echo "acu.yaml not found — symlink it from ../acumatica-baseline"; exit 1; }
	test -e .env || { echo ".env missing — run 'make decrypt' in ../acumatica-baseline"; exit 1; }
	$(call header,Running live E2E (creates and destroys tenant E2E))
	uv run pytest -o addopts= -m e2e -v -s tests/e2e

py-format:
	$(call header,Running Ruff format)
	uv run ruff format

py-lint:
	$(call header,Running Ruff lint)
	uv run ruff check --fix

py-types:
	$(call header,Running basedpyright typecheck)
	uv run basedpyright

py-update: ## Recreate venv, upgrade all dependencies
	uv venv --clear && hash -r && uv sync --upgrade

py-reset:
	rm -rf build/ dist/ *.egg-info
	find . -type d -name "__pycache__" -exec rm -rf {} +
	uv venv --clear && hash -r && uv sync --quiet

.venv: uv.lock
	uv venv --clear && hash -r && uv sync

uv.lock: pyproject.toml
	uv lock --upgrade && touch $(@)

###############################################################################
# Install
###############################################################################

install: .venv ## Install Python dependencies (uv sync)

build: ## Install acu globally as an editable uv tool
	$(call header,Installing acu via uv tool)
	uv tool install --editable .

###############################################################################
# Release
###############################################################################

# `make release <part>` passes the part as an extra goal; pick it out and
# give the part words no-op recipes so make does not try to build them.
part := $(word 1,$(filter major minor patch,$(MAKECMDGOALS)))

release: ## Bump version, commit, tag, and publish a GitHub release (make release major|minor|patch)
	test -n "$(part)" || { echo "usage: make release major|minor|patch"; exit 1; }
	git diff --quiet && git diff --cached --quiet \
		|| { echo "working tree not clean — commit or stash first"; exit 1; }
	$(call header,Bumping $(part) version)
	uv version --bump $(part)
	version=$$(uv version --short)
	git add pyproject.toml uv.lock
	git commit -m "Release v$$version"
	git tag "v$$version"
	$(MAKE) build
	$(call header,Publishing v$$version to GitHub)
	git push && git push --tags
	gh release create "v$$version" --title "v$$version" --generate-notes
	echo "$(green)Released v$$version$(reset)"

major minor patch:
	@:

###############################################################################
# Colors and Headers
###############################################################################

TERM := xterm-256color

blue := $$(tput setaf 4)
green := $$(tput setaf 2)
yellow := $$(tput setaf 3)
reset := $$(tput sgr0)

define header
echo "$(blue)==> $(1) <==$(reset)"
endef

help:
	echo "$(blue)Usage: $(green)make [recipe]$(reset)"
	echo "$(blue)Recipes:$(reset)"
	awk 'BEGIN {FS = ":.*?## "; sort_cmd = "sort"} /^[a-zA-Z0-9_-]+:.*?## / \
	{ printf "  \033[33m%-10s\033[0m %s\n", $$1, $$2 | sort_cmd; } \
	END {close(sort_cmd)}' $(MAKEFILE_LIST)
