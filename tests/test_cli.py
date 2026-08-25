"""Tests for the command-line entrypoint.

`main` returns an exit code rather than calling sys.exit, which is what makes
these assertions possible without wrapping every case in pytest.raises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import responses

from flightops.cli import build_parser, main, replace_fallback
from flightops.ingest import SOURCE_FIXTURE, SOURCE_LIVE

PREFIX = "FLIGHTOPS_"
BASE_URL = "https://opensky.invalid/api"
STATES_URL = f"{BASE_URL}/states/all"
FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the CLI at a fake endpoint and a temporary data root."""
    monkeypatch.setenv(PREFIX + "OPENSKY_BASE_URL", BASE_URL)
    monkeypatch.setenv(PREFIX + "DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv(PREFIX + "FIXTURE_DIR", str(FIXTURE_DIR))
    monkeypatch.setenv(PREFIX + "MAX_RETRIES", "0")
    for var in ("BBOX_LAMIN", "BBOX_LOMIN", "BBOX_LAMAX", "BBOX_LOMAX"):
        monkeypatch.delenv(PREFIX + var, raising=False)
    monkeypatch.delenv(PREFIX + "ALLOW_FIXTURE_FALLBACK", raising=False)
    return tmp_path / "data"


def _written_envelopes(data_root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(data_root.rglob("states_*.json"))
    ]


def test_version_flag_exits_zero() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0


def test_missing_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2


@responses.activate
def test_ingest_writes_bronze_and_returns_zero(
    cli_env: Path, states_payload: dict[str, Any]
) -> None:
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)

    assert main(["ingest"]) == 0

    envelopes = _written_envelopes(cli_env)
    assert len(envelopes) == 1
    assert envelopes[0]["ingest"]["source"] == SOURCE_LIVE
    assert envelopes[0]["payload"] == states_payload


@responses.activate
def test_ingest_failure_returns_one(cli_env: Path) -> None:
    responses.add(responses.GET, STATES_URL, status=503)

    assert main(["ingest"]) == 1
    assert _written_envelopes(cli_env) == [], "a failed fetch must write nothing"


@responses.activate
def test_fallback_flag_beats_the_environment(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env says no fallback; the flag says yes. The flag must win, and the
    resulting object must still be tagged as replay rather than observation."""
    monkeypatch.setenv(PREFIX + "ALLOW_FIXTURE_FALLBACK", "0")
    responses.add(responses.GET, STATES_URL, status=503)

    assert main(["ingest", "--allow-fixture-fallback"]) == 0

    envelopes = _written_envelopes(cli_env)
    assert len(envelopes) == 1
    assert envelopes[0]["ingest"]["source"] == SOURCE_FIXTURE


def test_invalid_configuration_returns_two(monkeypatch: pytest.MonkeyPatch, cli_env: Path) -> None:
    monkeypatch.setenv(PREFIX + "HTTP_TIMEOUT", "not-a-number")

    assert main(["ingest"]) == 2


@responses.activate
def test_verbose_flag_is_accepted(cli_env: Path, states_payload: dict[str, Any]) -> None:
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)

    assert main(["-v", "ingest"]) == 0


def test_parser_exposes_the_ingest_command() -> None:
    parsed = build_parser().parse_args(["ingest", "--allow-fixture-fallback"])

    assert parsed.command == "ingest"
    assert parsed.allow_fixture_fallback is True


def test_replace_fallback_does_not_mutate_the_original(settings: Any) -> None:
    permissive = replace_fallback(settings, allowed=True)

    assert permissive.allow_fixture_fallback is True
    assert settings.allow_fixture_fallback is False, "Settings is frozen for a reason"


def test_context_formatter_renders_extra_fields() -> None:
    """Regression guard: `extra=` context must reach the output, not vanish."""
    import logging

    from flightops.cli import LOG_FORMAT, ContextFormatter

    record = logging.LogRecord(
        name="flightops.ingest",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="wrote bronze snapshot",
        args=None,
        exc_info=None,
    )
    record.states = 142
    record.source = "opensky-live"

    rendered = ContextFormatter(LOG_FORMAT).format(record)

    assert "wrote bronze snapshot" in rendered
    assert "[source=opensky-live states=142]" in rendered


def test_context_formatter_omits_brackets_when_no_context() -> None:
    import logging

    from flightops.cli import LOG_FORMAT, ContextFormatter

    record = logging.LogRecord(
        name="flightops",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="plain message",
        args=None,
        exc_info=None,
    )

    assert ContextFormatter(LOG_FORMAT).format(record).endswith("plain message")


# --------------------------------------------------------------------------
# normalise
# --------------------------------------------------------------------------


def _seed_bronze(data_root: Path) -> None:
    """Copy the committed fixtures into a bronze partition."""
    import shutil

    partition = data_root / "bronze" / "dt=2026-08-25" / "hour=09"
    partition.mkdir(parents=True, exist_ok=True)
    for source in sorted(FIXTURE_DIR.glob("states_*.json")):
        shutil.copy(source, partition / source.name)


def test_normalise_writes_silver_and_returns_zero(cli_env: Path) -> None:
    _seed_bronze(cli_env)

    assert main(["normalise"]) == 0

    written = sorted((cli_env / "silver").rglob("*.parquet"))
    assert len(written) == 1
    assert written[0].parent.name.startswith("hour=")


def test_normalise_with_no_bronze_returns_one(cli_env: Path) -> None:
    assert main(["normalise"]) == 1


def test_normalise_deduplicates_the_overlapping_fixtures(cli_env: Path) -> None:
    import pyarrow.parquet as pq

    _seed_bronze(cli_env)
    main(["normalise"])

    table = pq.read_table(next((cli_env / "silver").rglob("*.parquet")))
    assert table.num_rows < 440, "the three fixtures overlap and must collapse"


def test_quality_failure_returns_three_and_writes_nothing(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch that fails the contract must leave no silver behind for a
    downstream reader to trip over."""
    from flightops import cli as cli_module
    from flightops.quality import QualityError

    def always_fails(_table: Any) -> None:
        raise QualityError("synthetic contract failure")

    _seed_bronze(cli_env)
    monkeypatch.setattr(cli_module, "assert_quality", always_fails)

    assert main(["normalise"]) == 3
    assert not list((cli_env / "silver").rglob("*.parquet")), "nothing may be written"


def test_skip_quality_writes_despite_a_failing_contract(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flightops import cli as cli_module
    from flightops.quality import QualityError

    def always_fails(_table: Any) -> None:
        raise QualityError("synthetic contract failure")

    _seed_bronze(cli_env)
    monkeypatch.setattr(cli_module, "assert_quality", always_fails)

    assert main(["normalise", "--skip-quality"]) == 0
    assert list((cli_env / "silver").rglob("*.parquet"))


def test_unreadable_bronze_returns_one(cli_env: Path) -> None:
    partition = cli_env / "bronze" / "dt=2026-08-25" / "hour=09"
    partition.mkdir(parents=True, exist_ok=True)
    (partition / "states_broken.json").write_text("{not json", encoding="utf-8")

    assert main(["normalise"]) == 1
