# GitHub Actions deploy role, assumed via OIDC.
#
# This is the module to read carefully. It is the only thing standing between a
# public repository's CI and a real AWS account, and the two ways it typically
# goes wrong are both silent:
#
#   1. A trust policy scoped with `repo:owner/*` or a `sub` wildcard, which
#      grants the role to every repository the owner has -- including one
#      created five minutes ago by someone who just got write access.
#   2. `Resource = "*"`, which turns a deploy role into an account-wide role
#      the moment anyone can influence what CI runs.
#
# Neither is present here, and both are asserted by tests.
#
# There are no long-lived access keys anywhere in this design. OIDC exchanges a
# short-lived GitHub-signed token for temporary AWS credentials, so there is no
# secret to leak, rotate, or accidentally commit -- which is the entire reason
# the repository can safely be public.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  oidc_provider_arn = var.create_oidc_provider ? (
    aws_iam_openid_connect_provider.github[0].arn
    ) : (
    "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
  )

  # Exactly one repository, at exactly one ref. Not a prefix, not a wildcard.
  subject_claim = "repo:${var.github_repository}:ref:${var.github_ref}"

  glue_catalog_arn  = "arn:aws:glue:${local.region}:${local.account_id}:catalog"
  glue_database_arn = "arn:aws:glue:${local.region}:${local.account_id}:database/${var.glue_database_name}"
  glue_tables_arn   = "arn:aws:glue:${local.region}:${local.account_id}:table/${var.glue_database_name}/*"

  # Object-level ARNs, one per prefix. Deliberately not "${bucket}/*": the role
  # has no reason to touch anything outside the lake's own prefixes.
  writable_prefix_arns = [
    for prefix in var.writable_prefixes :
    "${var.bucket_arn}/${prefix}/*"
  ]
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = var.thumbprints
}

# ---------------------------------------------------------------------------
# Trust policy
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "trust" {
  statement {
    sid     = "GitHubActionsOIDC"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    # Audience must be AWS STS. Without this, a token issued for any other
    # audience would be accepted.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # StringEquals, not StringLike. This is the line that matters: StringLike
    # with a trailing wildcard is the standard way this gets quietly widened.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.subject_claim]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = var.role_name
  description          = "OIDC deploy role for ${var.github_repository} at ${var.github_ref}."
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = var.max_session_duration

  # An explicit boundary caps what this role can ever do, even if its inline
  # policy is later widened by mistake. Optional because it requires a policy
  # that must already exist in the account.
  permissions_boundary = var.permissions_boundary_arn
}

# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "deploy" {

  # -- S3 ------------------------------------------------------------------

  statement {
    sid    = "ListLakeBucketWithinPrefixes"
    effect = "Allow"

    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]

    resources = [var.bucket_arn]

    # Listing is confined to the project's own prefixes, so the role cannot
    # enumerate the rest of the bucket even if something else lives there.
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = concat(
        [for prefix in var.writable_prefixes : "${prefix}/*"],
        var.writable_prefixes,
      )
    }
  }

  statement {
    sid    = "ReadWriteLakeObjects"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]

    resources = local.writable_prefix_arns
  }

  # -- Glue ----------------------------------------------------------------

  statement {
    sid    = "ReadGlueCatalog"
    effect = "Allow"

    actions = [
      "glue:GetDatabase",
      "glue:GetDatabases",
      "glue:GetTable",
      "glue:GetTables",
      "glue:GetPartition",
      "glue:GetPartitions",
    ]

    resources = [
      local.glue_catalog_arn,
      local.glue_database_arn,
      local.glue_tables_arn,
    ]
  }

  statement {
    sid    = "ManageDbtManagedTables"
    effect = "Allow"

    # dbt-athena creates and replaces a table per mart, so these are required
    # for `dbt build` to work at all. They are scoped to tables inside this
    # one database and cannot reach any other.
    actions = [
      "glue:CreateTable",
      "glue:UpdateTable",
      "glue:DeleteTable",
      "glue:BatchCreatePartition",
      "glue:BatchDeletePartition",
      "glue:CreatePartition",
      "glue:DeletePartition",
      "glue:UpdatePartition",
    ]

    resources = [
      local.glue_catalog_arn,
      local.glue_database_arn,
      local.glue_tables_arn,
    ]
  }

  # -- Athena --------------------------------------------------------------

  statement {
    sid    = "RunQueriesInTheCappedWorkgroup"
    effect = "Allow"

    actions = [
      "athena:StartQueryExecution",
      "athena:StopQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:GetQueryResultsStream",
      "athena:GetWorkGroup",
    ]

    # Scoped to the one workgroup that carries the bytes-scanned cutoff. The
    # role cannot run a query in a workgroup without a cost ceiling, which is
    # what makes that ceiling a control rather than a convention.
    resources = [var.athena_workgroup_arn]
  }

  statement {
    sid    = "ResolveTheDefaultDataCatalog"
    effect = "Allow"

    actions = ["athena:GetDataCatalog"]

    resources = [
      "arn:aws:athena:${local.region}:${local.account_id}:datacatalog/AwsDataCatalog",
    ]
  }
}

resource "aws_iam_policy" "deploy" {
  name        = "${var.role_name}-policy"
  description = "Least-privilege permissions for the ${var.github_repository} deploy role."
  policy      = data.aws_iam_policy_document.deploy.json
}

resource "aws_iam_role_policy_attachment" "deploy" {
  role       = aws_iam_role.deploy.name
  policy_arn = aws_iam_policy.deploy.arn
}
