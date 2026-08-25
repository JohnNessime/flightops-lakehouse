# Glue Data Catalog: one database, and the silver table declared explicitly.
#
# No crawlers. See docs/adr/0002-declarative-glue-tables-over-crawlers.md --
# a crawler on an hourly schedule would cost ~$13/month to rediscover a schema
# that is already declared in normalise.py, and it would *infer* types rather
# than take the ones we know, which is how a squawk of "0021" becomes 21.
#
# Ownership boundary, stated explicitly because it is the one thing about this
# module that is not obvious:
#
#   Terraform owns  the database and the SILVER table. Silver is produced by
#                   the Python layer, which has no way to register a catalog
#                   entry, so the entry must be declared here.
#   dbt owns        the GOLD tables. dbt-athena creates and replaces them as
#                   part of materialising each mart.
#
# Declaring the gold marts here as well would put two systems in charge of the
# same resources: every `dbt build` would drop and recreate tables Terraform
# believes it manages, and every `terraform apply` would report drift it did
# not cause. One owner per resource is worth more than literal symmetry
# between the prefixes.

resource "aws_glue_catalog_database" "lake" {
  name        = var.database_name
  description = "Flight telemetry lakehouse: silver declared here, gold managed by dbt."

  location_uri = var.silver_uri
}

# The silver table. Column types mirror SILVER_SCHEMA in normalise.py exactly;
# the data dictionary in docs/DATA_DICTIONARY.md documents every one.
resource "aws_glue_catalog_table" "silver_states" {
  name          = var.silver_table_name
  database_name = aws_glue_catalog_database.lake.name
  table_type    = "EXTERNAL_TABLE"

  parameters = merge(
    {
      EXTERNAL              = "TRUE"
      classification        = "parquet"
      "parquet.compression" = "SNAPPY"
    },
    local.partition_projection
  )

  storage_descriptor {
    location      = var.silver_uri
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "parquet"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"

      parameters = {
        "serialization.format" = "1"
      }
    }

    dynamic "columns" {
      for_each = local.silver_columns

      content {
        name    = columns.value.name
        type    = columns.value.type
        comment = columns.value.comment
      }
    }
  }

  # Partition keys are NOT stored inside the Parquet files -- the value lives in
  # the path, which is the standard Hive contract and avoids repeating the same
  # string on every row.
  partition_keys {
    name    = "dt"
    type    = "string"
    comment = "Observation date, YYYY-MM-DD, from the OpenSky observation time."
  }

  partition_keys {
    name    = "hour"
    type    = "string"
    comment = "Observation hour, zero-padded HH, UTC."
  }
}
