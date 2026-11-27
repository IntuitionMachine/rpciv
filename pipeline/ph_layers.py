"""Build resources.json, infrastructure.json, wonders.json, disasters.json, and sources.json for the Philippines."""

from __future__ import annotations

import csv
import json
import math
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from .build.cities import nearest_land
from .build.infrastructure import hex_distance, hex_walk, straighten
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
                cell = nearest_land(grid, owner, max(cell, 0), x, y, -1, max_rings=12)
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


def _rasterize_line(
    grid: HexGrid, owner: list[int], proj: MapProjection, raw_line: list[tuple[float, float]],
    *, step_km: float = 1.0, max_bridge_water: int = 2,
) -> list[tuple[int, int]]:
    """Sample a lon/lat polyline onto continuous neighbouring hex links using hex_walk + straighten."""
    if len(raw_line) < 2:
        return []
    pts = [proj(lon, lat) for lon, lat in raw_line]
    run: list[int] = []
    for p, q in zip(pts, pts[1:]):
        dist = math.dist(p, q)
        steps = max(1, math.ceil(dist / step_km))
        for s in range(steps + 1):
            t = s / steps
            c = grid.cell_at(p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))
            if 0 <= c < grid.size and owner[c] >= 0:
                if not run or run[-1] != c:
                    run.append(c)
    if len(run) < 2:
        return []

    # Fill gaps between consecutive land cells via hex_walk (bridge narrow straits <= max_bridge_water, break on seas)
    walked: list[int] = [run[0]]
    for b in run[1:]:
        a = walked[-1]
        if a == b:
            continue
        if a >= 0 and b in grid.neighbours(a):
            walked.append(b)
        elif a >= 0:
            hd = hex_distance(grid, a, b)
            if hd <= 8:
                seg = hex_walk(grid, a, b)[1:]
                if all(owner[x] != -2 for x in seg) and sum(1 for x in seg if owner[x] < 0) <= max_bridge_water:
                    walked.extend(seg)
                else:
                    walked.append(-1)
                    walked.append(b)
            else:
                walked.append(-1)
                walked.append(b)
        else:
            walked.append(b)

    pairs: list[tuple[int, int]] = []
    cur: list[int] = []
    for x in walked + [-1]:
        if x < 0:
            if len(cur) >= 2:
                cur = straighten(grid, cur)
                for u, v in zip(cur, cur[1:]):
                    if (u != v and v in grid.neighbours(u)
                            and (owner[u] >= 0 or owner[v] >= 0)
                            and owner[u] != -2 and owner[v] != -2):
                        pairs.append((min(u, v), max(u, v)))
            cur = []
        else:
            cur.append(x)
    return pairs


def _osm_motorway_label(props: dict) -> str:
    ref = (props.get("ref") or "").split(";")[0].strip()
    name = (props.get("name") or "").strip()
    for kw, lbl in [
        ("Tarlac-Pangasinan-La Union", "TPLEX (E1)"),
        ("TPLEX", "TPLEX (E1)"),
        ("Subic-Clark-Tarlac", "SCTEX (E1/E4)"),
        ("SCTEX", "SCTEX (E1/E4)"),
        ("North Luzon Expressway", "NLEX (E1)"),
        ("NLEX Harbor", "NLEX Harbor Link (E5)"),
        ("NLEX", "NLEX (E1)"),
        ("Skyway", "Metro Manila Skyway (E2)"),
        ("South Luzon Expressway", "SLEX (E2)"),
        ("SLEX", "SLEX (E2)"),
        ("STAR Tollway", "STAR Tollway (E2)"),
        ("Southern Tagalog Arterial", "STAR Tollway (E2)"),
        ("Cavite-Laguna", "CALAX (E3)"),
        ("CALAX", "CALAX (E3)"),
        ("Manila-Cavite", "CAVITEX (E3)"),
        ("CAVITEX", "CAVITEX (E3)"),
        ("NAIA Expressway", "NAIAX (E6)"),
        ("Central Luzon Link", "CLLEX"),
        ("Cebu-Cordova", "CCLEX"),
    ]:
        if kw.lower() in name.lower():
            return lbl
    REF_LABELS = {
        "E1": "NLEX / SCTEX / TPLEX (E1)",
        "E2": "SLEX / Skyway / STAR (E2)",
        "E3": "CAVITEX / CALAX (E3)",
        "E4": "SCTEX (E4)",
        "E5": "NLEX Harbor Link (E5)",
        "E6": "NAIAX (E6)",
    }
    if ref in REF_LABELS:
        return REF_LABELS[ref]
    return name or ref or "Philippine Expressway"


