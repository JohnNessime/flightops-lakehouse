variable "role_name" {
  description = "Name of the deploy role."
  type        = string
  default     = "flightops-github-deploy"
}

variable "github_repository" {
  description = "Repository permitted to assume the role, as owner/name. Never a wildcard."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.github_repository))
    error_message = "github_repository must be exactly owner/name, with no wildcard."
  }

  validation {
    condition     = !strcontains(var.github_repository, "*")
    error_message = "github_repository must not contain a wildcard: that grants the role to every matching repo."
  }
}

variable "github_ref" {
  description = "Git ref permitted to assume the role, e.g. refs/heads/main."
  type        = string
  default     = "refs/heads/main"

  validation {
    condition     = !strcontains(var.github_ref, "*")
    error_message = "github_ref must not contain a wildcard."
  }
}

variable "create_oidc_provider" {
  description = "Create the GitHub OIDC provider. False if the account already has one."
  type        = bool
  default     = true
}

variable "thumbprints" {
  description = "Public certificate thumbprints for the GitHub OIDC endpoint."
  type        = list(string)
  default = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

variable "bucket_arn" {
  description = "ARN of the lake bucket."
  type        = string
}

variable "writable_prefixes" {
  description = "Prefixes within the bucket this role may read and write."
  type        = list(string)
  default     = ["bronze", "silver", "gold", "athena-results"]
}

variable "glue_database_name" {
  description = "Glue database the role may operate on. Scoped to this one database."
  type        = string
}

variable "athena_workgroup_arn" {
  description = "ARN of the cost-capped Athena workgroup. The role can use no other."
  type        = string
}

variable "max_session_duration" {
  description = "Maximum assumed-session duration in seconds. One hour is ample for CI."
  type        = number
  default     = 3600

  validation {
    condition     = var.max_session_duration >= 900 && var.max_session_duration <= 43200
    error_message = "max_session_duration must be between 900 and 43200 seconds."
  }
}

variable "permissions_boundary_arn" {
  description = <<-EOT
    Optional permissions boundary ARN. Caps what this role can ever do even if
    its policy is later widened by mistake. Null means no boundary.
  EOT
  type        = string
  default     = null
}
