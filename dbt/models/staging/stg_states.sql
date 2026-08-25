{{
    config(
        materialized='view'
    )
}}

-- Staging does exactly three things: name, type and lightly clean. No business
-- logic, no aggregation, no filtering that loses a row a consumer might want.
--
-- The type casts on dt and hour are the one place the two adapters genuinely
-- disagree: DuckDB infers a DATE from the Hive path while Athena presents every
-- partition key as a string. Casting both explicitly here means no model
-- downstream ever has to care which engine it is running on.

with source as (

    select * from {{ source('flightops_silver', 'states') }}

),

renamed as (

    select
        icao24,
        nullif(trim(callsign), '')          as callsign,
        origin_country,

        time_position,
        last_contact,
        observed_at,

        longitude,
        latitude,
        baro_altitude_m,
        geo_altitude_m,
        velocity_ms,
        true_track_deg,
        vertical_rate_ms,

        on_ground,
        spi,
        squawk,
        position_source,
        position_source_label,
        ingest_source,

        cast(dt as date)                    as observation_date,
        cast(hour as integer)               as observation_hour

    from source

)

select * from renamed
