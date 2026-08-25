"""Data quality contracts for the silver layer.

Why contracts rather than dbt tests alone: dbt tests run after the data has
already been written, so a bad batch is caught downstream of the thing that
produced it. These checks run *before* the write, which means a violation is
attributable to the snapshot that caused it rather than discovered later
against a table that several runs have contributed to.

The distinction between a violation and a warning is deliberate and worth
stating plainly:

- A **violation** means the row cannot be trusted as an observation: a
  coordinate outside the possible range of the Earth, an altitude no aircraft
  has reached, a missing primary key. These fail the run.
- A **warning** means the row is legitimate but incomplete -- most often an
  aircraft transmitting without a position fix, which is normal and common.
  Failing on these would mean failing on almost every real batch, and a check
  that always fires is a check nobody reads.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

import pyarrow as pa

from flightops.normalise import SILVER_SCHEMA

logger = logging.getLogger(__name__)

# Physical plausibility bounds. Deliberately generous: the goal is to catch
# unit errors, sign flips and corruption, not to second-guess aviation.
LATITUDE_RANGE = (-90.0, 90.0)
LONGITUDE_RANGE = (-180.0, 180.0)
# Karman line at 100 km as an absolute ceiling; -500 m allows for aerodromes
# below sea level (Bar Yehuda in the Dead Sea sits at roughly -380 m).
ALTITUDE_RANGE_M = (-500.0, 100_000.0)
# The SR-71 topped out near 980 m/s. Anything faster is corruption.
VELOCITY_RANGE_MS = (0.0, 1_000.0)
TRUE_TRACK_RANGE_DEG = (0.0, 360.0)
VERTICAL_RATE_RANGE_MS = (-200.0, 200.0)
ICAO24_LENGTH = 6


class QualityError(AssertionError):
    """Raised when a batch violates the silver contract."""


@dataclass
class QualityReport:
    """The outcome of checking one batch."""

    rows: int = 0
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def ok(self) -> bool:
        return not self.violations

    def violation(self, message: str) -> None:
        self.violations.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def summary(self) -> str:
        return (
            f"{self.rows} rows, {len(self.violations)} violation(s), "
            f"{len(self.warnings)} warning(s)"
        )


def _check_schema(table: pa.Table, report: QualityReport) -> None:
    """The schema is a contract with every downstream consumer.

    Compared field by field rather than with `schema.equals` so the failure
    message names the offending column instead of dumping two full schemas and
    leaving the reader to diff them.
    """
    actual = {f.name: f.type for f in table.schema}
    expected = {f.name: f.type for f in SILVER_SCHEMA}

    for name, dtype in expected.items():
        if name not in actual:
            report.violation(f"schema: column {name!r} is missing")
        elif actual[name] != dtype:
            report.violation(f"schema: column {name!r} is {actual[name]}, expected {dtype}")

    for name in actual:
        if name not in expected:
            report.violation(f"schema: unexpected column {name!r}")


def _nulls(table: pa.Table, column: str) -> int:
    return table[column].null_count if column in table.schema.names else 0


def _check_keys(table: pa.Table, report: QualityReport) -> None:
    for column in ("icao24", "origin_country", "last_contact", "on_ground", "observed_at"):
        nulls = _nulls(table, column)
        if nulls:
            report.violation(f"{column}: {nulls} null value(s), the contract requires non-null")

    if "icao24" in table.schema.names:
        malformed = [
            value
            for value in table["icao24"].to_pylist()
            if value is None or len(value) != ICAO24_LENGTH or not _is_hex(value)
        ]
        if malformed:
            report.violation(
                f"icao24: {len(malformed)} value(s) are not {ICAO24_LENGTH}-digit hex, "
                f"e.g. {malformed[:3]}"
            )


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _check_range(
    table: pa.Table,
    column: str,
    bounds: tuple[float, float],
    report: QualityReport,
) -> None:
    if column not in table.schema.names:
        return
    low, high = bounds
    values = table[column].to_pylist()
    # A wrong-typed column is already reported by the schema check. Comparing
    # a string against a float here would raise TypeError and abort the whole
    # run, hiding every other violation behind a stack trace.
    mistyped = [v for v in values if v is not None and not isinstance(v, int | float)]
    if mistyped:
        report.violation(
            f"{column}: {len(mistyped)} non-numeric value(s), cannot range-check, "
            f"e.g. {mistyped[:3]}"
        )
        return
    offenders = [value for value in values if value is not None and not low <= value <= high]
    if offenders:
        report.violation(
            f"{column}: {len(offenders)} value(s) outside [{low}, {high}], e.g. {offenders[:3]}"
        )
    report.counts[f"{column}_null"] = _nulls(table, column)


def _check_uniqueness(table: pa.Table, report: QualityReport) -> None:
    """The silver table must hold one row per observation.

    Duplicates here mean deduplication failed, which would inflate every
    downstream count without any obvious symptom.
    """
    if not {"icao24", "time_position", "last_contact"} <= set(table.schema.names):
        return
    keys = list(
        zip(
            table["icao24"].to_pylist(),
            [
                tp if tp is not None else lc
                for tp, lc in zip(
                    table["time_position"].to_pylist(),
                    table["last_contact"].to_pylist(),
                    strict=True,
                )
            ],
            strict=True,
        )
    )
    duplicated = [key for key, count in Counter(keys).items() if count > 1]
    if duplicated:
        report.violation(
            f"uniqueness: {len(duplicated)} duplicate (icao24, time_position) key(s), "
            f"e.g. {duplicated[:2]}"
        )


def _check_completeness(table: pa.Table, report: QualityReport) -> None:
    """Report incompleteness that is expected, so it stays visible without failing."""
    if not table.num_rows:
        return
    for column in ("latitude", "longitude", "callsign", "baro_altitude_m"):
        nulls = _nulls(table, column)
        if nulls:
            share = nulls / table.num_rows
            report.warn(f"{column}: {nulls} null(s) ({share:.1%} of rows)")
        report.counts[f"{column}_null"] = nulls

    if {"latitude", "longitude"} <= set(table.schema.names):
        half_positions = [
            (lat, lon)
            for lat, lon in zip(
                table["latitude"].to_pylist(), table["longitude"].to_pylist(), strict=True
            )
            if (lat is None) != (lon is None)
        ]
        if half_positions:
            report.violation(
                f"position: {len(half_positions)} row(s) have one coordinate but not the other"
            )


def check(table: pa.Table) -> QualityReport:
    """Run every contract against a batch and return the findings."""
    report = QualityReport(rows=table.num_rows)

    _check_schema(table, report)
    _check_keys(table, report)
    _check_uniqueness(table, report)
    _check_range(table, "latitude", LATITUDE_RANGE, report)
    _check_range(table, "longitude", LONGITUDE_RANGE, report)
    _check_range(table, "baro_altitude_m", ALTITUDE_RANGE_M, report)
    _check_range(table, "geo_altitude_m", ALTITUDE_RANGE_M, report)
    _check_range(table, "velocity_ms", VELOCITY_RANGE_MS, report)
    _check_range(table, "true_track_deg", TRUE_TRACK_RANGE_DEG, report)
    _check_range(table, "vertical_rate_ms", VERTICAL_RATE_RANGE_MS, report)
    _check_completeness(table, report)

    for message in report.warnings:
        logger.warning("quality warning", extra={"detail": message})
    for message in report.violations:
        logger.error("quality violation", extra={"detail": message})
    logger.info("quality check complete", extra={"result": report.summary()})

    return report


def assert_quality(table: pa.Table) -> QualityReport:
    """Check a batch and fail loudly on any violation.

    Loudly is the operative word: the exception carries every violation, not
    just the first, so one run tells you everything that is wrong instead of
    forcing a fix-and-retry loop.
    """
    report = check(table)
    if not report.ok:
        detail = "\n  - ".join(report.violations)
        raise QualityError(
            f"silver batch failed {len(report.violations)} quality contract(s):\n  - {detail}"
        )
    return report
