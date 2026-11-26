"""Build states.json, cities.json, nation.json, counties.json, and lenses.json for the Philippines."""

from __future__ import annotations

import csv
import json
import math
import re
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from .build.cities import PEERS, RANK_KEYS, add_ranks, free_cell, nearest_land
from .build.states import PCT_KEYS, energy_by_state, label_anchor, land_shares
from .core.geo import MapProjection, geojson_rings
from .core.hexgrid import HexGrid
from .core.paths import DATA
from .core.store import load_interim, write_web
from .ph_world import PROVINCE_META, REGIONS, norm_name

PH_RAW = DATA / "philippines"

YIELDS = [
    {"key": "food", "icon": "y-food", "emoji": "🌾", "label": "Food", "color": "#7fbf3f", "unit": "workers",
     "description": "Estimated workers in agriculture, forestry, and fishing (scaled from PSA LFS & GRDP/PPA sector shares)."},
    {"key": "production", "icon": "y-prod", "emoji": "⚙️", "label": "Production", "color": "#d97b29", "unit": "workers",
     "description": "Estimated workers in manufacturing, construction, mining, and utilities (PSA LFS & GRDP/PPA)."},
    {"key": "gold", "icon": "y-gold", "emoji": "💰", "label": "Gold", "color": "#e8c03a", "unit": "usd",
     "description": "Gross Domestic Product in current Philippine Pesos (PSA GRDP & Provincial Product Accounts, 2024–2025)."},
    {"key": "science", "icon": "y-sci", "emoji": "🔬", "label": "Science", "color": "#4da6e8", "unit": "workers",
     "description": "Estimated workers in IT-BPM, information & communications, professional services, and education."},
    {"key": "culture", "icon": "y-cul", "emoji": "🎭", "label": "Culture", "color": "#b46be8", "unit": "workers",
     "description": "Estimated workers in tourism, accommodation, arts, entertainment, and recreation."},
]

INDUSTRIES = {
    "agriculture": "Agriculture, forestry & fishing",
    "industry": "Manufacturing, construction & utilities",
    "services": "Trade, finance, IT-BPM & services",
}

GDP_INDUSTRIES = {
    "agriculture": "Agriculture, Forestry, and Fishing",
    "industry": "Industry",
    "services": "Services",
}

# Region name aliases found in PSA CSVs -> 2-digit fips (note: norm_name strips parentheticals!)
REGION_ALIASES = {
    "nationalcapitalregion": "13",
    "ncr": "13",
    "cordilleraadministrativeregion": "14",
    "car": "14",
    "regioni": "01",
    "ilocosregion": "01",
    "regionii": "02",
    "cagayanvalley": "02",
    "regioniii": "03",
    "centralluzon": "03",
    "regioniva": "04",
    "calabarzon": "04",
    "mimaroparegion": "17",
    "regionivb": "17",
    "mimaropa": "17",
    "regionv": "05",
    "bicolregion": "05",
    "regionvi": "06",
    "westernvisayas": "06",
    "negrosislandregion": "18",
    "nir": "18",
    "regionvii": "07",
    "centralvisayas": "07",
    "regionviii": "08",
    "easternvisayas": "08",
    "regionix": "09",
    "zamboangapeninsula": "09",
    "regionx": "10",
    "northernmindanao": "10",
    "regionxi": "11",
    "davaoregion": "11",
    "regionxii": "12",
    "soccsksargen": "12",
    "regionxiii": "16",
    "caraga": "16",
    "bangsamoroautonomousregioninmuslimmindanao": "15",
    "barmm": "15",
    "armm": "15",
}


def parse_num(v: str | None) -> float | None:
    if v is None:
        return None
    s = v.strip().replace(",", "")
    if not s or s in (".", "..", "-", "/s", "NA", "N/A", "a/"):
        return None
    try:
        x = float(s)
        return x if math.isfinite(x) else None
    except ValueError:
        return None


def clean_geo(s: str) -> str:
    s = re.sub(r"^\.+", "", s.strip())
    s = re.sub(r"\s+(r1|a|a/|1/|2/|3/|,?\s*\d+/)[\s,\d/a-z]*$", "", s, flags=re.I)
    s = re.sub(r"\s*\(excluding[^)]*\)", "", s, flags=re.I)
    return s.strip()


def match_region(raw: str) -> str | None:
    k = norm_name(clean_geo(raw))
    if k in REGION_ALIASES:
        return REGION_ALIASES[k]
    for alias, fips in REGION_ALIASES.items():
        if alias in k and len(alias) >= 6:
            return fips
    return None


