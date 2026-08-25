{{ config(materialized='table') }}

-- How aircraft distribute across altitude bands, by hour.
--
-- The share column is computed against the hour's own total rather than the
-- grand total, so each hour's shares sum to 1 and hours remain comparable even
-- when traffic volume differs between them.

with enriched as (

    select * from {{ ref('int_states_enriched') }}

),

by_band as (

    select
        observation_date,
        observation_hour,
        altitude_band,

        count(distinct icao24)      as aircraft_count,
        count(*)                    as observation_count,
        avg(baro_altitude_m)        as avg_baro_altitude_m,
        avg(velocity_ms)            as avg_velocity_ms

    from enriched
    group by
        observation_date,
        observation_hour,
        altitude_band

),

hourly_total as (

    select
        observation_date,
        observation_hour,
        sum(observation_count) as total_observations
    from by_band
    group by observation_date, observation_hour

)

select
    cast(b.observation_date as varchar) || '|'
        || cast(b.observation_hour as varchar) || '|'
        || b.altitude_band                                          as band_key,

    b.observation_date,
    b.observation_hour,
    b.altitude_band,
    b.aircraft_count,
    b.observation_count,
    b.avg_baro_altitude_m,
    b.avg_velocity_ms,
    cast(b.observation_count as double) / cast(t.total_observations as double)
        as share_of_hour

from by_band as b
inner join hourly_total as t
    on b.observation_date = t.observation_date
    and b.observation_hour = t.observation_hour
