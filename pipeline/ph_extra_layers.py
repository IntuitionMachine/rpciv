"""Build optional Philippine data layers conforming to docs/DATA_CONTRACTS.md:
  - trade.json     (PSA Domestic Trade Statistics: road, air, coastwise)
  - disasters.json (NOAA IBTrACS typhoons + USGS M5+ earthquakes since 1980)
  - citizens.json  (PSA 2024 Census age/sex + PSA LFS occupation & education)
  - districts.json (PSA PPA 16-industry GVA specialization, ports, airports, universities)
"""

from __future__ import annotations

import csv
import heapq
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from .build.citizens import FIGURES as CITIZEN_FIGURES
from .build.citizens import GROUPS as CITIZEN_GROUPS
from .build.citizens import MAX_HIGH, MAX_LOW, MIN_SHARE, ROW_LABELS, SPECIALIST_HIGH, SPECIALIST_LOW
from .build.citizens import largest_remainder, specialists as citizen_specialists
from .build.districts import BADGE_LQ, BADGE_SHARE, CATALOG as DISTRICT_CATALOG
from .build.districts import COLLEGE_SHARE, MAX_PER_CITY, MIN_JOBS, MIN_LQ, MIN_SHARE as DIST_MIN_SHARE
from .build.districts import SPECIALTY_JOBS, SPECIALTY_LQ, SPECIALTY_SHARE, TOP_INDUSTRIES
from .build.districts import badge_district, badge_for, excess, wet_cities
from .build.trade import PARTNERS as TRADE_PARTNERS
from .build.trade import STEP_COST, road_graph
from .core.geo import MapProjection
from .core.hexgrid import HexGrid
from .core.paths import DATA, WEB_DATA
from .core.store import load_interim, write_web
from .ph_economy import clean_geo, match_region, norm_name, parse_num, read_psa_csv
from .ph_world import PROVINCE_META, REGIONS

PH_RAW = DATA / "philippines"
# Trade values in trade.json use million current Philippine Pesos (PHP) to match ₱ formatting across the app.
PHP_DIVISOR = 1e6


# ------------------------------------------------------------------ shared helpers


def _province_to_region_map() -> dict[str, str]:
    """Map normalized province & HUC names to 2-digit region FIPS."""
    m: dict[str, str] = {}
    for gadm_name, (disp, _geoid, rf) in PROVINCE_META.items():
        m[norm_name(gadm_name)] = rf
        m[norm_name(disp)] = rf
    extras = {
        "metromanila": "13", "ncr": "13", "nationalcapitalregion": "13",
        "manila": "13", "cityofmanila": "13", "quezoncity": "13", "makati": "13",
        "pasay": "13", "pasig": "13", "taguig": "13", "caloocan": "13", "paranaque": "13",
        "laspinas": "13", "malabon": "13", "mandaluyong": "13", "marikina": "13",
        "muntinlupa": "13", "navotas": "13", "sanjuan": "13", "valenzuela": "13", "pateros": "13",
        "ncrfirstdistrict": "13", "ncrseconddistrict": "13", "ncrthirddistrict": "13", "ncrfourthdistrict": "13",
        "mtprovince": "14", "mountainprovince": "14", "baguio": "14", "cityofbaguio": "14",
        "compostelavalley": "11", "davaodeoro": "11", "davaocity": "11", "cityofdavao": "11",
        "northcotabato": "12", "cotabato": "12", "southcotabato": "12", "generalsantos": "12",
        "cotabatocity": "15", "cityofcotabato": "15",
        "maguindanao": "15", "maguindanaodelnorte": "15", "maguindanaodelsur": "15",
        "shariffkabunsuan": "15", "basilan": "15", "sulu": "15", "tawitawi": "15",
        "isabelacity": "09", "cityofisabela": "09", "zamboangacity": "09", "cityofzamboanga": "09",
        "westernsamar": "08", "samar": "08", "tacloban": "08", "cityoftacloban": "08", "ormoc": "08",
        "cebucity": "07", "cityofcebu": "07", "lapulapu": "07", "mandaue": "07",
        "iloilocity": "06", "cityofiloilo": "06", "bacolod": "18", "cityofbacolod": "18",
        "negrosoccidental": "18", "negrosoriental": "18", "siquijor": "18",
        "cagayandeoro": "10", "cityofcagayandeoro": "10", "iligan": "10", "cityofiligan": "10",
        "butuan": "16", "cityofbutuan": "16", "dinagatislands": "16",
        "angeles": "03", "cityofangeles": "03", "olongapo": "03", "cityofolongapo": "03",
        "lucena": "04", "cityoflucena": "04", "puertoprincesa": "17", "cityofpuertoprincesa": "17",
        "naga": "05", "legazpi": "05", "santiago": "02", "tuguegarao": "02", "dagupan": "01", "laoag": "01",
    }
    m.update(extras)
    return m


IATA_TO_REGION = {
    "MNL": "13", "CRK": "03", "SFS": "03",
    "LAO": "01", "VGN": "01",
    "TUG": "02", "CYZ": "02", "BSO": "02",
    "BAG": "14",
    "LGP": "05", "DRP": "05", "WNP": "05", "MBT": "05", "VRC": "05", "DTE": "05",
    "PPS": "17", "USU": "17", "ENI": "17", "SJI": "17", "TBH": "17", "MRQ": "17",
    "ILO": "06", "KLO": "06", "MPH": "06", "RXS": "06", "EUQ": "06",
    "BCD": "18", "DGT": "18",
    "CEB": "07", "TAG": "07",
    "TAC": "08", "CRM": "08", "CYP": "08", "OMC": "08", "BQA": "08",
    "ZAM": "09", "DPL": "09", "PAG": "09",
    "CGY": "10", "OZC": "10", "CGM": "10", "IGN": "10",
    "DVO": "11", "MXI": "11",
    "GES": "12", "AAV": "12",
    "BXU": "16", "SUG": "16", "IAO": "16", "TDG": "16",
    "CBO": "15", "JOL": "15", "TWT": "15",
}


def _match_location_to_region(raw: str, prov_map: dict[str, str]) -> str | None:
    s = raw.strip()
    if not s:
        return None
    # Check leading 3-letter IATA code (e.g. "CEB Cebu City")
    m = re.match(r"^([A-Z]{3})\b", s)
    if m and m.group(1) in IATA_TO_REGION:
        return IATA_TO_REGION[m.group(1)]
    # Check direct region match
    rf = match_region(s)
    if rf:
        return rf
    # Strip leading 5-digit PSA port codes (e.g. "22110Cebu City")
    s_clean = re.sub(r"^\d{4,6}", "", s).strip()
    k = norm_name(clean_geo(s_clean))
    if k in prov_map:
        return prov_map[k]
    # Split on commas and check parts (e.g. "Nasipit, Agusan del Norte")
    for part in reversed(s_clean.split(",")):
        kp = norm_name(clean_geo(part))
        if kp in prov_map:
            return prov_map[kp]
        # Strip trailing "city"
        if kp.endswith("city") and kp[:-4] in prov_map:
            return prov_map[kp[:-4]]
    # Substring fallback for port names like "Other Municipal Ports (Cebu)"
    for parens in re.findall(r"\(([^)]+)\)", s_clean):
        kp = norm_name(parens)
        if kp in prov_map:
            return prov_map[kp]
    for pname, rf in sorted(prov_map.items(), key=lambda kv: -len(kv[0])):
        if len(pname) >= 5 and pname in k:
            return rf
    return None


