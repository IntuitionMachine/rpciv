# Philippines Data Inventory & USA-to-PH Mapping (`rpciv`)

All raw Philippine datasets have been downloaded to `data/philippines/` (**310 MB**, 28 source folders, 53 data files). Every folder includes a `manifest.json` recording `url`, `sha256`, `bytes`, and `retrieved` timestamp—matching the exact provenance format used by `pipeline/core/fetch.py` for the in-app **Almanac**.

> **Note on `.gitignore`:** `data/raw/` is currently ignored in `.gitignore`, while `data/philippines/` is not yet listed. Add `data/philippines/` to `.gitignore` before committing so the 310 MB of raw downloads are not committed to git.

---

## Recommended Geographic & Projection Adaptations

| Concept | `usaciv` (USA) | Recommended `rpciv` (Philippines) |
|---|---|---|
| **Map projection** (`pipeline/core/geo.py`) | Albers Equal-Area (`lat1=29.5, lat2=45.5, lat0=37.5, lon0=-96.0`) | Albers Equal-Area (`lat1=8.0, lat2=18.0, lat0=13.0, lon0=122.0`) |
| **Bounding box** | Contiguous US (~5,000 × 3,300 km) | Philippines (~1,100 × 1,850 km; lat `4.5°–21.5°N`, lon `116.8°–126.6°E`) |
| **Hex radius** (`pipeline/core/hexgrid.py`) | 25 km (~1,600 km²/hex) | **8–10 km** (~170–260 km²/hex) so Visayas/Palawan/Sulu islands stay distinct |
| **Top-level unit (`states.json`)** | 48 states + DC | **18 Regions** (NCR, CAR, Regions I–XIII, BARMM, NIR) |
| **Sub-unit (`counties.json`)** | ~3,100 counties | **Provinces + HUCs (115)** (matches PSA PPA GDP, FIES, Poverty, Census) or **Cities & Municipalities (1,634)** |
| **Cities (`cities.json`)** | ~536 CBSAs (metros/micros) | **33 Highly Urbanized Cities (HUCs) + 5 ICCs + 108 Component Cities** (or Metro Manila / Metro Cebu / Metro Davao + major cities) |
| **Currency** | USD | PHP (Philippine Peso) — update `web/js/format.js` |

---

## Downloaded Datasets (`data/philippines/`)

### 1. Boundaries, Geography & Terrain

| Folder | Files | Publisher | Replaces (`usaciv`) | Used For |
|---|---|---|---|---|
| `gadm_boundaries/` | `gadm41_PHL_0.json`, `gadm41_PHL_1.json.zip`, `gadm41_PHL_2.json.zip` | GADM 4.1 | `census_boundaries` | Country, Province (ADM1), and City/Municipality (ADM2) polygons |
| `hdx_psgc/` | `phl_admin_boundaries.xlsx` | OCHA / PSA / NAMRIA (HDX COD-AB) | `census_gazetteer`, `cbsa_delineation` | Official PSGC hierarchy (Region → Province → City/Mun → Barangay) |
| `natural_earth/` | `v5.1.2/ne_50m_admin_0_countries.geojson`, `ne_50m_lakes.geojson`, `ne_50m_rivers_lake_centerlines.geojson` | Natural Earth v5.1.2 | `natural_earth` | Neighboring countries (Taiwan, Malaysia, Indonesia, Brunei, Vietnam), lakes (Laguna de Bay, Lanao, Taal), rivers |
| `etopo/` | `etopo_phl_lat4_22_lon115_128_stride2.csv` | NOAA NCEI (ETOPO1 via ERDDAP) | `etopo` | Elevation, hill-shading, ocean depth (~3.7 km grid, lat 4–22°N, lon 115–128°E) |
| `psa_land/` | `land_classification_region_province_2021.csv` | PSA OpenSTAT (`DB/3A/2003A2BSLC0.px`) | `nlcd` (tabular partial) | Forest land vs. alienable & disposable land by region & province |

### 2. Demographics, Census & Socioeconomic Lenses

