"""AWS Lambda entrypoint for scheduled bronze ingestion.

This module is deliberately thin. Everything interesting -- the retry policy,
the provenance envelope, the partition layout -- lives in `flightops.ingest`
and is shared with the local path. A Lambda that reimplemented any of it would
drift from the code that CI actually exercises, and the drift would only show
up as a bronze object in the wrong place weeks later.

The one thing that genuinely differs is the destination: locally the writer
puts a file on disk, here it puts an object in S3. `object_key` derives that
key from the same `partition_segments` the filesystem writer uses, so the two
cannot disagree about where a snapshot belongs.

boto3 is not vendored -- it is present in the Lambda runtime and importing it
from there keeps the deployment package small. It is imported lazily so this
module can be unit-tested without it installed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from flightops.config import ConfigError, Settings
from flightops.ingest import IngestError, acquire_snapshot, object_key

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


class HandlerError(RuntimeError):
    """Raised when the invocation cannot complete. Surfaces to Step Functions."""


def _s3_client() -> Any:
    """Import boto3 lazily.

    The Lambda runtime provides it; the test suite does not need it, and
    importing at module scope would make the handler untestable without an AWS
    SDK on the machine running the tests.
    """
    import boto3

    return boto3.client("s3")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise HandlerError(f"{name} is not set; the function cannot know where to write")
    return value


def handler(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    """Fetch one snapshot and put it in the bronze prefix.

    Returns a JSON-serialisable summary rather than raising on the happy path,
    because Step Functions reads the return value and a structured result is
    what makes a failed execution diagnosable from the console alone.
    """
    bucket = _require_env("FLIGHTOPS_BUCKET")
    prefix = os.environ.get("FLIGHTOPS_BRONZE_PREFIX", "bronze")

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise HandlerError(f"invalid configuration: {exc}") from exc

    try:
        snapshot = acquire_snapshot(settings)
    except IngestError as exc:
        # Re-raised so Step Functions sees a failure and applies its retry
        # policy. Swallowing this would make a broken schedule look healthy.
        logger.error("ingestion failed: %s", exc)
        raise HandlerError(str(exc)) from exc

    key = object_key(snapshot, prefix)
    body = json.dumps(snapshot.to_envelope(), separators=(",", ":"), sort_keys=True)

    _s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
        # Belt and braces: the bucket already defaults to SSE-S3, but stating
        # it here means an object cannot land unencrypted if that default is
        # ever changed out from under this function.
        ServerSideEncryption="AES256",
    )

    result = {
        "bucket": bucket,
        "key": key,
        "states": snapshot.state_count,
        "source": snapshot.source,
        "observed_at": snapshot.observed_at.isoformat(),
        "bytes": len(body),
    }
    logger.info("wrote bronze object", extra=result)
    return result
