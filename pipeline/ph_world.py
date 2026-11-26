"""Build web/data/world.json and data/interim/_world.json for the Philippines.

Uses:
  - GADM 4.1 ADM1 (81 provinces + Metro Manila) grouped into 18 Philippine Regions
  - Natural Earth v5.1.2 (neighboring countries, lakes, rivers)
  - NOAA ETOPO1 (lat 4..22, lon 115..128, stride 2 ~3.7 km)
  - PSA Land Classification (2021) + 2024 Census urbanization for realistic per-hex land cover
"""

from __future__ import annotations

import csv
import json
import math
import re
import zipfile
from collections import Counter
from pathlib import Path

from .core.geo import MapProjection, Shape, ShapeIndex, geojson_lines, geojson_rings
from .core.hexgrid import HexGrid
from .core.paths import DATA
from .core.store import save_interim, write_web
from .build.world import (
    COAST,
    COAST_DEPTH_M,
    COVER,
    FLAT,
    FOREIGN,
    HILLS,
    LAKE,
    LAND,
    LAND_MIN_ALT_M,
    MOUNTAINS,
    OCEAN,
    PEAKS,
    PCT,
    TIDAL_SEA_SHARE,
    WATER,
    clip_polyline,
    is_valley_floor,
    join_lines,
    land_runs,
    majority,
    simplify,
)

PH_RAW = DATA / "philippines"

HEX_RADIUS_KM = 9.0
MARGIN_KM = 80.0

# 18 Philippine Regions (including Negros Island Region established 2024)
# fips = 2-digit PSGC region code, abbr = short map label, name = official name
REGIONS = [
    {"fips": "13", "abbr": "NCR", "name": "Metro Manila"},
    {"fips": "14", "abbr": "CAR", "name": "Cordillera"},
    {"fips": "01", "abbr": "I", "name": "Ilocos Region"},
    {"fips": "02", "abbr": "II", "name": "Cagayan Valley"},
    {"fips": "03", "abbr": "III", "name": "Central Luzon"},
    {"fips": "04", "abbr": "IV-A", "name": "CALABARZON"},
    {"fips": "17", "abbr": "IV-B", "name": "MIMAROPA"},
    {"fips": "05", "abbr": "V", "name": "Bicol Region"},
    {"fips": "06", "abbr": "VI", "name": "Western Visayas"},
    {"fips": "18", "abbr": "NIR", "name": "Negros Island"},
    {"fips": "07", "abbr": "VII", "name": "Central Visayas"},
    {"fips": "08", "abbr": "VIII", "name": "Eastern Visayas"},
    {"fips": "09", "abbr": "IX", "name": "Zamboanga Peninsula"},
    {"fips": "10", "abbr": "X", "name": "Northern Mindanao"},
    {"fips": "11", "abbr": "XI", "name": "Davao Region"},
    {"fips": "12", "abbr": "XII", "name": "SOCCSKSARGEN"},
    {"fips": "16", "abbr": "XIII", "name": "Caraga"},
    {"fips": "15", "abbr": "BARMM", "name": "Bangsamoro"},
]

