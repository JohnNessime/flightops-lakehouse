# Scheduled ingestion: EventBridge -> Step Functions -> Lambda -> S3.
#
# Why Step Functions for what is currently a single task, when EventBridge could
# invoke the Lambda directly: the retry policy becomes declarative and visible in
# the console rather than buried in function code, and a failed execution is
# diagnosable without reading CloudWatch. It is also the seam where normalisation
# becomes a second task later without rewriting the schedule.
#
# That is a real trade-off, not a free win. For one task it is arguably more
# machinery than the job needs, and the honest reason it is here rather than a
# bare EventBridge target is that the observability is worth more than the extra
# resource at this size. Both services stay inside the perpetual free tier at
# 24 executions a day; see docs/COSTS.md.

locals {
  function_name      = "${var.name_prefix}-ingest"
  state_machine_name = "${var.name_prefix}-ingest"
}

# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------

data "archive_file" "ingest" {
  type        = "zip"
  source_dir  = var.package_dir
  output_path = "${var.package_dir}.zip"
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days

  # checkov:skip=CKV_AWS_158:CloudWatch log groups are encrypted with an AWS-managed key by default. A customer-managed KMS key costs $1/month plus per-request charges to protect log lines that contain only aircraft counts and object keys, which is the same cost reasoning applied to the lake bucket in modules/lake_storage.
  # checkov:skip=CKV_AWS_338:One year of retention is a compliance default this project has no requirement for. Nobody investigates an hourly ingestion job a fortnight later, and a year of CloudWatch ingestion and storage would cost more than every other line in docs/COSTS.md combined. Fourteen days is deliberate, not an oversight: the alternative to a short retention is usually no retention at all, which never expires and leaks cost silently.
}

resource "aws_lambda_function" "ingest" {
  function_name = local.function_name
  description   = "Fetches one OpenSky snapshot into the bronze prefix."

  role    = aws_iam_role.lambda.arn
  handler = "handler.handler"
  runtime = var.python_runtime

  filename         = data.archive_file.ingest.output_path
  source_code_hash = data.archive_file.ingest.output_base64sha256

  # 128 MB is the floor and ample: the function fetches a few hundred kilobytes
  # of JSON and puts it in S3. Memory is the billing dimension, so the smallest
  # size that comfortably works is the correct one.
  memory_size = var.memory_mb
  timeout     = var.timeout_seconds

  environment {
    variables = merge(
      {
        FLIGHTOPS_BUCKET        = var.bucket_id
        FLIGHTOPS_BRONZE_PREFIX = var.bronze_prefix
      },
      var.bbox == null ? {} : {
        FLIGHTOPS_BBOX_LAMIN = tostring(var.bbox.lamin)
        FLIGHTOPS_BBOX_LOMIN = tostring(var.bbox.lomin)
        FLIGHTOPS_BBOX_LAMAX = tostring(var.bbox.lamax)
        FLIGHTOPS_BBOX_LOMAX = tostring(var.bbox.lomax)
      }
    )
  }

  # checkov:skip=CKV_AWS_117:A VPC would require a NAT Gateway for the function to reach the public OpenSky API, at roughly $32/month -- several hundred times this project's entire bill. The function holds no credential, reads one public API and writes to one S3 prefix, so there is nothing in the VPC for it to be isolated from.
  # checkov:skip=CKV_AWS_272:Code signing requires a signing profile and a publishing pipeline. The deployment package is built from this repository by a SHA-pinned workflow and its hash is tracked in Terraform state, which is the meaningful integrity control at this scale.
  # checkov:skip=CKV_AWS_115:A reserved concurrency limit protects other functions from this one exhausting the account pool. This account runs one function invoked once an hour, so there is no pool to protect and reserving concurrency would only add a failure mode.
  # checkov:skip=CKV_AWS_50:X-Ray tracing bills per trace and per scanned trace. A single-step function whose result is already returned to Step Functions and logged has nothing X-Ray would reveal.
  # checkov:skip=CKV_AWS_116:A dead letter queue would duplicate error handling that Step Functions already owns. The state machine retries with backoff and then transitions to an explicit Fail state, so a failed invocation is already visible and already retried; adding an SQS queue would create a second place to look and a second thing to monitor, for the same information.
  # checkov:skip=CKV_AWS_173:The environment holds a bucket name and four bounding-box coordinates. None of it is secret -- the bucket name is a Terraform output and the coordinates are in this repository. Lambda already encrypts environment variables at rest with an AWS-managed key; a customer-managed key would cost $1/month to protect values that are public by construction.
  depends_on = [aws_cloudwatch_log_group.ingest]
}

# ---------------------------------------------------------------------------
# Step Functions
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "state_machine" {
  name              = "/aws/vendedlogs/states/${local.state_machine_name}"
  retention_in_days = var.log_retention_days

  # checkov:skip=CKV_AWS_158:Same reasoning as the Lambda log group above -- AWS-managed encryption is enabled by default, and a customer-managed key would cost more per month than everything else in this project combined.
  # checkov:skip=CKV_AWS_338:Same reasoning as the Lambda log group above. Execution history for an hourly job has no value after two weeks, and a year of retention would dominate this project's entire bill.
}

resource "aws_sfn_state_machine" "ingest" {
  name     = local.state_machine_name
  role_arn = aws_iam_role.state_machine.arn
  type     = "STANDARD"

  definition = templatefile(
    "${path.module}/../../../orchestration/step_functions/ingest_state_machine.asl.json",
    { lambda_arn = aws_lambda_function.ingest.arn }
  )

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.state_machine.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }

  tracing_configuration {
    enabled = false
  }

  # checkov:skip=CKV_AWS_284:X-Ray tracing on a single-task state machine bills per trace to reveal what the execution history already shows. The task result carries bucket, key, state count and provenance, so a successful or failed execution is fully diagnosable from the console without it.
  # checkov:skip=CKV_AWS_285:Execution logging IS enabled above at ERROR level. ALL-level logging on an hourly schedule would multiply CloudWatch ingestion for executions that are almost always uneventful, and ERROR captures every case anyone would actually investigate.
}

# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${var.name_prefix}-ingest-schedule"
  description         = "Triggers bronze ingestion on a fixed schedule."
  schedule_expression = var.schedule_expression
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "state_machine" {
  rule     = aws_cloudwatch_event_rule.schedule.name
  arn      = aws_sfn_state_machine.ingest.arn
  role_arn = aws_iam_role.events.arn
}
