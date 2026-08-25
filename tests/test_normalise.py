"""Tests for the bronze -> silver normalisation layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from flightops.config import Settings
from flightops.normalise import (
    PARTITION_COLUMNS,
    SILVER_SCHEMA,
    NormaliseError,
    dedup_key,
    deduplicate,
    find_bronze_objects,
    normalise,
    parse_state_vector,
    read_bronze_object,
    write_silver,
)

OBSERVED = datetime(2026, 8, 25, 9, 46, 5, tzinfo=UTC)
META = {"source": "opensky-live", "observed_at": OBSERVED}


def _vector(**overrides: Any) -> list[Any]:
    """A well-formed state vector, with per-test overrides by field name."""
    base = {
        "icao24": "3C4B4C",
        "callsign": "EWG47B  ",
        "origin_country": "Germany",
        "time_position": 1_787_651_164,
        "last_contact": 1_787_651_164,
        "longitude": 6.8475,
        "latitude": 45.9781,
        "baro_altitude": 11879.58,
        "on_ground": False,
        "velocity": 224.73,
        "true_track": 174.75,
        "vertical_rate": 0.0,
        "sensors": None,
        "geo_altitude": 12298.68,
        "squawk": "1000",
        "spi": False,
        "position_source": 0,
    }
    base.update(overrides)
    return list(base.values())


# --------------------------------------------------------------------------
# parse_state_vector
# --------------------------------------------------------------------------


def test_parses_a_well_formed_vector() -> None:
    row = parse_state_vector(_vector(), META)

    assert row["icao24"] == "3c4b4c"
    assert row["origin_country"] == "Germany"
    assert row["on_ground"] is False
    assert row["baro_altitude_m"] == pytest.approx(11879.58)
    assert row["time_position"] == datetime(2026, 8, 25, 9, 46, 4, tzinfo=UTC)


def test_callsign_padding_is_trimmed() -> None:
    """OpenSky right-pads to eight characters. Storing the padding means every
    downstream join has to remember to trim, and one that forgets is silently
    wrong."""
    assert parse_state_vector(_vector(), META)["callsign"] == "EWG47B"


def test_empty_callsign_becomes_null_not_blank() -> None:
    assert parse_state_vector(_vector(callsign="        "), META)["callsign"] is None


def test_icao24_is_lowercased_for_stable_joins() -> None:
    assert parse_state_vector(_vector(icao24="ABCDEF"), META)["icao24"] == "abcdef"


def test_position_source_is_decoded_to_a_label() -> None:
    assert parse_state_vector(_vector(position_source=2), META)["position_source_label"] == "MLAT"


def test_unknown_position_source_is_labelled_not_dropped() -> None:
    assert (
        parse_state_vector(_vector(position_source=9), META)["position_source_label"] == "UNKNOWN"
    )


def test_partition_columns_come_from_the_observation_time() -> None:
    row = parse_state_vector(_vector(), META)

    assert row["dt"] == "2026-08-25"
    assert row["hour"] == "09"


def test_provenance_is_carried_into_silver() -> None:
    row = parse_state_vector(_vector(), {"source": "fixture-replay", "observed_at": OBSERVED})

    assert row["ingest_source"] == "fixture-replay"


def test_missing_icao24_raises() -> None:
    with pytest.raises(NormaliseError, match="no icao24"):
        parse_state_vector(_vector(icao24=None), META)


def test_missing_last_contact_raises() -> None:
    with pytest.raises(NormaliseError, match="last_contact is required"):
        parse_state_vector(_vector(last_contact=None), META)


def test_short_vector_raises() -> None:
    with pytest.raises(NormaliseError, match="expected at least"):
        parse_state_vector(["abc123", "TEST"], META)


def test_null_optional_fields_survive_as_null() -> None:
    row = parse_state_vector(
        _vector(longitude=None, latitude=None, baro_altitude=None, velocity=None), META
    )

    assert row["longitude"] is None
    assert row["baro_altitude_m"] is None
    assert row["icao24"] == "3c4b4c", "a missing position must not discard the row"


def test_missing_origin_country_defaults_rather_than_nulling() -> None:
    """origin_country is non-nullable in the schema, so a missing value needs a
    defined stand-in rather than breaking the write."""
    assert parse_state_vector(_vector(origin_country=None), META)["origin_country"] == "Unknown"


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------


def test_dedup_key_uses_time_position_when_present() -> None:
    row = parse_state_vector(_vector(), META)

    assert dedup_key(row) == ("3c4b4c", datetime(2026, 8, 25, 9, 46, 4, tzinfo=UTC))


def test_dedup_key_falls_back_to_last_contact_when_position_is_null() -> None:
    row = parse_state_vector(_vector(time_position=None, last_contact=1_787_651_100), META)

    assert dedup_key(row) == ("3c4b4c", datetime(2026, 8, 25, 9, 45, 0, tzinfo=UTC))


def test_aircraft_without_position_fix_do_not_collapse_together() -> None:
    """The bug this guards: keying on a null time_position would merge every
    position-less aircraft into a single row."""
    rows = [
        parse_state_vector(_vector(icao24="aaa111", time_position=None), META),
        parse_state_vector(_vector(icao24="bbb222", time_position=None), META),
    ]

    assert len(deduplicate(rows)) == 2


def test_duplicate_observations_collapse_to_the_freshest() -> None:
    stale = parse_state_vector(_vector(last_contact=1_787_651_100, velocity=100.0), META)
    fresh = parse_state_vector(_vector(last_contact=1_787_651_200, velocity=250.0), META)
    stale["time_position"] = fresh["time_position"]

    deduped = deduplicate([stale, fresh])

    assert len(deduped) == 1
    assert deduped[0]["velocity_ms"] == pytest.approx(250.0)


def test_dedup_is_order_independent() -> None:
    stale = parse_state_vector(_vector(last_contact=1_787_651_100, velocity=100.0), META)
    fresh = parse_state_vector(_vector(last_contact=1_787_651_200, velocity=250.0), META)
    stale["time_position"] = fresh["time_position"]

    assert deduplicate([fresh, stale])[0]["velocity_ms"] == pytest.approx(250.0)


def test_distinct_aircraft_are_all_kept() -> None:
    rows = [parse_state_vector(_vector(icao24=f"aaa{i:03d}"), META) for i in range(5)]

    assert len(deduplicate(rows)) == 5


# --------------------------------------------------------------------------
# Reading bronze
# --------------------------------------------------------------------------


def _write_bronze(path: Path, payload: dict[str, Any], enveloped: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"ingest": {"source": "opensky-live"}, "payload": payload} if enveloped else payload
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_reads_an_enveloped_bronze_object(tmp_path: Path) -> None:
    path = _write_bronze(tmp_path / "states_1.json", {"time": 1_787_651_165, "states": [_vector()]})

    rows = list(read_bronze_object(path))

    assert len(rows) == 1
    assert rows[0]["ingest_source"] == "opensky-live"


def test_reads_a_bare_payload_too(tmp_path: Path) -> None:
    """A snapshot captured by any means should be replayable, not just ours."""
    path = _write_bronze(
        tmp_path / "states_2.json", {"time": 1_787_651_165, "states": [_vector()]}, enveloped=False
    )

    rows = list(read_bronze_object(path))

    assert rows[0]["ingest_source"] == "unknown"


def test_unreadable_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "states_3.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(NormaliseError, match="not readable as JSON"):
        list(read_bronze_object(path))


def test_payload_without_time_raises(tmp_path: Path) -> None:
    path = _write_bronze(tmp_path / "states_4.json", {"states": []})

    with pytest.raises(NormaliseError, match="no usable time"):
        list(read_bronze_object(path))


def test_payload_without_states_raises(tmp_path: Path) -> None:
    path = _write_bronze(tmp_path / "states_5.json", {"time": 1_787_651_165})

    with pytest.raises(NormaliseError, match="no states payload"):
        list(read_bronze_object(path))


def test_non_list_state_vector_raises(tmp_path: Path) -> None:
    path = _write_bronze(
        tmp_path / "states_6.json", {"time": 1_787_651_165, "states": [{"icao24": "abc123"}]}
    )

    with pytest.raises(NormaliseError, match="not a list"):
        list(read_bronze_object(path))


# --------------------------------------------------------------------------
# End to end against the committed fixtures
# --------------------------------------------------------------------------


def _bronze_from_fixtures(settings: Settings, tmp_path: Path) -> list[Path]:
    paths = []
    for source in sorted(settings.fixture_dir.glob("states_*.json")):
        payload = json.loads(source.read_text(encoding="utf-8"))
        paths.append(
            _write_bronze(settings.bronze_root / "dt=2026-08-25" / "hour=09" / source.name, payload)
        )
    return paths


def test_normalise_deduplicates_real_overlapping_snapshots(
    settings: Settings, tmp_path: Path
) -> None:
    """The three committed fixtures overlap heavily by design, so this asserts
    on real duplicate resolution rather than a contrived case."""
    paths = _bronze_from_fixtures(settings, tmp_path)

    result = normalise(paths)

    assert result.files_read == 3
    assert result.rows_in == 440, "146 + 145 + 149 aircraft across the three fixtures"
    assert result.rows_out < result.rows_in, "overlapping snapshots must collapse"
    assert result.duplicates_removed == result.rows_in - result.rows_out


def test_normalised_table_matches_the_silver_schema(settings: Settings, tmp_path: Path) -> None:
    result = normalise(_bronze_from_fixtures(settings, tmp_path))

    assert result.table.schema.equals(SILVER_SCHEMA)


def test_find_bronze_objects_walks_partitions(settings: Settings, tmp_path: Path) -> None:
    _bronze_from_fixtures(settings, tmp_path)

    assert len(find_bronze_objects(settings)) == 3


# --------------------------------------------------------------------------
# Writing silver
# --------------------------------------------------------------------------


def test_write_silver_creates_hive_partitions(settings: Settings, tmp_path: Path) -> None:
    result = normalise(_bronze_from_fixtures(settings, tmp_path))

    written = write_silver(result.table, settings)

    assert len(written) == 1
    parts = written[0].relative_to(settings.silver_root).parts
    assert parts[0].startswith("dt=")
    assert parts[1].startswith("hour=")
    assert parts[2] == "states.parquet"


def test_partition_columns_are_not_duplicated_inside_the_file(
    settings: Settings, tmp_path: Path
) -> None:
    """Hive contract: the partition value lives in the path, so storing it on
    every row as well is pure waste."""
    result = normalise(_bronze_from_fixtures(settings, tmp_path))

    written = write_silver(result.table, settings)

    stored = pq.read_table(written[0])
    for column in PARTITION_COLUMNS:
        assert column not in stored.schema.names


def test_written_parquet_round_trips(settings: Settings, tmp_path: Path) -> None:
    result = normalise(_bronze_from_fixtures(settings, tmp_path))

    written = write_silver(result.table, settings)

    stored = pq.read_table(written[0])
    assert stored.num_rows == result.rows_out
    assert stored.column("icao24").null_count == 0


def test_separate_hours_land_in_separate_partitions(settings: Settings, tmp_path: Path) -> None:
    early = {"time": 1_787_647_565, "states": [_vector(icao24="aaa111")]}  # hour 08
    late = {"time": 1_787_651_165, "states": [_vector(icao24="bbb222")]}  # hour 09
    paths = [
        _write_bronze(settings.bronze_root / "dt=2026-08-25" / "hour=08" / "states_a.json", early),
        _write_bronze(settings.bronze_root / "dt=2026-08-25" / "hour=09" / "states_b.json", late),
    ]

    written = write_silver(normalise(paths).table, settings)

    assert len(written) == 2
    assert {p.parent.name for p in written} == {"hour=08", "hour=09"}