| Folder | Files | Publisher | Replaces (`usaciv`) | Used For |
|---|---|---|---|---|
| `psa_census_2024/` | `pop_region_province_huc_2024.csv`, `age_sex_region_province_huc_2024.csv`, `pop_growth_2010_2024.csv`, `pop_area_density_2015_2024.csv`, `urban_pop_2024.csv`, `city_pop_area_density_2010_2024.csv` | PSA OpenSTAT (2024 POPCEN-CBMS) | `acs_1yr`, `acs_5yr`, `pep` | Population, age/sex mix, 2010–2024 growth, land area, density, urbanization |
| `psa_census_2020/` | `pop_region_province_huc_2020.csv`, `housing_tenure_2020.csv` | PSA OpenSTAT (2020 CPH) | `acs_5yr` (homeownership) | 2020 population baseline & housing tenure status |
| `hdx_pop/` | `phl_admpop_2022_2023_2024_2025_v2.xlsx`, `phl_admpop_adm1_2023.csv`, `phl_admpop_adm2_2023_v2.csv` | HDX / UNFPA / PSA (`cod-ps-phl`) | `pep` | Clean PSGC-keyed subnational population tables (2022–2025) |
| `psa_fies_2023/` | `avg_family_income_expenditure_2023.csv`, `median_family_income_expenditure_2023.csv`, `gini_coefficient_2023.csv` | PSA OpenSTAT (FIES 2023) | `acs_5yr` (income) | Median & average family income, expenditure, Gini inequality by Province/HUC |
| `psa_poverty/` | `poverty_families_region_province.csv`, `poverty_population_region_province_huc.csv` | PSA OpenSTAT (2018, 2021, 2023) | `acs_5yr` (poverty) | Poverty threshold & poverty incidence by Region, Province, and HUC |
| `psa_digital/` | `internet_use_by_region_2024.csv` | PSA OpenSTAT (`DB/3M/IU`) | `acs_5yr` (broadband) | Internet use share by region (2024) |
| `psa_religion/` | `religion_0111A2BPRA0.csv` | PSA OpenSTAT (2020 Census) | `usrc_2020` | Religious affiliation (Roman Catholic, Islam, Iglesia ni Cristo, Evangelical, etc.) |
| `psa_health_edu/` | `gov_medical_practitioners_by_region.csv`, `school_enrolment_2000_2024.csv` | PSA OpenSTAT | `cdc_places` / `acs_5yr` | Regional medical practitioners & school enrolment |
| `hdx_health/` | `hotosm_phl_health_facilities_osm_geojson.zip` | HOT OSM / HDX | `cdc_places` (facilities) | Georeferenced hospitals, clinics, and health centers |

### 3. Economy, GDP, Workforce & Trade

| Folder | Files | Publisher | Replaces (`usaciv`) | Used For |
|---|---|---|---|---|
| `psa_gdp_ppa/` | `gdp_by_province_huc.csv`, `gdp_by_industry_province_huc.csv`, `gdp_per_capita_province_huc.csv` | PSA OpenSTAT (Provincial Product Accounts) | `bea_gdp` (`CAGDP2`) | Gold yield (GDP) and GDP by industry for every Province & HUC |
| `psa_gdp_grdp/` | `grdp_current_by_region_industry_2018_2025.csv` | PSA OpenSTAT (GRDP) | `bea_gdp` (states) | Regional GDP at current prices by major industry (2018–2025) |
| `psa_lfs/` | `employment_rates_key_indicators.csv`, `employed_by_industry_psic.csv`, `employed_by_occupation_psoc.csv`, `employed_by_education_psced.csv`, `avg_daily_basic_pay_by_industry.csv` | PSA OpenSTAT (Labor Force Survey, to Sep 2026) | `acs_1yr` (yields/citizens), `bls_qcew` | Employment by industry (PSIC), occupation (PSOC), education (PSCED), daily pay, unemployment |
| `psa_domestic_trade/` | `air_commodity_flow_annual.csv`, `coastwise_commodity_flow_annual.csv`, `road_domestic_trade_2025_2026.csv` | PSA OpenSTAT (Domestic Trade Statistics) | `faf5` | Inter-regional air, coastwise (maritime), and road commodity freight flows (`trade.json`) |

### 4. Energy, Infrastructure, Hazards & Wonders

