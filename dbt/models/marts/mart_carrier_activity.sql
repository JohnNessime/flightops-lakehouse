{{ config(materialized='table') }}

-- Callsign-prefix rollup to operator, joined to the seeded ICAO designator
-- reference.
--
-- The join is a LEFT join on purpose. The seed covers the operators that
-- actually appear over the sampled region, not the full ICAO register, and
-- dropping unrecognised carriers would quietly understate traffic. Unknown
-- designators surface as such instead.

with enriched as (

    select * from {{ ref('int_states_enriched') }}
    where carrier_code is not null

),

carriers as (

    select * from {{ ref('carrier_codes') }}

),

aggregated as (

    select
        e.observation_date,
        e.observation_hour,
        e.carrier_code,

        count(distinct e.icao24)                            as aircraft_count,
        count(*)                                            as observation_count,
        count(distinct e.callsign)                          as callsign_count,
        count(distinct e.origin_country)                    as country_count,

        avg(e.baro_altitude_m)                              as avg_baro_altitude_m,
        avg(e.velocity_ms)                                  as avg_velocity_ms,
        sum(case when e.on_ground then 1 else 0 end)        as on_ground_observations

    from enriched as e
    group by
        e.observation_date,
        e.observation_hour,
        e.carrier_code

)

select
    cast(a.observation_date as varchar) || '|'
        || cast(a.observation_hour as varchar) || '|'
        || a.carrier_code                                           as carrier_key,

    a.observation_date,
    a.observation_hour,
    a.carrier_code,
    coalesce(c.carrier_name, 'Unknown operator')    as carrier_name,
    coalesce(c.carrier_country, 'Unknown')          as carrier_country,
    a.aircraft_count,
    a.observation_count,
    a.callsign_count,
    a.country_count,
    a.avg_baro_altitude_m,
    a.avg_velocity_ms,
    a.on_ground_observations

from aggregated as a
left join carriers as c
    on a.carrier_code = c.carrier_code
