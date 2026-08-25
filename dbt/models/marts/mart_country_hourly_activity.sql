{{ config(materialized='table') }}

-- Flights per origin country per hour: the headline mart.
--
-- Counts distinct aircraft rather than rows. A row is one observation, and the
-- same aircraft appears in every snapshot it is airborne for, so counting rows
-- would measure how often we polled rather than how much was flying.

with enriched as (

    select * from {{ ref('int_states_enriched') }}

)

select
    cast(observation_date as varchar) || '|'
        || cast(observation_hour as varchar) || '|'
        || origin_country                                           as activity_key,

    observation_date,
    observation_hour,
    origin_country,

    count(distinct icao24)                                          as aircraft_count,
    count(*)                                                        as observation_count,
    count(distinct carrier_code)                                    as carrier_count,

    sum(case when on_ground then 1 else 0 end)                      as on_ground_observations,
    sum(case when has_position then 1 else 0 end)                   as positioned_observations,

    avg(baro_altitude_m)                                            as avg_baro_altitude_m,
    max(baro_altitude_m)                                            as max_baro_altitude_m,
    avg(velocity_ms)                                                as avg_velocity_ms,
    max(velocity_ms)                                                as max_velocity_ms

from enriched
group by
    observation_date,
    observation_hour,
    origin_country
