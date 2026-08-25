# Infrastructure

Terraform for the AWS side of the lakehouse. Four modules, no VPC, no
always-on compute, and a hard ceiling on the one service that can bill without
bound.

> **This has not been applied to a real AWS account.** `fmt`, `validate`,
> `tflint` and `checkov` all pass, and `tests/test_infra.py` asserts the
> security properties that matter — but static analysis proves the
> configuration *says* the right thing, not that AWS *did* the right thing.
> Deploying is a manual step, and nothing in CI touches AWS. Said plainly here
> rather than implied away.

## Modules

| Module | What it creates | Why it looks like this |
| --- | --- | --- |
| `lake_storage` | One S3 bucket, versioning, all four public-access blocks, SSE-S3, TLS-only policy, lifecycle rules | One bucket rather than three: the access patterns are identical and lifecycle rules are per-prefix anyway |
| `glue_catalog` | Glue database + declarative silver table with partition projection | No crawlers — [ADR 0002](../docs/adr/0002-declarative-glue-tables-over-crawlers.md) |
| `athena` | Workgroup with a bytes-scanned cutoff, plus example named queries | The cutoff is the single most important cost control here |
| `oidc_role` | GitHub OIDC provider + a repo-scoped, ref-scoped deploy role | No long-lived keys anywhere — which is why the repo can be public |

## Ownership boundary

Worth stating because it is the one non-obvious thing:

- **Terraform owns** the Glue database and the **silver** table. Silver is
  written by the Python layer, which has no way to register a catalog entry.
- **dbt owns** the **gold** tables. `dbt-athena` creates and replaces them as
  part of materialising each mart.

Declaring the gold marts here as well would put two systems in charge of the
same resources: every `dbt build` would drop and recreate tables Terraform
believes it manages, and every `terraform apply` would report drift it did not
cause. One owner per resource beats literal symmetry between the prefixes.

## Cost controls, in the code

```hcl
bytes_scanned_cutoff_per_query  = 10737418240  # 10 GiB — a runaway query is cancelled, not billed
enforce_workgroup_configuration = true         # without this, the cutoff is merely a default
```

Athena bills per byte scanned with no upper bound and no retroactive undo. At
$5/TB, the cutoff caps a single mistake at roughly $0.05. Enforcement is what
turns it from a suggestion a client can override into a control.

S3 lifecycle: bronze expires after 30 days (raw snapshots are re-fetchable),
silver and gold move to Infrequent Access after 60. Incomplete multipart
uploads are aborted after 7 — they bill as storage and appear nowhere in the
console.

Full breakdown: [docs/COSTS.md](../docs/COSTS.md).

## Security notes

**The trust policy is scoped to one repository at one ref**, with
`StringEquals` rather than `StringLike`:

```
repo:OWNER/REPO:ref:refs/heads/main
```

A `StringLike` with a trailing wildcard is the standard way this gets quietly
widened to every repository an owner has — including one created five minutes
ago by someone who just got write access. Variable validation rejects a `*` in
either the repository or the ref outright.

**No statement uses `Resource = "*"`.** S3 access is scoped to named prefixes,
Glue to a single named database, Athena to the one workgroup that carries the
cost ceiling. A role able to query in any workgroup could query in one without
a cutoff.

The one unavoidable wildcard is `table/${database}/*`, because dbt creates a
table per mart at build time and their names are not known in advance. It is
scoped inside a single database and cannot reach another.

**Five checkov suppressions**, each with its reasoning inline. They are the
thing a reviewer should be most suspicious of in this directory, so
`test_every_checkov_suppression_carries_a_real_reason` fails the build on a
justification shorter than 60 characters. `skip=CKV_X:wontfix` does not pass
review, so it does not pass CI.

The load-bearing one is **CKV_AWS_145** (KMS encryption). SSE-S3 is used
instead: KMS costs $1/month per key plus $0.03 per 10k requests, which would
exceed every other line in this project's bill combined, and buys no
confidentiality property that matters for data published by an open API.
Encryption at rest is still enforced unconditionally. This is the first thing
that should change if the bucket ever holds data with a real confidentiality
requirement.

## Deploying

Nothing here has been applied. If you want to:

```bash
# 1. Set the budget alert FIRST. See docs/COSTS.md.
# 2. Provide your own values.
cp infra/terraform.tfvars.example infra/terraform.tfvars
$EDITOR infra/terraform.tfvars     # bucket_name and github_repository are required

# 3. Review, then apply.
terraform -chdir=infra init
terraform -chdir=infra plan
terraform -chdir=infra apply
```

`terraform.tfvars` is gitignored — it will hold a real bucket name and a real
repository, neither of which belongs in a public repository.

The outputs map directly onto the environment variables the dbt `athena` target
reads:

| Output | Environment variable |
| --- | --- |
| `glue_database_name` | `FLIGHTOPS_GLUE_DATABASE` |
| `athena_workgroup_name` | `FLIGHTOPS_ATHENA_WORKGROUP` |
| `athena_results_uri` | `FLIGHTOPS_ATHENA_STAGING_DIR` |
| `deploy_role_arn` | `role-to-assume` in the GitHub Actions workflow |

## Checks

```bash
make tf-fmt tf-validate tf-lint tf-checkov
```

All four pass clean. Checkov reports 51 passed, 0 failed, 5 skipped.

## On the lock file

`.terraform.lock.hcl` is gitignored, per this project's build contract. That
runs **against** HashiCorp's own recommendation — the lock file pins provider
*hashes*, so committing it is what makes `terraform init` reproducible across
machines and protects against a compromised provider release.

The contract's reasoning is that a lock file is generated state. That is
defensible for a portfolio repository where nobody else runs `init`, and it is
recorded here as a deliberate deviation rather than an oversight. Provider
versions are still pinned in `versions.tf`, so the exposure is a same-minor
provider release, not an arbitrary one.