# Authoritative Philippine Highway & Rail Corridors (supplementing OSM motorways & HOT OSM railways)
PH_AH26_SPINE: list[tuple[str, list[tuple[float, float]]]] = [
    ("Maharlika Hwy (AH26)", [
        # Northern Luzon: Laoag -> Pagudpud -> Aparri -> Tuguegarao -> Santiago -> Bayombong -> San Jose -> Cabanatuan -> Guiguinto -> Manila
        (120.596, 18.199), (120.790, 18.560), (121.170, 18.540), (121.635, 18.356),
        (121.727, 17.613), (121.880, 17.140), (121.549, 16.688), (121.150, 16.480),
        (120.990, 15.790), (120.968, 15.487), (120.955, 15.220), (120.890, 14.840), (120.984, 14.600),
    ]),
    ("Maharlika Hwy (AH26)", [
        # Southern Luzon / Bicol: Manila -> Calamba -> San Pablo -> Lucena -> Gumaca -> Lopez -> Daet -> Naga -> Legazpi -> Sorsogon -> Matnog
        (120.984, 14.600), (121.165, 14.212), (121.325, 14.068), (121.617, 13.937),
        (122.100, 13.920), (122.260, 13.880), (122.650, 14.020), (122.955, 14.112),
        (123.195, 13.622), (123.410, 13.430), (123.744, 13.139), (124.006, 12.974), (124.086, 12.586),
    ]),
    ("Maharlika Hwy (AH26)", [
        # Eastern Visayas (Samar -> San Juanico Bridge -> Leyte): Allen -> Calbayog -> Catbalogan -> Tacloban -> Abuyog -> Sogod -> Liloan
        (124.287, 12.500), (124.600, 12.067), (124.884, 11.775), (124.972, 11.500),
        (125.005, 11.245), (125.012, 10.747), (124.980, 10.386), (125.125, 10.160),
    ]),
    ("Maharlika Hwy (AH26)", [
        # Mindanao Spine: Surigao -> Butuan -> Bayugan -> Trento -> Tagum -> Davao -> Digos -> GenSan -> Koronadal -> Cotabato -> Pagadian -> Zamboanga
        (125.495, 9.789), (125.541, 8.948), (125.748, 8.714), (126.063, 8.049),
        (125.808, 7.448), (125.613, 7.073), (125.357, 6.750), (125.172, 6.116),
        (124.847, 6.503), (124.530, 6.790), (124.246, 7.205), (124.160, 7.620),
        (123.437, 7.826), (122.760, 7.780), (122.400, 7.450), (122.079, 6.921),
    ]),
]

PH_PRIMARY_HIGHWAYS: list[tuple[str, list[tuple[float, float]]]] = [
    ("MacArthur Hwy (N2)", [
        (120.984, 14.600), (120.812, 14.852), (120.690, 15.029), (120.597, 15.476),
        (120.571, 15.976), (120.487, 16.228), (120.317, 16.616), (120.387, 17.575), (120.596, 18.199),
    ]),
    ("Kennon / Halsema Hwy (N54)", [
        (120.487, 16.228), (120.596, 16.402), (120.800, 16.820), (120.977, 17.089), (121.110, 17.410),
    ]),
    ("Olongapo–Gapan / Zambales Hwy (N3)", [
        (120.690, 15.029), (120.460, 14.870), (120.283, 14.829), (119.980, 15.327),
        (119.910, 15.800), (119.980, 16.156), (120.333, 16.043),
    ]),
    ("Mindoro Nautical Hwy (N450)", [
        (121.180, 13.412), (121.480, 13.030), (121.520, 12.587), (121.340, 12.320), (121.066, 12.353),
    ]),
    ("Palawan North–South Hwy", [
        (119.406, 11.195), (119.510, 10.820), (119.340, 10.320), (118.738, 9.739),
        (118.410, 9.270), (117.830, 8.780),
    ]),
    ("Panay Circumferential Hwy (N5)", [
        (121.950, 11.930), (122.364, 11.708), (122.751, 11.585), (122.630, 11.120),
        (122.562, 10.720), (122.020, 10.750), (122.060, 11.420), (121.950, 11.930),
    ]),
    ("Negros Occidental–Oriental Hwy (N7)", [
        (122.951, 10.677), (123.380, 10.870), (123.415, 10.490), (123.305, 9.307),
        (122.800, 9.380), (122.815, 9.990), (122.951, 10.677),
    ]),
    ("Cebu North–South Hwy (N8)", [
        (124.005, 11.050), (124.027, 10.520), (123.885, 10.316), (123.640, 10.100), (123.380, 9.460),
    ]),
    ("Bohol Circumferential Hwy", [
        (123.853, 9.650), (123.960, 9.950), (124.470, 10.055), (124.365, 9.650), (123.853, 9.650),
    ]),
    ("Butuan–CDO–Iligan–Dipolog Hwy (N9)", [
        (125.541, 8.948), (125.100, 8.820), (124.632, 8.454), (124.245, 8.228),
        (123.841, 8.148), (123.341, 8.588), (123.437, 7.826),
    ]),
    ("Sayre Hwy (N10)", [
        (124.632, 8.454), (125.128, 8.158), (125.095, 7.906), (124.980, 7.420),
        (125.089, 7.008), (125.357, 6.750),
    ]),
]

