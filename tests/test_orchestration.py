"""Tests for the scheduled ingestion path.

The handler lives outside `src/` because it is a deployment artefact rather than
part of the importable package, so it is loaded by path. boto3 is never
installed here: the handler imports it lazily and the tests replace the client
outright, which also means these tests cannot accidentally reach AWS.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import responses

from flightops.ingest import SOURCE_LIVE

ROOT = Path(__file__).parent.parent
HANDLER_PATH = ROOT / "orchestration" / "lambda_ingest" / "handler.py"
ASL_PATH = ROOT / "orchestration" / "step_functions" / "ingest_state_machine.asl.json"

BASE_URL = "https://opensky.invalid/api"
STATES_URL = f"{BASE_URL}/states/all"


def _load_handler() -> Any:
    spec = importlib.util.spec_from_file_location("lambda_ingest_handler", HANDLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeS3:
    """Records put_object calls instead of making them."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ETag": "fake"}


@pytest.fixture
def handler_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = _load_handler()
    monkeypatch.setenv("FLIGHTOPS_BUCKET", "example-lake-bucket")
    monkeypatch.setenv("FLIGHTOPS_BRONZE_PREFIX", "bronze")
    monkeypatch.setenv("FLIGHTOPS_OPENSKY_BASE_URL", BASE_URL)
    monkeypatch.setenv("FLIGHTOPS_MAX_RETRIES", "0")
    monkeypatch.setenv("FLIGHTOPS_DATA_ROOT", str(tmp_path))
    for var in ("BBOX_LAMIN", "BBOX_LOMIN", "BBOX_LAMAX", "BBOX_LOMAX"):
        monkeypatch.delenv(f"FLIGHTOPS_{var}", raising=False)
    return module


@pytest.fixture
def fake_s3(handler_module: Any, monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    client = FakeS3()
    monkeypatch.setattr(handler_module, "_s3_client", lambda: client)
    return client


# --------------------------------------------------------------------------
# The handler
# --------------------------------------------------------------------------


@responses.activate
def test_writes_one_object_and_returns_a_summary(
    handler_module: Any, fake_s3: FakeS3, states_payload: dict[str, Any]
) -> None:
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)

    result = handler_module.handler({}, None)

    assert len(fake_s3.calls) == 1
    assert result["bucket"] == "example-lake-bucket"
    assert result["states"] == 2
    assert result["source"] == SOURCE_LIVE


@responses.activate
def test_object_key_matches_the_local_partition_layout(
    handler_module: Any, fake_s3: FakeS3, states_payload: dict[str, Any]
) -> None:
    """A snapshot written by the Lambda and one written on a laptop must land
    at the same relative location, or the catalog only sees one of them."""
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)

    handler_module.handler({}, None)

    key = fake_s3.calls[0]["Key"]
    assert key == "bronze/dt=2023-11-14/hour=22/states_1700000000.json"


@responses.activate
def test_object_body_is_the_provenance_envelope(
    handler_module: Any, fake_s3: FakeS3, states_payload: dict[str, Any]
) -> None:
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)

    handler_module.handler({}, None)

    envelope = json.loads(fake_s3.calls[0]["Body"].decode("utf-8"))
    assert envelope["ingest"]["source"] == SOURCE_LIVE
    assert envelope["payload"] == states_payload


@responses.activate
def test_encryption_is_requested_explicitly(
    handler_module: Any, fake_s3: FakeS3, states_payload: dict[str, Any]
) -> None:
    """The bucket already defaults to SSE-S3. Stating it on the request means
    an object cannot land unencrypted if that default is ever changed."""
    responses.add(responses.GET, STATES_URL, json=states_payload, status=200)

    handler_module.handler({}, None)

    assert fake_s3.calls[0]["ServerSideEncryption"] == "AES256"


