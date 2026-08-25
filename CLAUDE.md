# CLAUDE.md — Build Contract for `flightops-lakehouse`

You are building a public GitHub portfolio repository. Treat this file as the
authoritative specification. Do not deviate without asking me first.

---

## 0. NON-NEGOTIABLE SECURITY CONTRACT (do this FIRST, before any other work)

This repository will be **public**. Nothing personal, private, or credential-like
may ever enter the working tree or git history.

### 0.1 Absolute prohibitions

Never write, commit, or embed any of the following — not in code, not in docs,
not in comments, not in example values, not in test fixtures:

- AWS account IDs (12-digit numbers) — use `123456789012` as an obvious placeholder
- Real ARNs — use `arn:aws:iam::123456789012:role/EXAMPLE_ROLE`
- Access keys, secret keys, session tokens, `.aws/credentials` content
- API keys or tokens of any kind
- My real email address, my legal name in machine-readable metadata, phone
  numbers, physical addresses
- Real S3 bucket names I own, real domain names I own, real hostnames, real IPs
- `.tfstate` / `.tfstate.backup` files
- `.env` files
- Any downloaded data file (`.parquet`, `.csv`, `.json` snapshots, `.duckdb`)

### 0.2 Commit identity

Before the first commit, run and show me the output of:

```bash
git config user.email
```

If it is anything other than a GitHub `users.noreply.github.com` address, STOP
and tell me. I will fetch the correct noreply address from
GitHub → Settings → Emails and give it to you. Then set it repo-locally:

```bash
git config user.email "<ID>+JohnNessime@users.noreply.github.com"
git config user.name "JohnNessime"
```

Do not guess this address.

### 0.3 Pre-commit tooling — install before the first commit

Create `.pre-commit-config.yaml` with, at minimum:

- `gitleaks` (secret scanning)
- `detect-private-key`
- `check-added-large-files` (max 500 KB)
- `end-of-file-fixer`, `trailing-whitespace`
- `terraform_fmt`, `terraform_validate`, `tflint`, `checkov` (via
  `antonbabenko/pre-commit-terraform`)
- `ruff` and `ruff-format` for Python

Then run `pre-commit install` and confirm the hooks fire.

### 0.4 `.gitignore` — write this before writing any code

Must cover at least:

```
.env
.env.*
*.tfstate
*.tfstate.*
.terraform/
.terraform.lock.hcl
crash.log
data/
*.duckdb
*.parquet
*.csv
!tests/fixtures/*.csv
__pycache__/
.venv/
venv/
.pytest_cache/
.ruff_cache/
target/
dbt_packages/
logs/
.DS_Store
.idea/
.vscode/
```

Verify each pattern actually works — run `git status --ignored` and show me that
`data/` and `*.tfstate` are genuinely excluded. A `.gitignore` pattern that
silently fails is worse than none.

### 0.5 Gate before publishing

Do **not** create the GitHub repo or push until all of the following pass and
you have shown me the output:

```bash
gitleaks detect --source . --no-git -v
gitleaks detect --source . -v          # scans history too
git log --all --format='%an <%ae>' | sort -u
grep -rInE '[0-9]{12}' --exclude-dir=.git . || echo "no 12-digit strings"
```

The third command must show only the noreply address. If anything looks wrong,
stop and report rather than "fixing" it silently.

---

## 1. What we are building

A cost-safe, portfolio-grade **lakehouse on AWS** built around live flight
telemetry, with a **fully local development and CI path** so the entire
transformation layer can be tested with zero AWS credentials.

**Data source:** OpenSky Network public REST API (`/api/states/all`). Anonymous
access, rate-limited, no key required, no personal data. If the API is
unreachable, fall back to the committed fixture data — never fabricate records
and pass them off as real.

**Core architecture:**

