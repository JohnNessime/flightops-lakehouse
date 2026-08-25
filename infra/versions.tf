terraform {
  # Pinned deliberately. An unpinned provider means the plan you reviewed and
  # the plan that applies next month are different plans.
  required_version = "~> 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # Every resource carries these, so a stray bucket is traceable to this
  # project rather than becoming an unexplained line on a bill.
  default_tags {
    tags = {
      Project     = var.project_name
      ManagedBy   = "terraform"
      Repository  = var.github_repository
      Environment = var.environment
    }
  }
}
