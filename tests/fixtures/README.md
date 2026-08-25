# Test fixtures

## Provenance

These are **real, unmodified OpenSky Network API responses**, captured on
2026-08-25 from the anonymous public endpoint:

```
GET https://opensky-network.org/api/states/all
      ?lamin=45.8389&lomin=5.9962&lamax=47.8229&lomax=10.5226
```

The bounding box covers Switzerland. It was chosen so each response is a
*complete* API result at ~19 KB rather than a truncated slice of a multi-megabyte
worldwide payload — a truncated capture would not be a faithful example of what
the API returns.

Nothing in these files is synthetic. No value has been edited, reordered or
substituted. The only transformation applied is JSON re-serialisation with
sorted keys and no whitespace, so that diffs stay reviewable.

Where a test needs exact, stable numbers to assert on, it uses the small
hand-built payload in `conftest.py` instead — which is clearly synthetic and
never presented as observation.

| File | Aircraft | Size |
| --- | --- | --- |
| `states_1787650939.json` | 146 | 18.8 KB |
| `states_1787650966.json` | 145 | 18.6 KB |
| `states_1787650997.json` | 149 | 19.2 KB |

The three snapshots are ~25 seconds apart and share 139–143 `icao24` addresses
between consecutive captures. That overlap is deliberate: it gives the Phase 3
deduplication on `(icao24, time_position)` real duplicate keys to resolve rather
than a contrived case.

## Privacy review

Reviewed before committing, per §0 of the build contract. The OpenSky state
vector has 17 positional fields and exactly two of them are free text:

- **`origin_country`** — 22 distinct values across these fixtures, all ISO
  country names.
- **`callsign`** — 157 distinct values, all ICAO flight identifiers
  (`SWR123`, `BAW600`), which are operational identifiers for a *flight*, not a
  person.

`icao24` is a transponder address broadcast unencrypted over open radio by the
aircraft itself, and republished by OpenSky under an open licence. It identifies
an airframe, not an individual.

Scanned for and confirmed absent: email addresses, phone-shaped numbers,
12-digit AWS account IDs, URLs, AWS key prefixes, and any owner/operator/pilot
/registrant/address field. Every `icao24` matched `[0-9a-f]{6}` and every
`callsign` matched `[A-Z0-9 -]{1,12}`, so no unexpected free text is hiding in
either column.

`test_ingest.py::test_captured_fixtures_match_the_opensky_schema` re-checks the
structural half of this on every CI run, so silent corruption or hand-editing
of these files fails the build.

## Licence

OpenSky Network data is published for research and non-commercial use. See
<https://opensky-network.org/about/terms-of-use>.