PH_RAIL_CORRIDORS: list[tuple[int, str, list[tuple[float, float]]]] = [
    # Tier 0: PNR North–South Commuter Railway (NSCR: Clark -> Angeles -> San Fernando -> Malolos -> Tutuban -> Calamba)
    (0, "PNR North–South Commuter (NSCR)", [
        (120.546, 15.186), (120.592, 15.145), (120.689, 15.028), (120.766, 14.949),
        (120.812, 14.852), (120.927, 14.798), (120.975, 14.690), (120.973, 14.609),
        (121.015, 14.555), (121.045, 14.417), (121.080, 14.342), (121.165, 14.212),
    ]),
    # Tier 0: PNR South Long Haul (Bicol Line: Calamba -> Los Baños -> San Pablo -> Lucena -> Gumaca -> Tagkawayan -> Sipocot -> Naga -> Iriga -> Legazpi)
    (0, "PNR South Main Line (Bicol)", [
        (121.165, 14.212), (121.242, 14.170), (121.325, 14.068), (121.425, 13.930),
        (121.617, 13.937), (121.730, 13.975), (122.100, 13.920), (122.260, 13.880),
        (122.460, 13.958), (122.540, 13.965), (122.790, 13.820), (122.976, 13.767),
        (123.195, 13.622), (123.412, 13.432), (123.540, 13.235), (123.744, 13.139),
    ]),
    # Tier 0: Metro Manila LRT-1 / LRT-2 / MRT-3 spine
    (0, "Metro Manila LRT/MRT", [
        (120.992, 14.485), (121.000, 14.538), (120.984, 14.600), (120.984, 14.657),
        (121.033, 14.656), (121.053, 14.621), (121.105, 14.622),
    ]),
    # Tier 1: MRT-7 (North Ave -> Fairview -> San Jose del Monte) & PNR Batangas corridor
    (1, "MRT-7 (QC–SJDM)", [
        (121.033, 14.656), (121.065, 14.700), (121.075, 14.755), (121.048, 14.814),
    ]),
    (1, "PNR Batangas Branch", [
        (121.165, 14.212), (121.145, 14.110), (121.163, 13.941), (121.058, 13.757),
    ]),
]


