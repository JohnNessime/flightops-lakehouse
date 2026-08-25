output "bucket_id" {
  description = "Name of the lake bucket."
  value       = aws_s3_bucket.lake.id
}

output "bucket_arn" {
  description = "ARN of the lake bucket."
  value       = aws_s3_bucket.lake.arn
}

output "bronze_uri" {
  description = "s3:// URI of the bronze prefix."
  value       = "s3://${aws_s3_bucket.lake.id}/${var.bronze_prefix}"
}

output "silver_uri" {
  description = "s3:// URI of the silver prefix."
  value       = "s3://${aws_s3_bucket.lake.id}/${var.silver_prefix}"
}

output "gold_uri" {
  description = "s3:// URI of the gold prefix."
  value       = "s3://${aws_s3_bucket.lake.id}/${var.gold_prefix}"
}

output "athena_results_uri" {
  description = "s3:// URI Athena writes results to."
  value       = "s3://${aws_s3_bucket.lake.id}/${var.athena_results_prefix}/"
}