```
OpenSky API
    │  (ingest: Python)
    ▼
BRONZE  raw JSON snapshots, partitioned dt=YYYY-MM-DD/hour=HH
    │  (normalise: Python → Parquet, snappy)
    ▼
SILVER  typed, deduplicated, partitioned Parquet
    │  (transform: dbt)
    ▼
GOLD    aggregate marts (Parquet, registered in Glue Catalog)
    │
    ▼
Athena (AWS)  ·  DuckDB (local)
```

**The key design decision:** the same dbt models run against two adapters —
`dbt-duckdb` locally and in CI, `dbt-athena-community` in AWS. One codebase, two
profiles. Document this explicitly in an ADR; it is the most interesting thing
about the repo.

---

## 2. Cost discipline (hard requirements)

I am building a portfolio, not paying an AWS bill. Enforce all of these:

- **No Glue crawlers.** Define tables declaratively as
  `aws_glue_catalog_table` resources in Terraform. Crawlers bill per DPU-hour
  with a 10-minute minimum; declarative tables are free.
- **No Glue ETL jobs.** Ingestion and normalisation are plain Python (local) or
  a small Lambda. No Spark.
- **No VPC, no NAT Gateway, no ECS, no Redshift, no MWAA, no OpenSearch.**
  S3 / Glue Catalog / Athena / Lambda / Step Functions are all VPC-free.
- **Athena workgroup must set `bytes_scanned_cutoff_per_query`** (10 GB) and
  `enforce_workgroup_configuration = true`. This is a real cost guardrail and a
  good talking point.
- **S3 lifecycle rules**: bronze → expire after 30 days; silver/gold →
  transition to Infrequent Access after 60 days.
- Keep the working dataset **small** — a handful of snapshots is plenty to
  demonstrate partitioning. This is not a scale exercise.
- Add a `docs/COSTS.md` documenting expected monthly spend line by line, and the
  exact AWS Budget alert to configure before deploying.

Everything in CI must run on DuckDB. CI must never need AWS credentials.

---

## 3. Repository layout

```
flightops-lakehouse/
├── README.md
├── CLAUDE.md
├── LICENSE                      # MIT
├── Makefile
├── .gitignore
├── .pre-commit-config.yaml
├── pyproject.toml
├── .github/workflows/
│   ├── ci.yml
│   └── security.yml
├── src/flightops/
│   ├── __init__.py
│   ├── config.py                # env-driven, no hardcoded values
│   ├── ingest.py                # OpenSky → bronze JSON
│   ├── normalise.py             # bronze → silver Parquet
│   ├── quality.py               # schema + range contracts
│   └── cli.py
├── dbt/
│   ├── dbt_project.yml
│   ├── profiles.example.yml     # example only — never a real profile
│   ├── models/
│   │   ├── staging/
│   │   ├── intermediate/
│   │   └── marts/
│   ├── seeds/
│   └── tests/
├── infra/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── versions.tf
│   └── modules/
│       ├── lake_storage/        # S3 + lifecycle + encryption
│       ├── glue_catalog/        # database + declarative tables
│       ├── athena/              # workgroup + cost cutoff
│       └── oidc_role/           # GitHub OIDC deploy role
├── orchestration/
│   ├── lambda_ingest/
│   └── step_functions/
├── tests/
│   ├── fixtures/
│   ├── test_ingest.py
│   ├── test_normalise.py
│   └── test_quality.py
└── docs/
    ├── ARCHITECTURE.md
    ├── COSTS.md
    ├── DATA_DICTIONARY.md
    └── adr/
        ├── 0001-duckdb-and-athena-dual-adapter.md
        ├── 0002-declarative-glue-tables-over-crawlers.md
        └── 0003-partitioning-strategy.md
```

---

## 4. Build phases

Build **one phase per session**. At the end of each phase: run the full test
suite, run `pre-commit run --all-files`, commit with a conventional-commit
message, and push. Do not run ahead to the next phase.

