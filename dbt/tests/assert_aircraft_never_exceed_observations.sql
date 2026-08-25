-- Distinct aircraft cannot outnumber the observations they were counted from.
-- If this fires, a COUNT(DISTINCT) has been applied across the wrong grain.

select *
from {{ ref('mart_country_hourly_activity') }}
where aircraft_count > observation_count
