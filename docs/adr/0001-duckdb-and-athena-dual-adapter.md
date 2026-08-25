# 1. One dbt codebase on two adapters: DuckDB locally, Athena in AWS

**Status:** Accepted · 2026-08-25

## Context

The transformation layer needs to run in two places with contradictory
requirements.

**In CI and on a laptop** it must run with no AWS account, no credentials and no
spend. A contributor should be able to clone, install and get a green build in
minutes. A test suite that needs cloud credentials is a test suite that gets run
rarely and trusted less.

**In AWS** it must run on Athena over Parquet in S3, catalogued in Glue —
because demonstrating a lakehouse that only ever ran on a laptop demonstrates
very little.

The obvious options were all bad:

1. **Athena only.** Every test run costs money and needs credentials. CI needs a
   real AWS account. Contribution becomes gated on someone's billing.
2. **DuckDB only.** Nothing is proven about the cloud target. The word
   "lakehouse" becomes decorative.
3. **Two codebases.** They diverge. Everyone knows they diverge. The
   local one gets the attention and the cloud one rots.

## Decision

**One set of dbt models, two profile targets.** `dbt-duckdb` locally and in CI,
`dbt-athena-community` in AWS. The models are identical; only the profile and
the source definition differ.

Three constraints make it hold:

**No adapter conditionals in model SQL.** Not one `{% if target.type %}`. The
moment models branch on the engine, there are two codebases again wearing one
directory. `tests/test_dbt_project.py` fails the build if `target.type` or
`adapter.type` appears in any model.

**ANSI-portable SQL only.** The two engines diverge in exactly the places you
would expect — regex is `regexp_matches` in DuckDB and `regexp_like` in Trino;
date formatting is `strftime` versus `date_format`. Both are avoided outright.
The carrier-code extraction uses `substr` with `BETWEEN 'A' AND 'Z'` character
comparisons rather than a regex, which is uglier and portable. That trade is
made deliberately and is enforced by a test.

**One adapter-aware file.** `models/staging/_sources.yml` carries a
`meta.external_location` that DuckDB honours to read Parquet off disk, and that
Athena ignores entirely in favour of resolving the source through Glue by
database and table name. One declaration, both engines, no branching.

Partition-key types are the other real divergence: DuckDB infers `dt` as a
`DATE` from the Hive path while Athena presents every partition key as a string.
`stg_states` casts both explicitly, so nothing downstream ever has to know.

## Consequences

**Good.** CI needs no credentials and costs nothing. The full transformation
layer is testable offline. A contributor sees a green build without an AWS
account. Marts materialise as tables on both engines — on Athena that directly
reduces bytes scanned, which is the only thing Athena bills for.

**Bad.** The portable-SQL constraint is real and occasionally costs clarity; the
carrier-prefix logic would be one clean regex otherwise. Any future model
needing an engine-specific feature has to be solved portably or the property
breaks.

**Honest limitation.** `dbt-athena-community` is declared in the `[aws]` extra
and is deliberately **not installed** in CI or in the development environment.
So the Athena path is **configured and reviewed, not executed**. dbt cannot even
resolve the target without the adapter present:

```
$ dbt parse --target athena
Error importing adapter: No module named 'dbt.adapters.athena'
Runtime Error  Could not find adapter type athena!
```

What *is* verified automatically: the profile defines both targets, the default
target needs no credentials, and every AWS-shaped value in the Athena target
comes from `env_var` with no literal bucket, region or account id anywhere in
the file. What is **not** verified is that the models execute correctly on
Athena. Claiming otherwise would be claiming a test that does not exist.

Executing against Athena requires the Phase 5 infrastructure and an AWS account,
and is a manual step documented in [COSTS.md](../COSTS.md).

## Alternatives considered

**Trino locally via Docker** to match Athena's dialect exactly. Rejected: it
makes the quickstart depend on Docker and a multi-hundred-megabyte image, for a
dialect match that portable SQL already achieves. The five-minute local path is
worth more than dialect fidelity here.

**`dbt-glue` (Spark).** Rejected on cost grounds alone — see
[ADR 0002](0002-declarative-glue-tables-over-crawlers.md). Spark to aggregate a
few hundred kilobytes is indefensible.