### Phase 1 — Foundation
Security contract from §0, repo scaffold, `pyproject.toml`, `Makefile`,
`.gitignore`, pre-commit hooks, MIT licence, README skeleton. Create the GitHub
repo only after the §0.5 gate passes. Push.

### Phase 2 — Ingestion
`ingest.py`: fetch OpenSky `/states/all`, handle rate limiting with backoff,
write bronze JSON partitioned by `dt=`/`hour=`. Config strictly from environment
variables with safe defaults. Commit 2–3 small fixture snapshots to
`tests/fixtures/` (verify they contain no PII first). Unit tests with a mocked
HTTP layer — no network calls in tests.

### Phase 3 — Normalisation and quality
`normalise.py`: bronze JSON → typed silver Parquet, deduplicated on
`(icao24, time_position)`, partitioned by `dt` and `origin_country`.
`quality.py`: assert expected schema, non-null keys, latitude/longitude within
valid ranges, altitude within plausible bounds. Fail loudly on violation. Write
`docs/DATA_DICTIONARY.md` describing every column.

### Phase 4 — dbt transformation layer
dbt project with staging → intermediate → marts. At least four marts, e.g.
flights per origin country per hour, altitude-band distribution, on-ground
ratio, and a callsign-prefix carrier rollup. Add `schema.yml` tests
(`not_null`, `unique`, `accepted_values`, `relationships`). `profiles.example.yml`
holds both the `duckdb` and `athena` targets with placeholder values only. Prove
`dbt build` passes end to end on DuckDB against the fixtures.

### Phase 5 — Terraform
The four modules in §3. S3 with versioning, `block_public_access`, SSE-S3,
lifecycle rules. Glue catalog database plus declarative external tables pointing
at the silver/gold prefixes. Athena workgroup with the bytes-scanned cutoff.
OIDC role scoped to this repository only, with least-privilege policy — no
wildcards on resources. Every value that could identify my account comes from a
variable with a placeholder default. `terraform fmt`, `validate`, `tflint`, and
`checkov` must all pass clean. Commit no state and no lock file.

### Phase 6 — CI/CD and documentation
`ci.yml`: ruff lint and format check, pytest with coverage, `dbt build` on
DuckDB, `terraform fmt -check`, `validate`, `tflint`, `checkov`. Runs on push
and PR. **No AWS credentials.** SHA-pin every third-party action.
`security.yml`: gitleaks plus `pip-audit`, scheduled weekly.
Then write the real README: architecture diagram in Mermaid, what it
demonstrates, how to run locally in under five minutes, cost notes, screenshots
of green CI. Write the three ADRs. Add repository topics
(`aws`, `terraform`, `dbt`, `data-engineering`, `duckdb`, `athena`, `lakehouse`).

---

## 5. Engineering standards

- Python 3.11+, type hints throughout, `ruff` clean.
- Every module has a docstring explaining *why*, not just *what*.
- No `print` — use `logging` with structured context.
- Config via environment variables only. No hardcoded paths, buckets, or regions.
- Tests must not touch the network or AWS.
- Terraform: pinned provider versions, no `local-exec`, no wildcard IAM resources.
- Conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, `test:`, `ci:`).
- Small, frequent commits over large ones.

---

## 6. README requirements

The README is what a recruiter actually reads. It must open with a one-paragraph
statement of what the project does and what it demonstrates, followed by a
Mermaid architecture diagram, then a "Run it locally in 5 minutes" section that
genuinely works with no AWS account. Cost transparency, tech-stack table, and
links to the ADRs after that. No skill claims that the code does not back up.

---

## 7. Working agreement

- Ask before installing anything not listed here.
- If a design decision has a meaningful trade-off, present the options and let
  me choose rather than picking silently.
- If something in this file conflicts with what you think is best, say so.
- Never push directly to `main` without running the full local check suite first.
- If you are ever unsure whether something is sensitive: assume it is, and ask.