# GADM NAME_1 -> (clean display name, 5-digit geoid = RRPP0, 2-digit region fips)
PROVINCE_META: dict[str, tuple[str, str, str]] = {
    "Abra": ("Abra", "14010", "14"),
    "AgusandelNorte": ("Agusan del Norte", "16020", "16"),
    "AgusandelSur": ("Agusan del Sur", "16030", "16"),
    "Aklan": ("Aklan", "06040", "06"),
    "Albay": ("Albay", "05050", "05"),
    "Antique": ("Antique", "06060", "06"),
    "Apayao": ("Apayao", "14810", "14"),
    "Aurora": ("Aurora", "03770", "03"),
    "Basilan": ("Basilan", "15070", "15"),
    "Bataan": ("Bataan", "03080", "03"),
    "Batanes": ("Batanes", "02090", "02"),
    "Batangas": ("Batangas", "04100", "04"),
    "Benguet": ("Benguet", "14110", "14"),
    "Biliran": ("Biliran", "08780", "08"),
    "Bohol": ("Bohol", "07120", "07"),
    "Bukidnon": ("Bukidnon", "10130", "10"),
    "Bulacan": ("Bulacan", "03140", "03"),
    "Cagayan": ("Cagayan", "02150", "02"),
    "CamarinesNorte": ("Camarines Norte", "05160", "05"),
    "CamarinesSur": ("Camarines Sur", "05170", "05"),
    "Camiguin": ("Camiguin", "10180", "10"),
    "Capiz": ("Capiz", "06190", "06"),
    "Catanduanes": ("Catanduanes", "05200", "05"),
    "Cavite": ("Cavite", "04210", "04"),
    "Cebu": ("Cebu", "07220", "07"),
    "CompostelaValley": ("Davao de Oro", "11820", "11"),
    "DavaoOriental": ("Davao Oriental", "11250", "11"),
    "DavaodelNorte": ("Davao del Norte", "11230", "11"),
    "DavaodelSur": ("Davao del Sur", "11240", "11"),
    "DinagatIslands": ("Dinagat Islands", "16850", "16"),
    "EasternSamar": ("Eastern Samar", "08260", "08"),
    "Guimaras": ("Guimaras", "06790", "06"),
    "Ifugao": ("Ifugao", "14270", "14"),
    "IlocosNorte": ("Ilocos Norte", "01280", "01"),
    "IlocosSur": ("Ilocos Sur", "01290", "01"),
    "Iloilo": ("Iloilo", "06300", "06"),
    "Isabela": ("Isabela", "02310", "02"),
    "Kalinga": ("Kalinga", "14320", "14"),
    "LaUnion": ("La Union", "01330", "01"),
    "Laguna": ("Laguna", "04340", "04"),
    "LanaodelNorte": ("Lanao del Norte", "10350", "10"),
    "LanaodelSur": ("Lanao del Sur", "15360", "15"),
    "Leyte": ("Leyte", "08370", "08"),
    "Maguindanao": ("Maguindanao", "15380", "15"),
    "Marinduque": ("Marinduque", "17400", "17"),
    "Masbate": ("Masbate", "05410", "05"),
    "MetropolitanManila": ("Metro Manila", "13390", "13"),
    "MisamisOccidental": ("Misamis Occidental", "10420", "10"),
    "MisamisOriental": ("Misamis Oriental", "10430", "10"),
    "MountainProvince": ("Mountain Province", "14440", "14"),
    "NegrosOccidental": ("Negros Occidental", "18450", "18"),
    "NegrosOriental": ("Negros Oriental", "18460", "18"),
    "NorthCotabato": ("Cotabato", "12470", "12"),
    "NorthernSamar": ("Northern Samar", "08480", "08"),
    "NuevaEcija": ("Nueva Ecija", "03490", "03"),
    "NuevaVizcaya": ("Nueva Vizcaya", "02500", "02"),
    "OccidentalMindoro": ("Occidental Mindoro", "17510", "17"),
    "OrientalMindoro": ("Oriental Mindoro", "17520", "17"),
    "Palawan": ("Palawan", "17530", "17"),
    "Pampanga": ("Pampanga", "03540", "03"),
    "Pangasinan": ("Pangasinan", "01550", "01"),
    "Quezon": ("Quezon", "04560", "04"),
    "Quirino": ("Quirino", "02570", "02"),
    "Rizal": ("Rizal", "04580", "04"),
    "Romblon": ("Romblon", "17590", "17"),
    "Samar": ("Samar", "08600", "08"),
    "Sarangani": ("Sarangani", "12800", "12"),
    "Siquijor": ("Siquijor", "18610", "18"),
    "Sorsogon": ("Sorsogon", "05620", "05"),
    "SouthCotabato": ("South Cotabato", "12630", "12"),
    "SouthernLeyte": ("Southern Leyte", "08640", "08"),
    "SultanKudarat": ("Sultan Kudarat", "12650", "12"),
    "Sulu": ("Sulu", "15660", "15"),
    "SurigaodelNorte": ("Surigao del Norte", "16670", "16"),
    "SurigaodelSur": ("Surigao del Sur", "16680", "16"),
    "Tarlac": ("Tarlac", "03690", "03"),
    "Tawi-Tawi": ("Tawi-Tawi", "15700", "15"),
    "Zambales": ("Zambales", "03710", "03"),
    "ZamboangaSibugay": ("Zamboanga Sibugay", "09830", "09"),
    "ZamboangadelNorte": ("Zamboanga del Norte", "09720", "09"),
    "ZamboangadelSur": ("Zamboanga del Sur", "09730", "09"),
}


def norm_name(s: str) -> str:
    s = re.sub(r"^\.+", "", s.strip())
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return s