def test_missing_bucket_env_fails_fast(
    handler_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FLIGHTOPS_BUCKET", raising=False)

    with pytest.raises(handler_module.HandlerError, match="FLIGHTOPS_BUCKET"):
        handler_module.handler({}, None)


def test_invalid_configuration_fails_fast(
    handler_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLIGHTOPS_HTTP_TIMEOUT", "not-a-number")

    with pytest.raises(handler_module.HandlerError, match="invalid configuration"):
        handler_module.handler({}, None)


@responses.activate
def test_ingestion_failure_raises_so_step_functions_can_retry(
    handler_module: Any, fake_s3: FakeS3
) -> None:
    """Swallowing this would make a broken schedule look healthy: executions
    would succeed while producing nothing."""
    responses.add(responses.GET, STATES_URL, status=503)

    with pytest.raises(handler_module.HandlerError):
        handler_module.handler({}, None)

    assert fake_s3.calls == [], "nothing may be written when the fetch failed"


def test_handler_does_not_import_boto3_at_module_scope() -> None:
    """Lazy import keeps the handler unit-testable without an AWS SDK, and
    keeps boto3 out of the deployment package since the runtime provides it."""
    source = HANDLER_PATH.read_text(encoding="utf-8")
    module_level = [line for line in source.splitlines() if line.startswith(("import ", "from "))]

    assert not any("boto3" in line for line in module_level)


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------


@pytest.fixture
def asl() -> dict[str, Any]:
    return json.loads(ASL_PATH.read_text(encoding="utf-8"))


def test_state_machine_is_valid_json_with_the_expected_states(asl: dict[str, Any]) -> None:
    assert asl["StartAt"] == "Ingest"
    assert set(asl["States"]) == {"Ingest", "IngestSucceeded", "IngestFailed"}


def test_state_machine_retries_and_catches(asl: dict[str, Any]) -> None:
    """The retry policy is declared here rather than in function code so it is
    visible in the console and changeable without a redeploy."""
    ingest = asl["States"]["Ingest"]

    assert ingest["Retry"][0]["MaxAttempts"] >= 1
    assert ingest["Retry"][0]["BackoffRate"] > 1
    assert ingest["Catch"][0]["Next"] == "IngestFailed"


def test_state_machine_fails_loudly_rather_than_succeeding_quietly(asl: dict[str, Any]) -> None:
    """A schedule that swallows errors looks healthy while producing nothing."""
    assert asl["States"]["IngestFailed"]["Type"] == "Fail"


def test_state_machine_has_a_task_timeout(asl: dict[str, Any]) -> None:
    assert asl["States"]["Ingest"]["TimeoutSeconds"] > 0


def test_lambda_arn_is_templated_not_hardcoded(asl: dict[str, Any]) -> None:
    """A literal ARN would embed an account id in a public repository."""
    resource = asl["States"]["Ingest"]["Resource"]

    assert resource == "${lambda_arn}"


# --------------------------------------------------------------------------
# The Terraform module
# --------------------------------------------------------------------------


def test_lambda_role_can_write_bronze_but_not_read_it() -> None:
    """A write-only credential cannot be used to exfiltrate. The function
    produces snapshots; nothing about its job requires reading one back."""
    iam = (ROOT / "infra" / "modules" / "orchestration" / "iam.tf").read_text(encoding="utf-8")
    grant = iam[iam.index("WriteBronzeObjectsOnly") : iam.index("WriteOwnLogs")]

    assert "s3:PutObject" in grant
    assert "s3:GetObject" not in grant
    assert "s3:DeleteObject" not in grant
    assert "s3:ListBucket" not in grant


def test_schedule_is_disabled_by_default() -> None:
    """Applying this module must not silently start a recurring job in
    someone's account."""
    variables = (ROOT / "infra" / "modules" / "orchestration" / "variables.tf").read_text(
        encoding="utf-8"
    )
    block = variables[variables.index('variable "schedule_enabled"') :]

    assert "default     = false" in block[: block.index("}\n\nvariable")]


def test_log_groups_have_retention() -> None:
    """CloudWatch logs never expire by default, which is a slow, silent cost
    leak rather than a dramatic one."""
    main = (ROOT / "infra" / "modules" / "orchestration" / "main.tf").read_text(encoding="utf-8")

    assert main.count("retention_in_days") == 2
