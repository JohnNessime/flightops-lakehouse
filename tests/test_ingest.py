"""Tests for the bronze ingestion path.

Every HTTP interaction is mocked with `responses`; `conftest._no_network`
guarantees that a missed mock surfaces as a failure rather than a real request.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests
import responses

from flightops.config import Settings
from flightops.ingest import (
    SNAPSHOT_SCHEMA_VERSION,
    SOURCE_FIXTURE,
    SOURCE_LIVE,
    IngestError,
    Snapshot,
    acquire_snapshot,
    fetch_states,
    ingest_once,
    load_fixture,
    partition_path,
    replay_all_fixtures,
    write_snapshot,
)

STATES_URL = "https://opensky.invalid/api/states/all"


def _recording_sleep() -> tuple[list[float], Any]:
    """A stand-in for time.sleep that records delays instead of taking them."""
    calls: list[float] = []
    return calls, calls.append


# --------------------------------------------------------------------------
# fetch_states
# --------------------------------------------------------------------------


@responses.activate
def test_fetch_states_returns_live_snapshot(
    settings: Settings, states_payload: dict[str, Any]
) -> None:
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)

    snapshot = fetch_states(settings)

    assert snapshot.source == SOURCE_LIVE
    assert snapshot.state_count == 2
    assert snapshot.payload == states_payload


@responses.activate
def test_bounding_box_is_sent_as_query_parameters(
    settings: Settings, states_payload: dict[str, Any]
) -> None:
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)

    fetch_states(settings)

    sent = responses.calls[0].request
    assert "lamin=45.0" in sent.url
    assert "lomax=11.0" in sent.url


@responses.activate
def test_retries_on_429_then_succeeds(settings: Settings, states_payload: dict[str, Any]) -> None:
    responses.add(responses.GET, STATES_URL, status=429, headers={"Retry-After": "0.03"})
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)
    delays, sleep = _recording_sleep()

    snapshot = fetch_states(settings, sleep=sleep)

    assert snapshot.state_count == 2
    assert len(responses.calls) == 2
    assert delays == [0.03], "an explicit Retry-After must be honoured exactly"


@responses.activate
def test_retry_after_is_capped_at_backoff_max(
    settings: Settings, states_payload: dict[str, Any]
) -> None:
    """A hostile or mistaken Retry-After must not stall the process for hours."""
    responses.add(responses.GET, STATES_URL, status=429, headers={"Retry-After": "86400"})
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)
    delays, sleep = _recording_sleep()

    fetch_states(settings, sleep=sleep)

    assert delays == [settings.backoff_max_seconds]


@responses.activate
def test_unparseable_retry_after_falls_back_to_jittered_backoff(
    settings: Settings, states_payload: dict[str, Any]
) -> None:
    responses.add(responses.GET, STATES_URL, status=503, headers={"Retry-After": "soon"})
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)
    delays, sleep = _recording_sleep()

    fetch_states(settings, sleep=sleep)

    assert len(delays) == 1
    assert 0.0 <= delays[0] <= settings.backoff_max_seconds


@responses.activate
def test_exhausting_retries_raises(settings: Settings) -> None:
    for _ in range(settings.max_retries + 1):
        responses.add(responses.GET, STATES_URL, status=503)
    _, sleep = _recording_sleep()

    with pytest.raises(IngestError, match="exhausted 3 attempts"):
        fetch_states(settings, sleep=sleep)

    assert len(responses.calls) == settings.max_retries + 1


@responses.activate
def test_non_retryable_status_fails_immediately(settings: Settings) -> None:
    responses.add(responses.GET, STATES_URL, status=404)
    _, sleep = _recording_sleep()

    with pytest.raises(IngestError, match="non-retryable HTTP 404"):
        fetch_states(settings, sleep=sleep)

    assert len(responses.calls) == 1, "a 404 must not be retried"


@responses.activate
def test_connection_error_is_retried(settings: Settings, states_payload: dict[str, Any]) -> None:
    responses.add(responses.GET, STATES_URL, body=requests.ConnectionError("boom"))
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)
    _, sleep = _recording_sleep()

    assert fetch_states(settings, sleep=sleep).state_count == 2


@responses.activate
def test_non_json_body_raises(settings: Settings) -> None:
    responses.add(responses.GET, STATES_URL, body="<html>not json</html>", status=200)

    with pytest.raises(IngestError, match="not valid JSON"):
        fetch_states(settings)


@responses.activate
def test_payload_without_states_key_raises(settings: Settings) -> None:
    responses.add(responses.GET, STATES_URL, json={"time": 1}, status=200)

    with pytest.raises(IngestError, match="no states key"):
        fetch_states(settings)


@responses.activate
def test_json_array_body_raises(settings: Settings) -> None:
    responses.add(responses.GET, STATES_URL, json=[1, 2, 3], status=200)

    with pytest.raises(IngestError, match="expected a JSON object"):
        fetch_states(settings)


# --------------------------------------------------------------------------
# Partitioning and writing
# --------------------------------------------------------------------------


def _snapshot(payload: dict[str, Any], source: str = SOURCE_LIVE) -> Snapshot:
    return Snapshot(
        payload=payload,
        source=source,
        fetched_at=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        request_url=STATES_URL,
    )


def test_partition_uses_observation_time_not_fetch_time(
    settings: Settings, states_payload: dict[str, Any]
) -> None:
    """The regression this guards: a snapshot fetched just after midnight that
    describes the previous hour must land in the previous hour's partition."""
    snapshot = _snapshot(states_payload)

    path = partition_path(snapshot, settings)

    assert path.parent.name == "dt=2023-11-14"
    assert path.name == "hour=22"
    assert snapshot.fetched_at.year == 2030, "fetch time must not influence the partition"


