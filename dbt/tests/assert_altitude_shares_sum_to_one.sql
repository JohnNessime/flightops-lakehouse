-- Each hour's altitude-band shares must sum to 1.
--
-- This is the test that catches the mistake the mart is most likely to make:
-- dividing by a grand total instead of the hour's own total. That error is
-- invisible in a single-hour dataset and silently wrong the moment a second
-- hour arrives -- which is exactly why the fixtures deliberately span two.

select
    observation_date,
    observation_hour,
    sum(share_of_hour) as total_share

from {{ ref('mart_altitude_band_distribution') }}
group by observation_date, observation_hour
having abs(sum(share_of_hour) - 1.0) > 0.000001
