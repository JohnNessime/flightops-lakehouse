"""Silver layer: bronze JSON snapshots -> typed, deduplicated Parquet.

Three decisions in here are worth explaining, because each one is a departure
from the obvious implementation.

*Partitioning by dt/hour, not by origin_country.* The original plan partitioned
on `origin_country` as well. A single snapshot over one country-sized bounding
box carries ~150 aircraft spread across ~22 countries -- roughly seven rows per
partition. Small-file fragmentation is precisely what makes Athena slow and
expensive: per-request S3 costs and per-file open overhead swamp any pruning
benefit at this cardinality. `origin_country` is kept as an ordinary column,
where a predicate on it still filters fine.

*Deduplication on (icao24, coalesce(time_position, last_contact)).* The natural
key is `(icao24, time_position)`, but `time_position` is null for any aircraft
transmitting without a position fix. A null key silently collapses every such
aircraft into one row, so the key falls back to `last_contact`, which OpenSky
always populates. Rows are kept, not dropped: an aircraft with no position fix
is still a real observation, and the quality layer flags rather than discards.

*The `sensors` column is dropped.* It is always null for anonymous API access,
and a nullable list-of-int column costs real complexity in both Parquet and the
Glue catalog for a field that never carries a value here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from flightops.config import Settings

logger = logging.getLogger(__name__)

# Positional layout of an OpenSky state vector, per the upstream API contract.
STATE_VECTOR_FIELDS = (
    "icao24",
    "callsign",
    "origin_country",
    "time_position",
    "last_contact",
    "longitude",
    "latitude",
    "baro_altitude",
    "on_ground",
    "velocity",
    "true_track",
    "vertical_rate",
    "sensors",
    "geo_altitude",
    "squawk",
    "spi",
    "position_source",
)

# How OpenSky determined the position. Decoded to a label so that a reader of
# the silver table does not need the API documentation open beside them.
POSITION_SOURCE_LABELS = {
    0: "ADS-B",
    1: "ASTERIX",
    2: "MLAT",
    3: "FLARM",
}

SILVER_SCHEMA = pa.schema(
    [
        pa.field("icao24", pa.string(), nullable=False),
        pa.field("callsign", pa.string()),
        pa.field("origin_country", pa.string(), nullable=False),
        pa.field("time_position", pa.timestamp("s", tz="UTC")),
        pa.field("last_contact", pa.timestamp("s", tz="UTC"), nullable=False),
        pa.field("longitude", pa.float64()),
        pa.field("latitude", pa.float64()),
        pa.field("baro_altitude_m", pa.float64()),
        pa.field("on_ground", pa.bool_(), nullable=False),
        pa.field("velocity_ms", pa.float64()),
        pa.field("true_track_deg", pa.float64()),
        pa.field("vertical_rate_ms", pa.float64()),
        pa.field("geo_altitude_m", pa.float64()),
        pa.field("squawk", pa.string()),
        pa.field("spi", pa.bool_(), nullable=False),
        pa.field("position_source", pa.int8(), nullable=False),
        pa.field("position_source_label", pa.string()),
        # Provenance, carried through from the bronze envelope so that a silver
        # row can always be traced back to observation or fixture replay.
        pa.field("ingest_source", pa.string(), nullable=False),
        pa.field("observed_at", pa.timestamp("s", tz="UTC"), nullable=False),
        # Partition columns.
        pa.field("dt", pa.string(), nullable=False),
        pa.field("hour", pa.string(), nullable=False),
    ]
)

PARTITION_COLUMNS = ("dt", "hour")


class NormaliseError(RuntimeError):
    """Raised when a bronze object cannot be interpreted as state vectors."""


@dataclass(frozen=True, slots=True)
class NormaliseResult:
    """What one normalisation run produced, for logging and for tests."""

    table: pa.Table
    files_read: int
    rows_in: int
    rows_out: int

    @property
    def duplicates_removed(self) -> int:
        return self.rows_in - self.rows_out


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def _as_timestamp(value: Any) -> datetime | None:
    epoch = _as_int(value)
    if epoch is None or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)


def _clean_string(value: Any) -> str | None:
    """Trim and normalise a text field.

    OpenSky right-pads callsigns to eight characters ('SWR123  '). Storing the
    padding means every downstream join and group-by has to remember to trim,
    and one that forgets produces silently wrong results.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def parse_state_vector(raw: list[Any], envelope_meta: dict[str, Any]) -> dict[str, Any]:
    """Turn one positional state vector into a typed, named row."""
    if len(raw) < len(STATE_VECTOR_FIELDS):
        raise NormaliseError(
            f"state vector has {len(raw)} fields, expected at least {len(STATE_VECTOR_FIELDS)}"
        )
    field = dict(zip(STATE_VECTOR_FIELDS, raw, strict=False))

    icao24 = _clean_string(field["icao24"])
    if not icao24:
        raise NormaliseError("state vector has no icao24; it cannot be keyed")

    last_contact = _as_timestamp(field["last_contact"])
    if last_contact is None:
        raise NormaliseError(f"{icao24}: last_contact is required but missing")

    position_source = _as_int(field["position_source"]) or 0
    observed_at: datetime = envelope_meta["observed_at"]

    return {
        "icao24": icao24.lower(),
        "callsign": _clean_string(field["callsign"]),
        "origin_country": _clean_string(field["origin_country"]) or "Unknown",
        "time_position": _as_timestamp(field["time_position"]),
        "last_contact": last_contact,
        "longitude": _as_float(field["longitude"]),
        "latitude": _as_float(field["latitude"]),
        "baro_altitude_m": _as_float(field["baro_altitude"]),
        "on_ground": bool(field["on_ground"]),
        "velocity_ms": _as_float(field["velocity"]),
        "true_track_deg": _as_float(field["true_track"]),
        "vertical_rate_ms": _as_float(field["vertical_rate"]),
        "geo_altitude_m": _as_float(field["geo_altitude"]),
        "squawk": _clean_string(field["squawk"]),
        "spi": bool(field["spi"]),
        "position_source": position_source,
        "position_source_label": POSITION_SOURCE_LABELS.get(position_source, "UNKNOWN"),
        "ingest_source": envelope_meta["source"],
        "observed_at": observed_at,
        "dt": f"{observed_at:%Y-%m-%d}",
        "hour": f"{observed_at:%H}",
    }