def _shortest_path(grid: HexGrid, owner: list[int], links: dict, src: int, dst: int,
                   *, maritime: bool = False) -> list[int]:
    """Fast weighted A* chain of neighbouring hex cells from src to dst (scales to 150k+ hexes)."""
    if src == dst:
        return [src]
    land_cost = 4.0 if maritime else STEP_COST["land"]
    water_cost = 1.4 if maritime else 6.0
    min_step = min(STEP_COST["interstate"], water_cost)
    inv_step = 1.15 * min_step / (math.sqrt(3) * grid.radius)
    dx_t, dy_t = grid.center(*grid.col_row(dst))
    cols = grid.cols
    r_val = grid.radius
    sq3 = math.sqrt(3)

    def h(c: int) -> float:
        row, col = divmod(c, cols)
        x0 = sq3 * r_val * (col + 0.5 * (row & 1))
        y0 = 1.5 * r_val * row
        return math.hypot(dx_t - x0, dy_t - y0) * inv_step

    best = {src: 0.0}
    prev: dict[int, int] = {}
    heap = [(h(src), 0.0, src)]
    while heap:
        _, d, c = heapq.heappop(heap)
        if c == dst:
            break
        if d > best.get(c, math.inf):
            continue
        for n in grid.neighbours(c):
            if maritime and owner[n] < 0:
                step = water_cost
            else:
                key = (c, n) if c < n else (n, c)
                step = links.get(key)
                if step is None:
                    step = land_cost if owner[n] >= 0 else water_cost
            nd = d + step
            if nd < best.get(n, math.inf):
                best[n] = nd
                prev[n] = c
                heapq.heappush(heap, (nd + h(n), nd, n))
    if dst not in best:
        return []
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    return path[::-1]


# ------------------------------------------------------------------ 1. trade.json


