"""Tests for the silver quality contracts.

Tables are built directly rather than routed through normalisation, because the
point is to exercise what happens when bad data *does* reach the check -- which
normalisation is specifically designed to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pytest

from flightops.normalise import SILVER_SCHEMA
from flightops.quality import QualityError, assert_quality, check

OBSERVED = datetime(2026, 8, 25, 9, 46, 5, tzinfo=UTC)

# Nullability is relaxed so that tests can construct the very rows the contract
# is meant to reject. Types are unchanged, so the schema check still passes and
# the key checks are what fire.
RELAXED_SCHEMA = pa.schema([pa.field(f.name, f.type, nullable=True) for f in SILVER_SCHEMA])


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "icao24": "3c4b4c",
        "callsign": "EWG47B",
        "origin_country": "Germany",
        "time_position": OBSERVED,
        "last_contact": OBSERVED,
        "longitude": 6.8475,
        "latitude": 45.9781,
        "baro_altitude_m": 11879.58,
        "on_ground": False,
        "velocity_ms": 224.73,
        "true_track_deg": 174.75,
        "vertical_rate_ms": 0.0,
        "geo_altitude_m": 12298.68,
        "squawk": "1000",
        "spi": False,
        "position_source": 0,
        "position_source_label": "ADS-B",
        "ingest_source": "opensky-live",
        "observed_at": OBSERVED,
        "dt": "2026-08-25",
        "hour": "09",
    }
    base.update(overrides)
    return base


def _table(*rows: dict[str, Any], relaxed: bool = False) -> pa.Table:
    schema = RELAXED_SCHEMA if relaxed else SILVER_SCHEMA
    return pa.Table.from_pylist(list(rows) or [_row()], schema=schema)


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_clean_batch_passes() -> None:
    report = check(_table())

    assert report.ok
    assert report.violations == []
    assert report.rows == 1


def test_assert_quality_returns_the_report_on_success() -> None:
    assert assert_quality(_table()).ok


def test_summary_is_human_readable() -> None:
    assert "1 rows" in check(_table()).summary()


def test_empty_batch_is_not_a_violation() -> None:
    """An empty window is a legitimate outcome -- no aircraft in the box -- not
    a data defect."""
    assert check(pa.Table.from_pylist([], schema=SILVER_SCHEMA)).ok


# --------------------------------------------------------------------------
# Range contracts
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value", "fragment"),
    [
        ("latitude", 91.0, "latitude"),
        ("latitude", -90.5, "latitude"),
        ("longitude", 181.0, "longitude"),
        ("longitude", -180.1, "longitude"),
        ("baro_altitude_m", 150_000.0, "baro_altitude_m"),
        ("baro_altitude_m", -900.0, "baro_altitude_m"),
        ("geo_altitude_m", 250_000.0, "geo_altitude_m"),
        ("velocity_ms", 2_000.0, "velocity_ms"),
        ("velocity_ms", -5.0, "velocity_ms"),
        ("true_track_deg", 400.0, "true_track_deg"),
        ("vertical_rate_ms", 900.0, "vertical_rate_ms"),
    ],
)
def test_out_of_range_values_are_violations(column: str, value: float, fragment: str) -> None:
    report = check(_table(_row(**{column: value})))

    assert not report.ok
    assert any(fragment in v for v in report.violations)


def test_violation_message_names_the_offending_value() -> None:
    """A message that says only 'range check failed' costs the reader a query."""
    report = check(_table(_row(latitude=91.0)))

    assert "91.0" in report.violations[0]


def test_values_at_the_boundary_are_accepted() -> None:
    assert check(_table(_row(latitude=90.0, longitude=180.0, true_track_deg=360.0))).ok


def test_nulls_are_exempt_from_range_checks() -> None:
    """A missing altitude is incomplete, not out of range."""
    assert check(_table(_row(baro_altitude_m=None, velocity_ms=None))).ok


# --------------------------------------------------------------------------
# Key contracts
# --------------------------------------------------------------------------


def test_null_primary_key_is_a_violation() -> None:
    report = check(_table(_row(icao24=None), relaxed=True))

    assert not report.ok
    assert any("icao24" in v for v in report.violations)


def test_null_origin_country_is_a_violation() -> None:
    report = check(_table(_row(origin_country=None), relaxed=True))

    assert any("origin_country" in v and "null" in v for v in report.violations)


def test_malformed_icao24_is_a_violation() -> None:
    report = check(_table(_row(icao24="XYZ!!!"), relaxed=True))

    assert any("hex" in v for v in report.violations)


def test_wrong_length_icao24_is_a_violation() -> None:
    report = check(_table(_row(icao24="abc"), relaxed=True))

    assert any("icao24" in v for v in report.violations)


def test_duplicate_keys_are_a_violation() -> None:
    """Duplicates here mean deduplication failed upstream, which would inflate
    every downstream count with no visible symptom."""
    report = check(_table(_row(), _row()))

    assert any("uniqueness" in v for v in report.violations)


def test_same_aircraft_at_different_times_is_not_a_duplicate() -> None:
    later = OBSERVED.replace(second=30)
    report = check(_table(_row(), _row(time_position=later, last_contact=later)))

    assert report.ok


def test_position_less_rows_are_keyed_by_last_contact_not_collapsed() -> None:
    other = OBSERVED.replace(second=40)
    report = check(
        _table(
            _row(icao24="aaa111", time_position=None),
            _row(icao24="bbb222", time_position=None, last_contact=other),
        )
    )

    assert report.ok


# --------------------------------------------------------------------------
# Schema contract
# --------------------------------------------------------------------------


def test_missing_column_is_a_violation() -> None:
    table = _table().drop_columns(["squawk"])

    report = check(table)

    assert any("squawk" in v and "missing" in v for v in report.violations)


def test_unexpected_column_is_a_violation() -> None:
    table = _table().append_column("smuggled", pa.array(["value"]))

    report = check(table)

    assert any("unexpected column" in v for v in report.violations)


def test_wrong_type_is_a_violation() -> None:
    """A silent type drift breaks every downstream consumer at once."""
    wrong = pa.schema(
        [pa.field(f.name, pa.string() if f.name == "latitude" else f.type) for f in SILVER_SCHEMA]
    )
    table = pa.Table.from_pylist([_row(latitude="45.9")], schema=wrong)

    report = check(table)

    assert any("latitude" in v and "expected" in v for v in report.violations)


# --------------------------------------------------------------------------
# Warnings versus violations
# --------------------------------------------------------------------------


def test_missing_position_warns_but_does_not_fail() -> None:
    """Aircraft transmitting without a position fix are normal and common. A
    check that fires on every real batch is a check nobody reads."""
    report = check(_table(_row(latitude=None, longitude=None)))

    assert report.ok
    assert any("latitude" in w for w in report.warnings)


def test_missing_callsign_warns_but_does_not_fail() -> None:
    report = check(_table(_row(callsign=None)))

    assert report.ok
    assert any("callsign" in w for w in report.warnings)


def test_half_a_position_is_a_violation_not_a_warning() -> None:
    """One coordinate without the other is corruption, not incompleteness."""
    report = check(_table(_row(latitude=45.9, longitude=None)))

    assert not report.ok
    assert any("one coordinate but not the other" in v for v in report.violations)


# --------------------------------------------------------------------------
# assert_quality
# --------------------------------------------------------------------------


def test_assert_quality_raises_on_violation() -> None:
    with pytest.raises(QualityError, match="quality contract"):
        assert_quality(_table(_row(latitude=91.0)))


def test_assert_quality_reports_every_violation_at_once() -> None:
    """One run should tell you everything that is wrong, not force a
    fix-and-rerun loop."""
    with pytest.raises(QualityError) as exc_info:
        assert_quality(_table(_row(latitude=91.0, longitude=181.0, velocity_ms=5_000.0)))

    message = str(exc_info.value)
    assert "latitude" in message
    assert "longitude" in message
    assert "velocity_ms" in message
    assert "3 quality contract(s)" in message


def test_assert_quality_does_not_raise_on_warnings_alone() -> None:
    assert assert_quality(_table(_row(callsign=None))).warnings
