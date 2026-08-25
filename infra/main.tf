# Root module. Wires the four modules together and nothing else -- every
# resource lives in a module so that each concern can be read, reviewed and
# reasoned about on its own.
#
# Dependency order is expressed through variables rather than depends_on:
# storage produces URIs, the catalog consumes them, Athena consumes both, and
# the deploy role is scoped to the ARNs the others produced. Terraform derives
# the graph from that, and it stays correct when the modules change.

locals {
  name_prefix = "${var.project_name}-${var.environment}"
}

module "lake_storage" {
  source = "./modules/lake_storage"

  bucket_name            = var.bucket_name
  bronze_expiration_days = var.bronze_expiration_days
  ia_transition_days     = var.ia_transition_days
}

module "glue_catalog" {
  source = "./modules/glue_catalog"

  database_name = var.project_name
  # Trailing slash trimmed: Glue treats "s3://b/silver" and "s3://b/silver/"
  # as different locations, and the projection template appends its own.
  silver_uri            = trimsuffix(module.lake_storage.silver_uri, "/")
  projection_start_date = var.partition_projection_start_date
}

module "athena" {
  source = "./modules/athena"

  workgroup_name       = local.name_prefix
  results_uri          = module.lake_storage.athena_results_uri
  database_name        = module.glue_catalog.database_name
  silver_table_name    = module.glue_catalog.silver_table_name
  bytes_scanned_cutoff = var.athena_bytes_scanned_cutoff
}

module "oidc_role" {
  source = "./modules/oidc_role"

  role_name            = "${local.name_prefix}-github-deploy"
  github_repository    = var.github_repository
  github_ref           = var.github_ref
  create_oidc_provider = var.create_oidc_provider
  thumbprints          = var.github_oidc_thumbprints

  bucket_arn           = module.lake_storage.bucket_arn
  glue_database_name   = module.glue_catalog.database_name
  athena_workgroup_arn = module.athena.workgroup_arn
}

module "orchestration" {
  source = "./modules/orchestration"

  name_prefix = local.name_prefix
  package_dir = var.lambda_package_dir

  bucket_id     = module.lake_storage.bucket_id
  bucket_arn    = module.lake_storage.bucket_arn
  bronze_prefix = "bronze"

  schedule_expression = var.ingest_schedule_expression
  schedule_enabled    = var.ingest_schedule_enabled
  bbox                = var.ingest_bbox
}
