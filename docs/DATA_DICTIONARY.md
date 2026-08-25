# Data dictionary

Every column in the lake, what it means, and where it came from.

Upstream source: OpenSky Network `/api/states/all`. The API returns each
aircraft as a **positional array of 17 elements** with no field names — the
mapping below is the contract, and getting an index wrong silently shifts every
column. That is the single largest correctness risk in the ingestion path, which
is why `STATE_VECTOR_FIELDS` in [`normalise.py`](../src/flightops/normalise.py)
is a named tuple rather than a set of magic indices.

---

## Bronze — raw JSON

`data/bronze/dt=YYYY-MM-DD/hour=HH/states_<epoch>.json`

Bronze stores the upstream payload **verbatim** under `payload`, wrapped in a
thin provenance envelope. Nothing in `payload` is rewritten, reordered or
retyped.

| Field | Type | Description |
| --- | --- | --- |
| `ingest.schema_version` | int | Envelope version. Bumped when this shape changes, so a reader can tell what it understands without guessing from the keys present. |
| `ingest.source` | string | `opensky-live` or `fixture-replay`. **The contract forbids passing fabricated records off as real; this field is how that is enforced downstream rather than trusted.** |
| `ingest.fetched_at` | ISO-8601 | When *we* called the API. |
| `ingest.observed_at` | ISO-8601 | When *OpenSky* says the observation belongs. Partitioning uses this, not `fetched_at`. |
| `ingest.request_url` | string | Exact URL called, bounding box included. |
| `ingest.state_count` | int | Aircraft in this snapshot. |
| `payload.time` | int | Unix epoch of the observation window. |
| `payload.states` | array | State vectors, positional and unmodified. |

**Partitioning:** `dt` / `hour`, derived from `observed_at`. A snapshot fetched
at 00:00:03 that describes 23:59:58 lands in the *earlier* hour — using wall
clock instead would produce partitions that disagree with their own contents.

---

## Silver — typed Parquet

`data/silver/dt=YYYY-MM-DD/hour=HH/states.parquet` · snappy compression

One row per distinct observation, deduplicated and typed.

### Identity

| Column | Type | Null | Source index | Description |
| --- | --- | --- | --- | --- |
| `icao24` | string | no | 0 | 24-bit ICAO transponder address, 6 lowercase hex digits. Identifies an **airframe**, not a flight and not a person. Lowercased on ingest so joins are stable. |
| `callsign` | string | yes | 1 | Flight identifier (`SWR123`). OpenSky right-pads to 8 characters; the padding is **trimmed** here, because a downstream join that forgets to trim is silently wrong. Empty becomes `NULL`, never `''`. |
| `origin_country` | string | no | 2 | Country inferred from the ICAO address range. Defaults to `Unknown` rather than null, since the column is non-nullable. |

### Time

| Column | Type | Null | Source index | Description |
| --- | --- | --- | --- | --- |
| `time_position` | timestamp[s, UTC] | **yes** | 3 | Last position report. **Null when the aircraft is transmitting without a position fix** — normal and common. |
| `last_contact` | timestamp[s, UTC] | no | 4 | Last message of any kind. Always populated. |
| `observed_at` | timestamp[s, UTC] | no | — | Snapshot window this row came from. Derived, not upstream. |

### Position and motion

| Column | Type | Null | Source index | Unit | Description |
| --- | --- | --- | --- | --- | --- |
| `longitude` | double | yes | 5 | degrees | WGS-84. Range −180 to 180. |
| `latitude` | double | yes | 6 | degrees | WGS-84. Range −90 to 90. |
| `baro_altitude_m` | double | yes | 7 | **metres** | Barometric altitude. Renamed with a unit suffix — the API returns metres and the aviation world speaks feet, so an unsuffixed name invites a 3.28× error. |
| `geo_altitude_m` | double | yes | 13 | **metres** | GPS-derived altitude. |
| `velocity_ms` | double | yes | 9 | **m/s** | Ground speed. Not knots. |
| `true_track_deg` | double | yes | 10 | degrees | Clockwise from true north, 0–360. |
| `vertical_rate_ms` | double | yes | 11 | **m/s** | Positive is climbing. |
| `on_ground` | boolean | no | 8 | — | Surface position report. |

### Transponder

