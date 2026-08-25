# 3. Partitioning on dt and hour, not on origin_country

**Status:** Accepted · 2026-08-25 · Supersedes the original design

## Context

The original design partitioned the silver layer by `dt` **and**
`origin_country`. The reasoning was ordinary and reasonable: `origin_country` is
a common filter, partitioning on a common filter enables partition pruning, and
pruning is the main lever on Athena cost because bytes scanned is the only thing
Athena bills for.

Then the layer was built and measured against real data.

## The measurement

One snapshot over a Switzerland-sized bounding box:

| | |
| --- | --- |
| Aircraft per snapshot | ~150 |
| Distinct `origin_country` values | 22 |
| **Mean rows per country partition** | **~7** |

Even after aggregating six snapshots across two hours — 818 deduplicated rows —
the largest country partition holds 308 rows and the median holds fewer than
ten. Roughly half the countries contribute a single-digit number of rows.

Those are not partitions. They are files with a directory around them.

## Decision

**Partition on `dt` and `hour`. Keep `origin_country` as an ordinary column.**

### Why the small-file problem outweighs the pruning benefit

Every partition is at least one object in S3. At ~7 rows per file:

- **Per-request cost dominates.** S3 GET requests are billed per request. With
  Parquet, reading a file means a footer read plus column-chunk reads — several
  requests per file. Twenty-two tiny files per hour costs more in requests than
  one file costs in scanned bytes.
- **Per-file overhead dominates the scan.** A Parquet file carries a footer with
  schema and row-group statistics. At seven rows, the metadata is a substantial
  fraction of the file. Athena opens, parses and plans each one.
- **The 10 MB minimum settles it.** Athena bills a minimum of 10 MB scanned per
  query. Every file in this project is far below that, so *pruning saves nothing
  at all* — the entire silver layer already scans as a single minimum-billing
  unit. The benefit that justified country partitioning does not exist at this
  scale.

Meanwhile `WHERE origin_country = 'Switzerland'` against a regular column still
filters perfectly well. Parquet stores per-row-group min/max statistics, so the
predicate is pushed down and irrelevant row groups are skipped without any
directory structure. The filter works; it just does not need its own folder.

### Why `dt` and `hour` do earn their place

Time is the axis this data grows along. A day from now there are 24 hour
partitions; a month from now, 720. That is the dimension where partition count
scales with **data volume** rather than with the cardinality of a categorical
field — which is the actual test for whether something should be a partition
key.

`hour` is kept separate from `dt` rather than folded into one timestamp
partition because hourly granularity is what the time-series marts group by, and
because it keeps each partition at a useful size as the dataset grows.

## Consequences

**Good.** Partition count grows with time, not with country cardinality. Files
stay large enough to be worth opening. `origin_country` predicates still push
down via Parquet statistics. Two partition keys instead of three is simpler to
declare in Glue and to project.

**Bad.** A query filtering *only* on country, across all time, scans everything.
At this volume that lands under Athena's 10 MB minimum, so it is free either way
— but this decision would need revisiting if the dataset grew by several orders
of magnitude, at which point country partitions might genuinely earn their
place.

**This is a threshold decision, not a universal rule.** Partition on a
categorical field when the partitions would be large; partition on time when the
data grows along time. Here the data is small and the categorical field is
high-cardinality relative to volume, so time wins. The balance is the thing to
re-examine later, not the conclusion.

## Note on process

This reverses the original specification. It was changed because building the
layer and measuring it produced a number — seven rows per partition — that the
original reasoning had not accounted for. The specification was right about the
mechanism and wrong about the scale.

## Bronze partitioning

Bronze uses the same `dt`/`hour` scheme, derived from the **observation time
OpenSky reports**, not from local wall clock. A snapshot fetched at 00:00:03
describing 23:59:58 belongs in the earlier hour. Using fetch time would produce
partitions that disagree with their own contents — a bug that is invisible until
someone queries a boundary hour and gets an answer that is quietly wrong.
