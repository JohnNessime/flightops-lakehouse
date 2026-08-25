# 2. Declarative Glue catalog tables instead of crawlers

**Status:** Accepted · 2026-08-25

## Context

Athena needs table definitions in the Glue Data Catalog to query Parquet in S3.
There are two ways to get them there.

**A Glue crawler** scans the S3 prefix, infers a schema and registers or updates
the table. It is the path every AWS tutorial takes, and it is genuinely
convenient when you do not know your own schema.

**Declarative `aws_glue_catalog_table` resources** in Terraform state the schema
explicitly and register it directly.

## Decision

**Declarative tables. No crawlers anywhere in this project.**

### Cost

Crawlers bill per DPU-hour with a **10-minute minimum charge per run**, at
roughly $0.44 per DPU-hour. A crawler on an hourly schedule costs on the order
of **$13/month** — to rediscover, every hour, a schema that is defined in
`normalise.py` and has not changed.

Declarative tables cost **nothing**. The Glue Data Catalog is free for the first
million objects, and this project has a few dozen.

For a project whose entire steady-state bill is under $0.10/month, a crawler
would be **over 99% of the cost** and would buy nothing.

### Correctness

The cost argument is the loud one. The correctness argument is better.

A crawler *infers* schema. Inference is a guess, and the guess changes with the
data. A partition where every `baro_altitude_m` happens to be null gets typed
`string`. A `squawk` column of `"1000"`, `"2000"`, `"7000"` gets typed `bigint` —
and then `"0021"` arrives and either fails or silently becomes `21`, losing a
leading zero that is significant in an octal transponder code.

The schema is not something to be discovered. It is **already known**: declared
in `SILVER_SCHEMA` and enforced by `quality.py` before a single byte is written.
Having a crawler re-derive it downstream is not merely wasteful, it introduces a
second source of truth that can disagree with the first.

### Reviewability

A declarative table is a diff. Adding a column, changing a type or altering a
partition key shows up in a pull request and gets reviewed. A crawler changing
its mind about a type shows up as a query returning wrong results next Tuesday.

## Consequences

**Good.** Zero cost. Schema is version-controlled, reviewable, and
single-sourced with the producer. No inference, so no type drift.

**Bad.** A schema change now requires editing Terraform as well as
`normalise.py`. That is real friction — and it is the right friction. The two
places that define the schema should be changed together and deliberately, which
is exactly what a crawler lets you avoid doing until it hurts.

**Partitions still need registering.** Declaring the table does not tell Glue
which partitions exist. This project uses **partition projection** on `dt` and
`hour`, so Athena computes partition locations from the query predicate rather
than reading a partition list. That keeps it free and avoids `MSCK REPAIR TABLE`
scans entirely — the same reasoning as above, applied one level down.

## Alternatives considered

**Crawler on a manual schedule**, run only after a schema change. Rejected: it
still infers rather than declares, so the correctness problem stands. Scheduling
merely reduces the bill.

**`athena_partition_projection` without declarative tables** is not an
alternative — projection is configured *as* table properties, so it presupposes
this decision.
