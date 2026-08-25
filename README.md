# flightops-lakehouse

A cost-safe, portfolio-grade **lakehouse on AWS** built around live flight
telemetry from the [OpenSky Network](https://opensky-network.org/) public API —
with a **fully local development and CI path**, so the entire transformation
layer runs and is tested with zero AWS credentials.

> **Status: Phase 2 of 6 — ingestion.** Scaffold, security contract, tooling and
> the bronze ingestion path are in place. Normalisation, dbt, Terraform and CI
> land in subsequent phases. This README is a skeleton and will be rewritten in
> Phase 6.

---

## Architecture

```mermaid
flowchart TD
    A[OpenSky Network REST API<br/>/api/states/all] -->|ingest: Python| B
    B[("BRONZE<br/>raw JSON<br/>dt=YYYY-MM-DD/hour=HH")] -->|normalise: Python → Parquet| C
    C[("SILVER<br/>typed, deduplicated<br/>partitioned Parquet")] -->|transform: dbt| D
    D[("GOLD<br/>aggregate marts<br/>Glue Catalog")]
    D --> E[Athena · AWS]
    D --> F[DuckDB · local & CI]
```

The central design decision: **the same dbt models run against two adapters** —
`dbt-duckdb` locally and in CI, `dbt-athena-community` in AWS. One model
codebase, two profile targets. See `docs/adr/0001-duckdb-and-athena-dual-adapter.md`.

---

## Run it locally in 5 minutes

No AWS account, no credentials, no cloud spend.

```bash
make install
make check
```

That installs the package with its local + dev extras, runs `ruff`, and runs the
full test suite offline — the suite severs outbound sockets, so a test that
reaches the network fails rather than passing quietly on your machine.

To pull a live snapshot into the bronze layer:

```bash
.venv/bin/flightops ingest
```

Which lands a Hive-partitioned object and tells you exactly what it did:

```
INFO flightops.ingest fetched live snapshot [attempt=1 bytes=17998 states=140]
INFO flightops.ingest wrote bronze snapshot [path=data/bronze/dt=2026-08-25/hour=09/states_1787651200.json source=opensky-live states=140]
```

Offline, or OpenSky rate-limiting you? Replay the committed fixtures instead:

```bash
.venv/bin/flightops ingest --allow-fixture-fallback
```

The fallback is **opt-in**. A pipeline that silently substitutes demonstration
data for a failed fetch looks healthy while producing fiction, so the resulting
object is tagged `fixture-replay` rather than `opensky-live` and downstream
layers can always tell the difference.

### Configuration

Everything is environment-driven, with defaults that work on a clean checkout.

| Variable | Default | Purpose |
| --- | --- | --- |
| `FLIGHTOPS_OPENSKY_BASE_URL` | `https://opensky-network.org/api` | API root |
| `FLIGHTOPS_DATA_ROOT` | `data` | local lake root |
| `FLIGHTOPS_BRONZE_PREFIX` | `bronze` | bronze prefix under the root |
| `FLIGHTOPS_BBOX_{LAMIN,LOMIN,LAMAX,LOMAX}` | unset | geographic filter; all four or none |
| `FLIGHTOPS_HTTP_TIMEOUT` | `30` | per-request timeout, seconds |
| `FLIGHTOPS_MAX_RETRIES` | `4` | retries on 429/5xx |
| `FLIGHTOPS_BACKOFF_BASE` / `_MAX` | `2` / `60` | exponential backoff bounds, seconds |
| `FLIGHTOPS_ALLOW_FIXTURE_FALLBACK` | `0` | permit fixture replay on failure |

Access is **anonymous by design**. OpenSky's OAuth2 client secret is exactly the
class of value this repository must never hold, so the ingest path is built to
need no credential at all.

---

## Cost discipline

This repository is deliberately architected to cost approximately nothing:

| Avoided | Why |
| --- | --- |
| Glue crawlers | Billed per DPU-hour with a 10-minute minimum. Tables are declared in Terraform instead. |
| Glue ETL / Spark | Ingestion is plain Python (local) or a small Lambda. |
| VPC, NAT Gateway, ECS, Redshift, MWAA, OpenSearch | S3, Glue Catalog, Athena, Lambda and Step Functions are all VPC-free. |

Athena runs behind a workgroup with `bytes_scanned_cutoff_per_query` and
`enforce_workgroup_configuration` set. S3 lifecycle rules expire bronze after
30 days and move silver/gold to Infrequent Access after 60.

Full line-by-line breakdown: `docs/COSTS.md` (Phase 2+).

---

## Tech stack

| Layer | Tool |
| --- | --- |
| Ingestion | Python 3.11+, `requests` |
| Storage format | Parquet (snappy), Hive-style partitioning |
| Transformation | dbt (`dbt-duckdb` / `dbt-athena-community`) |
| Local query engine | DuckDB |
| Cloud query engine | Amazon Athena |
| Catalog | AWS Glue Data Catalog (declarative tables) |
| Infrastructure | Terraform |
| CI/CD | GitHub Actions, OIDC (no long-lived AWS keys) |
| Quality gates | ruff, pytest, gitleaks, tflint, checkov |

---

## Repository layout

```
src/flightops/     ingestion, normalisation, quality contracts, CLI
dbt/               staging -> intermediate -> marts
infra/             Terraform: lake_storage, glue_catalog, athena, oidc_role
orchestration/     Lambda ingest + Step Functions state machine
tests/             offline unit tests and committed fixtures
docs/              architecture, costs, data dictionary, ADRs
```

---

## Licence

[MIT](LICENSE)
