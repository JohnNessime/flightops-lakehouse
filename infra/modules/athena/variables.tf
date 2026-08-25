variable "workgroup_name" {
  description = "Athena workgroup name."
  type        = string
  default     = "flightops"
}

variable "results_uri" {
  description = "s3:// URI Athena writes query results to. Must end with a slash."
  type        = string
}

variable "database_name" {
  description = "Glue database the named queries target."
  type        = string
}

variable "silver_table_name" {
  description = "Silver table the named queries read."
  type        = string
  default     = "states"
}

variable "bytes_scanned_cutoff" {
  description = "Per-query bytes-scanned ceiling. Queries exceeding this are cancelled."
  type        = number
  default     = 10737418240
}

variable "publish_metrics" {
  description = <<-EOT
    Publish per-query CloudWatch metrics. On by default: bytes scanned is the
    only thing Athena bills for, so not measuring it is choosing not to know.
  EOT
  type        = bool
  default     = true
}

variable "force_destroy" {
  description = "Allow destroying the workgroup even if it holds named queries."
  type        = bool
  default     = true
}
