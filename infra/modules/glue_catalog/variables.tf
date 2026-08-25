variable "database_name" {
  description = "Glue Data Catalog database name."
  type        = string
  default     = "flightops"
}

variable "silver_table_name" {
  description = "Name of the declaratively-defined silver table."
  type        = string
  default     = "states"
}

variable "silver_uri" {
  description = "s3:// URI of the silver prefix, without a trailing slash."
  type        = string
}

variable "projection_start_date" {
  description = "First date the partition projection covers, YYYY-MM-DD."
  type        = string
  default     = "2026-01-01"
}