def build_trade(world: dict, world_web: dict, cities: list[dict], states: list[dict]) -> None:
    """Build web/data/trade.json from PSA Domestic Trade Statistics (road, air, coastwise)."""
    grid = HexGrid(**world["grid"])
    owner = world["owner"]
    ipath = WEB_DATA / "infrastructure.json"
    infra = json.loads(ipath.read_text()) if ipath.exists() else None
    links = road_graph(infra)
    prov_map = _province_to_region_map()

    abbr_to_fips = {r["abbr"]: r["fips"] for r in REGIONS}
    fips_to_reg = {r["fips"]: r for r in REGIONS}
    reg_gdp = {s["fips"]: (s.get("gdp") or 1e11) for s in states}
    prov_count = Counter(rf for _, (_, _, rf) in PROVINCE_META.items())

    # Map cities (already population-sorted) to their region zone
    reg_cities: dict[str, list[dict]] = defaultdict(list)
    for c in cities:
        rf = abbr_to_fips.get(c["states"][0])
        if rf:
            reg_cities[rf].append(c)

    # Build 18 regional zones
    reg_cells: dict[str, list[int]] = defaultdict(list)
    for i, o in enumerate(owner):
        if o >= 0:
            reg_cells[REGIONS[o]["fips"]].append(i)

    zones: dict[str, dict] = {}
    for r in REGIONS:
        rf = r["fips"]
        cs = reg_cities.get(rf, [])
        if cs:
            lead = cs[0]
            anchor = {"cell": lead["cell"], "x": lead["x"], "y": lead["y"], "city": lead["id"]}
            kind = "metro" if lead.get("metro") else "rest_of_state"
            short = lead["name"] if rf == "13" else r["name"]
        else:
            cells = reg_cells.get(rf, [])
            if cells:
                pts = [grid.center(*grid.col_row(i)) for i in cells]
                mx, my = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
                cell = min(cells, key=lambda i: math.dist(grid.center(*grid.col_row(i)), (mx, my)))
                x, y = grid.center(*grid.col_row(cell))
                anchor = {"cell": cell, "x": round(x, 1), "y": round(y, 1), "city": None}
            else:
                anchor = None
            kind = "rest_of_state"
            short = r["name"]
        zones[rf] = {
            "id": rf,
            "faf": [rf],
            "name": f"{r['name']} ({r['abbr']})",
            "short": short,
            "kind": kind,
            "states": [r["abbr"]],
            "cities": [c["id"] for c in cs],
            "counties": prov_count.get(rf, 1),
            "anchor": anchor,
        }

    # Aggregate flows: pair (za, zb) -> modes {truck|water|air: [value_musd, tons_k]}, commodities {code: value_musd}
    pair_modes: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    pair_comms: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    within_musd: dict[str, float] = defaultdict(float)
    comm_names: dict[str, str] = {}

    def short_comm(raw_c: str) -> tuple[str, str]:
        s = raw_c.strip()
        m = re.match(r"^Section\s+([IVX]+)\s*-\s*(.+)$", s, flags=re.I)
        if m:
            code = f"PSCC-{m.group(1).upper()}"
            label = m.group(2).split(";")[0].strip()[:48]
            return code, label
        m2 = re.match(r"^(\d{2,5})\s+(.+)$", s)
        if m2:
            code = m2.group(1)[:3]
            label = m2.group(2).strip().title()[:48]
            return code, label
        return "MISC", s[:48] or "Mixed freight"

    # 1) Road domestic trade (Q4 2025 -> annualized ×4)
    road_path = PH_RAW / "psa_domestic_trade/road_domestic_trade_2025_2026.csv"
    for row in read_psa_csv(road_path)[1:]:
        if len(row) < 5:
            continue
        vol = parse_num(row[3])
        val = parse_num(row[4])
        if val is None or val <= 0:
            continue
        oa = _match_location_to_region(row[1], prov_map)
        ob = _match_location_to_region(row[2], prov_map)
        if not oa or not ob:
            continue
        # Annualize Q4 2025 (×4), convert PHP -> million PHP, kg -> thousand metric tons
        val_musd = (val * 4.0) / PHP_DIVISOR
        tons_k = ((vol or 0.0) * 4.0) / 1e6
        code, label = short_comm(row[0])
        comm_names.setdefault(code, label)
        if oa == ob:
            within_musd[oa] += val_musd
        else:
            key = (oa, ob) if oa < ob else (ob, oa)
            pair_modes[key]["truck"][0] += val_musd
            pair_modes[key]["truck"][1] += tons_k
            pair_comms[key][code] += val_musd

    # 2) Air commodity flow (2025 annual)
    air_path = PH_RAW / "psa_domestic_trade/air_commodity_flow_annual.csv"
    with open(air_path, encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) < 5:
                continue
            val = parse_num(row[4])
            if val is None or val <= 0:
                continue
            vol = parse_num(row[3]) or 0.0
            oa = _match_location_to_region(row[1], prov_map)
            ob = _match_location_to_region(row[2], prov_map)
            if not oa or not ob:
                continue
            val_musd = val / PHP_DIVISOR
            tons_k = vol / 1e6
            code, label = short_comm(row[0])
            comm_names.setdefault(code, label)
            if oa == ob:
                within_musd[oa] += val_musd
            else:
                key = (oa, ob) if oa < ob else (ob, oa)
                pair_modes[key]["air"][0] += val_musd
                pair_modes[key]["air"][1] += tons_k
                pair_comms[key][code] += val_musd

    # 3) Coastwise commodity flow (2025 annual; PSA publishes origin port totals -> allocate to inter-island destinations by GRDP gravity)
    coast_orig_val: dict[str, float] = defaultdict(float)
    coast_orig_tons: dict[str, float] = defaultdict(float)
    coast_orig_comm: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    coast_path = PH_RAW / "psa_domestic_trade/coastwise_commodity_flow_annual.csv"
    with open(coast_path, encoding="utf-8-sig", errors="replace") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) < 4:
                continue
            val = parse_num(row[3])
            if val is None or val <= 0:
                continue
            vol = parse_num(row[2]) or 0.0
            oa = _match_location_to_region(row[1], prov_map)
            if not oa:
                continue
            val_musd = val / PHP_DIVISOR
            tons_k = vol / 1e6
            coast_orig_val[oa] += val_musd
            coast_orig_tons[oa] += tons_k
            code, label = short_comm(row[0])
            comm_names.setdefault(code, label)
            coast_orig_comm[oa][code] += val_musd

    # Distribute each region's coastwise outflow across other coastal regions using gravity (GRDP / dist^0.75)
    coastal_regs = [rf for rf, z in zones.items() if z["anchor"] and rf != "14"]  # CAR (14) is landlocked
    for oa, tot_val in coast_orig_val.items():
        if oa not in zones or not zones[oa]["anchor"] or tot_val <= 0:
            continue
        ax, ay = zones[oa]["anchor"]["x"], zones[oa]["anchor"]["y"]
        weights = {}
        for ob in coastal_regs:
            if ob == oa:
                continue
            bx, by = zones[ob]["anchor"]["x"], zones[ob]["anchor"]["y"]
            dist = max(80.0, math.dist((ax, ay), (bx, by)))
            weights[ob] = reg_gdp.get(ob, 1e11) / (dist ** 0.75)
        wsum = sum(weights.values())
        if wsum <= 0:
            continue
        top_c = max(coast_orig_comm[oa].items(), key=lambda kv: kv[1])[0] if coast_orig_comm[oa] else "MISC"
        for ob, w in weights.items():
            share = w / wsum
            if share < 0.03:
                continue
            key = (oa, ob) if oa < ob else (ob, oa)
            v_part = tot_val * share
            t_part = coast_orig_tons[oa] * share
            pair_modes[key]["water"][0] += v_part
            pair_modes[key]["water"][1] += t_part
            pair_comms[key][top_c] += v_part

    # Build top routes (up to 35 inter-regional pairs >= 80 km apart)
    mode_defs = [
        {"id": "truck", "name": "Road / Truck"},
        {"id": "water", "name": "Coastwise / Sea"},
        {"id": "air", "name": "Air cargo"},
    ]
    mode_ids = [m["id"] for m in mode_defs]

    cands = []
    partner_vals: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for (a, b), ms in pair_modes.items():
        total_v = sum(v[0] for v in ms.values())
        if total_v <= 0:
            continue
        for m, (v, _) in ms.items():
            partner_vals[a][b][m] += v
            partner_vals[b][a][m] += v
        za, zb = zones[a], zones[b]
        if not za["anchor"] or not zb["anchor"]:
            continue
        dist = math.dist((za["anchor"]["x"], za["anchor"]["y"]), (zb["anchor"]["x"], zb["anchor"]["y"]))
        if dist < 60.0:
            continue
        cands.append((total_v, dist, a, b, ms))

    cands.sort(key=lambda t: (-t[0], t[2], t[3]))
    routes = []
    used_comms = set()
    for rank, (val, dist, a, b, ms) in enumerate(cands[:35], 1):
        shares = {m: round(ms[m][0] / val, 4) if m in ms else 0.0 for m in mode_ids}
        # Normalize rounding drift so sum(shares) == 1.0
        top_m = max(mode_ids, key=lambda m: shares[m])
        shares[top_m] = round(shares[top_m] + (1.0 - sum(shares.values())), 4)
        tons_k = sum(v[1] for v in ms.values())
        tmiles_m = tons_k * (dist * 0.621371) / 1000.0
        top_c = max(pair_comms[(a, b)].items(), key=lambda kv: kv[1])[0] if pair_comms[(a, b)] else None
        if top_c:
            used_comms.add(top_c)
        path = _shortest_path(
            grid, owner, links,
            zones[a]["anchor"]["cell"], zones[b]["anchor"]["cell"],
            maritime=(top_m == "water"),
        )
        routes.append({
            "a": a, "b": b, "rank": rank,
            "value_musd": round(val, 2),
            "tons_k": round(tons_k, 2),
            "tmiles_m": round(tmiles_m, 2),
            "modes": shares,
            "top_mode": top_m,
            "top_commodity": top_c,
            "dist_km": round(dist),
            "path": path,
        })

    # Populate zone partners & totals
    nat_dom = 0.0
    for zid, z in zones.items():
        ps = []
        other_tot = 0.0
        for other, ms in partner_vals[zid].items():
            v = sum(ms.values())
            other_tot += v
            if v > 0:
                ps.append([other, round(v, 2), max(ms, key=ms.get)])
        ps.sort(key=lambda p: (-p[1], p[0]))
        z["partners"] = ps[:TRADE_PARTNERS]
        w_val = within_musd.get(zid, 0.0)
        z["totals_musd"] = {
            "within": round(w_val, 2),
            "other_zones": round(other_tot, 2),
            "import": 0.0,
            "export": 0.0,
        }
        nat_dom += w_val + other_tot * 0.5

    commodities = {k: comm_names.get(k, "Mixed freight") for k in sorted(used_comms)}
    doc = {
        "version": 1,
        "vintage": "PSA Domestic Trade Statistics 2025 (Road Q4×4, Air & Coastwise Annual)",
        "year": 2025,
        "sources": ["psa_domestic_trade"],
        "rule": ("Top 35 inter-regional Philippine domestic freight corridors by value (million current PHP). "
                 "Road flows use PSA Q4 2025 inter-provincial O-D data annualized (×4); air flows "
                 "use 2025 station-to-station records; coastwise sea flows allocate 2025 PSA origin-port shipments "
                 "across inter-island coastal regions by GRDP gravity."),
        "path_note": ("PSA Domestic Trade Statistics report origin and destination regions/ports, not exact routes: "
                      "drawn hex paths follow highways on land and direct sea corridors between islands as a visual guide."),
        "units": {"value_musd": "million current Philippine Pesos (PHP)", "tons_k": "thousand metric tons", "tmiles_m": "million ton-miles"},
        "modes": mode_defs,
        "commodities": commodities,
        "national_musd": {"domestic": round(nat_dom, 1), "import": 0.0, "export": 0.0},
        "zones": sorted(zones.values(), key=lambda z: z["id"]),
        "routes": routes,
    }
    write_web("trade.json", doc)
    print(f"  trade.json: {len(zones)} zones, {len(routes)} routes (top: {zones[routes[0]['a']]['short']} – {zones[routes[0]['b']]['short']})")


# ------------------------------------------------------------------ 2. disasters.json


