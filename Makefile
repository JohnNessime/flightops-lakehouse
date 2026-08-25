# Developer entrypoints for flightops-lakehouse.
#
# Every target below runs with zero AWS credentials. The AWS-facing work lives
# behind the `tf-*` targets and is never invoked by CI.
#
# Uses .RECIPEPREFIX so recipes are indented with '>' rather than a tab, which
# survives copy/paste and cross-platform editors intact. Requires GNU Make 4.x.

.RECIPEPREFIX = >
SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV := .venv
ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
else
BIN := $(VENV)/bin
endif
PY := $(BIN)/python
PIP := $(BIN)/pip

.PHONY: help venv install lint format test cov check precommit hooks \
        tf-fmt tf-validate tf-lint tf-checkov security gate clean

help:  ## Show this help
> @grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
>   | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv:  ## Create the local virtualenv
> python -m venv $(VENV)
> $(PY) -m pip install --upgrade pip

install: venv  ## Install the package with local + dev extras (no AWS)
> $(PIP) install -e ".[local,dev]"

hooks:  ## Install the pre-commit git hooks
> $(BIN)/pre-commit install

lint:  ## Ruff lint
> $(BIN)/ruff check .

format:  ## Ruff format (writes)
> $(BIN)/ruff format .

test:  ## Run the offline test suite
> $(PY) -m pytest

cov:  ## Run tests with coverage
> $(PY) -m pytest --cov --cov-report=term-missing

precommit:  ## Run every pre-commit hook against every file
> $(BIN)/pre-commit run --all-files

check: lint test  ## Fast local gate: lint + tests

tf-fmt:  ## terraform fmt -check -recursive
> terraform -chdir=infra fmt -check -recursive

tf-validate:  ## terraform init -backend=false && validate
> terraform -chdir=infra init -backend=false -input=false
> terraform -chdir=infra validate

tf-lint:  ## tflint over infra/
> tflint --chdir=infra --recursive

tf-checkov:  ## checkov static analysis over infra/
> $(BIN)/checkov -d infra --quiet --compact

security:  ## Secret scan the working tree and the full git history
> gitleaks detect --source . --no-git -v
> gitleaks detect --source . -v

gate: precommit check security  ## Full local suite — run before every push

clean:  ## Remove build, cache and dbt artefacts
> rm -rf .pytest_cache .ruff_cache .coverage htmlcov build dist *.egg-info
> rm -rf dbt/target dbt/dbt_packages dbt/logs
> find . -type d -name __pycache__ -prune -exec rm -rf {} +