def test_snapshot_without_time_falls_back_to_fetch_time(settings: Settings) -> None:
    snapshot = _snapshot({"time": None, "states": []})

    assert partition_path(snapshot, settings).parent.name == "dt=2030-01-01"


def test_write_snapshot_lands_in_hive_partition(
    settings: Settings, states_payload: dict[str, Any]
) -> None:
    written = write_snapshot(_snapshot(states_payload), settings)

    assert written.exists()
    assert written.name == "states_1700000000.json"
    parts = written.relative_to(settings.data_root).parts
    assert parts[0] == "bronze"
    assert parts[1] == "dt=2023-11-14"
    assert parts[2] == "hour=22"


def test_written_envelope_records_provenance(
    settings: Settings, states_payload: dict[str, Any]
) -> None:
    written = write_snapshot(_snapshot(states_payload, source=SOURCE_FIXTURE), settings)

    envelope = json.loads(written.read_text(encoding="utf-8"))

    assert envelope["ingest"]["source"] == SOURCE_FIXTURE
    assert envelope["ingest"]["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert envelope["ingest"]["state_count"] == 2
    assert envelope["payload"] == states_payload, "the upstream payload must be verbatim"


def test_rewriting_the_same_window_overwrites(
    settings: Settings, states_payload: dict[str, Any]
) -> None:
    first = write_snapshot(_snapshot(states_payload), settings)
    second = write_snapshot(_snapshot(states_payload), settings)

    assert first == second
    assert len(list(first.parent.iterdir())) == 1


# --------------------------------------------------------------------------
# Fixture replay and fallback
# --------------------------------------------------------------------------


def test_committed_fixtures_exist(captured_fixture_names: list[str]) -> None:
    assert len(captured_fixture_names) >= 2, "the contract asks for 2-3 snapshots"


def test_load_fixture_tags_replay_not_observation(settings: Settings) -> None:
    snapshot = load_fixture(settings)

    assert snapshot.source == SOURCE_FIXTURE
    assert snapshot.state_count > 0
    assert snapshot.request_url.startswith("file://")


def test_load_named_fixture(settings: Settings, captured_fixture_names: list[str]) -> None:
    snapshot = load_fixture(settings, captured_fixture_names[-1])

    assert snapshot.state_count > 0


def test_missing_named_fixture_raises(settings: Settings) -> None:
    with pytest.raises(IngestError, match="fixture not found"):
        load_fixture(settings, "states_does_not_exist.json")


def test_no_fixtures_available_raises(settings: Settings, tmp_path: Path) -> None:
    import dataclasses

    empty = dataclasses.replace(settings, fixture_dir=tmp_path / "empty")
    (tmp_path / "empty").mkdir()

    with pytest.raises(IngestError, match="no fixtures matching"):
        load_fixture(empty)


@responses.activate
def test_fallback_is_off_by_default(settings: Settings) -> None:
    """A silent substitution of demo data for a failed fetch is the failure
    mode this guards against: the pipeline must fail visibly instead."""
    responses.add(responses.GET, STATES_URL, status=503)
    _, sleep = _recording_sleep()

    with pytest.raises(IngestError):
        acquire_snapshot(settings, sleep=sleep)


@responses.activate
def test_fallback_when_enabled_returns_tagged_fixture(settings: Settings) -> None:
    import dataclasses

    responses.add(responses.GET, STATES_URL, status=503)
    permissive = dataclasses.replace(settings, allow_fixture_fallback=True)
    _, sleep = _recording_sleep()

    snapshot = acquire_snapshot(permissive, sleep=sleep)

    assert snapshot.source == SOURCE_FIXTURE


@responses.activate
def test_ingest_once_end_to_end(settings: Settings, states_payload: dict[str, Any]) -> None:
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)

    written = ingest_once(settings)

    envelope = json.loads(written.read_text(encoding="utf-8"))
    assert envelope["ingest"]["source"] == SOURCE_LIVE
    assert len(envelope["payload"]["states"]) == 2