def read_bronze_object(path: Path) -> Iterator[dict[str, Any]]:
    """Yield typed rows from one bronze object.

    Accepts both the enveloped form this project writes and a bare OpenSky
    payload, so a snapshot captured by any means can be replayed.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NormaliseError(f"{path.name}: not readable as JSON") from exc

    if not isinstance(document, dict):
        raise NormaliseError(f"{path.name}: expected a JSON object")

    payload = document.get("payload", document)
    ingest = document.get("ingest", {})
    if not isinstance(payload, dict) or "states" not in payload:
        raise NormaliseError(f"{path.name}: no states payload found")

    observed_at = _as_timestamp(payload.get("time"))
    if observed_at is None:
        raise NormaliseError(f"{path.name}: payload has no usable time field")

    meta = {
        "source": ingest.get("source", "unknown"),
        "observed_at": observed_at,
    }

    for raw in payload.get("states") or []:
        if not isinstance(raw, list):
            raise NormaliseError(f"{path.name}: a state vector is not a list")
        yield parse_state_vector(raw, meta)


def dedup_key(row: dict[str, Any]) -> tuple[str, datetime]:
    """The natural key of an observation.

    `time_position` is the correct discriminator but is null whenever an
    aircraft transmits without a position fix; `last_contact` is always
    populated and stands in for those rows so they do not collapse together.
    """
    return row["icao24"], row["time_position"] or row["last_contact"]


def deduplicate(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated observations, keeping the freshest of each key.

    Consecutive snapshots overlap heavily -- the same aircraft is still in the
    air 25 seconds later -- so this is the difference between a silver table
    that grows with time and one that grows with distinct observations.
    """
    freshest: dict[tuple[str, datetime], dict[str, Any]] = {}
    for row in rows:
        key = dedup_key(row)
        incumbent = freshest.get(key)
        if incumbent is None or row["last_contact"] > incumbent["last_contact"]:
            freshest[key] = row
    return sorted(freshest.values(), key=lambda r: (r["dt"], r["hour"], r["icao24"]))


def find_bronze_objects(settings: Settings) -> list[Path]:
    return sorted(settings.bronze_root.rglob("states_*.json"))


def normalise(paths: Iterable[Path]) -> NormaliseResult:
    """Read bronze objects and produce a typed, deduplicated Arrow table."""
    paths = list(paths)
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(read_bronze_object(path))

    deduped = deduplicate(rows)
    table = pa.Table.from_pylist(deduped, schema=SILVER_SCHEMA)

    logger.info(
        "normalised bronze objects",
        extra={
            "files": len(paths),
            "rows_in": len(rows),
            "rows_out": len(deduped),
            "duplicates_removed": len(rows) - len(deduped),
        },
    )
    return NormaliseResult(
        table=table, files_read=len(paths), rows_in=len(rows), rows_out=len(deduped)
    )


def write_silver(table: pa.Table, settings: Settings) -> list[Path]:
    """Write the table to Hive-partitioned Parquet under the silver root.

    Snappy compression is chosen over the alternatives because it is what
    Athena reads fastest and what every engine in this stack supports without
    configuration; the data is small enough that a better ratio buys nothing.
    """
    root = settings.silver_root
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    partition_values = sorted(
        {
            (dt, hour)
            for dt, hour in zip(table["dt"].to_pylist(), table["hour"].to_pylist(), strict=True)
        }
    )
    for dt, hour in partition_values:
        mask = [
            d == dt and h == hour
            for d, h in zip(table["dt"].to_pylist(), table["hour"].to_pylist(), strict=True)
        ]
        chunk = table.filter(pa.array(mask))
        directory = root / f"dt={dt}" / f"hour={hour}"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / "states.parquet"
        # Partition values live in the path, so dropping the columns avoids
        # storing the same string on every row -- the standard Hive contract.
        pq.write_table(
            chunk.drop_columns(list(PARTITION_COLUMNS)),
            destination,
            compression="snappy",
        )
        written.append(destination)
        logger.info(
            "wrote silver partition",
            extra={"path": destination.as_posix(), "rows": chunk.num_rows},
        )

    return written