def classify_relief_ph(mean: float, std: float) -> int:
    """Tropical archipelago relief thresholds (tuned for Cordillera, Sierra Madre, Mt. Apo, etc.)."""
    if mean >= 1700:
        return PEAKS
    if std >= 200 or mean >= 1000:
        return MOUNTAINS
    if std >= 65 or mean >= 350:
        return HILLS
    return FLAT


def load_psa_land_forest() -> dict[str, float]:
    """Return normalized province name -> forestland share (0..1) from PSA Land Classification 2021."""
    path = PH_RAW / "psa_land/land_classification_region_province_2021.csv"
    out: dict[str, float] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 4:
                continue
            try:
                total = float(row[1])
                forest = float(row[3])
                if total > 0:
                    out[norm_name(row[0])] = max(0.0, min(1.0, forest / total))
            except ValueError:
                continue
    return out


def load_psa_urban_pct() -> dict[str, float]:
    """Return normalized province name -> urban share (0..1) from 2024 Census."""
    path = PH_RAW / "psa_census_2024/urban_pop_2024.csv"
    out: dict[str, float] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 4:
                continue
            try:
                pct = float(row[3])
                out[norm_name(row[0])] = max(0.0, min(1.0, pct / 100.0))
            except ValueError:
                continue
    return out


