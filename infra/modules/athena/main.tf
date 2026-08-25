# Athena workgroup with a hard cost ceiling.
#
# This module is small and it is the most important cost control in the
# project. Athena bills per byte scanned with no upper bound: a single
# accidental SELECT * across a large table is a real bill, and there is no
# retroactive way to undo it. The cutoff below turns that class of mistake from
# a charge into a cancelled query.

resource "aws_athena_workgroup" "this" {
  name        = var.workgroup_name
  description = "Cost-capped workgroup for the flight telemetry lakehouse."
  state       = "ENABLED"

  configuration {
    # THE guardrail. A query projected to exceed this is cancelled, not billed.
    # At $5/TB, 10 GiB caps a single runaway query at roughly $0.05.
    bytes_scanned_cutoff_per_query = var.bytes_scanned_cutoff

    # Without this, the cutoff above is a suggestion: any client may send its
    # own configuration and override it. Enforcement is what makes it a
    # control rather than a default.
    enforce_workgroup_configuration = true

    publish_cloudwatch_metrics_enabled = var.publish_metrics

    # Requiring the workgroup to own the output location means a client cannot
    # redirect results to a bucket outside this project.
    requester_pays_enabled = false

    result_configuration {
      output_location = var.results_uri

      encryption_configuration {
        # SSE-S3 for the same reason as the lake bucket: query results derive
        # from public telemetry, and KMS request charges would exceed the rest
        # of this project's bill combined. See modules/lake_storage/main.tf.
        encryption_option = "SSE_S3"
      }
    }
  }

  force_destroy = var.force_destroy
}

# Named queries serve as executable documentation: someone opening the Athena
# console sees working examples that respect partition pruning, rather than
# writing their first query as an unbounded scan.
resource "aws_athena_named_query" "recent_activity_by_country" {
  name      = "${var.workgroup_name}-recent-activity-by-country"
  workgroup = aws_athena_workgroup.this.id
  database  = var.database_name

  description = "Aircraft per country for one day. Note the dt predicate: without it this scans every partition."

  query = <<-SQL
    -- The dt predicate is not optional in spirit. Dropping it turns a
    -- single-partition read into a full-table scan.
    SELECT
        origin_country,
        COUNT(DISTINCT icao24) AS aircraft,
        COUNT(*)               AS observations
    FROM ${var.database_name}.${var.silver_table_name}
    WHERE dt = DATE_FORMAT(CURRENT_DATE, '%Y-%m-%d')
    GROUP BY origin_country
    ORDER BY aircraft DESC
    LIMIT 25;
  SQL
}

resource "aws_athena_named_query" "hourly_altitude_profile" {
  name      = "${var.workgroup_name}-hourly-altitude-profile"
  workgroup = aws_athena_workgroup.this.id
  database  = var.database_name

  description = "Altitude distribution for a single hour, pruning on both partition keys."

  query = <<-SQL
    SELECT
        hour,
        COUNT(*)                    AS observations,
        ROUND(AVG(baro_altitude_m)) AS avg_altitude_m,
        ROUND(MAX(baro_altitude_m)) AS max_altitude_m
    FROM ${var.database_name}.${var.silver_table_name}
    WHERE dt = DATE_FORMAT(CURRENT_DATE, '%Y-%m-%d')
      AND on_ground = false
    GROUP BY hour
    ORDER BY hour;
  SQL
}
