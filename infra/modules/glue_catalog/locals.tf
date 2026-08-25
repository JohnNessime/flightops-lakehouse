locals {
  # Mirrors SILVER_SCHEMA in src/flightops/normalise.py. If one changes, the
  # other must -- that coupling is deliberate, and it is the friction a crawler
  # exists to avoid. See ADR 0002 for why that friction is the point.
  silver_columns = [
    { name = "icao24", type = "string", comment = "24-bit ICAO transponder address, 6 lowercase hex digits. Identifies an airframe." },
    { name = "callsign", type = "string", comment = "Flight identifier, trimmed of OpenSky's right-padding. Null when absent." },
    { name = "origin_country", type = "string", comment = "Country inferred from the ICAO address range." },
    { name = "time_position", type = "timestamp", comment = "Last position report. Null when transmitting without a position fix." },
    { name = "last_contact", type = "timestamp", comment = "Last message of any kind. Always populated." },
    { name = "longitude", type = "double", comment = "WGS-84 degrees, -180 to 180." },
    { name = "latitude", type = "double", comment = "WGS-84 degrees, -90 to 90." },
    { name = "baro_altitude_m", type = "double", comment = "Barometric altitude in METRES, not feet." },
    { name = "on_ground", type = "boolean", comment = "Surface position report." },
    { name = "velocity_ms", type = "double", comment = "Ground speed in METRES PER SECOND, not knots." },
    { name = "true_track_deg", type = "double", comment = "Degrees clockwise from true north, 0 to 360." },
    { name = "vertical_rate_ms", type = "double", comment = "Metres per second; positive is climbing." },
    { name = "geo_altitude_m", type = "double", comment = "GPS-derived altitude in METRES." },
    { name = "squawk", type = "string", comment = "4-digit octal transponder code. STRING because leading zeros are significant." },
    { name = "spi", type = "boolean", comment = "Special Position Identification flag." },
    { name = "position_source", type = "tinyint", comment = "0 ADS-B, 1 ASTERIX, 2 MLAT, 3 FLARM." },
    { name = "position_source_label", type = "string", comment = "Decoded position_source." },
    { name = "ingest_source", type = "string", comment = "Provenance: opensky-live or fixture-replay. Never fabricated." },
    { name = "observed_at", type = "timestamp", comment = "Snapshot window this row came from." },
  ]

  # Partition projection rather than a registered partition list.
  #
  # Without it, every new hour needs either MSCK REPAIR TABLE (which scans the
  # whole prefix and is billed) or an explicit ALTER TABLE ADD PARTITION from
  # something that has to remember to run. Projection makes Athena compute
  # partition locations from the query predicate instead: no scan, no state to
  # maintain, no cost.
  partition_projection = {
    "projection.enabled" = "true"

    "projection.dt.type"          = "date"
    "projection.dt.format"        = "yyyy-MM-dd"
    "projection.dt.range"         = "${var.projection_start_date},NOW"
    "projection.dt.interval"      = "1"
    "projection.dt.interval.unit" = "DAYS"

    # digits = 2 matches the zero-padded hour the writer produces. A mismatch
    # here yields a table that returns nothing, with no error to explain it.
    "projection.hour.type"   = "integer"
    "projection.hour.range"  = "0,23"
    "projection.hour.digits" = "2"

    "storage.location.template" = "${var.silver_uri}/dt=$${dt}/hour=$${hour}"
  }
}
