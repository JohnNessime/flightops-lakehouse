# S3 storage for the whole lake: one bucket, three prefixes.
#
# One bucket rather than three. Bucket-per-layer is common and buys nothing
# here -- the access patterns are identical, the lifecycle rules are per-prefix
# anyway, and three buckets means three sets of public-access blocks to get
# right instead of one.

resource "aws_s3_bucket" "lake" {
  bucket = var.bucket_name

  # checkov:skip=CKV_AWS_145:SSE-S3 rather than SSE-KMS is a deliberate cost decision for public flight telemetry. KMS costs $1/month per key plus $0.03 per 10k requests, which would exceed every other line in this project's bill combined, and buys no confidentiality property that matters for data published by an open API. Encryption at rest is still enforced unconditionally. This is the first thing that should change if the bucket ever holds data with a real confidentiality requirement.
  # checkov:skip=CKV_AWS_144:Cross-region replication doubles storage cost to protect data that is re-fetchable from a public API. Not justified for a portfolio project; see docs/COSTS.md.
  # checkov:skip=CKV_AWS_18:Server access logging needs a second bucket, which doubles the bucket count and adds per-request log-delivery charges. CloudTrail data events cover audit needs if they are ever required.
  # checkov:skip=CKV2_AWS_62:Event notifications have no consumer in this architecture; ingestion is scheduled, not event-driven.
  # checkov:skip=CKV2_AWS_61:Lifecycle configuration is defined in aws_s3_bucket_lifecycle_configuration below; checkov does not always associate the separate resource.
}

# Versioning protects against an ingestion bug overwriting a good snapshot with
# a bad one. Combined with the lifecycle rules below, old versions do not
# accumulate cost indefinitely.
resource "aws_s3_bucket_versioning" "lake" {
  bucket = aws_s3_bucket.lake.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 rather than SSE-KMS. This is a deliberate cost decision:
#
#   SSE-S3  encryption at rest, no per-request charge, no key to manage
#   SSE-KMS encryption at rest, $1/month per key plus $0.03 per 10k requests
#
# The data is public flight telemetry from an open API. There is no
# confidentiality requirement that KMS would satisfy and SSE-S3 would not, and
# KMS request charges would exceed every other line in this project's bill
# combined. Encryption at rest is still on, unconditionally.
#
# If this ever held data with a real confidentiality requirement, this is the
# first thing that should change.
resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# All four settings, explicitly. Relying on the account-level default means
# relying on a setting this code cannot see.
resource "aws_s3_bucket_public_access_block" "lake" {
  bucket = aws_s3_bucket.lake.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Deny any request that is not over TLS. Encryption at rest without encryption
# in transit is half a control.
resource "aws_s3_bucket_policy" "enforce_tls" {
  bucket = aws_s3_bucket.lake.id
  policy = data.aws_iam_policy_document.enforce_tls.json

  # The policy must not be applied before public access is blocked, or there is
  # a window where a policy change could expose the bucket.
  depends_on = [aws_s3_bucket_public_access_block.lake]
}

data "aws_iam_policy_document" "enforce_tls" {
  statement {
    sid    = "DenyUnencryptedTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.lake.arn,
      "${aws_s3_bucket.lake.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  # Bronze is a landing zone, not an archive. Raw snapshots can be re-fetched
  # from the API, so paying to keep them indefinitely buys nothing.
  rule {
    id     = "expire-bronze"
    status = "Enabled"

    filter {
      prefix = "${var.bronze_prefix}/"
    }

    expiration {
      days = var.bronze_expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }
  }

  # Silver and gold are derived but expensive to rebuild, so they are kept and
  # moved to cheaper storage instead of expired.
  dynamic "rule" {
    for_each = toset([var.silver_prefix, var.gold_prefix])

    content {
      id     = "transition-${rule.value}-to-ia"
      status = "Enabled"

      filter {
        prefix = "${rule.value}/"
      }

      transition {
        days          = var.ia_transition_days
        storage_class = "STANDARD_IA"
      }

      noncurrent_version_expiration {
        noncurrent_days = 30
      }
    }
  }

  # A multipart upload that fails partway leaves parts that are billed as
  # storage but appear nowhere in the console. This is the cheapest bug-fix in
  # AWS and almost nobody enables it.
  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.lake]
}