| Column | Type | Null | Source index | Description |
| --- | --- | --- | --- | --- |
| `squawk` | string | yes | 14 | 4-digit octal transponder code. **String, not integer** — leading zeros are significant, and `0021` is not `21`. |
| `spi` | boolean | no | 15 | Special Position Identification flag. |
| `position_source` | int8 | no | 16 | 0 ADS-B · 1 ASTERIX · 2 MLAT · 3 FLARM. |
| `position_source_label` | string | yes | — | Decoded label, so a reader of the table does not need the API docs open beside them. Unrecognised codes become `UNKNOWN`, never null. |

### Provenance and partitioning

| Column | Type | Null | Description |
| --- | --- | --- | --- |
| `ingest_source` | string | no | Carried from the bronze envelope: `opensky-live` or `fixture-replay`. Lets any consumer separate observation from demonstration data. |
| `dt` | string | no | **Partition.** `YYYY-MM-DD` from `observed_at`. |
| `hour` | string | no | **Partition.** Zero-padded `HH`, UTC. |

### Dropped column

`sensors` (index 12) is discarded. It is always null for anonymous API access,
and a nullable list-of-int costs real complexity in both Parquet and the Glue
catalog for a field that never carries a value here.

---

## Deduplication

**Key: `(icao24, COALESCE(time_position, last_contact))`**, keeping the row with
the greatest `last_contact`.

The obvious key is `(icao24, time_position)`, and that is what the original
design called for. It has a defect: `time_position` is null for any aircraft
transmitting without a position fix, and a null key collapses *every* such
aircraft into a single row. `last_contact` stands in for those cases. Rows are
never dropped — an aircraft with no position fix is still a real observation.

This matters because consecutive snapshots overlap heavily by design. In the
committed fixtures, captured 25 seconds apart, 139–143 of ~146 aircraft appear
in both — so deduplication is the difference between a table that grows with
time and one that grows with distinct observations.

---

## Partitioning

**`dt` / `hour`. Not `origin_country`.**

The original design partitioned on `origin_country` as well. Measured against
real data, one snapshot over a country-sized bounding box carries ~150 aircraft
spread across ~22 countries — roughly **seven rows per partition**. Small-file
fragmentation is precisely what makes Athena slow and expensive: per-request S3
charges and per-file open overhead swamp any pruning benefit at that cardinality.

`origin_country` is kept as an ordinary column, where a predicate on it still
filters perfectly well — it just does not get its own directory.

---

## Quality contracts

Enforced by [`quality.py`](../src/flightops/quality.py) **before** the silver
write, not after. Checking afterwards would leave a bad batch on disk for a
downstream reader to find first, which defeats the point of having a contract at
this boundary at all.

### Violations — these fail the run

| Contract | Rule |
| --- | --- |
| Schema | Exact column set and types. Compared field by field so the error names the column instead of dumping two schemas. |
| Primary key | `icao24`, `origin_country`, `last_contact`, `on_ground`, `observed_at` non-null. |
| `icao24` format | Exactly 6 hexadecimal digits. |
| Uniqueness | No duplicate `(icao24, COALESCE(time_position, last_contact))`. A duplicate here means dedup failed, which would inflate every downstream count with no visible symptom. |
| Latitude | −90 to 90 |
| Longitude | −180 to 180 |
| Altitude | −500 m to 100,000 m. Kármán line as ceiling; −500 m allows aerodromes below sea level. |
| Velocity | 0 to 1,000 m/s. The SR-71 topped out near 980. |
| True track | 0 to 360 degrees |
| Vertical rate | −200 to 200 m/s |
| Half a position | One coordinate present without the other. That is corruption, not incompleteness. |

Bounds are deliberately generous. The goal is catching unit errors, sign flips
and corruption — not second-guessing aviation.

### Warnings — reported, do not fail

Null `latitude`, `longitude`, `callsign` or `baro_altitude_m`. These are
legitimate but incomplete rows, overwhelmingly aircraft transmitting without a
position fix. Failing on them would mean failing on nearly every real batch, and
a check that always fires is a check nobody reads.

A run reports **every** violation at once rather than stopping at the first, so
one run tells you everything that is wrong instead of forcing a fix-and-retry
loop.

---

## Gold — marts

Arrives in Phase 4. Aggregate tables built with dbt, running on `dbt-duckdb`
locally and `dbt-athena-community` in AWS from the same model code.
