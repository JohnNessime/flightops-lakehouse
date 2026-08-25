# Every value that could identify a real AWS account comes from a variable with
# an obviously-fake default. Nothing here names a bucket, account or domain that
# exists.

variable "aws_region" {
  description = "AWS region for every resource in this project."
  type        = string
  default     = "eu-west-1"

  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]$", var.aws_region))
    error_message = "aws_region must look like a valid AWS region, e.g. eu-west-1."
  }
}

variable "project_name" {
  description = "Short name used as a prefix for every resource."
  type        = string
  default     = "flightops"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.project_name))
    error_message = "project_name must be lowercase alphanumeric with hyphens, 3-21 characters."
  }
}

variable "environment" {
  description = "Deployment environment, used in tags and resource names."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "bucket_name" {
  description = <<-EOT
    Globally unique S3 bucket name for the lake. The default is a placeholder
    and will not work: S3 bucket names are global, so this must be set to
    something you own before applying.
  EOT
  type        = string
  default     = "EXAMPLE-flightops-lakehouse-changeme"

  validation {
    condition     = length(var.bucket_name) >= 3 && length(var.bucket_name) <= 63
    error_message = "bucket_name must be between 3 and 63 characters."
  }
}

variable "github_repository" {
  description = <<-EOT
    The GitHub repository permitted to assume the deploy role, as owner/name.
    The OIDC trust policy is scoped to exactly this value -- no wildcard owner,
    no wildcard repository.
  EOT
  type        = string
  default     = "EXAMPLE-OWNER/EXAMPLE-REPO"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.github_repository))
    error_message = "github_repository must be in owner/name form."
  }
}

variable "github_ref" {
  description = <<-EOT
    Git ref permitted to assume the deploy role. Defaults to the main branch
    only: a role assumable from any branch is a role assumable from any pull
    request, which is a role anyone who can open a PR effectively holds.
  EOT
  type        = string
  default     = "refs/heads/main"
}

variable "create_oidc_provider" {
  description = <<-EOT
    Whether to create the GitHub OIDC provider. An AWS account can hold only
    one provider per URL, so set this to false if another stack in the same
    account already created it.
  EOT
  type        = bool
  default     = true
}

variable "bronze_expiration_days" {
  description = "Days before bronze objects expire. Raw snapshots are re-fetchable."
  type        = number
  default     = 30

  validation {
    condition     = var.bronze_expiration_days >= 1
    error_message = "bronze_expiration_days must be at least 1."
  }
}

variable "ia_transition_days" {
  description = <<-EOT
    Days before silver and gold objects move to Infrequent Access. AWS enforces
    a 30-day minimum before any transition out of Standard.
  EOT
  type        = number
  default     = 60

  validation {
    condition     = var.ia_transition_days >= 30
    error_message = "ia_transition_days must be at least 30: AWS rejects earlier transitions."
  }
}

variable "athena_bytes_scanned_cutoff" {
  description = <<-EOT
    Per-query bytes-scanned ceiling. A query exceeding this is cancelled rather
    than billed. 10 GiB caps a single runaway query at roughly $0.05.
    AWS requires at least 10 MB.
  EOT
  type        = number
  default     = 10737418240 # 10 GiB

  validation {
    condition     = var.athena_bytes_scanned_cutoff >= 10485760
    error_message = "athena_bytes_scanned_cutoff must be at least 10485760 (10 MB), the AWS minimum."
  }
}

variable "partition_projection_start_date" {
  description = "First date the Glue partition projection covers, as YYYY-MM-DD."
  type        = string
  default     = "2026-01-01"

  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", var.partition_projection_start_date))
    error_message = "partition_projection_start_date must be YYYY-MM-DD."
  }
}

variable "github_oidc_thumbprints" {
  description = <<-EOT
    Certificate thumbprints for token.actions.githubusercontent.com. These are
    public values published by GitHub, not secrets. AWS no longer verifies them
    for this provider but the API still requires the field.
  EOT
  type        = list(string)
  default = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

variable "lambda_package_dir" {
  description = <<-EOT
    Directory holding the built Lambda deployment package. Run
    `make lambda-package` before `terraform plan`: the package is a build
    artefact, so it is gitignored rather than committed.
  EOT
  type        = string
  default     = "../build/lambda_ingest"
}

variable "ingest_schedule_expression" {
  description = "EventBridge schedule for bronze ingestion."
  type        = string
  default     = "rate(1 hour)"
}

variable "ingest_schedule_enabled" {
  description = <<-EOT
    Whether the ingestion schedule fires. False by default so that applying
    this configuration does not silently start a recurring job in someone's
    account. Enabling it should be a deliberate act.
  EOT
  type        = bool
  default     = false
}

variable "ingest_bbox" {
  description = <<-EOT
    Optional geographic filter for the scheduled ingest, passed to the function
    as environment variables. Null queries the whole world, which is valid but
    heavy and consumes a much larger share of a shared public API's anonymous
    rate budget.
  EOT
  type = object({
    lamin = number
    lomin = number
    lamax = number
    lomax = number
  })
  default = null
}