def build_disasters(world: dict, world_web: dict, proj: MapProjection, grid: HexGrid) -> None:
    """Build web/data/disasters.json from NOAA IBTrACS typhoons (1980+) & USGS M5+ earthquakes (1980+)."""
    county_arr = world["county"]
    owner = world["owner"]
    counties_list = world_web["counties"]
    geoids = [c["geoid"] for c in counties_list]
    states_abbr = [s["abbr"] for s in world_web["states"]]

    HAZARD_TYPES = [
        {"id": "hurricane", "name": "Typhoons & super typhoons", "incident_types": ["Super Typhoon", "Typhoon"]},
        {"id": "severe_storm", "name": "Tropical storms & depressions", "incident_types": ["Tropical Storm", "Tropical Depression"]},
        {"id": "other", "name": "Earthquakes (M5.0+)", "incident_types": ["Earthquake M5+"]},
    ]
    type_ids = [t["id"] for t in HAZARD_TYPES]

    per_county = {g: {"total": 0, **{t: 0 for t in type_ids}, "last_date": None} for g in geoids}
    per_state = {ab: {"total": 0, "statewide": 0, "by_type": {t: 0 for t in type_ids}} for ab in states_abbr}
    noaa_state = {ab: {"count": {"tropical_cyclone": 0, "severe_storm": 0},
                       "cost_usd": {"tropical_cyclone": 0, "severe_storm": 0}} for ab in states_abbr}

    # 13 sample offsets (centre + 6 at 11 km + 6 at 22 km) for resolution-independent ~25 km exposure
    offsets = [(0.0, 0.0)] + [
        (r * math.cos(a), r * math.sin(a))
        for r in (11.0, 22.0)
        for a in (0.0, math.pi / 3, 2 * math.pi / 3, math.pi, 4 * math.pi / 3, 5 * math.pi / 3)
    ]

    # 1) Parse IBTrACS typhoons
    storms: dict[str, dict] = {}
    with open(PH_RAW / "ibtracs_typhoons/ibtracs_wp_par_since1980.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            sid = (row.get("SID") or "").strip()
            if not sid:
                continue
            try:
                lat, lon = float(row["LAT"]), float(row["LON"])
            except (ValueError, KeyError):
                continue
            iso = (row.get("ISO_TIME") or "")[:10]
            name = (row.get("NAME") or "UNNAMED").strip()
            wind = parse_num(row.get("USA_WIND")) or parse_num(row.get("WMO_WIND")) or 0.0
            x, y = proj(lon, lat)
            hit_g = set()
            hit_st = set()
            for dx, dy in offsets:
                c = grid.cell_at(x + dx, y + dy)
                if 0 <= c < grid.size:
                    ci = county_arr[c]
                    if ci >= 0:
                        hit_g.add(geoids[ci])
                    oi = owner[c]
                    if oi >= 0:
                        hit_st.add(states_abbr[oi])
            if not hit_g and not hit_st:
                continue
            st = storms.setdefault(sid, {"name": name, "date": iso, "max_wind": 0.0, "counties": set(), "states": set()})
            if iso and (not st["date"] or iso > st["date"]):
                st["date"] = iso
            if wind > st["max_wind"]:
                st["max_wind"] = wind
            if name and name != "UNNAMED" and st["name"] == "UNNAMED":
                st["name"] = name
            st["counties"].update(hit_g)
            st["states"].update(hit_st)

    by_year_events: Counter[int] = Counter()
    all_major_events = []
    max_date = "1980-01-01"

    for sid, st in storms.items():
        dstr = st["date"] or "1980-01-01"
        if dstr > max_date:
            max_date = dstr
        yr = int(dstr[:4])
        by_year_events[yr] += 1
        wind = st["max_wind"]
        if wind >= 130:
            tid, inc_t = "hurricane", "Super Typhoon"
        elif wind >= 64:
            tid, inc_t = "hurricane", "Typhoon"
        elif wind >= 34:
            tid, inc_t = "severe_storm", "Tropical Storm"
        else:
            tid, inc_t = "severe_storm", "Tropical Depression"

        for g in st["counties"]:
            rec = per_county[g]
            rec["total"] += 1
            rec[tid] += 1
            if rec["last_date"] is None or dstr > rec["last_date"]:
                rec["last_date"] = dstr
        for ab in st["states"]:
            per_state[ab]["total"] += 1
            per_state[ab]["by_type"][tid] += 1
            noaa_state[ab]["count"]["tropical_cyclone"] += 1
        all_major_events.append({
            "score": wind * 10 + len(st["counties"]),
            "name": f"{inc_t} {st['name'].title()} ({dstr[:4]})" if st["name"] != "UNNAMED" else f"{inc_t} ({dstr})",
            "type": "Tropical Cyclone",
            "begin": dstr,
            "cost_usd": 0,
            "deaths": 0,
            "tid": tid,
            "inc_t": inc_t,
            "counties": sorted(st["counties"]),
            "states": sorted(st["states"]),
            "id_num": abs(hash(sid)) % 900000 + 100000,
        })

    # 2) Parse USGS M5+ earthquakes
    with open(PH_RAW / "usgs_earthquakes/phl_earthquakes_m5_since1980.csv", encoding="utf-8", errors="replace") as f:
        for idx_q, row in enumerate(csv.DictReader(f), 1):
            try:
                lat, lon = float(row["latitude"]), float(row["longitude"])
                mag = float(row.get("mag") or 5.0)
            except (ValueError, KeyError):
                continue
            dstr = (row.get("time") or "")[:10]
            if not dstr:
                continue
            x, y = proj(lon, lat)
            hit_g = set()
            hit_st = set()
            for dx, dy in offsets:
                c = grid.cell_at(x + dx, y + dy)
                if 0 <= c < grid.size:
                    ci = county_arr[c]
                    if ci >= 0:
                        hit_g.add(geoids[ci])
                    oi = owner[c]
                    if oi >= 0:
                        hit_st.add(states_abbr[oi])
            if not hit_g and not hit_st:
                continue
            if dstr > max_date:
                max_date = dstr
            yr = int(dstr[:4])
            by_year_events[yr] += 1
            for g in hit_g:
                rec = per_county[g]
                rec["total"] += 1
                rec["other"] += 1
                if rec["last_date"] is None or dstr > rec["last_date"]:
                    rec["last_date"] = dstr
            for ab in hit_st:
                per_state[ab]["total"] += 1
                per_state[ab]["by_type"]["other"] += 1
                noaa_state[ab]["count"]["severe_storm"] += 1
            place = (row.get("place") or "Philippines").strip()
            all_major_events.append({
                "score": mag * 150.0,
                "name": f"M{mag:.1f} Earthquake – {place}",
                "type": "Earthquake",
                "begin": dstr,
                "cost_usd": 0,
                "deaths": 0,
                "tid": "other",
                "inc_t": "Earthquake M5+",
                "counties": sorted(hit_g),
                "states": sorted(hit_st),
                "id_num": 500000 + idx_q,
            })

    cols = ["geoid", "total", *type_ids, "last_date"]
    rows = [[g, *(per_county[g][c] for c in cols[1:])] for g in geoids]

    as_of = max_date
    cutoff = (date.fromisoformat(as_of) - timedelta(days=365)).isoformat()
    recent = []
    for ev in all_major_events:
        if ev["begin"] >= cutoff:
            recent.append({
                "disaster": ev["id_num"],
                "type": ev["tid"],
                "incident_type": ev["inc_t"],
                "title": ev["name"],
                "date": ev["begin"],
                "state": ev["states"][0] if ev["states"] else "NCR",
                "counties": ev["counties"],
                "statewide": len(ev["counties"]) >= 5,
                "areas": len(ev["counties"]),
            })
    recent.sort(key=lambda r: (r["date"], r["disaster"]), reverse=True)

    y0, y1 = 1980, int(as_of[:4])
    by_year = [{"year": y, "events": by_year_events.get(y, 0), "cost_usd": 0} for y in range(y0, y1 + 1)]
    costliest = [
        {"name": e["name"], "type": e["type"], "begin": e["begin"], "cost_usd": e["cost_usd"], "deaths": e["deaths"]}
        for e in sorted(all_major_events, key=lambda x: -x["score"])[:10]
    ]

    doc = {
        "version": 1,
        "vintage": {
            "fema": f"IBTrACS v04r01 & USGS Catalog (1980–{y1})",
            "noaa": f"Philippine Tropical Cyclones & M5+ Earthquakes (1980–{y1})",
        },
        "sources": ["ibtracs_typhoons", "usgs_earthquakes"],
        "fema": {
            "window": ["1980-01-01", as_of],
            "as_of": as_of,
            "rule": ("Distinct NOAA IBTrACS tropical cyclones passing within ~18 km (1 hex ring) of each province "
                     "and USGS M≥5.0 earthquakes located in or adjacent to each province since 1980. Track counts "
                     "indicate exposure frequency, not monetary damage."),
            "types": HAZARD_TYPES,
            "counties": {"columns": cols, "rows": rows},
            "states": dict(sorted(per_state.items())),
            "recent_days": 365,
            "recent": recent[:60],
        },
        "noaa": {
            "years": [y0, y1],
            "final": False,
            "unit": "Recorded events (IBTrACS + USGS M5+)",
            "rule": ("Historical tropical cyclones inside the Philippine Area of Responsibility impacting Philippine "
                     "land hexes (IBTrACS) and M≥5.0 earthquakes (USGS) since 1980."),
            "types": [
                {"id": "tropical_cyclone", "name": "Typhoons & tropical storms"},
                {"id": "severe_storm", "name": "Earthquakes (M5.0+)"},
            ],
            "states": dict(sorted(noaa_state.items())),
            "national": {
                "events": sum(y["events"] for y in by_year),
                "cost_usd": 0,
                "deaths": 0,
                "by_year": by_year,
                "costliest": costliest,
            },
        },
    }
    write_web("disasters.json", doc)
    print(f"  disasters.json: {len(storms)} storms, {sum(y['events'] for y in by_year)} total events, {len(recent)} recent")


# ------------------------------------------------------------------ 3. citizens.json


def _load_psa_age_2024() -> dict[str, dict[str, int]]:
    """Return norm_name(geo) -> {age_group: count} from PSA 2024 Census age/sex table."""
    by_geo: dict[str, dict[str, int]] = defaultdict(dict)
    for row in read_psa_csv(PH_RAW / "psa_census_2024/age_sex_region_province_huc_2024.csv")[1:]:
        if len(row) < 3:
            continue
        age_grp = row[0].strip()
        geo = norm_name(clean_geo(row[1]))
        val = parse_num(row[2])
        if val is not None and val >= 0:
            by_geo[geo][age_grp] = int(round(val))
    return by_geo


def _age_row_from_brackets(brackets: dict[str, int]) -> tuple[dict | None, int]:
    """Convert PSA 5-year age brackets to citizens.json age row + adults 25+ count."""
    if not brackets or "Under 5" not in brackets:
        return None, 0
    g = lambda k: brackets.get(k, 0)
    b15_19 = g("15 - 19")
    child = g("Under 5") + g("5 - 9") + g("10 - 14") + round(b15_19 * 0.6)
    young = (b15_19 - round(b15_19 * 0.6)) + g("20 - 24") + g("25 - 29") + g("30 - 34")
    midlife = g("35 - 39") + g("40 - 44") + g("45 - 49") + g("50 - 54") + g("55 - 59") + g("60 - 64")
    elder = g("65 - 69") + g("70 - 74") + g("75 - 79") + g("80 - 84") + g("85 and over")
    counts = {"child": child, "young": young, "midlife": midlife, "elder": elder}
    total = sum(counts.values())
    if total <= 0:
        return None, 0
    adults25 = g("25 - 29") + g("30 - 34") + midlife + elder

    # Compute median age from 5-year brackets
    ordered = [
        ("Under 5", 0, 5), ("5 - 9", 5, 5), ("10 - 14", 10, 5), ("15 - 19", 15, 5),
        ("20 - 24", 20, 5), ("25 - 29", 25, 5), ("30 - 34", 30, 5), ("35 - 39", 35, 5),
        ("40 - 44", 40, 5), ("45 - 49", 45, 5), ("50 - 54", 50, 5), ("55 - 59", 55, 5),
        ("60 - 64", 60, 5), ("65 - 69", 65, 5), ("70 - 74", 70, 5), ("75 - 79", 75, 5),
        ("80 - 84", 80, 5), ("85 and over", 85, 10),
    ]
    half = total / 2.0
    cum = 0
    med = 26.0
    for lbl, lo, width in ordered:
        c = g(lbl)
        if cum + c >= half and c > 0:
            med = lo + ((half - cum) / c) * width
            break
        cum += c

    row_obj = {
        "total": total,
        "counts": counts,
        "shares": {k: round(v / total, 3) for k, v in counts.items()},
        "figures": largest_remainder(counts, CITIZEN_FIGURES["age"]),
        "median": round(med, 1),
    }
    return row_obj, adults25


def _load_psa_lfs_shares() -> tuple[dict[str, float], dict[str, float]]:
    """Return (national_workforce_shares, national_edu_shares) from latest PSA LFS rows."""
    # Occupation (PSOC)
    occ_rows = read_psa_csv(PH_RAW / "psa_lfs/employed_by_occupation_psoc.csv")
    hdr = occ_rows[0]
    valid = [r for r in occ_rows[1:] if len(r) >= 12 and parse_num(r[2])]
    last = valid[-1]
    col = {h.strip(): parse_num(last[i]) or 0.0 for i, h in enumerate(hdr) if i < len(last)}
    # Map PSA PSOC major groups to the 9 contract workforce keys
    prof = col.get("Professionals", 0.0)
    tech = col.get("Technicians and Associate Professionals", 0.0)
    srv_sales = col.get("Service and Sales Workers", 0.0)
    elem = col.get("Elementary Occupations", 0.0)
    wf_raw = {
        "executive": col.get("Managers", 0.0),
        "engineer": prof * 0.30 + tech * 0.55,
        "scholar": prof * 0.48 + tech * 0.15,
        "healer": prof * 0.22 + tech * 0.30,
        "service": srv_sales * 0.65 + elem * 0.35,
        "clerk": col.get("Clerical Support Workers", 0.0) + srv_sales * 0.35,
        "farmer": col.get("Skilled Agricultural Forestry and Fishery Workers", 0.0) + elem * 0.30,
        "builder": col.get("Craft and Related Trades Workers", 0.0) + elem * 0.15,
        "maker": col.get("Plant and Machine Operators and Assemblers", 0.0) + elem * 0.20 + col.get("Armed Forces Occupations", 0.0),
    }
    wf_sum = sum(wf_raw.values()) or 1.0
    wf_shares = {k: v / wf_sum for k, v in wf_raw.items()}

    # Education (PSCED)
    edu_rows = read_psa_csv(PH_RAW / "psa_lfs/employed_by_education_psced.csv")
    ehdr = edu_rows[0]
    evalid = [r for r in edu_rows[1:] if len(r) >= 19 and parse_num(r[2])]
    elast = evalid[-1]
    ecol = {h.strip(): parse_num(elast[i]) or 0.0 for i, h in enumerate(ehdr) if i < len(elast)}
    col_grad = ecol.get("College Graduate", 0.0)
    edu_raw = {
        "less_than_hs": ecol.get("No grade completed", 0.0) + ecol.get("Elementary", 0.0) + ecol.get("Junior High Undergraduate", 0.0),
        "high_school": ecol.get("Junior High Completed", 0.0) + ecol.get("Senior High School", 0.0),
        "some_college": ecol.get("Post Secondary", 0.0) + ecol.get("College Undergraduate", 0.0),
        "bachelors": col_grad * 0.88,
        "graduate": col_grad * 0.12,
    }
    edu_sum = sum(edu_raw.values()) or 1.0
    edu_shares = {k: v / edu_sum for k, v in edu_raw.items()}
    return wf_shares, edu_shares


def _load_ppa_16_industries() -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """Return (by_geo_norm -> {sector: php}, national_sector_totals)."""
    by_geo: dict[str, dict[str, float]] = defaultdict(dict)
    rows = read_psa_csv(PH_RAW / "psa_gdp_ppa/gdp_by_industry_province_huc.csv")
    hdr = rows[0]
    c2024 = next((i for i, h in enumerate(hdr) if "Current Prices 2024" in h), None)
    for row in rows[1:]:
        if len(row) < 3 or c2024 is None or c2024 >= len(row):
            continue
        sec = row[0].strip()
        k = norm_name(clean_geo(row[1]))
        v = parse_num(row[c2024])
        if v is not None and v >= 0:
            by_geo[k][sec] = v * 1000.0
    # National baseline = sum across all non-region entries (or sum of all 17 regions present)
    nat: dict[str, float] = defaultdict(float)
    reg_keys = set()
    for k in by_geo:
        if match_region(k) is not None and k not in ("cebu", "davao", "iloilo", "zamboanga"):
            reg_keys.add(k)
    src_keys = reg_keys if len(reg_keys) >= 15 else set(by_geo.keys())
    for k in src_keys:
        for sec, v in by_geo[k].items():
            nat[sec] += v
    return by_geo, dict(nat)


def build_citizens(cities: list[dict], nation: dict) -> None:
    """Build web/data/citizens.json from PSA 2024 Census age/sex & PSA LFS occupation/education."""
    age_by_geo = _load_psa_age_2024()
    nat_wf_shares, nat_edu_shares = _load_psa_lfs_shares()
    ppa_ind, nat_ppa = _load_ppa_16_industries()
    geoid_to_prov = {pg: pname for _, (pname, pg, _) in PROVINCE_META.items()}

    # National row
    nat_age, nat_adults25 = _age_row_from_brackets(age_by_geo.get("philippines", {}))
    nat_emp = nation["nation"]["employed"]
    nat_wf_counts = {k: round(nat_emp * nat_wf_shares[k]) for k in nat_wf_shares}
    nat_wf_tot = sum(nat_wf_counts.values())
    nat_wf = {
        "total": nat_wf_tot,
        "counts": nat_wf_counts,
        "shares": {k: round(v / nat_wf_tot, 3) for k, v in nat_wf_counts.items()},
        "figures": largest_remainder(nat_wf_counts, CITIZEN_FIGURES["workforce"]),
    }
    nat_edu_counts = {k: round(nat_adults25 * nat_edu_shares[k]) for k in nat_edu_shares}
    nat_edu_tot = sum(nat_edu_counts.values())
    nat_edu = {
        "total": nat_edu_tot,
        "counts": nat_edu_counts,
        "shares": {k: round(v / nat_edu_tot, 3) for k, v in nat_edu_counts.items()},
        "bachelors_plus": round((nat_edu_counts["bachelors"] + nat_edu_counts["graduate"]) / nat_edu_tot, 4),
    }
    national = {
        "workforce": nat_wf,
        "age": nat_age,
        "commute": None,
        "education": nat_edu,
        "nativity": None,
        "specialists": [],
    }

    nat_inc = nation["nation"].get("median_income") or 261570.0
    nat_ppa_tot = sum(nat_ppa.values()) or 1.0
    nat_ag = nat_ppa.get("Agriculture, Forestry, and Fishing", 0.0) / nat_ppa_tot
    nat_mfg = nat_ppa.get("Manufacturing", 0.0) / nat_ppa_tot
    nat_ict = (nat_ppa.get("Information and Communication", 0.0) + nat_ppa.get("Professional and Business Services", 0.0)) / nat_ppa_tot
    nat_edu_gva = nat_ppa.get("Education", 0.0) / nat_ppa_tot
    nat_hlt = nat_ppa.get("Human Health and Social Work Activities", 0.0) / nat_ppa_tot
    nat_fin = nat_ppa.get("Financial and Insurance Activities", 0.0) / nat_ppa_tot
    nat_con = nat_ppa.get("Construction", 0.0) / nat_ppa_tot

    cities_out: dict[str, dict | None] = {}
    for c in cities:
        k = norm_name(c["name"])
        prov_name = geoid_to_prov.get(c["counties"][0], "") if c.get("counties") else ""
        pk = norm_name(prov_name)

        # 1) Age from PSA 2024 Census
        if c.get("capital"):
            age_br = age_by_geo.get("nationalcapitalregion") or age_by_geo.get("cityofmanila")
        else:
            age_br = age_by_geo.get("cityof" + k) or age_by_geo.get(k) or age_by_geo.get(pk)
        age_row, adults25 = _age_row_from_brackets(age_br or {})
        if age_row is not None and c.get("population"):
            # Scale province age counts to the city's population if province fallback was used
            scale = c["population"] / age_row["total"]
            if abs(scale - 1.0) > 0.15:
                sc_counts = {ak: max(1, round(av * scale)) for ak, av in age_row["counts"].items()}
                sc_tot = sum(sc_counts.values())
                age_row = {
                    "total": sc_tot,
                    "counts": sc_counts,
                    "shares": {ak: round(av / sc_tot, 3) for ak, av in sc_counts.items()},
                    "figures": largest_remainder(sc_counts, CITIZEN_FIGURES["age"]),
                    "median": age_row["median"],
                }
                adults25 = round(adults25 * scale)

        # 2) Workforce modulated by PPA 16-industry GVA shares
        sec_map = (ppa_ind.get("nationalcapitalregion") if c.get("capital")
                   else (ppa_ind.get("cityof" + k) or ppa_ind.get(k) or ppa_ind.get(pk)))
        wf_w = dict(nat_wf_shares)
        if sec_map and sum(sec_map.values()) > 0:
            st = sum(sec_map.values())
            def ratio(val, base):
                return max(0.35, min(2.6, (val / st) / base)) if base > 0 else 1.0
            wf_w["farmer"] *= ratio(sec_map.get("Agriculture, Forestry, and Fishing", 0.0), nat_ag)
            wf_w["maker"] *= ratio(sec_map.get("Manufacturing", 0.0), nat_mfg)
            wf_w["engineer"] *= ratio(
                sec_map.get("Information and Communication", 0.0) + sec_map.get("Professional and Business Services", 0.0),
                nat_ict,
            )
            wf_w["scholar"] *= ratio(sec_map.get("Education", 0.0), nat_edu_gva)
            wf_w["healer"] *= ratio(sec_map.get("Human Health and Social Work Activities", 0.0), nat_hlt)
            wf_w["executive"] *= ratio(sec_map.get("Financial and Insurance Activities", 0.0), nat_fin)
            wf_w["builder"] *= ratio(sec_map.get("Construction", 0.0), nat_con)
        wsum = sum(wf_w.values()) or 1.0
        emp = c.get("employed") or round(c["population"] * 0.43)
        wf_counts = {wk: round(emp * (wv / wsum)) for wk, wv in wf_w.items()}
        wf_tot = sum(wf_counts.values())
        wf_row = {
            "total": wf_tot,
            "counts": wf_counts,
            "shares": {wk: round(wv / wf_tot, 3) for wk, wv in wf_counts.items()},
            "figures": largest_remainder(wf_counts, CITIZEN_FIGURES["workforce"]),
        }

        # 3) Education modulated by relative income
        inc_ratio = max(0.65, min(1.55, (c.get("median_income") or nat_inc) / nat_inc))
        edu_w = {
            "less_than_hs": nat_edu_shares["less_than_hs"] / (inc_ratio ** 0.7),
            "high_school": nat_edu_shares["high_school"],
            "some_college": nat_edu_shares["some_college"] * (inc_ratio ** 0.3),
            "bachelors": nat_edu_shares["bachelors"] * (inc_ratio ** 0.8),
            "graduate": nat_edu_shares["graduate"] * (inc_ratio ** 1.0),
        }
        esum = sum(edu_w.values()) or 1.0
        a25 = adults25 if adults25 > 0 else round(c["population"] * 0.52)
        edu_counts = {ek: round(a25 * (ev / esum)) for ek, ev in edu_w.items()}
        edu_tot = sum(edu_counts.values())
        edu_row = {
            "total": edu_tot,
            "counts": edu_counts,
            "shares": {ek: round(ev / edu_tot, 3) for ek, ev in edu_counts.items()},
            "bachelors_plus": round((edu_counts["bachelors"] + edu_counts["graduate"]) / edu_tot, 4),
        }

        parts = {
            "workforce": wf_row,
            "age": age_row,
            "commute": None,
            "education": edu_row,
            "nativity": None,
        }
        parts["specialists"] = citizen_specialists(parts, national)
        parts["reliable"] = bool(c.get("metro"))
        cities_out[c["id"]] = parts

    groups_out = {}
    for name, gs in CITIZEN_GROUPS.items():
        groups_out[name] = [
            {
                "key": g["key"],
                "label": g["label"],
                "icon": g["icon"],
                "lines": (
                    "PSA 2024 Census 5-year age brackets" if name == "age"
                    else "PSA LFS PSOC + PPA industry shares" if name == "workforce"
                    else "PSA LFS PSCED education attainment" if name == "education"
                    else "Not published at city level by PSA"
                ),
            }
            for g in gs
        ]

    doc = {
        "version": 1,
        "vintage": "PSA 2024 Census & 2026 LFS",
        "year": 2024,
        "sources": ["psa_census_2024", "psa_lfs"],
        "figures": dict(CITIZEN_FIGURES),
        "rows": dict(ROW_LABELS),
        "groups": groups_out,
        "rule": {
            "specialist_high": SPECIALIST_HIGH,
            "specialist_low": SPECIALIST_LOW,
            "min_share": MIN_SHARE,
            "max_high": MAX_HIGH,
            "max_low": MAX_LOW,
            "text": (f"Figures are rounded with the largest-remainder method so each row adds up exactly. Age "
                     f"brackets come directly from the PSA 2024 Census; workforce and education combine PSA Labor "
                     f"Force Survey baselines with Provincial Product Accounts industry mix. Commute and nativity "
                     f"are null where PSA does not publish city-level tables. A specialist is ≥ {SPECIALIST_HIGH:g}× "
                     f"or ≤ {SPECIALIST_LOW:g}× the national share."),
        },
        "national": national,
        "cities": cities_out,
    }
    write_web("citizens.json", doc)
    print(f"  citizens.json: {len(cities_out)} cities built")


# ------------------------------------------------------------------ 4. districts.json


PSIC_CODES = {
    "AGRI": "Agriculture, Forestry, and Fishing",
    "MINE": "Mining and Quarrying",
    "MFG": "Manufacturing",
    "UTIL": "Electricity, Steam, Water and Waste Management",
    "CONS": "Construction",
    "TRAD": "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
    "TRAN": "Transportation and Storage",
    "ACCM": "Accommodation and Food Service Activities",
    "INFO": "Information and Communication",
    "FINA": "Financial and Insurance Activities",
    "REAL": "Real Estate and Ownership of Dwellings",
    "PROF": "Professional and Business Services",
    "GOVT": "Public Administration and Defense; Compulsory Social Activities",
    "EDUC": "Education",
    "HLTH": "Human Health and Social Work Activities",
    "OTHR": "Other Services",
}
SEC_TO_CODE = {v: k for k, v in PSIC_CODES.items()}

# Map the 10 contract district IDs to Philippine PPA PSIC codes
PH_DISTRICT_CODES = {
    "campus": ["EDUC", "INFO", "PROF"],
    "commercial_hub": ["FINA", "TRAD"],
    "industrial_zone": ["MFG", "MINE", "CONS"],
    "harbor": ["TRAN"],
    "theater_square": ["OTHR", "INFO"],
    "entertainment_complex": ["ACCM"],
    "aerodrome": ["TRAN"],
    "government_plaza": ["GOVT"],
    "encampment": [],
    "aqueduct": ["UTIL"],
}

REGIONAL_CAPITALS = {
    "manila", "baguio", "sanfernando", "tuguegarao", "calamba", "calapan", "legazpi",
    "iloilo", "bacolod", "cebu", "tacloban", "pagadian", "cagayandeoro", "davao",
    "koronadal", "butuan", "cotabato",
}


def build_districts(world: dict, world_web: dict, cities: list[dict], nation: dict) -> None:
    """Build web/data/districts.json from PSA PPA 16-industry GVA, ports, airports & universities."""
    ppa_ind, nat_ppa = _load_ppa_16_industries()
    geoid_to_prov = {pg: pname for _, (pname, pg, _) in PROVINCE_META.items()}

    ipath = WEB_DATA / "infrastructure.json"
    infra = json.loads(ipath.read_text()) if ipath.exists() else {}
    wpath = WEB_DATA / "wonders.json"
    wonders_doc = json.loads(wpath.read_text()) if wpath.exists() else {}

    port_cbsas = {p["cbsa"] for p in infra.get("ports", []) if p.get("cbsa")}
    hub_airport_cbsas = {a["cbsa"] for a in infra.get("airports", []) if a.get("cbsa") and a.get("hub") in ("L", "M", "S")}
    uni_cbsas = {w["cbsa"] for w in wonders_doc.get("wonders", []) if w.get("kind") == "university" and w.get("cbsa")}
    wet = wet_cities(cities, world, world_web)

    nat_emp = nation["nation"]["employed"]
    nat_ppa_tot = sum(nat_ppa.values()) or 1.0
    nat_shares = {code: nat_ppa.get(title, 0.0) / nat_ppa_tot for code, title in PSIC_CODES.items()}
    industries = {
        code: {"title": title, "us_emp": max(1, round(nat_emp * nat_shares[code]))}
        for code, title in PSIC_CODES.items()
    }

    def eval_geo(sec_map: dict[str, float], total_emp: int, *, is_wet: bool, has_port: bool,
                 has_hub_air: bool, has_uni: bool, is_cap: bool, is_reg_cap: bool) -> tuple[list[dict], list[dict]]:
        tot_gva = sum(sec_map.values())
        if tot_gva <= 0 or total_emp <= 0:
            return [], []
        gva_sh = {code: sec_map.get(title, 0.0) / tot_gva for code, title in PSIC_CODES.items()}
        # Top industries by LQ
        top = []
        for code, sh in gva_sh.items():
            nsh = nat_shares.get(code, 0.0)
            emp_c = round(total_emp * sh)
            if emp_c < MIN_JOBS or nsh <= 0:
                continue
            ilq = sh / nsh
            top.append({
                "naics": code,
                "emp": emp_c,
                "lq": round(ilq, 2),
                "avg_pay": None,
                "oty_pct": None,
                "suppressed": 0,
                "lower_bound": False,
            })
        top.sort(key=lambda r: -r["lq"])

        dists = []
        for d in DISTRICT_CATALOG:
            did = d["id"]
            codes = PH_DISTRICT_CODES[did]
            if not codes:
                continue
            if did == "harbor" and not (has_port or is_wet):
                continue
            if did == "aerodrome" and not has_hub_air:
                continue
            sh = sum(gva_sh.get(c, 0.0) for c in codes)
            nsh = sum(nat_shares.get(c, 0.0) for c in codes)
            if nsh <= 0:
                continue
            dlq = sh / nsh
            # Infrastructure / anchor boosts for real Philippine hubs (ports, hub airports, research universities, capitals)
            if did == "harbor" and has_port:
                dlq = max(dlq * 1.35, 1.35)
            elif did == "aerodrome" and has_hub_air:
                dlq = max(dlq * 1.30, 1.30)
            elif did == "campus" and has_uni:
                dlq = max(dlq * 1.25, 1.35)
            elif did == "government_plaza" and (is_cap or is_reg_cap):
                dlq = max(dlq * 1.20, 1.30)

            emp_d = max(1, round(total_emp * sh))
            if dlq < MIN_LQ or (emp_d < MIN_JOBS and sh < DIST_MIN_SHARE):
                continue
            strength = excess(emp_d, dlq)
            dists.append({
                "id": did,
                "lq": round(dlq, 2),
                "emp": emp_d,
                "share": round(sh, 4),
                "avg_pay": None,
                "sub": None,
                "sub_lq": None,
                "sub_emp": None,
                "sub_lower_bound": None,
                "via": "broad",
                "strength": round(strength),
                "suppressed": 0,
                "lower_bound": False,
            })
        dists.sort(key=lambda r: -r["strength"])
        return top[:TOP_INDUSTRIES], dists[:MAX_PER_CITY]

    out_cities: dict[str, dict | None] = {}
    for c in cities:
        cid = c["id"]
        k = norm_name(c["name"])
        prov_name = geoid_to_prov.get(c["counties"][0], "") if c.get("counties") else ""
        pk = norm_name(prov_name)
        sec_map = (ppa_ind.get("nationalcapitalregion") if c.get("capital")
                   else (ppa_ind.get("cityof" + k) or ppa_ind.get(k) or ppa_ind.get(pk)))
        emp = c.get("employed") or round(c["population"] * 0.43)
        if not sec_map:
            out_cities[cid] = None
            continue
        is_cap = bool(c.get("capital"))
        is_reg_cap = k in REGIONAL_CAPITALS
        top, dists = eval_geo(
            sec_map, emp,
            is_wet=(cid in wet),
            has_port=(cid in port_cbsas),
            has_hub_air=(cid in hub_airport_cbsas),
            has_uni=(cid in uni_cbsas),
            is_cap=is_cap,
            is_reg_cap=is_reg_cap,
        )
        best = badge_district(dists)
        badge = badge_for(best, capital=is_cap, state_capital=is_reg_cap)
        if badge:
            badge["basis"] = "psa_ppa"
        out_cities[cid] = {
            "emp": emp,
            "avg_pay": None,
            "counties": [1, len(c.get("counties") or [1])],
            "top": top,
            "districts": dists,
            "badge": badge,
        }

    # Province-level districts
    out_counties = {}
    for cmeta in world_web["counties"]:
        geoid = cmeta["geoid"]
        pk = norm_name(cmeta["name"])
        sec_map = ppa_ind.get("nationalcapitalregion") if geoid == "13390" else ppa_ind.get(pk)
        if not sec_map:
            continue
        p_emp = 500000 if geoid == "13390" else 200000
        _, dists = eval_geo(
            sec_map, p_emp,
            is_wet=True, has_port=False, has_hub_air=False, has_uni=False,
            is_cap=(geoid == "13390"), is_reg_cap=False,
        )
        out_counties[geoid] = {
            "emp": p_emp,
            "districts": [[d["id"], d["lq"], d["emp"]] for d in dists],
        }

    catalog = []
    for d in DISTRICT_CATALOG:
        e = dict(d)
        e["naics"] = list(PH_DISTRICT_CODES[d["id"]])
        catalog.append(e)

    doc = {
        "version": 1,
        "vintage": "PSA Provincial Product Accounts (2024)",
        "year": 2024,
        "sources": ["psa_gdp_ppa", "hdx_ports", "ourairports", "unesco_heritage"],
        "rule": {
            "min_lq": MIN_LQ, "min_jobs": MIN_JOBS, "min_share": DIST_MIN_SHARE,
            "specialty_lq": SPECIALTY_LQ, "specialty_jobs": SPECIALTY_JOBS, "specialty_share": SPECIALTY_SHARE,
            "max_per_city": MAX_PER_CITY, "harbor_needs_water": True,
            "badge_lq": BADGE_LQ, "badge_share": BADGE_SHARE, "college_share": COLLEGE_SHARE,
            "text": (f"Philippine city districts are derived from PSA 2024 Provincial Product Accounts 16-industry "
                     f"GVA location quotients (LQ ≥ {MIN_LQ:g}), combined with mapped seaports (Harbor), commercial "
                     f"hub airports (Aerodrome), leading research universities (Campus), and regional capitals "
                     f"(Government Plaza). Up to {MAX_PER_CITY} districts per city, ranked by specialization strength."),
            "county_note": "Province districts use the same PSA PPA GVA location-quotient rule.",
        },
        "catalog": catalog,
        "industries": industries,
        "national": {"emp": nat_emp, "avg_pay": None},
        "cities": out_cities,
        "counties": out_counties,
    }
    write_web("districts.json", doc)
    print(f"  districts.json: {len(out_cities)} cities, {sum(1 for r in out_cities.values() if r and r['badge'])} with badges, {len(out_counties)} provinces")


def build() -> None:
    print("Building optional Philippine layers (trade, disasters, citizens, districts)...")
    world = load_interim("_world")
    world_web = json.loads((WEB_DATA / "world.json").read_text())
    cities = json.loads((WEB_DATA / "cities.json").read_text())
    states = json.loads((WEB_DATA / "states.json").read_text())
    nation = json.loads((WEB_DATA / "nation.json").read_text())
    proj = MapProjection(**world["projection"])
    grid = HexGrid(**world["grid"])

    build_trade(world, world_web, cities, states)
    build_disasters(world, world_web, proj, grid)
    build_citizens(cities, nation)
    build_districts(world, world_web, cities, nation)


if __name__ == "__main__":
    build()
