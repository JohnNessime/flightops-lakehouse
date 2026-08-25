-- A ratio outside [0, 1] means the numerator and denominator disagree about
-- what they are counting.

select *
from {{ ref('mart_ground_activity_ratio') }}
where on_ground_ratio < 0
   or on_ground_ratio > 1
   or on_ground_count + airborne_count <> observation_count
