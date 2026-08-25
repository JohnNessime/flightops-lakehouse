-- Guards against an epoch-parsing regression: a millisecond timestamp read as
-- seconds lands in 1970, and a seconds timestamp read as milliseconds lands
-- tens of thousands of years out. Either would sail through a null check.

select *
from {{ ref('stg_states') }}
where observation_date < date '2020-01-01'
   or observation_date > current_date + interval '1' day
