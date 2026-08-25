"""Bronze-layer ingestion: OpenSky /states/all -> partitioned raw JSON.

Design notes worth the reader's time:

*Provenance over rawness.* A pure bronze layer would store the API response
byte-for-byte. This module wraps it in a thin envelope instead, recording where
each snapshot came from and when it was captured. The build contract forbids
fabricating records and passing them off as real, and the only way to honour
that downstream is to make the distinction between a live fetch and a fixture
replay machine-readable rather than a matter of trust. The upstream payload is
preserved verbatim under `payload`; nothing is rewritten.

*Backoff, not hammering.* OpenSky is a free public service funded by
researchers. Anonymous callers get a modest budget, and the correct response to
429 is to wait exactly as long as we were asked to, not to retry in a tight
loop. `Retry-After` is honoured when the server sends it.

*No credentials.* Anonymous access only, by design. OpenSky's OAuth2 client
secret is precisely the class of value this repository must never hold.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from flightops.config import Settings

logger = logging.getLogger(__name__)

# Bumped whenever the envelope shape changes, so a reader can tell which
# snapshots it understands without guessing from the keys present.
SNAPSHOT_SCHEMA_VERSION = 1

SOURCE_LIVE = "opensky-live"
SOURCE_FIXTURE = "fixture-replay"

# Status codes worth retrying: rate limiting, plus transient upstream failure.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

FIXTURE_GLOB = "states_*.json"


class IngestError(RuntimeError):
    """Raised when a snapshot could not be obtained by any permitted route."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One captured observation window, ready to be written to bronze."""

    payload: dict[str, Any]
    source: str
    fetched_at: datetime
    request_url: str

    @property
    def state_count(self) -> int:
        return len(self.payload.get("states") or [])

    @property
    def observed_at(self) -> datetime:
        """The instant OpenSky says the observation belongs to.

        Partitioning uses this rather than the local wall clock: a snapshot
        fetched at 00:00:03 that describes 23:59:58 belongs in the earlier
        hour, and getting that wrong produces partitions that disagree with
        their own contents.
        """
        epoch = self.payload.get("time")
        if isinstance(epoch, int | float) and not isinstance(epoch, bool) and epoch > 0:
            return datetime.fromtimestamp(float(epoch), tz=UTC)
        logger.warning(
            "snapshot has no usable time field; falling back to fetch time",
            extra={"source": self.source, "fetched_at": self.fetched_at.isoformat()},
        )
        return self.fetched_at

    def to_envelope(self) -> dict[str, Any]:
        """Wrap the verbatim payload with the provenance a reader needs."""
        return {
            "ingest": {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "source": self.source,
                "fetched_at": self.fetched_at.isoformat(),
                "observed_at": self.observed_at.isoformat(),
                "request_url": self.request_url,
                "state_count": self.state_count,
            },
            "payload": self.payload,
        }


def _backoff_delay(attempt: int, settings: Settings, retry_after: str | None) -> float:
    """Compute the delay before the next attempt.

    An explicit Retry-After from the server always wins -- it is the operator
    telling us what they want. Otherwise: exponential backoff with full jitter,
    which avoids a thundering herd if this ever runs on a schedule alongside
    other clients.
    """
    if retry_after:
        try:
            return min(float(retry_after), settings.backoff_max_seconds)
        except ValueError:
            logger.debug("unparseable Retry-After header: %r", retry_after)
    ceiling = min(settings.backoff_base_seconds * (2**attempt), settings.backoff_max_seconds)
    return random.uniform(0, ceiling)  # noqa: S311 - jitter, not cryptography


def fetch_states(
    settings: Settings,
    *,
    session: requests.Session | None = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> Snapshot:
    """Fetch one live snapshot, retrying on rate limits and transient errors.

    `session` and `sleep` are injectable so the retry logic can be exercised in
    tests without a network and without a real delay.
    """
    http = session or requests.Session()
    params = settings.bbox.as_params() if settings.bbox else {}
    headers = {"User-Agent": settings.user_agent, "Accept": "application/json"}
    attempts = settings.max_retries + 1
    last_error = "no attempt was made"

    for attempt in range(attempts):
        retry_after: str | None = None
        try:
            response = http.get(
                settings.states_url,
                params=params,
                headers=headers,
                timeout=settings.timeout_seconds,
            )
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "opensky request failed",
                extra={"attempt": attempt + 1, "of": attempts, "error": last_error},
            )
        else:
            if response.status_code == requests.codes.ok:
                payload = _decode(response)
                logger.info(
                    "fetched live snapshot",
                    extra={
                        "states": len(payload.get("states") or []),
                        "attempt": attempt + 1,
                        "bytes": len(response.content),
                    },
                )
                return Snapshot(
                    payload=payload,
                    source=SOURCE_LIVE,
                    fetched_at=datetime.now(tz=UTC),
                    request_url=response.url,
                )

            last_error = f"HTTP {response.status_code}"
            if response.status_code not in RETRYABLE_STATUS:
                raise IngestError(f"opensky returned a non-retryable {last_error}")
            retry_after = response.headers.get("Retry-After")
            logger.warning(
                "opensky returned a retryable status",
                extra={
                    "status": response.status_code,
                    "attempt": attempt + 1,
                    "of": attempts,
                    "retry_after": retry_after,
                },
            )

        if attempt < settings.max_retries:
            delay = _backoff_delay(attempt, settings, retry_after)
            logger.info("backing off", extra={"seconds": round(delay, 2)})
            sleep(delay)

    raise IngestError(f"exhausted {attempts} attempts against opensky; last error: {last_error}")