| Folder | Files | Publisher | Replaces (`usaciv`) | Used For |
|---|---|---|---|---|
| `wri_power_plants/` | `phl_power_plants.csv` (123 plants) | WRI Global Power Plant Database | `eia_860m` | Georeferenced power plants (coal, geothermal, hydro, gas, solar, wind, oil, biomass) |
| `psa_energy/` | `primary_energy_supply_2014_2024.csv` | PSA OpenSTAT / DOE | `eia_860m` (national) | National primary energy supply by fuel source (2014–2024) |
| `natural_earth_roads/` | `v5.1.2/ne_10m_roads.geojson`, `ne_10m_railroads.geojson` | Natural Earth v5.1.2 | `natural_earth_roads` | Major highways & rail network for map links |
| `hdx_railways/` | `hotosm_phl_railways_osm_geojson.zip` | HOT OSM / HDX | `natural_earth_roads` (rail) | Detailed PH rail lines (PNR, LRT, MRT) |
| `hdx_ports/` | `hotosm_phl_sea_ports_osm_geojson.zip` | HOT OSM / HDX | `usace_ports` | Seaports & RoRo ferry terminals |
| `ourairports/` + `hdx_airports/` | `phl_airports.csv` (856 airfields), `hotosm_phl_airports_osm_geojson.zip` | OurAirports & HOT OSM / HDX | `faa_airports` | Airports with IATA/ICAO codes, type, and coordinates |
| `ibtracs_typhoons/` | `ibtracs_wp_par_since1980.csv` (30,608 track points) | NOAA NCEI (IBTrACS v04r01) | `openfema`, `noaa_billion` | Historical typhoons in the Philippine Area of Responsibility (1980–present) |
| `usgs_earthquakes/` | `phl_earthquakes_m5_since1980.csv` | USGS Earthquake Catalog | `openfema` | M ≥ 5.0 earthquakes in the Philippines (1980–2026) |
| `unesco_heritage/` | `phl_world_heritage.json` | UNESCO World Heritage Centre | `nrhp_nhl`, `nps` | UNESCO World Heritage Sites in the Philippines (`wonders.json`) |

---

## Remaining Gaps / Optional Enhancements

A few USA-specific datasets do not have a 1-to-1 open API equivalent in the Philippines and will benefit from targeted follow-up when building those specific layers:

1. **Pixel-level Land Cover (`nlcd` equivalent):** `psa_land/` gives tabular forest vs. alienable land by province. For hex-level painted terrain (`forest`, `crop`, `grass`, `urban`, `wetland`), download **ESA WorldCover 10m** or **Copernicus Global Land Cover 100m** GeoTIFF tiles covering `N04–N21, E116–E127`.
2. **Mines & Oil/Gas (`msha_mines`, `eia_coal`, `eia_shale_plays`):** Download the **MGB (Mines and Geosciences Bureau)** list of operating metallic mines (nickel, gold, copper, chromite) and **DOE** coal/petroleum contracts (Semirara coal, Malampaya gas).
3. **Port Tonnage & Airport Enplanements:** `hdx_ports/` and `ourairports/` provide exact coordinates and classifications; to rank by volume, supplement with **PPA (Philippine Ports Authority)** annual cargo tables and **CAAP** passenger traffic PDFs/CSVs.
4. **Universities & National Parks (`ipeds`, `herd`, `nps`):** Supplement `unesco_heritage/` with a curated list of **ENIPAS Protected Areas / National Parks** (e.g., Mt. Apo, Mt. Pulag, Mayon, Chocolate Hills, Apo Reef, Siargao) and top **CHED / research universities** (UP System, Ateneo, DLSU, UST, USC, MSU-IIT, Mapúa).
5. **Military (`mirta`, `dmdc`, `oldcc`):** The Philippines does not publish a public GIS shapefile of AFP installations or provincial troop counts. If keeping the Military layer, either curate a small public list of major AFP headquarters/bases (Camp Aguinaldo, Fort Bonifacio, Fort Magsaysay, Basa/Clark/Benito Ebuen Air Bases, Naval Base Cavite/Subic) or disable the optional `military.json` layer (the frontend supports omitting optional layers).
