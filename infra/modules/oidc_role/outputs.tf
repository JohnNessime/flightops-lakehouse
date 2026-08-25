output "role_arn" {
  description = "ARN to configure as the GitHub Actions role-to-assume."
  value       = aws_iam_role.deploy.arn
}

output "role_name" {
  description = "Name of the deploy role."
  value       = aws_iam_role.deploy.name
}

output "trusted_subject" {
  description = "The exact OIDC subject claim permitted to assume the role."
  value       = local.subject_claim
}

output "oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider in use."
  value       = local.oidc_provider_arn
}
