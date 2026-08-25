# Cost model

This project is deliberately engineered to run at **effectively zero AWS spend**.
That is a design constraint, not an accident, and several architectural choices
exist only to satisfy it. This document states what each component costs, what
was deliberately avoided, and the budget alert to configure *before* deploying.

All figures are `eu-west-1` on-demand pricing as of August 2026 and exclude the
AWS Free Tier. Treat them as order-of-magnitude, not as a quote — verify against
the [AWS pricing calculator](https://calculator.aws/) before you deploy.

---

## Expected monthly spend

The working dataset is intentionally tiny: a handful of ~19 KB snapshots is
enough to demonstrate partitioning, and this is not a scale exercise. Assume
24 snapshots/day at ~20 KB, retained per the lifecycle rules below.

| Service | What it does here | Usage | Est. monthly |
| --- | --- | --- | --- |
| **S3 Standard** | bronze / silver / gold objects | < 100 MB | **< $0.01** |
| **S3 requests** | PUT on ingest, GET on query | ~1,500 PUT, ~5,000 GET | **< $0.01** |
| **Glue Data Catalog** | database + declarative tables | < 100 objects | **$0.00** — first 1M objects free |
| **Athena** | ad-hoc SQL over silver/gold | < 1 GB scanned | **< $0.01** — $5.00/TB |
| **Lambda** | scheduled ingest | 720 invocations × ~2 s × 128 MB | **$0.00** — inside the perpetual free tier |
| **Step Functions** | ingest orchestration | ~720 state transitions | **$0.00** — first 4,000/month free |
| **CloudWatch Logs** | Lambda output | < 50 MB ingested | **< $0.03** |
| | | **Total** | **well under $0.10/month** |

The dominant risk is not steady-state spend. It is a single mistake — an
unbounded Athena scan, a crawler left running, an accidental NAT Gateway — so
the guardrails below matter far more than the table above.

---

## What was deliberately avoided, and why

| Avoided | Why it would cost | Used instead |
| --- | --- | --- |
| **Glue crawlers** | Billed per DPU-hour with a **10-minute minimum** per run. Hourly crawling is ~$13/month to rediscover a schema that never changes. | `aws_glue_catalog_table` resources declared in Terraform. Free, version-controlled, and reviewable in a diff. |
| **Glue ETL jobs / Spark** | Minimum 2 DPU, per-second billing with a 1-minute floor. Spark to reshape 20 KB of JSON is absurd. | Plain Python locally; a 128 MB Lambda in AWS. |
| **NAT Gateway** | **~$32/month** hourly charge alone, before data processing — the single most common surprise line on a hobby AWS bill. | No VPC at all. S3, Glue, Athena, Lambda and Step Functions are VPC-free. |
| **Redshift / OpenSearch / MWAA** | Provisioned, billed hourly whether idle or not. MWAA is ~$350/month minimum. | Athena is serverless and billed per byte scanned; Step Functions replaces the scheduler. |
| **ECS / Fargate** | Billed per vCPU-second while a task runs. | Lambda, comfortably inside the free tier at this volume. |

---

## Guardrails

These are enforced in code, not by discipline:

**Athena workgroup** (Phase 5, `infra/modules/athena`)
- `bytes_scanned_cutoff_per_query = 10 GB` — a runaway query is *killed*, not
  billed. At $5/TB, 10 GB caps a single mistake at about $0.05.
- `enforce_workgroup_configuration = true` — a client cannot override the cutoff
  by passing its own settings. Without this the cutoff is a suggestion.

**S3 lifecycle** (Phase 5, `infra/modules/lake_storage`)
- bronze → **expire after 30 days**. Raw snapshots are reproducible; keeping
  them forever is paying to store something you can re-fetch.
- silver / gold → **transition to Infrequent Access after 60 days**.

**Partition pruning** — silver and gold are partitioned by `dt`, so a
well-written query scans one day rather than the whole table. Partitioning is
the actual Athena cost lever; everything else is rounding error.

---

## Configure this before you deploy

Terraform is not applied until an AWS Budget exists. Create it first:

```bash
aws budgets create-budget \
  --account-id "$AWS_ACCOUNT_ID" \
  --budget '{
    "BudgetName": "flightops-lakehouse-monthly",
    "BudgetLimit": {"Amount": "5", "Unit": "USD"},
    "TimeUnit": "MONTHLY",
    "BudgetType": "COST"
  }' \
  --notifications-with-subscribers '[{
    "Notification": {
      "NotificationType": "ACTUAL",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 50,
      "ThresholdType": "PERCENTAGE"
    },
    "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "YOUR_EMAIL_HERE"}]
  }]'
```

A **$5** limit alerting at **50%** is deliberately far below any plausible
correct spend. If this fires, something is wrong — that is the entire point.
Set a second notification at `FORECASTED` / 100% to catch a runaway early.

---

## The local path costs nothing at all

Everything in `make check` — ingestion, normalisation, quality contracts and the
full dbt build — runs against DuckDB on local Parquet. CI never authenticates to
AWS and never provisions anything. The cloud deployment is a demonstration that
the same models run on Athena, not a dependency for developing or testing them.
