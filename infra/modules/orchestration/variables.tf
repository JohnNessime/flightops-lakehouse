variable "name_prefix" {
  description = "Prefix for every resource this module creates."
  type        = string
}

variable "package_dir" {
  description = <<-EOT
    Directory holding the built Lambda deployment package. Populated by
    `make lambda-package`, which vendors the flightops package and its runtime
    dependencies. Terraform zips it rather than shelling out to a build tool,
    so there is no local-exec anywhere in this configuration.
  EOT
  type        = string
}

variable "bucket_id" {
  description = "Name of the lake bucket the function writes to."
  type        = string
}

variable "bucket_arn" {
  description = "ARN of the lake bucket."
  type        = string
}

variable "bronze_prefix" {
  description = "The only prefix this function may write to."
  type        = string
  default     = "bronze"
}

variable "schedule_expression" {
  description = <<-EOT
    EventBridge schedule. Hourly by default: OpenSky's anonymous feed is rate
    limited and a handful of snapshots per day is ample to demonstrate
    partitioning. More frequent polling would cost nothing in AWS terms and
    would be discourteous to a free public service.
  EOT
  type        = string
  default     = "rate(1 hour)"
}

variable "schedule_enabled" {
  description = <<-EOT
    Whether the schedule fires. Defaults to false so that applying this module
    does not silently start a recurring job in someone's account -- enabling it
    should be a deliberate act.
  EOT
  type        = bool
  default     = false
}

variable "python_runtime" {
  description = "Lambda Python runtime."
  type        = string
  default     = "python3.12"
}

variable "memory_mb" {
  description = "Lambda memory. Memory is the billing dimension, so the floor is correct here."
  type        = number
  default     = 128

  validation {
    condition     = var.memory_mb >= 128 && var.memory_mb <= 1024
    error_message = "memory_mb must be between 128 and 1024; this function needs no more."
  }
}

variable "timeout_seconds" {
  description = "Lambda timeout. Generous enough to absorb the in-function backoff."
  type        = number
  default     = 90
}

variable "log_retention_days" {
  description = <<-EOT
    CloudWatch retention. Logs default to never expiring, which is a slow,
    silent cost leak; 14 days is well past the point anyone investigates an
    hourly job.
  EOT
  type        = number
  default     = 14
}

variable "bbox" {
  description = "Optional bounding box passed to the function as environment variables."
  type = object({
    lamin = number
    lomin = number
    lamax = number
    lomax = number
  })
  default = null
}