def build() -> None:
    print("Building Philippines world.json...")
    # 1. Load GADM ADM1 provinces
    with zipfile.ZipFile(PH_RAW / "gadm_boundaries/gadm41_PHL_1.json.zip") as z:
        gadm1 = json.loads(z.read(z.namelist()[0]))

    counties = []
    region_rings: dict[str, list] = {r["fips"]: [] for r in REGIONS}
    for feat in gadm1["features"]:
        raw_name = feat["properties"]["NAME_1"]
        name, geoid, reg_fips = PROVINCE_META[raw_name]
        rings = geojson_rings(feat["geometry"])
        counties.append({"geoid": geoid, "name": name, "state_fips": reg_fips, "rings": rings})
        region_rings[reg_fips].extend(rings)
    counties.sort(key=lambda c: c["geoid"])

    states = [{**r, "rings": region_rings[r["fips"]]} for r in REGIONS]

    # 2. Fit Albers projection to Philippines
    probe = MapProjection(0, 0, 0)
    xs, ys = [], []
    for c in counties:
        for ring in c["rings"]:
            for lon, lat in ring:
                x, y = probe._albers(lon, lat)
                xs.append(x)
                ys.append(y)
    proj = MapProjection(min(xs), max(ys), MARGIN_KM)
    width = max(xs) - min(xs) + 2 * MARGIN_KM
    height = max(ys) - min(ys) + 2 * MARGIN_KM
    grid = HexGrid.covering(width, height, HEX_RADIUS_KM)
    print(f"  grid {grid.cols}×{grid.rows} = {grid.size} hexes of {HEX_RADIUS_KM:g} km ({width:.0f}×{height:.0f} km)")

    def make_shape(rings):
        return Shape([[proj(lon, lat) for lon, lat in r] for r in rings])

    state_idx = ShapeIndex([make_shape(s["rings"]) for s in states], cell_km=80)
    county_idx = ShapeIndex([make_shape(c["rings"]) for c in counties], cell_km=40)

    # 3. Natural Earth: neighboring countries, lakes, rivers (filtered near PH bbox)
    def in_ph_window(rings_or_line, is_rings=True):
        pts = [p for r in rings_or_line for p in r] if is_rings else rings_or_line
        return any(112 <= lon <= 130 and 1 <= lat <= 25 for lon, lat in pts)

    ne_dir = PH_RAW / "natural_earth/v5.1.2"
    foreign_shapes = []
    for feat in json.loads((ne_dir / "ne_50m_admin_0_countries.geojson").read_text())["features"]:
        iso = feat["properties"].get("ISO_A3") or feat["properties"].get("ADM0_A3") or ""
        if iso == "PHL":
            continue
        rings = geojson_rings(feat["geometry"])
        if in_ph_window(rings, True):
            foreign_shapes.append(make_shape(rings))
    foreign_idx = ShapeIndex(foreign_shapes, cell_km=120)

    lake_shapes = []
    for feat in json.loads((ne_dir / "ne_50m_lakes.geojson").read_text())["features"]:
        rings = geojson_rings(feat["geometry"])
        if in_ph_window(rings, True):
            lake_shapes.append(make_shape(rings))
    lake_idx = ShapeIndex(lake_shapes, cell_km=40)

    # 4. ETOPO1 elevation & bathymetry
    stats_all = [[0, 0.0, 0.0] for _ in range(grid.size)]
    stats_land = [[0, 0.0, 0.0] for _ in range(grid.size)]
    samples_land: list[list[float]] = [[] for _ in range(grid.size)]
    below_sea = [0] * grid.size
    below_sum = [0.0] * grid.size

    with open(PH_RAW / "etopo/etopo_phl_lat4_22_lon115_128_stride2.csv", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # header
        next(reader)  # units
        for row in reader:
            if len(row) < 3:
                continue
            lat, lon, alt = float(row[0]), float(row[1]), float(row[2])
            i = grid.cell_at(*proj(lon, lat))
            if i < 0:
                continue
            if alt < 0:
                below_sea[i] += 1
                below_sum[i] += alt
            for st in (stats_all[i], stats_land[i]) if alt >= LAND_MIN_ALT_M else (stats_all[i],):
                st[0] += 1
                st[1] += alt
                st[2] += alt * alt
            if alt >= LAND_MIN_ALT_M:
                samples_land[i].append(alt)

    def sea_share(i):
        return below_sea[i] / stats_all[i][0] if stats_all[i][0] else 0.0

    def mean_std(st):
        n, s1, s2 = st
        if not n:
            return None, None
        m = s1 / n
        return m, math.sqrt(max(0.0, s2 / n - m * m))

    # 5. Ownership via 7 sample points per hex (for small islands, >= 2/7 or centre on PH land keeps islands crisp)
    offsets = [(0.0, 0.0)] + [
        (0.6 * HEX_RADIUS_KM * math.cos(math.radians(60 * k - 30)), 0.6 * HEX_RADIUS_KM * math.sin(math.radians(60 * k - 30)))
        for k in range(6)
    ]
    owner = [WATER] * grid.size
    water = [OCEAN] * grid.size
    for i in range(grid.size):
        cx, cy = grid.center(*grid.col_row(i))
        kinds = []
        for dx, dy in offsets:
            x, y = cx + dx, cy + dy
            if lake_idx.locate(x, y) >= 0:
                kinds.append("lake")
            elif (s := state_idx.locate(x, y)) >= 0:
                kinds.append(s)
            elif foreign_idx.locate(x, y) >= 0:
                kinds.append(FOREIGN)
            else:
                kinds.append("sea")
        land = [k for k in kinds if not isinstance(k, str)]
        ph_pts = [k for k in land if k >= 0]
        m_all, _ = mean_std(stats_all[i])
        # Keep narrow Philippine islands visible if >=3 of 7 samples are PH land OR centre is PH land
        is_ph_island = len(ph_pts) >= 3 or (isinstance(kinds[0], int) and kinds[0] >= 0)
        tidal = kinds[0] == "sea" and m_all is not None and (m_all <= -5 or sea_share(i) >= 0.7)
        if (is_ph_island or len(land) * 2 > len(kinds)) and not tidal:
            if ph_pts:
                owner[i] = majority(ph_pts, kinds[0] if isinstance(kinds[0], int) and kinds[0] >= 0 else ph_pts[0])
            else:
                owner[i] = FOREIGN
            water[i] = LAND
        else:
            wet = [k for k in kinds if isinstance(k, str)]
            above_sea = m_all is not None and m_all > 0
            lakeish = "lake" in wet and (above_sea or majority(wet, kinds[0] if kinds[0] in wet else "lake") == "lake")
            water[i] = LAKE if lakeish else OCEAN

    # 6. Counties (Provinces) for PH cells
    county = [-1] * grid.size
    county_samples: list[list[int]] = [[] for _ in range(grid.size)]
    for i in range(grid.size):
        if owner[i] >= 0:
            cx, cy = grid.center(*grid.col_row(i))
            county_samples[i] = [county_idx.locate(cx + dx, cy + dy) for dx, dy in offsets]
    for i in range(grid.size):
        smp = county_samples[i]
        if smp and max(smp) < 0:
            nb = [k for j in grid.neighbours(i) if owner[j] == owner[i] for k in county_samples[j] if k >= 0]
            if nb:
                smp[0] = Counter(nb).most_common(1)[0][0]
    county_state = [c["state_fips"] for c in counties]
    for i, smp in enumerate(county_samples):
        valid = [k for k in smp if k >= 0]
        if not valid:
            continue
        same = [k for k in valid if county_state[k] == states[owner[i]]["fips"]]
        pool = same or valid
        county[i] = smp[0] if smp[0] in pool else Counter(pool).most_common(1)[0][0]

    # 7. Elevation, relief, coast/ocean
    elev = [0] * grid.size
    relief = [FLAT] * grid.size
    elev_max = [0] * grid.size
    for i in range(grid.size):
        if water[i] == LAND:
            m, sd = mean_std(stats_land[i])
            if m is None:
                m, sd = mean_std(stats_all[i])
                m = None if m is None else max(m, 0.0)
            if m is None:
                continue
            elev[i] = round(m)
            relief[i] = classify_relief_ph(max(m, 0.0), sd or 0.0)
            if relief[i] in (HILLS, MOUNTAINS) and is_valley_floor(samples_land[i]):
                relief[i] -= 1
            if samples_land[i]:
                elev_max[i] = max(0, round(max(samples_land[i])))
        else:
            m, _ = mean_std(stats_all[i])
            if m is None:
                elev[i] = -4000
            elif water[i] != LAKE and below_sea[i]:
                elev[i] = round(below_sum[i] / below_sea[i])
            else:
                elev[i] = min(round(m), 0)
            if water[i] == OCEAN:
                near_land = any(water[j] == LAND for j in grid.neighbours(i))
                water[i] = COAST if near_land or elev[i] > COAST_DEPTH_M else OCEAN

    # 8. Land cover from PSA provincial forest/urban data + elevation/relief
    # Cover ids: 1 deciduous, 2 evergreen, 3 mixed, 4 scrub, 5 grass, 6 pasture, 7 cropland, 8 wetland, 9 urban
    forest_by_prov = load_psa_land_forest()
    urban_by_prov = load_psa_urban_pct()
    cover = [0] * grid.size
    pct = {k: [0] * grid.size for k in PCT}
    for i in range(grid.size):
        if owner[i] < 0:
            continue
        cidx = county[i]
        pname = norm_name(counties[cidx]["name"]) if cidx >= 0 else ""
        is_ncr = states[owner[i]]["fips"] == "13"
        f_base = forest_by_prov.get(pname, 0.52)
        u_base = urban_by_prov.get(pname, 0.35)
        e = elev[i]
        rl = relief[i]

        if is_ncr:
            dev, fr, cr, gr, wt = 78, 6, 4, 8, 4
            cid = 9  # urban
        elif rl >= MOUNTAINS or e >= 600:
            fr = min(92, round(60 + f_base * 30))
            gr = max(4, round((100 - fr) * 0.5))
            cr = max(0, 100 - fr - gr - 2)
            dev, wt = 2, 0
            cid = 2 if e >= 900 else 3  # evergreen pine/montane or tropical mixed forest
        elif rl == HILLS or e >= 220:
            fr = min(82, round(35 + f_base * 45))
            cr = round((100 - fr) * 0.45)
            gr = round((100 - fr) * 0.35)
            dev = min(20, round(u_base * 18))
            wt = max(0, 100 - fr - cr - gr - dev)
            cid = 1 if fr >= cr else 7  # tropical broadleaf forest vs upland crop
        else:
            # Flat lowlands: rice/crop plains, coastal wetlands, or urban centers
            dev = min(45, round(u_base * 32)) if u_base > 0.65 else min(22, round(u_base * 18))
            fr = min(55, round(f_base * 45))
            wt = 14 if (e <= 12 and any(water[j] in (COAST, LAKE) for j in grid.neighbours(i))) else 4
            rem = max(0, 100 - dev - fr - wt)
            cr = round(rem * 0.65)
            gr = rem - cr
            if dev >= 30:
                cid = 9  # urban
            elif cr >= fr and cr >= wt:
                cid = 7  # cropland (rice/coconut/sugar plains)
            elif wt >= fr:
                cid = 8  # coastal wetland / mangrove
            else:
                cid = 1  # tropical forest

        pct["forest"][i] = fr
        pct["crop"][i] = cr
        pct["pasture"][i] = gr // 2
        pct["grass"][i] = gr - gr // 2
        pct["shrub"][i] = 0
        pct["wetland"][i] = wt
        pct["developed"][i] = dev
        cover[i] = cid

    # 9. Rivers from Natural Earth
    def on_land(pt):
        c = grid.cell_at(*pt)
        return c >= 0 and water[c] == LAND

    by_river: dict[tuple, list] = {}
    for feat in json.loads((ne_dir / "ne_50m_rivers_lake_centerlines.geojson").read_text())["features"]:
        props = feat["properties"]
        rank = int(props.get("scalerank") or 6)
        name = props.get("name") or ""
        for raw_line in geojson_lines(feat["geometry"]):
            if not in_ph_window(raw_line, False):
                continue
            line = [proj(lon, lat) for lon, lat in raw_line]
            if (props.get("featurecla") or "").startswith("Lake Centerline"):
                parts = [run for p in clip_polyline(line, width, height) for run in land_runs(p, on_land)]
            else:
                parts = clip_polyline(line, width, height)
            by_river.setdefault((name, rank), []).extend(parts)

    rivers = []
    for (name, rank), parts in by_river.items():
        for piece in join_lines(parts):
            if sum(math.dist(a, b) for a, b in zip(piece, piece[1:])) < 1.0:
                continue
            pts = simplify(piece, 2.0)
            if len(pts) >= 2:
                rivers.append({"name": name, "rank": rank, "pts": [round(v, 1) for p in pts for v in p]})

    water_table = [
        {"id": LAND, "name": "Land", "rule": "Philippine archipelago land (GADM 4.1) or neighboring countries (Natural Earth)."},
        {"id": OCEAN, "name": "Ocean", "color": "#1b4266", "rule": "Sea deeper than 200 m and not next to land."},
        {"id": COAST, "name": "Coast", "color": "#2d6d97", "rule": "Sea next to a land hex, or shallower than 200 m (ETOPO1)."},
        {"id": LAKE, "name": "Lake", "color": "#3a80b0", "rule": "Inland lakes (Laguna de Bay, Lake Lanao, Taal Lake)."},
    ]
    relief_table = [
        {"id": FLAT, "name": "Flat", "rule": "Lowland plains and coastal flats (std < 65 m, mean < 350 m)."},
        {"id": HILLS, "name": "Hills", "rule": "Rolling uplands (std ≥ 65 m or mean ≥ 350 m)."},
        {"id": MOUNTAINS, "name": "Mountains", "rule": "Mountain ranges (std ≥ 200 m or mean ≥ 1,000 m)."},
        {"id": PEAKS, "name": "Peaks", "rule": "High peaks (mean ≥ 1,700 m: Cordillera, Mt. Apo, Mt. Kitanglad)."},
    ]
    cover_table = [{"id": 0, "key": "none", "name": "None", "rule": "Water or foreign land."}] + [
        {"id": cid, "key": key, "name": name, "color": color, "nlcd": codes, "rule": rule}
        for cid, key, name, color, codes, rule in COVER
    ]
    tables = {
        "water": water_table,
        "relief": relief_table,
        "cover": cover_table,
        "notes": {
            "water": "Philippines archipelago hexes (9 km radius) from GADM 4.1 boundaries and NOAA ETOPO1 bathymetry.",
            "relief": "From NOAA ETOPO1 (~3.7 km grid) tuned for Philippine mountain ranges.",
            "cover": "Modelled per hex from PSA Land Classification (2021) provincial forest shares, 2024 Census urbanization, and ETOPO1 relief.",
        },
    }

    ph_hexes = sum(1 for o in owner if o >= 0)
    foreign_hexes = sum(1 for o in owner if o == FOREIGN)
    print(f"  {ph_hexes} Philippine land hexes, {foreign_hexes} foreign land hexes, {len(rivers)} rivers")

    write_web(
        "world.json",
        {
            "version": 2,
            "grid": grid.to_dict(),
            "width": round(width, 1),
            "height": round(height, 1),
            "margin": MARGIN_KM,
            "owner": owner,
            "county": county,
            "elev": elev,
            "elev_max": elev_max,
            "water": water,
            "relief": relief,
            "cover": cover,
            "pct": pct,
            "tables": tables,
            "states": [{"fips": s["fips"], "abbr": s["abbr"], "name": s["name"]} for s in states],
            "counties": [{"geoid": c["geoid"], "name": c["name"]} for c in counties],
            "rivers": rivers,
        },
    )
    save_interim(
        "_world",
        {
            "grid": grid.to_dict(),
            "projection": proj.to_dict(),
            "owner": owner,
            "county": county,
            "county_samples": county_samples,
            "states": [s["fips"] for s in states],
            "counties": [c["geoid"] for c in counties],
            "water": water,
            "cover": cover,
            "pct": pct,
        },
    )


if __name__ == "__main__":
    build()
