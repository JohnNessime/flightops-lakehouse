output "bucket_name" {
  description = "Lake bucket name."
  value       = module.lake_storage.bucket_id
}

output "bronze_uri" {
  description = "Where ingestion writes raw snapshots."
  value       = module.lake_storage.bronze_uri
}

output "silver_uri" {
  description = "Where normalisation writes typed Parquet."
  value       = module.lake_storage.silver_uri
}

output "gold_uri" {
  description = "Where dbt materialises the marts."
  value       = module.lake_storage.gold_uri
}

output "glue_database_name" {
  description = "Set this as FLIGHTOPS_GLUE_DATABASE for the dbt athena target."
  value       = module.glue_catalog.database_name
}

output "athena_workgroup_name" {
  description = "Set this as FLIGHTOPS_ATHENA_WORKGROUP for the dbt athena target."
  value       = module.athena.workgroup_name
}

output "athena_results_uri" {
  description = "Set this as FLIGHTOPS_ATHENA_STAGING_DIR for the dbt athena target."
  value       = module.lake_storage.athena_results_uri
}

output "athena_bytes_scanned_cutoff" {
  description = "Per-query byte ceiling actually in force."
  value       = module.athena.bytes_scanned_cutoff
}

output "deploy_role_arn" {
  description = "Configure this as role-to-assume in the GitHub Actions workflow."
  value       = module.oidc_role.role_arn
}

output "deploy_role_trusted_subject" {
  description = "The single OIDC subject permitted to assume the deploy role."
  value       = module.oidc_role.trusted_subject
}

output "ingest_function_name" {
  description = "Name of the scheduled ingestion Lambda."
  value       = module.orchestration.function_name
}

output "ingest_state_machine_arn" {
  description = "ARN of the ingestion state machine."
  value       = module.orchestration.state_machine_arn
}

output "ingest_schedule_enabled" {
  description = "Whether the ingestion schedule is actually firing."
  value       = module.orchestration.schedule_enabled
}
