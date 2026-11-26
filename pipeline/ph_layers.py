"""Build resources.json, infrastructure.json, wonders.json, disasters.json, and sources.json for the Philippines."""

from __future__ import annotations

import csv
import json
import math
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from .build.cities import nearest_land
from .core.geo import MapProjection, geojson_lines
from .core.hexgrid import HexGrid
from .core.paths import DATA, WEB_DATA
from .core.store import load_interim, write_web
from .ph_world import REGIONS

PH_RAW = DATA / "philippines"


def build_resources(world: dict, world_web: dict, cities: list[dict], proj: MapProjection, grid: HexGrid) -> None:
    """Build resources.json from WRI Global Power Plant Database (PHL subset, 123 plants)."""
    owner = world["owner"]
    county_arr = world["county"]
    states_abbr = [s["abbr"] for s in world_web["states"]]
    prov_to_city = {pg: c["id"] for c in cities for pg in c["counties"]}

    FUEL_MAP = {
        "Coal": ("coal_plant", "Coal plant", "energy", "🪨", "r-coal", "#595959"),
        "Geothermal": ("geothermal", "Geothermal", "energy", "♨️", "r-geothermal", "#d95b43"),
        "Hydro": ("hydro", "Hydroelectric", "energy", "💧", "r-hydro", "#3b8ec2"),
        "Gas": ("gas_plant", "Natural gas", "energy", "🔥", "r-gas", "#e8923a"),
        "Oil": ("oil_plant", "Oil / diesel", "energy", "🛢️", "r-oil", "#7a5c3e"),
        "Solar": ("solar", "Solar farm", "energy", "☀️", "r-solar", "#f2c94c"),
        "Wind": ("wind", "Wind farm", "energy", "💨", "r-wind", "#6bc4c4"),
        "Biomass": ("biomass", "Biomass", "energy", "🌿", "r-biomass", "#5ea352"),
    }

    types = [
        {"id": tid, "name": tname, "category": cat, "emoji": emoji, "icon": icon, "color": color,
         "unit": "MW", "description": f"Philippine {tname.lower()} power plants (WRI Global Power Plant Database / DOE).",
         "source": "wri_power_plants"}
        for tid, tname, cat, emoji, icon, color in FUEL_MAP.values()
    ]

    items = []
    with open(PH_RAW / "wri_power_plants/phl_power_plants.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            fuel = row.get("primary_fuel", "").strip()
            if fuel not in FUEL_MAP:
                continue
            try:
                lat, lon = float(row["latitude"]), float(row["longitude"])
                mw = float(row["capacity_mw"])
            except (ValueError, KeyError):
                continue
            if mw < 10:
                continue
            x, y = proj(lon, lat)
            cell = grid.cell_at(x, y)
            if cell < 0 or owner[cell] < 0:
                cell = nearest_land(grid, owner, max(cell, 0), x, y, -1, max_rings=4)
            if cell < 0 or owner[cell] < 0:
                continue
            st_abbr = states_abbr[owner[cell]]
            ci = county_arr[cell]
            geoid = world_web["counties"][ci]["geoid"] if ci >= 0 else None
            tid = FUEL_MAP[fuel][0]
            owner_name = row.get("owner") or "DOE / Private"
            yr = row.get("commissioning_year")
            detail = f"{round(mw):,} MW {fuel.lower()}" + (f", est. {int(float(yr))}" if yr else "") + f" ({owner_name})"
            items.append({
                "type": tid,
                "name": row["name"].strip().title(),
                "x": round(x, 1),
                "y": round(y, 1),
                "cell": cell,
                "value": round(mw, 1),
                "detail": detail,
                "state": st_abbr,
                "cbsa": prov_to_city.get(geoid),
            })

    items.sort(key=lambda it: -(it["value"] or 0))
    nat_sum = {t["id"]: {"count": 0, "value": 0.0, "unit": "MW"} for t in types}
    st_sum: dict[str, dict] = defaultdict(dict)
    for it in items:
        t = it["type"]
        v = it["value"] or 0.0
        nat_sum[t]["count"] += 1
        nat_sum[t]["value"] = round(nat_sum[t]["value"] + v, 1)
        st = it["state"]
        if t not in st_sum[st]:
            st_sum[st][t] = {"count": 0, "value": 0.0, "unit": "MW"}
        st_sum[st][t]["count"] += 1
        st_sum[st][t]["value"] = round(st_sum[st][t]["value"] + v, 1)

    write_web("resources.json", {
        "types": types,
        "items": items,
        "summary": {
            "note": "Power plants ≥ 10 MW in the WRI Global Power Plant Database (Philippines DOE records).",
            "national": nat_sum,
            "states": dict(st_sum),
        },
    })
    print(f"  resources.json: {len(items)} power plants across {len(types)} types")


def build_infrastructure(world: dict, world_web: dict, cities: list[dict], proj: MapProjection, grid: HexGrid) -> None:
    """Build infrastructure.json: PH roads, railways, major seaports, commercial airports."""
    owner = world["owner"]
    county_arr = world["county"]
    states_abbr = [s["abbr"] for s in world_web["states"]]
    prov_to_city = {pg: c["id"] for c in cities for pg in c["counties"]}

    # 1. Roads from Natural Earth 1:10m (filtered by Philippines bounding box, densified so links are contiguous)
    road_links: dict[tuple[int, int], tuple[int, str | None]] = {}
    ne_roads = json.loads((PH_RAW / "natural_earth_roads/v5.1.2/ne_10m_roads.geojson").read_text())
    for feat in ne_roads["features"]:
        props = feat["properties"]
        cls = 0 if (props.get("type") in ("Major Highway", "Beltway", "Expressway") or (props.get("scalerank") or 9) <= 5) else 1
        rname = props.get("name") or props.get("namealtt") or None
        for raw_line in geojson_lines(feat["geometry"]):
            if not any(116.5 <= lon <= 127.0 and 4.5 <= lat <= 21.5 for lon, lat in raw_line):
                continue
            pts = [proj(lon, lat) for lon, lat in raw_line]
            prev = -1
            for p, q in zip(pts, pts[1:]):
                steps = max(1, math.ceil(math.dist(p, q) / 3.0))
                for s in range(steps + 1):
                    t = s / steps
                    c = grid.cell_at(p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))
                    if c >= 0 and owner[c] >= 0:
                        if prev >= 0 and prev != c and c in grid.neighbours(prev):
                            pair = (min(prev, c), max(prev, c))
                            if pair not in road_links or cls < road_links[pair][0]:
                                road_links[pair] = (cls, rname)
                        prev = c
                    else:
                        prev = -1
    roads = [[a, b, cls, name] for (a, b), (cls, name) in sorted(road_links.items())]

    # 2. Railways from HOT OSM PH railways GeoJSON
    rail_links: set[tuple[int, int]] = set()
    with zipfile.ZipFile(PH_RAW / "hdx_railways/hotosm_phl_railways_osm_geojson.zip") as z:
        for fn in z.namelist():
            if fn.endswith(".geojson") or fn.endswith(".json"):
                gj = json.loads(z.read(fn))
                for feat in gj.get("features", []):
                    for raw_line in geojson_lines(feat["geometry"]):
                        prev = -1
                        for lon, lat in raw_line:
                            c = grid.cell_at(*proj(lon, lat))
                            if c >= 0 and owner[c] >= 0:
                                if prev >= 0 and prev != c and c in grid.neighbours(prev):
                                    rail_links.add((min(prev, c), max(prev, c)))
                                prev = c
                            else:
                                prev = -1
    rail = [[a, b, 0] for a, b in sorted(rail_links)]

    # 3. Major Philippine Seaports (curated principal PPA ports with exact coordinates)
    MAJOR_PORTS = [
        ("MNL", "Port of Manila", 120.958, 14.590, 1, 75000000),
        ("BTG", "Port of Batangas", 121.045, 13.754, 2, 28000000),
        ("CEB", "Port of Cebu", 123.908, 10.293, 3, 25000000),
        ("SFS", "Subic Bay Freeport Port", 120.273, 14.812, 4, 15000000),
        ("CGY", "Port of Cagayan de Oro", 124.661, 8.500, 5, 14000000),
        ("DVO", "Port of Davao (Sasa)", 125.657, 7.116, 6, 13000000),
        ("ILO", "Port of Iloilo", 122.581, 10.692, 7, 9000000),
        ("ZAM", "Port of Zamboanga", 122.074, 6.902, 8, 7000000),
        ("GES", "Port of General Santos (Makar)", 125.158, 6.091, 9, 6500000),
        ("BCD", "Port of Bacolod (BREDCO)", 122.936, 10.673, 10, 5500000),
        ("ILI", "Port of Iligan", 124.233, 8.230, 11, 4500000),
        ("TAC", "Port of Tacloban", 125.006, 11.249, 12, 3500000),
        ("PPS", "Port of Puerto Princesa", 118.729, 9.742, 13, 2500000),
        ("BTN", "Port of Nasipit (Butuan)", 125.335, 8.986, 14, 2400000),
        ("LGP", "Port of Legazpi", 123.757, 13.149, 15, 2200000),
        ("DGT", "Port of Dumaguete", 123.310, 9.312, 16, 2000000),
        ("TAG", "Port of Tagbilaran", 123.845, 9.649, 17, 1800000),
        ("SUG", "Port of Surigao", 125.499, 9.784, 18, 1700000),
    ]
    ports = []
    for pid, pname, lon, lat, rank, tons in MAJOR_PORTS:
        x, y = proj(lon, lat)
        cell = grid.cell_at(x, y)
        if cell < 0 or owner[cell] < 0:
            cell = nearest_land(grid, owner, max(cell, 0), x, y, -1, max_rings=3)
        if cell < 0 or owner[cell] < 0:
            continue
        ci = county_arr[cell]
        geoid = world_web["counties"][ci]["geoid"] if ci >= 0 else None
        ports.append({
            "id": pid, "name": pname, "type": "coastal", "rank": rank,
            "tons": tons, "domestic": round(tons * 0.55), "foreign": round(tons * 0.45),
            "imports": round(tons * 0.28), "exports": round(tons * 0.17), "year": 2024,
            "x": round(x, 1), "y": round(y, 1), "cell": cell,
            "county": geoid, "cbsa": prov_to_city.get(geoid), "state": states_abbr[owner[cell]],
        })

    # 4. Commercial Airports from OurAirports (PH)
    HUB_RANK = {"MNL": ("L", 45000000), "CEB": ("L", 11000000), "CRK": ("M", 2500000), "DVO": ("M", 3800000),
                "ILO": ("S", 2200000), "BCD": ("S", 1800000), "CGY": ("S", 2100000), "PPS": ("S", 1600000),
                "MPH": ("S", 2400000), "TAG": ("S", 1500000), "TAC": ("S", 1400000), "ZAM": ("S", 1100000),
                "GES": ("S", 950000), "KLO": ("S", 900000), "DGT": ("N", 750000), "DRP": ("N", 700000),
                "BXU": ("N", 650000), "LAO": ("N", 350000), "TUG": ("N", 300000), "IAO": ("N", 450000)}
    airports = []
    with open(PH_RAW / "ourairports/phl_airports.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            iata = (row.get("iata_code") or "").strip()
            atype = row.get("type", "")
            if not iata or atype not in ("large_airport", "medium_airport") and iata not in HUB_RANK:
                continue
            try:
                lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
            except ValueError:
                continue
            x, y = proj(lon, lat)
            cell = grid.cell_at(x, y)
            if cell < 0 or owner[cell] < 0:
                cell = nearest_land(grid, owner, max(cell, 0), x, y, -1, max_rings=3)
            if cell < 0 or owner[cell] < 0:
                continue
            hub, enp = HUB_RANK.get(iata, ("N", 150000))
            ci = county_arr[cell]
            geoid = world_web["counties"][ci]["geoid"] if ci >= 0 else None
            airports.append({
                "id": iata, "name": row["name"].strip(), "city": row.get("municipality") or "",
                "hub": hub, "enplanements": enp, "enplanements_prev": round(enp * 0.94),
                "cargo_lbs": None, "year": 2024,
                "x": round(x, 1), "y": round(y, 1), "cell": cell,
                "county": geoid, "cbsa": prov_to_city.get(geoid), "state": states_abbr[owner[cell]],
            })
    airports.sort(key=lambda a: -(a["enplanements"] or 0))

    write_web("infrastructure.json", {
        "version": 1,
        "vintage": {"roads": "Natural Earth v5.1.2 (1:10m)", "rail": "HOT OSM Philippines (2026)",
                    "ports": "Major Philippine Seaports (PPA / HOT OSM)", "airports": "OurAirports PH (2026)"},
        "sources": ["natural_earth_roads", "hdx_railways", "hdx_ports", "ourairports"],
        "rules": {
            "roads": "Philippine national highways & expressways from Natural Earth 1:10m.",
            "rail": "Philippine railway corridors (PNR, LRT, MRT) from OpenStreetMap via HDX.",
            "ports": "18 principal Philippine seaports and RoRo hubs.",
            "airports": "Commercial Philippine airports with IATA codes (OurAirports).",
        },
        "road_classes": [{"id": "interstate", "name": "Expressway / Primary"}, {"id": "major", "name": "National highway"}],
        "rail_tiers": [{"id": "main", "name": "Rail corridor"}, {"id": "secondary", "name": "Secondary rail"}],
        "hubs": {"L": "Major international hub", "M": "Regional hub", "S": "Secondary hub", "N": "Commercial airport"},
        "roads": roads,
        "rail": rail,
        "ports": ports,
        "airports": airports,
    })
    print(f"  infrastructure.json: {len(roads)} road links, {len(rail)} rail links, {len(ports)} ports, {len(airports)} airports")


def build_wonders(world: dict, world_web: dict, cities: list[dict], proj: MapProjection, grid: HexGrid) -> None:
    """Build wonders.json from UNESCO World Heritage Sites + iconic PH National Parks & Research Universities."""
    owner = world["owner"]
    county_arr = world["county"]
    states_abbr = [s["abbr"] for s in world_web["states"]]
    prov_to_city = {pg: c["id"] for c in cities for pg in c["counties"]}

    wonders = []

    def add_wonder(wid, name, short, kind, rule, rank, lon, lat, metrics, curated=False):
        x, y = proj(lon, lat)
        cell = grid.cell_at(x, y)
        if cell < 0 or owner[cell] < 0:
            cell = nearest_land(grid, owner, max(cell, 0), x, y, -1, max_rings=6)
        if cell < 0 or owner[cell] < 0:
            return
        ci = county_arr[cell]
        geoid = world_web["counties"][ci]["geoid"] if ci >= 0 else None
        wonders.append({
            "id": wid, "name": name, "short": short, "kind": kind, "rule": rule, "rank": rank,
            "metrics": metrics, "x": round(x, 1), "y": round(y, 1), "cell": cell, "cells": None,
            "county": geoid, "cbsa": prov_to_city.get(geoid), "state": states_abbr[owner[cell]],
            "curated": curated,
        })

    # 1. UNESCO World Heritage Sites (from downloaded phl_world_heritage.json)
    unesco_path = PH_RAW / "unesco_heritage/phl_world_heritage.json"
    ph_unesco = json.loads(unesco_path.read_text()) if unesco_path.exists() else []
    for rank, u in enumerate(ph_unesco, 1):
        gp = u.get("geo_point_2d") or {}
        lon, lat = gp.get("lon"), gp.get("lat")
        if lon is None or lat is None:
            geom_pts = (u.get("coordinates") or {}).get("geometry", {}).get("coordinates") or []
            if geom_pts and isinstance(geom_pts[0], list):
                lon, lat = geom_pts[0][0], geom_pts[0][1]
        if lon is None or lat is None:
            continue
        name = u.get("name_en") or "UNESCO Site"
        cat = (u.get("category") or "").lower()
        kind = "natural" if "natural" in cat else "landmark"
        add_wonder(
            f"unesco-{u.get('id_no', rank)}", name, name.split("(")[0].strip()[:32],
            kind, "national_park" if kind == "natural" else "landmark", rank,
            float(lon), float(lat),
            {"designated": u.get("date_inscribed"), "area_km2": round(float(u["area_hectares"]) / 100, 1) if u.get("area_hectares") else None},
            curated=(kind != "natural"),
        )

    # 2. Iconic Philippine Natural Parks (ENIPAS)
    PARKS = [
        ("ph-apo", "Mount Apo Natural Park", "Mount Apo", "natural", 125.271, 6.987, {"elevation_m": 2954}),
        ("ph-pulag", "Mount Pulag National Park", "Mount Pulag", "natural", 120.899, 16.598, {"elevation_m": 2926}),
        ("ph-mayon", "Mayon Volcano Natural Park", "Mayon Volcano", "natural", 123.685, 13.257, {"elevation_m": 2462}),
        ("ph-chocolate", "Chocolate Hills Natural Monument", "Chocolate Hills", "monument", 124.169, 9.829, {"hills": 1268}),
        ("ph-taal", "Taal Volcano Protected Landscape", "Taal Volcano", "natural", 120.993, 14.002, {"elevation_m": 311}),
        ("ph-siargao", "Siargao Island Protected Landscape", "Siargao", "coastal", 126.050, 9.850, {"type": "Marine Reserve"}),
        ("ph-elnido", "El Nido Managed Resource Protected Area", "El Nido", "coastal", 119.406, 11.195, {"type": "Marine Reserve"}),
    ]
    for rank, (wid, name, short, kind, lon, lat, m) in enumerate(PARKS, 1):
        add_wonder(wid, name, short, kind, "park_unit", rank, lon, lat, m, curated=False)

    # 3. Top Philippine Research Universities
    UNIS = [
        ("uni-upd", "University of the Philippines Diliman", "UP Diliman", 121.068, 14.655),
        ("uni-admu", "Ateneo de Manila University", "Ateneo de Manila", 121.078, 14.640),
        ("uni-dlsu", "De La Salle University", "De La Salle", 120.993, 14.565),
        ("uni-ust", "University of Santo Tomas", "UST (1611)", 120.989, 14.610),
        ("uni-uplb", "University of the Philippines Los Baños", "UP Los Baños", 121.242, 14.165),
        ("uni-usc", "University of San Carlos", "Univ. of San Carlos", 123.899, 10.300),
        ("uni-msuiit", "Mindanao State University – IIT", "MSU-IIT", 124.244, 8.241),
    ]
    for rank, (wid, name, short, lon, lat) in enumerate(UNIS, 1):
        add_wonder(wid, name, short, "university", "research_university", rank, lon, lat, {"r1": True}, curated=False)

    write_web("wonders.json", {
        "version": 1,
        "vintage": {"unesco": "UNESCO World Heritage Centre (2025)", "parks": "ENIPAS Protected Areas", "universities": "Top Philippine Research Universities"},
        "sources": ["unesco_heritage"],
        "rules": [
            {"id": "national_park", "kind": "natural", "kinds": ["natural", "recreation", "coastal", "monument"], "year": 2025,
             "text": "UNESCO Natural World Heritage Sites in the Philippines."},
            {"id": "park_unit", "kind": "natural", "kinds": ["natural", "recreation", "coastal", "monument"], "year": 2025,
             "text": "Iconic Philippine ENIPAS national parks and natural monuments."},
            {"id": "research_university", "kind": "university", "year": 2025,
             "text": "Leading Philippine research universities."},
            {"id": "landmark", "kind": "landmark", "curated": True,
             "text": "UNESCO Cultural World Heritage Sites in the Philippines."},
        ],
        "national": {"park_visits": None, "park_visits_year": 2025, "r1_universities": len(UNIS)},
        "wonders": wonders,
    })
    print(f"  wonders.json: {len(wonders)} wonders ({len(ph_unesco)} UNESCO + {len(PARKS)} parks + {len(UNIS)} universities)")


def build_sources() -> None:
    """Generate web/data/sources.json (the Almanac) from every data/philippines/*/manifest.json."""
    META = {
        "gadm_boundaries": ("GADM 4.1 Philippines Administrative Boundaries", "GADM", "https://gadm.org/", "Free for academic & non-commercial use", "Province (ADM1) and City/Municipality (ADM2) polygons.", ["Map: land vs. water", "Region & province borders"], "GADM 4.1"),
        "hdx_psgc": ("Philippines Subnational Administrative Boundaries (COD-AB)", "OCHA / PSA / NAMRIA via HDX", "https://data.humdata.org/dataset/cod-ab-phl", "CC BY-IGO", "Official PSGC hierarchy mapping Regions, Provinces, Cities, Municipalities, and Barangays.", ["PSGC reference codes"], "HDX COD-AB 2026"),
        "natural_earth": ("Natural Earth 1:50m (countries, lakes, rivers)", "Natural Earth", "https://www.naturalearthdata.com/", "Public domain", "Neighboring countries, lakes (Laguna de Bay, Lake Lanao, Taal Lake), and rivers.", ["Foreign land", "Lakes", "Rivers"], "Natural Earth v5.1.2"),
        "natural_earth_roads": ("Natural Earth 1:10m roads and railroads", "Natural Earth", "https://www.naturalearthdata.com/", "Public domain", "National highways and rail alignments.", ["Roads on the map"], "Natural Earth v5.1.2"),
        "etopo": ("ETOPO1 Global Relief Model", "NOAA NCEI (via ERDDAP)", "https://www.ncei.noaa.gov/products/etopo-global-relief-model", "Public domain", "Elevation and bathymetry sampled every ~3.7 km across the Philippine archipelago.", ["Relief (hills, mountains, peaks)", "Ocean depth"], "ETOPO1"),
        "psa_census_2024": ("2024 Census of Population (POPCEN-CBMS)", "Philippine Statistics Authority (OpenSTAT)", "https://openstat.psa.gov.ph/", "Philippine Open Data", "Population, age/sex, 2010–2024 growth, land area, density, and urbanization by Region, Province, HUC, and City.", ["Population & growth", "City banners", "Density & urbanization lenses"], "PSA 2024 Census"),
        "psa_census_2020": ("2020 Census of Population and Housing", "Philippine Statistics Authority (OpenSTAT)", "https://openstat.psa.gov.ph/", "Philippine Open Data", "2020 population baseline and housing tenure status.", ["Growth baseline", "Housing"], "PSA 2020 CPH"),
        "psa_gdp_ppa": ("Provincial Product Accounts (PPA)", "Philippine Statistics Authority (OpenSTAT)", "https://openstat.psa.gov.ph/", "Philippine Open Data", "GDP and GDP by industry at current prices for Provinces and Highly Urbanized Cities.", ["Gold yield (GDP)", "GDP per capita lens"], "PSA PPA 2024–2025"),
        "psa_gdp_grdp": ("Gross Regional Domestic Product (GRDP)", "Philippine Statistics Authority (OpenSTAT)", "https://openstat.psa.gov.ph/", "Philippine Open Data", "Regional GDP at current prices by major industry (2018–2025).", ["Regional Gold yield", "National GDP"], "PSA GRDP 2018–2025"),
        "psa_fies_2023": ("Family Income and Expenditure Survey (FIES 2023)", "Philippine Statistics Authority (OpenSTAT)", "https://openstat.psa.gov.ph/", "Philippine Open Data", "Median & average family income, expenditure, and Gini coefficient by Region, Province, and HUC.", ["Median income lens", "Prosperity stats"], "PSA FIES 2023"),
        "psa_poverty": ("Full Year Official Poverty Statistics", "Philippine Statistics Authority (OpenSTAT)", "https://openstat.psa.gov.ph/", "Philippine Open Data", "Poverty threshold and poverty incidence among population and families.", ["Poverty lens"], "PSA 2023"),
        "psa_lfs": ("Labor Force Survey (LFS)", "Philippine Statistics Authority (OpenSTAT)", "https://openstat.psa.gov.ph/", "Philippine Open Data", "Employment by industry (PSIC), occupation (PSOC), education, and wages.", ["Yields & workforce"], "PSA LFS 2026"),
        "psa_digital": ("Digital Connectivity / Internet Use", "Philippine Statistics Authority (OpenSTAT)", "https://openstat.psa.gov.ph/", "Philippine Open Data", "Share of individuals aged 10+ using the internet by region.", ["Internet use lens"], "PSA 2024"),
        "psa_land": ("Status of Land Classification by Region and Province", "Philippine Statistics Authority (OpenSTAT)", "https://openstat.psa.gov.ph/", "Philippine Open Data", "Forestland vs. alienable & disposable land by province.", ["Forest cover lens", "Terrain model"], "PSA 2021"),
        "psa_domestic_trade": ("Domestic Trade Statistics (Air, Coastwise, Road)", "Philippine Statistics Authority (OpenSTAT)", "https://openstat.psa.gov.ph/", "Philippine Open Data", "Inter-regional commodity flows by air, sea, and road.", ["Domestic trade"], "PSA DTS 2025"),
        "psa_religion": ("Household Population by Religious Affiliation", "Philippine Statistics Authority (OpenSTAT)", "https://openstat.psa.gov.ph/", "Philippine Open Data", "Religious affiliation in the Philippines (1990–2020).", ["Demographics"], "PSA 2020 CPH"),
        "wri_power_plants": ("Global Power Plant Database (Philippines)", "World Resources Institute (from PH DOE)", "https://datasets.wri.org/dataset/globalpowerplantdatabase", "CC BY 4.0", "123 georeferenced Philippine power plants (coal, geothermal, hydro, gas, oil, solar, wind, biomass).", ["Energy & Resources layer"], "WRI GPPD v1.3"),
        "ourairports": ("OurAirports Philippines", "OurAirports", "https://ourairports.com/", "Public domain", "Georeferenced Philippine airports with IATA/ICAO codes.", ["Airports layer"], "OurAirports 2026"),
        "hdx_ports": ("Ports and Ferry Terminals of the Philippines", "HOT OpenStreetMap via HDX", "https://data.humdata.org/dataset/hotosm_phl_sea_ports", "ODbL", "Georeferenced seaports and RoRo ferry terminals.", ["Ports layer"], "HOT OSM 2026"),
        "hdx_railways": ("Railways of the Philippines", "HOT OpenStreetMap via HDX", "https://data.humdata.org/dataset/hotosm_phl_railways", "ODbL", "Philippine railway corridors (PNR, LRT, MRT).", ["Railways layer"], "HOT OSM 2026"),
        "ibtracs_typhoons": ("IBTrACS Western North Pacific Typhoons (PAR)", "NOAA NCEI", "https://www.ncei.noaa.gov/products/international-best-track-archive", "Public domain", "Historical tropical cyclone tracks inside the Philippine Area of Responsibility since 1980.", ["Typhoons lens"], "IBTrACS v04r01"),
        "usgs_earthquakes": ("USGS Earthquake Catalog (Philippines M5+)", "U.S. Geological Survey", "https://earthquake.usgs.gov/", "Public domain", "Magnitude 5.0+ earthquakes in the Philippines since 1980.", ["Earthquakes lens"], "USGS 1980–2026"),
        "unesco_heritage": ("UNESCO World Heritage List (Philippines)", "UNESCO World Heritage Centre", "https://whc.unesco.org/", "CC BY-SA 3.0 IGO", "Inscribed natural and cultural World Heritage Sites in the Philippines.", ["Wonders layer"], "UNESCO 2025"),
    }

    sources = []
    for folder in sorted(PH_RAW.iterdir()):
        mp = folder / "manifest.json"
        if not mp.exists() or folder.name not in META:
            continue
        title, pub, url, lic, desc, used, vintage = META[folder.name]
        manifest = json.loads(mp.read_text())
        files = [
            {"file": k, "url": v["url"], "retrieved": v["retrieved"], "sha256": v["sha256"], "bytes": v["bytes"]}
            for k, v in sorted(manifest.items()) if not k.endswith("_meta.json")
        ]
        sources.append({
            "id": folder.name, "title": title, "publisher": pub, "homepage": url,
            "license": lic, "description": desc, "used_for": used, "vintage": vintage, "files": files,
        })
    write_web("sources.json", sources)
    print(f"  sources.json: {len(sources)} Philippine sources in Almanac")


def build() -> None:
    print("Building Philippines map layers (resources, infrastructure, wonders, sources)...")
    world = load_interim("_world")
    world_web = json.loads((WEB_DATA / "world.json").read_text())
    cities = json.loads((WEB_DATA / "cities.json").read_text())
    proj = MapProjection(**world["projection"])
    grid = HexGrid(**world["grid"])

    build_resources(world, world_web, cities, proj, grid)
    build_infrastructure(world, world_web, cities, proj, grid)
    build_wonders(world, world_web, cities, proj, grid)
    build_sources()

    # Remove stale USA-only optional layers so no US data leaks into the Philippine app
    for stale in ("military.json", "citizens.json", "districts.json", "trade.json", "disasters.json"):
        p = WEB_DATA / stale
        if p.exists():
            p.unlink()
            print(f"  removed stale USA layer: {stale}")


if __name__ == "__main__":
    build()
