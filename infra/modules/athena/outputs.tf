output "workgroup_name" {
  description = "Name of the cost-capped Athena workgroup."
  value       = aws_athena_workgroup.this.name
}

output "workgroup_arn" {
  description = "ARN of the Athena workgroup."
  value       = aws_athena_workgroup.this.arn
}

output "bytes_scanned_cutoff" {
  description = "Per-query bytes-scanned ceiling actually applied."
  value       = var.bytes_scanned_cutoff
}
