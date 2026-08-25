# Orchestration

Scheduled bronze ingestion in AWS: **EventBridge → Step Functions → Lambda → S3**.

The local path (`make pipeline`) needs none of this. This exists so the cloud
deployment can keep the lake fed without anyone running a command, which is the
difference between a pipeline and a demo.

> **Not deployed.** Like the rest of `infra/`, this is verified by static
> analysis and unit tests, never applied to a real AWS account. The schedule is
> also **disabled by default**, so applying it does not silently start a
> recurring job in someone's account — enabling it should be a deliberate act.

## Layout

| Path | What it is |
| --- | --- |
| `lambda_ingest/handler.py` | Lambda entrypoint. Thin: it imports `flightops.ingest` and does not reimplement any of it. |
| `step_functions/ingest_state_machine.asl.json` | State machine definition, templated with the Lambda ARN at apply time. |
| `../infra/modules/orchestration/` | The Terraform that deploys both. |

## Why the handler is thin

Everything interesting — the backoff policy, the provenance envelope, the
partition layout — lives in `flightops.ingest` and is shared with the local
path. A Lambda that reimplemented any of it would drift from the code CI
actually exercises, and the drift would surface weeks later as a bronze object
in the wrong place.

The one genuine difference is the destination: locally the writer puts a file on
disk, here it puts an object in S3. Both derive the location from the same
`partition_segments`, so they cannot disagree about where a snapshot belongs —
and there is a test asserting the S3 key matches the local path exactly.

## Why Step Functions rather than EventBridge → Lambda directly

An honest answer, because for a single task this is arguably more machinery than
the job needs:

- The **retry policy becomes declarative** and visible in the console, rather
  than buried in function code and changeable only by redeploying.
- A **failed execution is diagnosable** from the execution history without
  reading CloudWatch, because the task result carries bucket, key, state count
  and provenance.
- It is the **seam where normalisation becomes a second task** later, without
  rewriting the schedule.

At 24 executions a day both services sit inside the perpetual free tier, so the
extra resource costs nothing. If it did, the calculus would be different.

The state machine **fails loudly** rather than succeeding quietly — a schedule
that swallows errors looks healthy while producing nothing, which is strictly
worse than a visible red execution.

## The deployment package

```bash
make lambda-package   # -> build/lambda_ingest, ~2 MiB
```

Built by the Makefile rather than by Terraform, because the build contract
forbids `local-exec`: Terraform zips a directory, it does not run a build tool.
Run it before `terraform plan`.

Two things are deliberately **not** in the package:

- **pyarrow** — `pip install .` would vendor it, at 84 MiB, for a handler that
  never touches Parquet. Parquet belongs to `normalise`, which does not run in
  Lambda. Installing only `requests` and copying the package source brings it to
  ~2 MiB.
- **boto3** — the Lambda runtime provides it. Shipping a second copy would dwarf
  everything else in the zip. It is imported lazily inside the handler, which
  also keeps the module unit-testable without an AWS SDK installed.

## Permissions

The function's role can do exactly two things:

```
s3:PutObject   on  <bucket>/bronze/*   (and only with AES256 encryption)
logs:CreateLogStream, logs:PutLogEvents  on its own log group
```

Not `GetObject`, not `DeleteObject`, not `ListBucket`. The function produces
snapshots; nothing about its job requires reading one back, and a write-only
credential cannot be used to exfiltrate. `logs:CreateLogGroup` is absent too —
Terraform creates the group, so the function neither needs the permission nor
can create groups elsewhere.
