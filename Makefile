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
# Exactly what CI installs, so a local audit and a CI audit see the same
# set. checkov lives in the [iac] extra and is deliberately absent: it is a
# lint tool, not a dependency, and it pins transitive packages with open
# CVEs and no upgrade path. tf-checkov runs it from pre-commit's own pinned,
# isolated environment instead.
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

# ---------------------------------------------------------------------------
# Pipeline. Everything below runs on DuckDB with no AWS credentials.
# ---------------------------------------------------------------------------

# dbt takes --project-dir / --profiles-dir AFTER the subcommand, not before.
DBT := $(BIN)/dbt
DBT_DIRS := --project-dir dbt --profiles-dir dbt

dbt/profiles.yml:
> cp dbt/profiles.example.yml dbt/profiles.yml
> @echo "created dbt/profiles.yml from the example (gitignored; duckdb target needs no edits)"

DATA_ROOT ?= data

ensure-data:
# dbt-duckdb will not create the parent directory of its database file, so a
# fresh clone fails with a bare IOException before any model runs. Done in
# Python rather than `mkdir -p`, which is unavailable when make falls back to
# cmd.exe on Windows.
> @$(PY) -c "import pathlib; pathlib.Path('$(DATA_ROOT)').mkdir(parents=True, exist_ok=True)"

ingest: | ensure-data  ## Fetch one live snapshot from OpenSky into bronze
> $(BIN)/flightops ingest

replay: | ensure-data  ## Replay the committed fixtures into bronze. No network.
> $(BIN)/flightops ingest --from-fixtures

normalise:  ## Bronze -> typed silver Parquet, gated on the quality contract
> $(BIN)/flightops normalise

dbt-build: dbt/profiles.yml | ensure-data  ## Seed, run and test every dbt model on DuckDB
> $(DBT) build $(DBT_DIRS)

dbt-test: dbt/profiles.yml  ## Run dbt tests only
> $(DBT) test $(DBT_DIRS)

dbt-docs: dbt/profiles.yml  ## Generate and serve the dbt docs site
> $(DBT) docs generate $(DBT_DIRS)
> $(DBT) docs serve $(DBT_DIRS)

pipeline: replay normalise dbt-build  ## Full offline pipeline from committed fixtures

# Mirrors .github/workflows/ci.yml step for step, including building the lake
# from fixtures first. Without that, dbt has no source data and this target
# fails on a fresh clone -- which is precisely how the omission was found.
ci: lint test pipeline  ## Exactly what CI runs. No AWS credentials at any point.

LAMBDA_BUILD := build/lambda_ingest

lambda-package:  ## Build the Lambda deployment package (run before terraform plan)
# Installs only what the ingest path actually imports, then copies the package
# source. `pip install .` would pull pyarrow -- 84 MiB of it -- for a handler
# that never touches Parquet: pyarrow belongs to normalise, which does not run
# in Lambda. boto3 is likewise omitted because the runtime provides it, and
# shipping a second copy would dwarf everything else in the zip.
#
# Built here rather than from Terraform because the contract forbids
# local-exec: Terraform zips a directory, it does not run a build tool.
> rm -rf $(LAMBDA_BUILD)
> $(PY) -c "import pathlib; pathlib.Path('$(LAMBDA_BUILD)').mkdir(parents=True, exist_ok=True)"
> $(PIP) install requests --target $(LAMBDA_BUILD) --quiet --no-compile
> $(PY) -c "import shutil; shutil.copytree('src/flightops', '$(LAMBDA_BUILD)/flightops', dirs_exist_ok=True)"
> cp orchestration/lambda_ingest/handler.py $(LAMBDA_BUILD)/
> @$(PY) -c "import pathlib; d=pathlib.Path('$(LAMBDA_BUILD)'); b=sum(f.stat().st_size for f in d.rglob('*') if f.is_file()); n=sum(1 for f in d.rglob('*') if f.is_file()); print(f'  packaged {n} files, {b/1024/1024:.1f} MiB -> $(LAMBDA_BUILD)')"

tf-fmt:  ## terraform fmt -check -recursive
> terraform -chdir=infra fmt -check -recursive

tf-validate:  ## terraform init -backend=false && validate
> terraform -chdir=infra init -backend=false -input=false
> terraform -chdir=infra validate

tf-lint:  ## tflint over infra/
> tflint --chdir=infra --recursive

tf-checkov:  ## checkov static analysis over infra/
# Runs through pre-commit, which installs and pins its own checkov in an
# isolated environment. That keeps the lint tool out of the project venv --
# so `make audit` and CI audit the same dependency set -- and means the hook
# and this target can never drift to different checkov versions.
> $(BIN)/pre-commit run checkov --all-files

tf: tf-fmt tf-validate tf-lint tf-checkov  ## Full Terraform gate. Never touches AWS.

audit:  ## Audit installed dependencies for known CVEs
> $(BIN)/pip-audit --skip-editable --progress-spinner=off

security:  ## Secret scan the working tree and the full git history
> gitleaks detect --source . --no-git -v
> gitleaks detect --source . -v

gate: precommit check security  ## Full local suite — run before every push

clean:  ## Remove build, cache and dbt artefacts
> rm -rf .pytest_cache .ruff_cache .coverage htmlcov build dist *.egg-info
> rm -rf dbt/target dbt/dbt_packages dbt/logs logs build
> find . -type d -name __pycache__ -prune -exec rm -rf {} +
