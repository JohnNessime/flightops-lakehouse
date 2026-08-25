"""Guards on the dbt project that do not require running dbt.

These exist because the most damaging mistakes in this layer are ones a green
`dbt build` would not catch: a credential accidentally committed to the example
profile, or an adapter-specific branch creeping into model SQL and quietly
ending the one-codebase-two-engines property the project is built on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

DBT_DIR = Path(__file__).parent.parent / "dbt"
MODELS = sorted(DBT_DIR.glob("models/**/*.sql"))


def test_models_exist() -> None:
    assert len(MODELS) >= 6, "staging + intermediate + four marts"


def test_there_are_at_least_four_marts() -> None:
    assert len(list((DBT_DIR / "models" / "marts").glob("mart_*.sql"))) >= 4


# --------------------------------------------------------------------------
# The example profile must never hold a real value
# --------------------------------------------------------------------------


@pytest.fixture
def example_profile() -> dict:
    return yaml.safe_load((DBT_DIR / "profiles.example.yml").read_text(encoding="utf-8"))


def test_example_profile_defines_both_adapters(example_profile: dict) -> None:
    outputs = example_profile["flightops"]["outputs"]

    assert set(outputs) == {"duckdb", "athena"}
    assert outputs["duckdb"]["type"] == "duckdb"
    assert outputs["athena"]["type"] == "athena"


def test_default_target_needs_no_credentials(example_profile: dict) -> None:
    """The quickstart promise: clone, install, build -- no AWS account."""
    assert example_profile["flightops"]["target"] == "duckdb"


def test_athena_target_holds_no_literal_account_or_bucket(example_profile: dict) -> None:
    """Every AWS-shaped value must come from the environment. There must be no
    place in this file where a real bucket or account id could sit."""
    athena = example_profile["flightops"]["outputs"]["athena"]
    literal_ok = {"type", "database", "table_type", "format", "write_compression"}

    for key, value in athena.items():
        if key in literal_ok or not isinstance(value, str):
            continue
        assert "env_var(" in value, f"{key} is a literal, it must come from env_var"


def test_no_twelve_digit_strings_anywhere_in_the_dbt_project() -> None:
    """An AWS account id is 12 digits and gitleaks does not flag it."""
    import re

    for path in DBT_DIR.rglob("*"):
        if not path.is_file() or "target" in path.parts or "dbt_packages" in path.parts:
            continue
        if path.name == "profiles.yml":  # gitignored, developer-local
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not re.search(r"\b\d{12}\b", text), f"12-digit string in {path.name}"


# --------------------------------------------------------------------------
# The dual-adapter property
# --------------------------------------------------------------------------


def _sql_without_comments(path: Path) -> str:
    """Strip SQL and Jinja comments.

    Without this, a comment explaining why a function is avoided trips the very
    guard that enforces avoiding it.
    """
    import re

    text = path.read_text(encoding="utf-8")
    text = re.sub(r"{#.*?#}", " ", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"--.*", " ", text)


def test_no_model_branches_on_the_adapter() -> None:
    """The point of the project is one model codebase on two engines. A
    `target.type` conditional in a model is the first crack in that, so it
    fails here rather than being noticed a year later."""
    offenders = [
        path.name
        for path in MODELS
        if "target.type" in _sql_without_comments(path)
        or "adapter.type" in _sql_without_comments(path)
    ]

    assert not offenders, f"adapter-specific branching found in {offenders}"


def test_no_model_uses_engine_specific_regex_functions() -> None:
    """DuckDB spells it regexp_matches, Athena spells it regexp_like. Either
    one silently ties a model to one engine."""
    offenders = [
        path.name
        for path in MODELS
        for token in ("regexp_matches", "regexp_like", "strftime")
        if token in _sql_without_comments(path).lower()
    ]

    assert not offenders, f"engine-specific function in {offenders}"


def test_marts_are_materialised_as_tables() -> None:
    """On Athena a view re-scans silver on every query, and bytes scanned is
    the only thing Athena bills for."""
    project = yaml.safe_load((DBT_DIR / "dbt_project.yml").read_text(encoding="utf-8"))

    assert project["models"]["flightops"]["marts"]["+materialized"] == "table"


def test_seed_is_committed_and_not_swallowed_by_gitignore() -> None:
    """*.csv is gitignored for lake data; seeds need an explicit exception, and
    a silently-ignored seed breaks the build only on a fresh clone."""
    seed = DBT_DIR / "seeds" / "carrier_codes.csv"

    assert seed.is_file()
    ignore_rules = (DBT_DIR.parent / ".gitignore").read_text(encoding="utf-8")
    assert "!dbt/seeds/*.csv" in ignore_rules
