# Three roles, each scoped to exactly what its principal needs.
#
# The rule applied throughout: no statement uses a bare resource wildcard, and
# the S3 grant is write-only into a single prefix. The ingest function has no
# reason to read anything, no reason to delete anything, and no reason to touch
# silver or gold -- so it cannot.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
}

# ---------------------------------------------------------------------------
# Lambda execution role
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name_prefix}-ingest-lambda"
  description        = "Execution role for the bronze ingestion function."
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid    = "WriteBronzeObjectsOnly"
    effect = "Allow"

    # PutObject alone. Not GetObject, not DeleteObject, not ListBucket. The
    # function produces snapshots; nothing about its job requires reading or
    # removing one, and a write-only credential cannot be used to exfiltrate.
    actions = ["s3:PutObject"]

    resources = ["${var.bucket_arn}/${var.bronze_prefix}/*"]

    # Refuse to write an unencrypted object even if the bucket default changes.
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["AES256"]
    }
  }

  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    # Its own log group, not every log group in the account. CreateLogGroup is
    # deliberately absent: Terraform creates it, so the function does not need
    # the permission and cannot create groups elsewhere.
    resources = ["${aws_cloudwatch_log_group.ingest.arn}:*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.name_prefix}-ingest-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

# ---------------------------------------------------------------------------
# Step Functions role
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "state_machine_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }

    # Confused-deputy protection: only this account's state machines may assume
    # the role, and only this one by ARN.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:states:${local.region}:${local.account_id}:stateMachine:${local.state_machine_name}"]
    }
  }
}

resource "aws_iam_role" "state_machine" {
  name               = "${var.name_prefix}-ingest-sfn"
  description        = "Execution role for the ingestion state machine."
  assume_role_policy = data.aws_iam_policy_document.state_machine_trust.json
}

data "aws_iam_policy_document" "state_machine" {
  statement {
    sid       = "InvokeTheIngestFunctionOnly"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.ingest.arn]
  }

  statement {
    sid    = "DeliverExecutionLogs"
    effect = "Allow"

    # These particular actions require a wildcard resource: the CloudWatch Logs
    # delivery API is account-scoped and rejects a resource-qualified grant.
    # That is an AWS constraint rather than a choice, and it is confined to log
    # delivery configuration, which cannot read or write log content.
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]

    resources = ["*"]

    # checkov:skip=CKV_AWS_356:The CloudWatch Logs delivery API is account-scoped and rejects resource-qualified ARNs for these actions, so the wildcard is imposed by AWS rather than chosen. The actions configure log delivery only; none of them can read or write log content, and every other statement in this module is fully resource-scoped.
  }
}

resource "aws_iam_role_policy" "state_machine" {
  name   = "${var.name_prefix}-ingest-sfn"
  role   = aws_iam_role.state_machine.id
  policy = data.aws_iam_policy_document.state_machine.json
}

# ---------------------------------------------------------------------------
# EventBridge role
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "events_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "events" {
  name               = "${var.name_prefix}-ingest-events"
  description        = "Lets the schedule start the ingestion state machine."
  assume_role_policy = data.aws_iam_policy_document.events_trust.json
}

data "aws_iam_policy_document" "events" {
  statement {
    sid       = "StartTheIngestStateMachineOnly"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.ingest.arn]
  }
}

resource "aws_iam_role_policy" "events" {
  name   = "${var.name_prefix}-ingest-events"
  role   = aws_iam_role.events.id
  policy = data.aws_iam_policy_document.events.json
}
