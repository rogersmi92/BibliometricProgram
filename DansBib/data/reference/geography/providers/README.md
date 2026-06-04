# Geography Providers

GeoCensus no longer uses GeoNames. Geography extraction is based on official statistical or census providers plus supplemental aliases.

Currently implemented provider:
- `us_census`: U.S. Census Gazetteer files under `data/reference/geography/us_census/`

Future official providers can be added here without using GeoNames as a fallback, for example:
- `eurostat_gisco` for EU NUTS/LAU data
- `stats_canada` for Canada
- `ons_uk` for the United Kingdom
- `ibge_brazil` for Brazil
- `estat_japan` for Japan
- `abs_australia` for Australia