def _decode(response: requests.Response) -> dict[str, Any]:
    """Validate just enough of the body to know we received a states payload."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise IngestError("opensky returned a body that is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise IngestError(f"expected a JSON object from opensky, got {type(payload).__name__}")
    if "states" not in payload:
        raise IngestError("opensky response has no states key")
    return payload


def load_fixture(settings: Settings, name: str | None = None) -> Snapshot:
    """Replay a committed fixture instead of calling the network.

    The returned snapshot is tagged `fixture-replay`, and that tag is written
    into bronze. Downstream layers can therefore always tell demonstration data
    from observation, which is the whole point of the contract's prohibition on
    passing fabricated records off as real.
    """
    if name:
        chosen = settings.fixture_dir / name
        if not chosen.is_file():
            raise IngestError(f"fixture not found: {chosen}")
    else:
        candidates = sorted(settings.fixture_dir.glob(FIXTURE_GLOB))
        if not candidates:
            raise IngestError(f"no fixtures matching {FIXTURE_GLOB} in {settings.fixture_dir}")
        chosen = candidates[0]

    raw = json.loads(chosen.read_text(encoding="utf-8"))
    # Fixtures are stored as bare API payloads, exactly as OpenSky returned
    # them; tolerate an enveloped file too so a bronze object can be replayed.
    payload = raw.get("payload", raw) if isinstance(raw, dict) else raw
    if not isinstance(payload, dict) or "states" not in payload:
        raise IngestError(f"fixture {chosen.name} does not contain a states payload")
    logger.info(
        "replaying committed fixture",
        extra={"fixture": chosen.name, "states": len(payload.get("states") or [])},
    )
    return Snapshot(
        payload=payload,
        source=SOURCE_FIXTURE,
        fetched_at=datetime.now(tz=UTC),
        request_url=f"file://{chosen.as_posix()}",
    )


def acquire_snapshot(
    settings: Settings,
    *,
    session: requests.Session | None = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> Snapshot:
    """Get a snapshot live, falling back to fixtures only if explicitly allowed.

    The fallback is opt-in rather than automatic. A pipeline that silently
    substitutes demonstration data for a failed fetch looks healthy while
    producing fiction, which is worse than a visible failure.
    """
    try:
        return fetch_states(settings, session=session, sleep=sleep)
    except IngestError:
        if not settings.allow_fixture_fallback:
            raise
        logger.warning("live fetch failed; falling back to committed fixture data")
        return load_fixture(settings)


def partition_path(snapshot: Snapshot, settings: Settings) -> Path:
    """Hive-style partition directory for a snapshot: dt=YYYY-MM-DD/hour=HH."""
    observed = snapshot.observed_at
    return settings.bronze_root / f"dt={observed:%Y-%m-%d}" / f"hour={observed:%H}"


def write_snapshot(snapshot: Snapshot, settings: Settings) -> Path:
    """Write one snapshot into the bronze partition layout.

    The filename carries the observation epoch so that re-running ingestion for
    the same window overwrites rather than accumulating near-duplicates; bronze
    is a landing zone, not an audit log.
    """
    directory = partition_path(snapshot, settings)
    directory.mkdir(parents=True, exist_ok=True)
    epoch = int(snapshot.observed_at.timestamp())
    destination = directory / f"states_{epoch}.json"
    destination.write_text(
        json.dumps(snapshot.to_envelope(), separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    logger.info(
        "wrote bronze snapshot",
        extra={
            "path": destination.as_posix(),
            "states": snapshot.state_count,
            "source": snapshot.source,
        },
    )
    return destination


def ingest_once(
    settings: Settings,
    *,
    session: requests.Session | None = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> Path:
    """Acquire one snapshot and land it in bronze. Returns the file written."""
    snapshot = acquire_snapshot(settings, session=session, sleep=sleep)
    return write_snapshot(snapshot, settings)
