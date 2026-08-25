{{
    config(
        materialized='view'
    )
}}

-- Derived attributes that more than one mart needs. Computing them once here
-- rather than repeating the CASE expressions in four marts is the difference
-- between changing a band boundary in one place and finding all four.
--
-- Every expression below is deliberately ANSI-portable. The BETWEEN checks on
-- single characters replace a regex, because DuckDB spells that regexp_matches
-- and Athena spells it regexp_like -- and a project whose whole premise is one
-- codebase on two engines should not need a conditional for something this
-- small.

with states as (

    select * from {{ ref('stg_states') }}

),

enriched as (

    select
        *,

        -- Altitude bands. Boundaries are the ones air traffic control actually
        -- thinks in: circuit, terminal manoeuvring, and the flight levels.
        case
            when on_ground then 'on_ground'
            when baro_altitude_m is null then 'unknown'
            when baro_altitude_m < 1000 then 'below_1km'
            when baro_altitude_m < 3000 then '1km_to_3km'
            when baro_altitude_m < 6000 then '3km_to_6km'
            when baro_altitude_m < 9000 then '6km_to_9km'
            when baro_altitude_m < 12000 then '9km_to_12km'
            else 'above_12km'
        end as altitude_band,

        -- Vertical phase. The +/- 1 m/s deadband keeps ordinary altitude-hold
        -- jitter from being reported as a climb.
        case
            when on_ground then 'on_ground'
            when vertical_rate_ms is null then 'unknown'
            when vertical_rate_ms > 1 then 'climbing'
            when vertical_rate_ms < -1 then 'descending'
            else 'level'
        end as vertical_phase,

        -- ICAO airline designator: the first three characters of the callsign,
        -- but only when all three are letters. Numeric prefixes belong to
        -- general aviation using registration-style callsigns, and folding
        -- those into a carrier rollup would invent operators that do not exist.
        case
            when callsign is null then null
            when length(callsign) < {{ var('carrier_prefix_length') }} then null
            when
                upper(substr(callsign, 1, 1)) between 'A' and 'Z'
                and upper(substr(callsign, 2, 1)) between 'A' and 'Z'
                and upper(substr(callsign, 3, 1)) between 'A' and 'Z'
                then upper(substr(callsign, 1, {{ var('carrier_prefix_length') }}))
            else null
        end as carrier_code,

        case
            when latitude is not null and longitude is not null then true
            else false
        end as has_position

    from states

)

select * from enriched
