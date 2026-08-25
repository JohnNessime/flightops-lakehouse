output "database_name" {
  description = "Glue database holding the silver table and the dbt-managed marts."
  value       = aws_glue_catalog_database.lake.name
}

output "database_arn" {
  description = "ARN of the Glue database."
  value       = aws_glue_catalog_database.lake.arn
}

output "silver_table_name" {
  description = "Name of the declarative silver table."
  value       = aws_glue_catalog_table.silver_states.name
}

output "silver_table_arn" {
  description = "ARN of the declarative silver table."
  value       = aws_glue_catalog_table.silver_states.arn
}
