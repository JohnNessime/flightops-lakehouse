{{ config(materialized='table') }}

-- Airborne versus on-ground split per country per hour.
--
-- A high ground ratio in a bounded region usually means the box contains a
-- busy airport; it is the cheapest available proxy for airport activity
-- without an airports reference dataset.

with enriched as (

    select * from {{ ref('int_states_enriched') }}

),

aggregated as (

    select
        observation_date,
        observation_hour,
        origin_country,

        count(*)                                            as observation_count,
        sum(case when on_ground then 1 else 0 end)          as on_ground_count,
        sum(case when on_ground then 0 else 1 end)          as airborne_count,

        count(distinct case when on_ground then icao24 end) as on_ground_aircraft,
        count(distinct case when on_ground then null else icao24 end)
            as airborne_aircraft

    from enriched
    group by
        observation_date,
        observation_hour,
        origin_country

)

select
    cast(observation_date as varchar) || '|'
        || cast(observation_hour as varchar) || '|'
        || origin_country                                           as ratio_key,
    *,
    -- observation_count cannot be zero here: a group only exists because rows
    -- fell into it, so this division needs no guard.
    cast(on_ground_count as double) / cast(observation_count as double)
        as on_ground_ratio
from aggregated