def load_census_2024() -> tuple[dict, dict, dict]:
    """Return (by_norm_name -> {pop, hh, hh_size}, growth_by_norm -> {p2010,p2015,p2020,p2024}, area_by_norm -> km2)."""
    pop_map, growth_map, area_map = {}, {}, {}

    with open(PH_RAW / "psa_census_2024/pop_region_province_huc_2024.csv", encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            if len(row) < 5:
                continue
            p = parse_num(row[1])
            if p:
                pop_map[norm_name(clean_geo(row[0]))] = {
                    "pop": int(round(p)),
                    "hh": int(round(parse_num(row[3]) or 0)),
                    "raw": clean_geo(row[0]),
                }

    with open(PH_RAW / "psa_census_2024/pop_growth_2010_2024.csv", encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            if len(row) < 5:
                continue
            p2010, p2015, p2020, p2024 = [parse_num(row[i]) for i in (1, 2, 3, 4)]
            if p2024:
                growth_map[norm_name(clean_geo(row[0]))] = {
                    "2010": int(round(p2010)) if p2010 else None,
                    "2015": int(round(p2015)) if p2015 else None,
                    "2020": int(round(p2020)) if p2020 else None,
                    "2024": int(round(p2024)),
                }

    with open(PH_RAW / "psa_census_2024/pop_area_density_2015_2024.csv", encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            if len(row) < 5:
                continue
            area = parse_num(row[4])
            if area and area > 0:
                area_map[norm_name(clean_geo(row[0]))] = area

    return pop_map, growth_map, area_map


def load_grdp() -> tuple[dict, dict]:
    """Return (nation_dict, region_fips -> {total, agriculture, industry, services}) in PHP (2024 current prices)."""
    nation = {}
    by_reg = {}
    with open(PH_RAW / "psa_gdp_grdp/grdp_current_by_region_industry_2018_2025.csv", encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        hdr = next(r)
        col = {h.strip(): i for i, h in enumerate(hdr)}
        # Use 2024 to align with 2024 Census year
        c_tot = col.get("Total 2024")
        c_ag = col.get("Agriculture, Forestry, and Fishing 2024")
        c_in = col.get("Industry 2024")
        c_sv = col.get("Services 2024")
        for row in r:
            if not row:
                continue
            raw = clean_geo(row[0])
            tot = parse_num(row[c_tot]) if c_tot is not None and c_tot < len(row) else None
            if tot is None:
                continue
            rec = {
                "total": round(tot * 1000.0),  # PSA GRDP is in thousand PHP
                "agriculture": round((parse_num(row[c_ag]) or 0) * 1000.0),
                "industry": round((parse_num(row[c_in]) or 0) * 1000.0),
                "services": round((parse_num(row[c_sv]) or 0) * 1000.0),
            }
            if norm_name(raw) == "philippines":
                nation = rec
            else:
                fips = match_region(raw)
                if fips:
                    by_reg[fips] = rec
    return nation, by_reg


def load_ppa() -> dict[str, dict]:
    """Return norm_name -> {total, agriculture, industry, services} in PHP (2024 current prices)."""
    out: dict[str, dict] = {}
    with open(PH_RAW / "psa_gdp_ppa/gdp_by_province_huc.csv", encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        hdr = next(r)
        c2024 = next((i for i, h in enumerate(hdr) if "Current Prices 2024" in h), None)
        for row in r:
            if not row or c2024 is None or c2024 >= len(row):
                continue
            v = parse_num(row[c2024])
            if v is not None:
                k = norm_name(clean_geo(row[0]))
                out[k] = {"total": round(v * 1000.0)}

    with open(PH_RAW / "psa_gdp_ppa/gdp_by_industry_province_huc.csv", encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        hdr = next(r)
        c2024 = next((i for i, h in enumerate(hdr) if "Current Prices 2024" in h), None)
        for row in r:
            if len(row) < 3 or c2024 is None or c2024 >= len(row):
                continue
            sec = row[0].strip().lower()
            k = norm_name(clean_geo(row[1]))
            v = parse_num(row[c2024])
            if v is None or k not in out:
                continue
            if "agriculture" in sec:
                out[k]["agriculture"] = round(v * 1000.0)
            elif sec == "industry":
                out[k]["industry"] = round(v * 1000.0)
            elif sec == "services":
                out[k]["services"] = round(v * 1000.0)
    return out


def load_fies_and_poverty() -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Return (median_income_php, poverty_pct_0_1, internet_by_region_0_1)."""
    inc: dict[str, float] = {}
    with open(PH_RAW / "psa_fies_2023/median_family_income_expenditure_2023.csv", encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            if len(row) >= 3 and "all income" in row[1].lower():
                v = parse_num(row[2])
                if v is not None:
                    inc[norm_name(clean_geo(row[0]))] = round(v * 1000.0)

    pov: dict[str, float] = {}
    with open(PH_RAW / "psa_poverty/poverty_population_region_province_huc.csv", encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        hdr = next(r)
        c2023 = next((i for i, h in enumerate(hdr) if "Poverty Incidence among Population (%) 2023" in h), None)
        for row in r:
            if not row or c2023 is None or c2023 >= len(row):
                continue
            v = parse_num(row[c2023])
            if v is not None:
                pov[norm_name(clean_geo(row[0]))] = round(max(0.0, min(1.0, v / 100.0)), 4)

    inet: dict[str, float] = {}
    with open(PH_RAW / "psa_digital/internet_use_by_region_2024.csv", encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            if len(row) >= 3:
                v = parse_num(row[2])
                if v is not None:
                    fips = match_region(row[0])
                    if fips:
                        inet[fips] = round(v / 100.0, 4)
                    elif norm_name(row[0]) == "philippines":
                        inet["PH"] = round(v / 100.0, 4)

    return inc, pov, inet


def load_urban_by_norm() -> dict[str, float]:
    out: dict[str, float] = {}
    with open(PH_RAW / "psa_census_2024/urban_pop_2024.csv", encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            if len(row) >= 4:
                v = parse_num(row[3])
                if v is not None:
                    out[norm_name(clean_geo(row[0]))] = round(max(0.0, min(1.0, v / 100.0)), 4)
    return out


def load_hazard_counts(world_counties: list[dict], county_idx: list[int], grid: HexGrid, proj: MapProjection) -> tuple[dict[str, int], dict[str, int]]:
    """Count distinct IBTrACS typhoons (1980+) and USGS M>=5 earthquakes (1980+) per province geoid."""
    typhoons: dict[str, set[str]] = defaultdict(set)
    with open(PH_RAW / "ibtracs_typhoons/ibtracs_wp_par_since1980.csv", encoding="utf-8", errors="replace") as f:
        r = csv.DictReader(f)
        for row in r:
            sid = row.get("SID")
            try:
                lat, lon = float(row["LAT"]), float(row["LON"])
            except (ValueError, KeyError):
                continue
            cell = grid.cell_at(*proj(lon, lat))
            if cell < 0:
                continue
            # A typhoon impacts nearby provinces within ~120 km (2 hex rings + centre)
            for c in [cell, *grid.neighbours(cell)]:
                ci = county_idx[c]
                if ci >= 0:
                    typhoons[world_counties[ci]["geoid"]].add(sid)

    quakes: dict[str, int] = Counter()
    with open(PH_RAW / "usgs_earthquakes/phl_earthquakes_m5_since1980.csv", encoding="utf-8", errors="replace") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                lat, lon = float(row["latitude"]), float(row["longitude"])
            except (ValueError, KeyError):
                continue
            cell = grid.cell_at(*proj(lon, lat))
            if cell < 0:
                continue
            seen = set()
            for c in [cell, *grid.neighbours(cell)]:
                ci = county_idx[c]
                if ci >= 0:
                    seen.add(world_counties[ci]["geoid"])
            for g in seen:
                quakes[g] += 1

    return {g: len(s) for g, s in typhoons.items()}, dict(quakes)


def make_econ(pop: int, gdp_rec: dict | None, med_inc: float | None, unemp: float = 0.039) -> dict:
    """Build standard economic profile dict matching docs/DATA_CONTRACTS.md."""
    employed = round(pop * 0.43)  # PH national employment-to-population ratio (~43%)
    gdp_tot = gdp_rec.get("total") if gdp_rec else None
    ag = gdp_rec.get("agriculture") if gdp_rec else None
    ind = gdp_rec.get("industry") if gdp_rec else None
    srv = gdp_rec.get("services") if gdp_rec else None

    if gdp_tot and ag is not None and ind is not None and srv is not None and (ag + ind + srv) > 0:
        s_sum = ag + ind + srv
        w_ag = ag / s_sum
        w_in = ind / s_sum
        w_sv = srv / s_sum
    else:
        w_ag, w_in, w_sv = 0.22, 0.19, 0.59  # PH national average shares

    # Weight agriculture employment slightly higher than GVA share (realistic labor intensity)
    emp_ag = round(employed * min(0.65, w_ag * 1.45))
    emp_in = round(employed * min(0.45, w_in * 0.85))
    emp_sv = max(0, employed - emp_ag - emp_in)

    yields = {
        "food": emp_ag,
        "production": emp_in,
        "gold": gdp_tot,
        "science": round(emp_sv * 0.24),
        "culture": round(emp_sv * 0.14),
    }
    gdp_by_ind = {"agriculture": ag, "industry": ind, "services": srv} if (ag is not None and ind is not None and srv is not None) else None
    out = {
        "population": pop,
        "employed": employed,
        "yields": yields,
        "jobs": {"agriculture": emp_ag, "industry": emp_in, "services": emp_sv},
        "median_income": med_inc,
        "median_rent": None,
        "home_value": None,
        "unemployment": unemp,
        "rent_burden": None,
        "gdp": gdp_tot,
        "gdp_year": 2024 if gdp_tot is not None else None,
    }
    if gdp_by_ind:
        out["gdp_by_industry"] = gdp_by_ind
    return out


def compute_growth(p2020: int | None, p2024: int | None) -> float | None:
    if not p2020 or not p2024 or p2020 <= 0:
        return None
    g = p2024 / p2020 - 1.0
    return round(g, 4) if abs(g) <= 0.25 else None


def load_gadm2_centroids() -> dict[str, tuple[float, float, str, str]]:
    """Return norm_name(city) -> (lon, lat, prov_geoid, region_fips) from GADM ADM2."""
    out: dict[str, tuple[float, float, str, str]] = {}
    with zipfile.ZipFile(PH_RAW / "gadm_boundaries/gadm41_PHL_2.json.zip") as z:
        g2 = json.loads(z.read(z.namelist()[0]))
    for feat in g2["features"]:
        p = feat["properties"]
        prov_raw = p["NAME_1"]
        if prov_raw not in PROVINCE_META:
            continue
        _, prov_geoid, reg_fips = PROVINCE_META[prov_raw]
        rings = geojson_rings(feat["geometry"])
        pts = [pt for r in rings for pt in r]
        if not pts:
            continue
        lon = sum(x for x, _ in pts) / len(pts)
        lat = sum(y for _, y in pts) / len(pts)
        cname = p["NAME_2"]
        k = norm_name(cname)
        # Prefer cities over duplicate municipality names
        is_city = "city" in (p.get("ENGTYPE_2") or "").lower()
        if k not in out or is_city:
            out[k] = (lon, lat, prov_geoid, reg_fips)
    return out


def build() -> None:
    print("Building Philippines states.json, cities.json, nation.json, counties.json, lenses.json...")
    world = load_interim("_world")
    world_web = json.loads((DATA.parent / "web/data/world.json").read_text())
    proj = MapProjection(**world["projection"])
    grid = HexGrid(**world["grid"])
    owner = world["owner"]
    county_arr = world["county"]

    pop_map, growth_map, area_map = load_census_2024()
    nat_gdp, reg_gdp = load_grdp()
    ppa = load_ppa()
    med_inc, poverty, inet_reg = load_fies_and_poverty()
    urban_map = load_urban_by_norm()
    typhoon_cnt, quake_cnt = load_hazard_counts(world_web["counties"], county_arr, grid, proj)

    fips_to_abbr = {r["fips"]: r["abbr"] for r in REGIONS}
    fips_to_idx = {r["fips"]: i for i, r in enumerate(REGIONS)}
    geoid_to_reg = {geoid: rf for _, (_, geoid, rf) in PROVINCE_META.items()}

    # --- 1. Build states.json (18 Philippine Regions) --------------------------------------------
    energy = energy_by_state()
    states_out = []
    reg_pop_by_fips = {}
    for i, reg in enumerate(REGIONS):
        fips = reg["fips"]
        # Match population & growth
        pop = None
        g_val = None
        for k, v in pop_map.items():
            if match_region(v["raw"]) == fips:
                pop = v["pop"]
                gr = growth_map.get(k)
                if gr:
                    g_val = compute_growth(gr.get("2020"), gr.get("2024"))
                break
        if pop is None:
            # Sum member provinces if region row wasn't matched directly
            pop = sum(
                pop_map.get(norm_name(pname), {}).get("pop", 0)
                for _, (pname, _, rf) in PROVINCE_META.items() if rf == fips
            ) or 1000000
        reg_pop_by_fips[fips] = pop
        if g_val is None:
            g_val = 0.032

        # Match FIES median income for region
        r_inc = None
        for k, v in med_inc.items():
            if match_region(k) == fips:
                r_inc = v
                break

        econ = make_econ(pop, reg_gdp.get(fips), r_inc)
        states_out.append({
            "fips": fips,
            "abbr": reg["abbr"],
            "name": reg["name"],
            **econ,
            "growth": g_val,
            "growth_years": [2020, 2024],
            "ranks": {},
            "label": label_anchor(grid, owner, i, reg["abbr"] if reg["abbr"] == "NCR" else reg["name"]),
            "land": land_shares(world, i),
            "energy": dict(energy.get(reg["abbr"]) or energy[""]) if energy is not None else None,
        })

    for y in YIELDS:
        key = y["key"]
        ranked = sorted((s for s in states_out if s["yields"][key] is not None), key=lambda s: -s["yields"][key])
        for r, s in enumerate(ranked, 1):
            s["ranks"][key] = r
        for s in states_out:
            s["ranks"].setdefault(key, None)

    write_web("states.json", states_out)
    print(f"  states.json: {len(states_out)} regions")

    # --- 2. Build cities.json (Top ~55 Philippine Cities & HUCs) ---------------------------------
    gadm2 = load_gadm2_centroids()
    # Explicit coordinates for key cities whose names differ slightly in GADM2
    CITY_COORDS_OVERRIDE = {
        "manila": (120.9842, 14.5995, "13390", "13"),
        "quezoncity": (121.0437, 14.6760, "13390", "13"),
        "cebu": (123.8854, 10.3157, "07220", "07"),
        "davao": (125.6128, 7.0731, "11240", "11"),
        "zamboanga": (122.0790, 6.9214, "09730", "09"),
        "cagayandeoro": (124.6319, 8.4542, "10430", "10"),
        "iloilo": (122.5621, 10.7202, "06300", "06"),
        "bacolod": (122.9511, 10.6765, "18450", "18"),
        "generalsantos": (125.1716, 6.1164, "12630", "12"),
        "baguio": (120.5960, 16.4023, "14110", "14"),
        "angeles": (120.5887, 15.1450, "03540", "03"),
        "lapulapu": (123.9494, 10.3103, "07220", "07"),
        "mandaue": (123.9227, 10.3236, "07220", "07"),
        "iligan": (124.2452, 8.2280, "10350", "10"),
        "butuan": (125.5406, 8.9475, "16020", "16"),
        "tacloban": (125.0048, 11.2447, "08370", "08"),
        "puertoprincesa": (118.7384, 9.7392, "17530", "17"),
        "lucena": (121.6172, 13.9373, "04560", "04"),
        "olongapo": (120.2828, 14.8292, "03710", "03"),
        "cotabato": (124.2464, 7.2047, "15380", "15"),
        "naga": (123.1948, 13.6218, "05170", "05"),
        "legazpi": (123.7438, 13.1391, "05050", "05"),
        "dagupan": (120.3333, 16.0433, "01550", "01"),
        "tuguegarao": (121.7270, 17.6132, "02150", "02"),
        "laoag": (120.5957, 18.1987, "01280", "01"),
        "dumaguete": (123.3054, 9.3068, "18460", "18"),
        "tagbilaran": (123.8530, 9.6500, "07120", "07"),
        "roxas": (122.7510, 11.5853, "06190", "06"),
        "ormoc": (124.6075, 11.0050, "08370", "08"),
        "surigao": (125.4950, 9.7890, "16670", "16"),
        "pagadian": (123.4370, 7.8257, "09730", "09"),
        "dipolog": (123.3409, 8.5883, "09720", "09"),
        "koronadal": (124.8469, 6.5030, "12630", "12"),
        "tagum": (125.8078, 7.4479, "11230", "11"),
        "malaybalay": (125.1277, 8.1575, "10130", "10"),
        "batangas": (121.0583, 13.7565, "04100", "04"),
        "calamba": (121.1653, 14.2117, "04340", "04"),
        "lipa": (121.1632, 13.9411, "04100", "04"),
        "sanfernando": (120.6897, 15.0286, "03540", "03"),
        "cabanatuan": (120.9679, 15.4865, "03490", "03"),
        "tarlac": (120.5969, 15.4755, "03690", "03"),
        "antipolo": (121.1758, 14.5864, "04580", "04"),
        "dasmarinas": (120.9367, 14.3294, "04210", "04"),
        "bacoor": (120.9640, 14.4590, "04210", "04"),
        "sanpablo": (121.3253, 14.0683, "04340", "04"),
        "santiago": (121.5487, 16.6879, "02310", "02"),
        "calbayog": (124.5997, 12.0669, "08600", "08"),
        "ozamiz": (123.8414, 8.1481, "10420", "10"),
        "digos": (125.3572, 6.7497, "11240", "11"),
        "kidapawan": (125.0894, 7.0083, "12470", "12"),
        "vigan": (120.3869, 17.5747, "01290", "01"),
        "calapan": (121.1803, 13.4117, "17520", "17"),
        "masbate": (123.6249, 12.3719, "05410", "05"),
    }

    # Skip individual NCR sub-cities except Manila & Quezon City so Metro Manila isn't cluttered with 16 overlapping banners
    NCR_SKIP = {
        "caloocan", "laspinas", "makati", "malabon", "mandaluyong", "marikina",
        "muntinlupa", "navotas", "paranaque", "pasay", "pasig", "sanjuan", "taguig", "valenzuela",
    }

    city_rows = []
    with open(PH_RAW / "psa_census_2024/city_pop_area_density_2010_2024.csv", encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            if len(row) < 6:
                continue
            cname = clean_geo(row[0]).replace("City of ", "").replace(" (Capital)", "").strip()
            k = norm_name(cname)
            if k in NCR_SKIP:
                continue
            p2010 = parse_num(row[1])
            p2015 = parse_num(row[2])
            p2020 = parse_num(row[3])
            p2024 = parse_num(row[4])
            if not p2024 or p2024 < 110000:
                # Keep regional capitals even if under 110k (e.g. Vigan, Tagbilaran, Masbate)
                if k not in ("vigan", "tagbilaran", "masbate", "calapan"):
                    continue
            loc = CITY_COORDS_OVERRIDE.get(k) or gadm2.get(k)
            if not loc:
                continue
            lon, lat, prov_geoid, reg_fips = loc
            city_rows.append((cname, k, int(round(p2024)), p2010, p2015, p2020, lon, lat, prov_geoid, reg_fips))

    # For Manila, represent the Metro Manila (NCR) capital agglomeration so its banner & GDP reflect the true capital metro!
    city_rows.sort(key=lambda x: -x[2])
    cities_out = []
    seen_k = set()
    for idx_c, (cname, k, pop24, p10, p15, p20, lon, lat, prov_geoid, reg_fips) in enumerate(city_rows):
        if k in seen_k:
            continue
        seen_k.add(k)
        is_cap = (k == "manila")
        if is_cap:
            # Use full NCR population & GRDP for Manila (Capital Metro)
            pop24 = reg_pop_by_fips["13"]
            p20 = 13484462
            p15 = 12877253
            p10 = 11855975
            gdp_rec = reg_gdp.get("13")
            c_inc = med_inc.get("nationalcapitalregion") or med_inc.get("cityofmanila")
            display = "Manila"
            full = "Metro Manila (National Capital Region)"
        else:
            # Match PPA by "cityof<k>" or "<k>" or fall back to province PPA scaled by pop share
            gdp_rec = ppa.get("cityof" + k) or ppa.get(k)
            if not gdp_rec:
                # Look up province PPA and scale proportionally
                prov_name = next((pn for _, (pn, pg, _) in PROVINCE_META.items() if pg == prov_geoid), "")
                p_gdp = ppa.get(norm_name(prov_name))
                p_pop = pop_map.get(norm_name(prov_name), {}).get("pop")
                if p_gdp and p_pop and p_pop > 0:
                    share = min(1.0, (pop24 / p_pop) * 1.35)
                    gdp_rec = {
                        kk: round(vv * share) for kk, vv in p_gdp.items()
                    }
            c_inc = med_inc.get("cityof" + k) or med_inc.get(k)
            if c_inc is None:
                prov_name = next((pn for _, (pn, pg, _) in PROVINCE_META.items() if pg == prov_geoid), "")
                c_inc = med_inc.get(norm_name(prov_name))
            display = cname
            full = f"{cname} City, {fips_to_abbr[reg_fips]}"

        x, y = proj(lon, lat)
        cid = f"{60000 + len(cities_out):05d}"  # 5-digit string ID
        g_val = compute_growth(int(round(p20)) if p20 else None, pop24)
        ps = {
            "2010": int(round(p10)) if p10 else None,
            "2015": int(round(p15)) if p15 else None,
            "2020": int(round(p20)) if p20 else None,
            "2024": pop24,
        }
        cities_out.append({
            "id": cid,
            "name": display,
            "full": full,
            "states": [fips_to_abbr[reg_fips]],
            "metro": pop24 >= 250000 or is_cap,
            "capital": is_cap,
            "x": round(x, 1),
            "y": round(y, 1),
            "cell": grid.cell_at(x, y),
            **make_econ(pop24, gdp_rec, c_inc),
            "growth": g_val,
            "growth_years": [2020, 2024],
            "counties": [prov_geoid],
            "pop_series": ps,
        })
        if len(cities_out) >= 55:
            break

    cities_out.sort(key=lambda c: -c["population"])

    # Assign unique land cells to city banners
    taken: set[int] = set()
    for c in cities_out:
        st_idx = fips_to_idx[next(r["fips"] for r in REGIONS if r["abbr"] == c["states"][0])]
        cell = c["cell"]
        if cell < 0 or owner[cell] < 0:
            cell = nearest_land(grid, owner, max(cell, 0), c["x"], c["y"], st_idx)
        if cell in taken:
            alt = free_cell(grid, owner, taken, cell, c["x"], c["y"], st_idx, max_rings=3)
            if alt is not None:
                cell = alt
        taken.add(cell)
        c["cell"] = cell
        # Footprint = cell + same-province neighbours
        prov_i = county_arr[cell]
        fp = {cell}
        for nb in grid.neighbours(cell):
            if owner[nb] >= 0 and (county_arr[nb] == prov_i or c["capital"] and owner[nb] == st_idx):
                fp.add(nb)
        c["footprint"] = sorted(fp)

    add_ranks(cities_out)
    write_web("cities.json", cities_out)
    print(f"  cities.json: {len(cities_out)} cities (capital: {next(c['name'] for c in cities_out if c['capital'])})")

    # --- 3. Build nation.json --------------------------------------------------------------------
    ph_pop = pop_map.get("philippines", {}).get("pop", 112729484)
    ph_g = growth_map.get("philippines", {})
    nation = make_econ(ph_pop, nat_gdp, med_inc.get("philippines", 261570))
    nation["growth"] = compute_growth(ph_g.get("2020"), ph_g.get("2024")) or 0.0339
    nation["growth_years"] = [2020, 2024]
    write_web(
        "nation.json",
        {
            "vintage": "PSA 2024 Census & 2023 FIES",
            "year": 2024,
            "gdp_vintage": "PSA GRDP & Provincial Product Accounts, 2024",
            "nation": nation,
            "yields": YIELDS,
            "industries": INDUSTRIES,
            "gdp_industries": GDP_INDUSTRIES,
        },
    )

    # --- 4. Build counties.json (81 Provinces + Metro Manila) & lenses.json ----------------------
    prov_city = {}
    for c in cities_out:
        for pg in c["counties"]:
            prov_city.setdefault(pg, c["id"])

    # Representative cell & mean land shares per province
    prov_cells: dict[int, list[int]] = defaultdict(list)
    for cell_i, ci in enumerate(county_arr):
        if ci >= 0 and owner[cell_i] >= 0:
            prov_cells[ci].append(cell_i)

    county_metrics = {}
    county_rows = []
    for ci, cmeta in enumerate(world_web["counties"]):
        geoid = cmeta["geoid"]
        name = cmeta["name"]
        k = norm_name(name)
        reg_fips = geoid_to_reg[geoid]
        abbr = fips_to_abbr[reg_fips]

        # Population, area, density, growth
        if geoid == "13390":
            pop = reg_pop_by_fips["13"]
            area = 620.61
            gr_val = compute_growth(13484462, pop)
            p_gdp = reg_gdp.get("13", {}).get("total")
            p_inc = med_inc.get("nationalcapitalregion")
            p_pov = poverty.get("nationalcapitalregionncr") or poverty.get("nationalcapitalregion")
            p_urb = 1.0
        else:
            pop = pop_map.get(k, {}).get("pop")
            area = area_map.get(k)
            gr = growth_map.get(k)
            gr_val = compute_growth(gr.get("2020"), gr.get("2024")) if gr else None
            p_gdp = ppa.get(k, {}).get("total")
            p_inc = med_inc.get(k)
            p_pov = poverty.get(k)
            p_urb = urban_map.get(k)

        dens = round(pop / area, 1) if (pop and area and area > 0) else None
        gdp_pc = round(p_gdp / pop) if (p_gdp and pop and pop > 0) else None
        p_inet = inet_reg.get(reg_fips)

        cells = prov_cells.get(ci, [])
        rep_cell = cells[len(cells) // 2] if cells else 0
        if cells:
            fr = round(sum(world["pct"]["forest"][x] for x in cells) / len(cells) / 100, 3)
            cr = round(sum(world["pct"]["crop"][x] for x in cells) / len(cells) / 100, 3)
            pa = round(sum(world["pct"]["pasture"][x] for x in cells) / len(cells) / 100, 3)
            dv = round(sum(world["pct"]["developed"][x] for x in cells) / len(cells) / 100, 3)
        else:
            fr = cr = pa = dv = None

        typ = typhoon_cnt.get(geoid, 0)
        qk = quake_cnt.get(geoid, 0)

        county_metrics[ci] = {
            "density": dens,
            "growth": round(gr_val * 100.0, 2) if gr_val is not None else None,
            "income": p_inc,
            "poverty": p_pov,
            "gdp_pc": gdp_pc,
            "urban": p_urb,
            "broadband": p_inet,
            "forest": fr,
            "crop": cr,
            "typhoons": typ,
            "earthquakes": qk,
        }
        county_rows.append([
            geoid, name, abbr, pop, round(area, 1) if area else None, dens,
            p_inc, gdp_pc, p_pov, round(gr_val * 100.0, 2) if gr_val is not None else None, p_urb, p_inet,
            fr, cr, pa, dv, prov_city.get(geoid), rep_cell, typ, qk,
        ])

    fields = [
        {"key": "geoid", "label": "PSGC", "format": "text", "unit": "", "higher_is": "neutral", "source": "gadm_boundaries", "description": "5-digit province code."},
        {"key": "name", "label": "Province", "format": "text", "unit": "", "higher_is": "neutral", "source": "gadm_boundaries", "description": "Province or NCR name."},
        {"key": "state", "label": "Region", "format": "text", "unit": "", "higher_is": "neutral", "source": "gadm_boundaries", "description": "Philippine Region abbreviation."},
        {"key": "population", "label": "Population", "format": "int", "unit": "people", "higher_is": "neutral", "source": "psa_census_2024", "description": "2024 Census population."},
        {"key": "land_km2", "label": "Land area", "format": "km2", "unit": "km²", "higher_is": "neutral", "source": "psa_census_2024", "description": "Official land area (PSA)."},
        {"key": "density", "label": "Density", "format": "float", "unit": "people / km²", "higher_is": "neutral", "source": "psa_census_2024", "description": "Population per km² (2024)."},
        {"key": "income", "label": "Median income", "format": "usd", "unit": "PHP / year", "higher_is": "good", "source": "psa_fies_2023", "description": "Median annual family income (FIES 2023, PHP)."},
        {"key": "gdp_per_capita", "label": "GDP per capita", "format": "usd", "unit": "PHP / person", "higher_is": "good", "source": "psa_gdp_ppa", "description": "Provincial Product Accounts GDP per person (2024, PHP)."},
        {"key": "poverty", "label": "Poverty", "format": "pct", "unit": "share", "higher_is": "bad", "source": "psa_poverty", "description": "Poverty incidence among population (PSA 2023)."},
        {"key": "growth", "label": "Pop. growth", "format": "float", "unit": "% (2020–2024)", "higher_is": "neutral", "source": "psa_census_2024", "description": "Population percent change from 2020 to 2024 Census."},
        {"key": "urban", "label": "Urban share", "format": "pct", "unit": "share", "higher_is": "neutral", "source": "psa_census_2024", "description": "Share of population in urban barangays (2024)."},
        {"key": "broadband", "label": "Internet use", "format": "pct", "unit": "share", "higher_is": "good", "source": "psa_digital", "description": "Share of residents aged 10+ using the internet (2024, regional)."},
        {"key": "forest", "label": "Forest", "format": "pct", "unit": "share", "higher_is": "neutral", "source": "psa_land", "description": "Forest share of province hexes."},
        {"key": "crop", "label": "Cropland", "format": "pct", "unit": "share", "higher_is": "neutral", "source": "psa_land", "description": "Cropland share of province hexes."},
        {"key": "pasture", "label": "Pasture", "format": "pct", "unit": "share", "higher_is": "neutral", "source": "psa_land", "description": "Pasture share of province hexes."},
        {"key": "developed", "label": "Developed", "format": "pct", "unit": "share", "higher_is": "neutral", "source": "psa_land", "description": "Developed share of province hexes."},
        {"key": "cbsa", "label": "City", "format": "text", "unit": "", "higher_is": "neutral", "source": "psa_census_2024", "description": "Primary city id in cities.json, if any."},
        {"key": "cell", "label": "Cell", "format": "int", "unit": "", "higher_is": "neutral", "source": "gadm_boundaries", "description": "Representative map hex."},
        {"key": "typhoons", "label": "Typhoons (1980+)", "format": "int", "unit": "storms", "higher_is": "bad", "source": "ibtracs_typhoons", "description": "Distinct IBTrACS tropical cyclones passing nearby since 1980."},
        {"key": "earthquakes", "label": "Quakes M5+ (1980+)", "format": "int", "unit": "events", "higher_is": "bad", "source": "usgs_earthquakes", "description": "USGS M ≥ 5.0 earthquakes nearby since 1980."},
    ]
    write_web("counties.json", {
        "vintage": "PSA 2024 Census / 2023 FIES / 2024 PPA",
        "fields": fields,
        "columns": [f["key"] for f in fields],
        "rows": county_rows,
    })

    # Build lenses.json
    def spread(key: str, *, log: bool = False, pct_domain: tuple | None = None):
        vals = [None] * grid.size
        for i in range(grid.size):
            if owner[i] < 0:
                continue
            ci = county_arr[i]
            if ci >= 0:
                v = county_metrics[ci].get(key)
                if v is not None and (not log or v > 0):
                    vals[i] = v
        nums = sorted(v for v in vals if v is not None)
        if pct_domain:
            dom = list(pct_domain)
        elif len(nums) >= 4:
            lo = nums[max(0, int(len(nums) * 0.02))]
            hi = nums[min(len(nums) - 1, int(len(nums) * 0.98))]
            if lo >= hi:
                lo, hi = nums[0], nums[-1] + 1
            dom = [round(lo, 4), round(hi, 4)]
        else:
            dom = [1, 100] if log else [0, 1]
        return vals, dom

    lens_specs = [
        ("density", "people", "Population", "l-people", "👥", "people / km²", "float", "log", True, None,
         ["#f7f4e9", "#c8e0b0", "#6ab372", "#2b7a78", "#1a3c6e"], "neutral", "psa_census_2024",
         "Population per square kilometer by province (PSA 2024 Census).", None),
        ("growth", "people", "Pop. growth", "l-growth", "📈", "% (2020–2024)", "float", "linear", False, (-5.0, 12.0),
         ["#b23b3b", "#e69a73", "#f4f1e6", "#7ec49c", "#1b7a57"], "neutral", "psa_census_2024",
         "Population percent change between the 2020 and 2024 Censuses.", None),
        ("income", "prosperity", "Median income", "l-income", "💰", "PHP / year", "usd", "linear", False, None,
         ["#f7f0d4", "#d9c675", "#8fb86e", "#3d8b6e", "#1b4d6e"], "good", "psa_fies_2023",
         "Median annual family income in Philippine Pesos (PSA FIES 2023).", None),
        ("gdp_pc", "prosperity", "GDP per capita", "l-gdp", "🏛️", "PHP / person", "usd", "log", True, None,
         ["#f6eedb", "#e6c373", "#d48844", "#9e4338", "#591d42"], "good", "psa_gdp_ppa",
         "Gross Domestic Product per person (PSA Provincial Product Accounts 2024).", None),
        ("poverty", "prosperity", "Poverty", "l-poverty", "🏚️", "share of pop.", "pct", "linear", False, (0.0, 0.45),
         ["#eaf4e8", "#f3d98b", "#e38c4b", "#b83c32", "#661224"], "bad", "psa_poverty",
         "Poverty incidence among population by province (PSA 2023).", None),
        ("urban", "life", "Urbanization", "l-urban", "🏙️", "urban share", "pct", "linear", False, (0.0, 1.0),
         ["#eef2e6", "#b8cfa4", "#7ca69c", "#4b6f9e", "#2b306e"], "neutral", "psa_census_2024",
         "Share of residents living in urban barangays (PSA 2024 Census).", None),
        ("broadband", "knowledge", "Internet use", "l-broadband", "📡", "share of 10+", "pct", "linear", False, (0.35, 0.85),
         ["#f2ebe1", "#c5d9e2", "#6aa8cf", "#2f6eb5", "#183370"], "good", "psa_digital",
         "Share of individuals aged 10+ using the internet (PSA 2024, regional figures).",
         "Published at the regional level by PSA; provinces inherit their region's rate."),
        ("typhoons", "hazards", "Typhoons", "l-disasters", "🌀", "storms since 1980", "int", "linear", False, None,
         ["#eef4f8", "#a8d2e8", "#5098cc", "#2858a6", "#1b1f6b"], "bad", "ibtracs_typhoons",
         "Distinct tropical cyclones passing through or adjacent to each province since 1980 (NOAA IBTrACS).",
         "Counts storm tracks passing within ~25 km of the province; tracks do not measure wind damage."),
        ("earthquakes", "hazards", "Earthquakes", "l-quake", "🌋", "M5+ since 1980", "int", "linear", False, None,
         ["#f8f3eb", "#f2c68a", "#df7b46", "#b0322c", "#5e1118"], "bad", "usgs_earthquakes",
         "Magnitude 5.0+ earthquakes located in or adjacent to each province since 1980 (USGS).", None),
        ("forest", "land", "Forest cover", "l-forest", "🌲", "forest share", "pct", "linear", False, (0.0, 1.0),
         ["#f5f0df", "#c5dca0", "#7ab864", "#3b8244", "#184a2c"], "neutral", "psa_land",
         "Share of land classified as forestland (PSA 2021 & relief model).", None),
    ]

    lenses_out = []
    for lid, grp, name, icon, emoji, unit, fmt, scale, is_log, dom_ov, ramp, hi, src, desc, note in lens_specs:
        vals, dom = spread(lid, log=is_log, pct_domain=dom_ov)
        entry = {
            "id": lid, "group": grp, "name": name, "icon": icon, "emoji": emoji,
            "unit": unit, "format": fmt, "scale": scale, "domain": dom, "ramp": ramp,
            "higher_is": hi, "source": src, "description": desc, "values": vals,
        }
        if note:
            entry["note"] = note
        lenses_out.append(entry)

    write_web("lenses.json", {
        "vintage": "PSA 2024 Census / 2023 FIES / 2024 PPA",
        "groups": [
            {"id": "people", "name": "People"},
            {"id": "prosperity", "name": "Prosperity"},
            {"id": "knowledge", "name": "Knowledge"},
            {"id": "life", "name": "Daily life"},
            {"id": "hazards", "name": "Hazards"},
            {"id": "land", "name": "Land"},
        ],
        "lenses": lenses_out,
    })
    print(f"  counties.json: {len(county_rows)} provinces; lenses.json: {len(lenses_out)} lenses")


if __name__ == "__main__":
    build()
