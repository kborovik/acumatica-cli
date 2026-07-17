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

check: .venv lint test ## Run all checks (lint + offline tests; live verification = make e2e)

lint: .venv py-format py-lint py-types ## Lint Python code

test: .venv ## Run the test suite (pytest, offline — no live instance needed)
	$(call header,Running pytest)
	uv run pytest

# `make e2e FILE=<path-or-stem>` scopes the run to one e2e file (tried as a
# path, then tests/e2e/<FILE>, then tests/e2e/<FILE>.py); unset = whole tier.
e2e_target := $(if $(FILE),$(firstword $(wildcard $(FILE) tests/e2e/$(FILE) tests/e2e/$(FILE).py)),tests/e2e)

e2e: ## Live E2E, self-contained: scaffold a tmp data repo from packaged templates, create scratch tenants, apply, diff, destroy (needs tailnet + decrypted .env); FILE=<path-or-stem> scopes to one file
	test -e .env || { echo ".env missing — decrypt .env.gpg at the repo root"; exit 1; }
	test -n "$(e2e_target)" || { echo "no e2e file matches FILE=$(FILE) (tried it as a path, tests/e2e/$(FILE), tests/e2e/$(FILE).py)"; exit 1; }
	$(call header,Running live E2E (creates and destroys scratch tenants))
	uv run pytest -o addopts= -m e2e -v -s $(e2e_target)

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

install: .venv ## Install acu globally as an editable uv tool
	$(call header,Installing acu via uv tool)
	uv tool install --editable .

###############################################################################
# Release
###############################################################################

# `make release <part>` passes the part as an extra goal; pick it out and
# give the part words no-op recipes so make does not try to build them.
part := $(word 1,$(filter major minor patch,$(MAKECMDGOALS)))

# Each recipe line is a fresh shell (Apple make ignores .ONESHELL). Re-read
# the version from pyproject via `uv version --short` on every line that
# needs it — a shell `version=...` assignment does not survive the next line.
release: ## Bump version, commit, tag, and publish a GitHub release (make release major|minor|patch)
	test -n "$(part)" || { echo "usage: make release major|minor|patch"; exit 1; }
	git diff --quiet && git diff --cached --quiet \
		|| { echo "working tree not clean — commit or stash first"; exit 1; }
	$(call header,Bumping $(part) version)
	uv version --bump $(part)
	git add pyproject.toml uv.lock
	git commit -m "Release v$$(uv version --short)"
	git tag "v$$(uv version --short)"
	$(MAKE) build
	$(call header,Publishing v$$(uv version --short) to GitHub)
	git push && git push --tags
	gh release create "v$$(uv version --short)" --title "v$$(uv version --short)" --generate-notes
	echo "$(green)Released v$$(uv version --short)$(reset)"

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
