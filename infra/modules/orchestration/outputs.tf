output "function_name" {
  description = "Name of the ingestion Lambda."
  value       = aws_lambda_function.ingest.function_name
}

output "function_arn" {
  description = "ARN of the ingestion Lambda."
  value       = aws_lambda_function.ingest.arn
}

output "state_machine_arn" {
  description = "ARN of the ingestion state machine."
  value       = aws_sfn_state_machine.ingest.arn
}

output "schedule_expression" {
  description = "The schedule actually configured."
  value       = aws_cloudwatch_event_rule.schedule.schedule_expression
}

output "schedule_enabled" {
  description = "Whether the schedule is firing. False unless deliberately enabled."
  value       = var.schedule_enabled
}