# --------------------------------------------------------------------------
# The captured fixtures themselves
# --------------------------------------------------------------------------


def test_captured_fixtures_match_the_opensky_schema(
    settings: Settings, captured_fixture_names: list[str]
) -> None:
    """Guards the fixtures against silent corruption or hand-editing."""
    for name in captured_fixture_names:
        payload = load_fixture(settings, name).payload
        assert set(payload) == {"time", "states"}
        assert isinstance(payload["time"], int)
        for state in payload["states"]:
            assert len(state) == 17
            assert isinstance(state[0], str) and len(state[0]) == 6


# --------------------------------------------------------------------------
# Offline replay: what CI uses
# --------------------------------------------------------------------------


def test_replay_all_fixtures_writes_every_one(settings: Settings) -> None:
    written = replay_all_fixtures(settings)

    expected = len(list(settings.fixture_dir.glob("states_*.json")))
    assert len(written) == expected
    assert all(path.exists() for path in written)


def test_replayed_objects_are_tagged_as_replay_not_observation(settings: Settings) -> None:
    """A CI run must never produce bronze that claims to be observation."""
    for path in replay_all_fixtures(settings):
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert envelope["ingest"]["source"] == SOURCE_FIXTURE


def test_replay_spans_the_partitions_the_fixtures_cover(settings: Settings) -> None:
    written = replay_all_fixtures(settings)

    hours = {path.parent.name for path in written}
    assert len(hours) >= 2, "the fixtures deliberately span more than one hour"


def test_replay_makes_no_network_call(settings: Settings) -> None:
    """conftest._no_network would raise on any real connection; `responses` is
    deliberately not activated here, so an accidental request cannot be mocked
    into passing either."""
    assert replay_all_fixtures(settings)


def test_replay_with_no_fixtures_raises(settings: Settings, tmp_path: Path) -> None:
    import dataclasses

    empty = dataclasses.replace(settings, fixture_dir=tmp_path / "none")
    (tmp_path / "none").mkdir()

    with pytest.raises(IngestError, match="no fixtures matching"):
        replay_all_fixtures(empty)