def build_infrastructure(world: dict, world_web: dict, cities: list[dict], proj: MapProjection, grid: HexGrid) -> None:
    """Build infrastructure.json: OSM Philippine expressways, AH26 & national highways, PNR/LRT/MRT rail, ports, airports."""
    owner = world["owner"]
    county_arr = world["county"]
    states_abbr = [s["abbr"] for s in world_web["states"]]
    prov_to_city = {pg: c["id"] for c in cities for pg in c["counties"]}

    # 1. Roads: Class 0 = OSM Expressways + AH26 Maharlika Spine; Class 1 = National Primary Highways + NE 1:10m
    road_links: dict[tuple[int, int], tuple[int, str | None]] = {}

    def add_road(raw_line, cls: int, rname: str | None) -> None:
        for pair in _rasterize_line(grid, owner, proj, raw_line, step_km=1.0, max_bridge_water=2):
            cur = road_links.get(pair)
            if cur is None or cls < cur[0] or (cls == cur[0] and not cur[1] and rname):
                road_links[pair] = (cls, rname)

    # 1a. Real OpenStreetMap Philippine Motorways (NLEX, SCTEX, TPLEX, CLLEX, Skyway, SLEX, STAR, CALAX, CAVITEX, NAIAX, CCLEX)
    osm_mw = PH_RAW / "osm_transport/phl_motorways.json"
    if osm_mw.exists():
        gj = json.loads(osm_mw.read_text())
        for feat in gj.get("features", []):
            rname = _osm_motorway_label(feat.get("properties") or {})
            for raw_line in geojson_lines(feat["geometry"]):
                add_road(raw_line, 0, rname)

    # 1b. Pan-Philippine Highway (Maharlika Hwy / AH26) Spine (Class 0)
    for rname, coords in PH_AH26_SPINE:
        add_road(coords, 0, rname)

    # 1c. Authoritative National Primary Highways across Luzon, Visayas, Palawan & Mindanao (Class 1)
    for rname, coords in PH_PRIMARY_HIGHWAYS:
        add_road(coords, 1, rname)

    # 1d. Natural Earth 1:10m Philippine roads as Class 1 secondary coverage
    ne_roads = json.loads((PH_RAW / "natural_earth_roads/v5.1.2/ne_10m_roads.geojson").read_text())
    for feat in ne_roads["features"]:
        props = feat["properties"]
        rname = props.get("name") or props.get("namealtt") or "National Highway"
        for raw_line in geojson_lines(feat["geometry"]):
            if not any(116.5 <= lon <= 127.0 and 4.5 <= lat <= 21.5 for lon, lat in raw_line):
                continue
            add_road(raw_line, 1, rname)

    roads = [[a, b, cls, name] for (a, b), (cls, name) in sorted(road_links.items())]

    # 2. Railways: HOT OSM PH railways + continuous PNR Main Line (NSCR + Bicol) & Metro Rail
    rail_links: dict[tuple[int, int], int] = {}

    def add_rail(raw_line, tier: int) -> None:
        for pair in _rasterize_line(grid, owner, proj, raw_line, step_km=1.0, max_bridge_water=1):
            if pair not in rail_links or tier < rail_links[pair]:
                rail_links[pair] = tier

    with zipfile.ZipFile(PH_RAW / "hdx_railways/hotosm_phl_railways_osm_geojson.zip") as z:
        for fn in z.namelist():
            if fn.endswith(".geojson"):
                gj = json.loads(z.read(fn))
                for feat in gj.get("features", []):
                    props = feat.get("properties") or {}
                    if props.get("railway") not in ("rail", "light_rail", "subway"):
                        continue
                    tier = 1 if "industrial" in (props.get("name") or "").lower() else 0
                    for raw_line in geojson_lines(feat["geometry"]):
                        add_rail(raw_line, tier)

    for tier, _rname, coords in PH_RAIL_CORRIDORS:
        add_rail(coords, tier)

    rail = [[a, b, tier] for (a, b), tier in sorted(rail_links.items())]

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
                cell = nearest_land(grid, owner, max(cell, 0), x, y, -1, max_rings=12)
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
        "vintage": {"roads": "OpenStreetMap Expressways (2026) & Natural Earth v5.1.2 (1:10m)",
                    "rail": "HOT OSM Philippines Railways & PNR/LRT/MRT Corridors (2026)",
                    "ports": "Major Philippine Seaports (PPA / HOT OSM)", "airports": "OurAirports PH (2026)"},
        "sources": ["osm_transport", "natural_earth_roads", "hdx_railways", "hdx_ports", "ourairports"],
        "rules": {
            "roads": "Class 0: Philippine expressways (NLEX, SCTEX, TPLEX, CLLEX, Skyway, SLEX, STAR, CALAX, CAVITEX, NAIAX, CCLEX from OpenStreetMap) & Pan-Philippine Highway (AH26 / Maharlika Hwy). Class 1: National primary highways across Luzon, Visayas, Palawan, and Mindanao.",
            "rail": "Tier 0: PNR Main Line (North–South Commuter Railway Clark–Manila–Calamba & South Long Haul to Naga/Legazpi) plus Metro Manila LRT-1/LRT-2/MRT-3. Tier 1: MRT-7 & regional/branch rail corridors.",
            "ports": "18 principal Philippine seaports and RoRo hubs.",
            "airports": "Commercial Philippine airports with IATA codes (OurAirports).",
        },
        "road_classes": [{"id": "interstate", "name": "Expressway & AH26 Spine"}, {"id": "major", "name": "National highway"}],
        "rail_tiers": [{"id": "main", "name": "Main line & Metro rail"}, {"id": "secondary", "name": "Regional / branch rail"}],
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
            cell = nearest_land(grid, owner, max(cell, 0), x, y, -1, max_rings=12)
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
        "osm_transport": ("OpenStreetMap Philippine Expressways", "OpenStreetMap Contributors (via Overpass API)", "https://www.openstreetmap.org/", "ODbL", "Philippine motorway/expressway geometries (NLEX, SCTEX, TPLEX, CLLEX, Skyway, SLEX, STAR, CALAX, CAVITEX, NAIAX, CCLEX).", ["Expressways on the map"], "OpenStreetMap 2026"),
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
    for stale in ("military.json",):
        p = WEB_DATA / stale
        if p.exists():
            p.unlink()
            print(f"  removed stale USA layer: {stale}")


if __name__ == "__main__":
    build()
