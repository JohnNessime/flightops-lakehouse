variable "bucket_name" {
  description = "Globally unique S3 bucket name for the lake."
  type        = string
}

variable "bronze_prefix" {
  description = "Prefix holding raw JSON snapshots."
  type        = string
  default     = "bronze"
}

variable "silver_prefix" {
  description = "Prefix holding typed, deduplicated Parquet."
  type        = string
  default     = "silver"
}

variable "gold_prefix" {
  description = "Prefix holding aggregate marts."
  type        = string
  default     = "gold"
}

variable "athena_results_prefix" {
  description = "Prefix Athena writes query results to."
  type        = string
  default     = "athena-results"
}

variable "bronze_expiration_days" {
  description = "Days before bronze objects expire."
  type        = number
  default     = 30
}

variable "ia_transition_days" {
  description = "Days before silver and gold move to Infrequent Access."
  type        = number
  default     = 60
}
