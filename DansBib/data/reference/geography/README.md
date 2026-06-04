# GeoCensus Geography References

GeoCensus no longer uses GeoNames. Geography extraction is based on official statistical/census providers plus supplemental aliases.

Currently implemented provider:
- U.S. Census Gazetteer files in `data/reference/geography/us_census/`

Supplemental geography controls live in `data/reference/geography/supplemental/`:
- `geography_keep_terms.txt`
- `geography_stopwords.txt`
- `geography_aliases.json`

Future official providers can be added under `data/reference/geography/providers/`. Do not use GeoNames as a fallback.
