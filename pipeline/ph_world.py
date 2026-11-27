"""Build web/data/world.json and data/interim/_world.json for the Philippines.

Uses:
  - GADM 4.1 ADM1 (81 provinces + Metro Manila) grouped into 18 Philippine Regions
  - GADM 4.1 ADM2 (1,628 municipalities/cities + 19 waterbodies/lakes)
  - Natural Earth v5.1.2 (1:50m neighboring countries, 1:10m lakes & rivers)
  - Curated major Philippine river polylines supplementing Natural Earth 1:10m
  - NOAA ETOPO1 stride=1 (~1.8 km native resolution; falls back to stride=2)
  - PSA Land Classification (2021) + 2024 Census municipality density & urban share for per-hex land cover
"""

from __future__ import annotations

import csv
import json
import math
import re
import time
import unicodedata
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
    WATER,
    clip_polyline,
    is_valley_floor,
    join_lines,
    land_runs,
    majority,
    simplify,
)

PH_RAW = DATA / "philippines"

HEX_RADIUS_KM = 2.5
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

# Major Philippine rivers omitted from Natural Earth 1:10m (source -> mouth, (lon, lat))
# Natural Earth 1:10m already provides Cagayan (187 pts) and Rio Grande de Mindanao / Pulangi (94 pts).
PH_SUPPLEMENTAL_RIVERS: list[tuple[str, int, list[tuple[float, float]]]] = [
    # --- Luzon ---
    (
        "Pampanga",
        4,
        [
            (121.14, 15.84), (121.08, 15.72), (121.02, 15.60), (120.96, 15.49),
            (120.89, 15.36), (120.84, 15.25), (120.79, 15.15), (120.82, 15.08),
            (120.80, 14.99), (120.76, 14.91), (120.72, 14.84), (120.69, 14.77),
        ],
    ),
    (
        "Agno",
        5,
        [
            (120.82, 16.82), (120.78, 16.65), (120.74, 16.46), (120.70, 16.29),
            (120.68, 16.14), (120.66, 16.01), (120.63, 15.89), (120.54, 15.83),
            (120.45, 15.81), (120.36, 15.85), (120.28, 15.94), (120.23, 16.03),
        ],
    ),
    (
        "Chico",
        5,
        [
            (120.88, 16.92), (120.94, 17.01), (120.98, 17.09), (121.05, 17.18),
            (121.17, 17.27), (121.31, 17.34), (121.44, 17.41), (121.45, 17.56),
            (121.45, 17.73), (121.55, 17.82), (121.65, 17.90),
        ],
    ),
    (
        "Magat",
        5,
        [
            (120.92, 16.36), (121.03, 16.37), (121.11, 16.39), (121.15, 16.48),
            (121.24, 16.60), (121.35, 16.72), (121.45, 16.82), (121.55, 16.90),
            (121.67, 16.96), (121.81, 17.02),
        ],
    ),
    (
        "Abra",
        5,
        [
            (120.73, 16.98), (120.69, 17.07), (120.67, 17.15), (120.66, 17.28),
            (120.64, 17.43), (120.62, 17.59), (120.53, 17.57), (120.45, 17.54),
            (120.38, 17.52),
        ],
    ),
    (
        "Abulog",
        6,
        [
            (121.18, 18.02), (121.24, 18.10), (121.31, 18.17), (121.37, 18.23),
            (121.42, 18.34), (121.45, 18.45),
        ],
    ),
    (
        "Angat",
        6,
        [
            (121.24, 14.92), (121.15, 14.91), (121.04, 14.91), (120.95, 14.94),
            (120.88, 14.93), (120.82, 14.90), (120.76, 14.91),
        ],
    ),
    (
        "Pasig",
        5,
        [
            (121.11, 14.52), (121.08, 14.55), (121.04, 14.57), (121.00, 14.59),
            (120.96, 14.59),
        ],
    ),
    (
        "Bicol",
        5,
        [
            (123.36, 13.34), (123.35, 13.42), (123.29, 13.50), (123.23, 13.57),
            (123.18, 13.62), (123.14, 13.68), (123.11, 13.73),
        ],
    ),
    # --- Visayas ---
    (
        "Panay",
        5,
        [
            (122.40, 11.24), (122.50, 11.27), (122.62, 11.30), (122.69, 11.39),
            (122.76, 11.46), (122.76, 11.53), (122.75, 11.59),
        ],
    ),
    (
        "Jalaur",
        5,
        [
            (122.42, 11.14), (122.53, 11.13), (122.64, 11.11), (122.67, 11.00),
            (122.64, 10.94), (122.67, 10.88), (122.70, 10.82),
        ],
    ),
    (
        "Ilog-Hilabangan",
        6,
        [
            (122.92, 9.75), (122.89, 9.84), (122.86, 9.92), (122.82, 9.99),
            (122.74, 10.04),
        ],
    ),
    (
        "Bago",
        6,
        [
            (123.10, 10.42), (123.03, 10.48), (122.95, 10.52), (122.83, 10.54),
        ],
    ),
    (
        "Catubig",
        6,
        [
            (125.12, 12.22), (125.07, 12.28), (125.03, 12.34), (125.05, 12.43),
            (125.01, 12.56),
        ],
    ),
    (
        "Gandara",
        6,
        [
            (125.02, 12.06), (124.92, 12.04), (124.81, 12.01), (124.72, 11.97),
        ],
    ),
    # --- Mindanao ---
    (
        "Agusan",
        4,
        [
            (126.15, 7.55), (126.10, 7.68), (126.05, 7.82), (126.03, 7.98),
            (126.00, 8.14), (125.92, 8.28), (125.81, 8.41), (125.73, 8.54),
            (125.64, 8.68), (125.59, 8.78), (125.55, 8.89), (125.53, 8.99),
        ],
    ),
    (
        "Davao",
        5,
        [
            (125.33, 7.90), (125.34, 7.72), (125.35, 7.52), (125.39, 7.35),
            (125.46, 7.19), (125.54, 7.11), (125.61, 7.06),
        ],
    ),
    (
        "Cagayan de Oro",
        5,
        [
            (124.66, 8.14), (124.66, 8.23), (124.67, 8.31), (124.65, 8.40),
            (124.64, 8.50),
        ],
    ),
    (
        "Tagoloan",
        6,
        [
            (125.05, 8.28), (124.95, 8.32), (124.86, 8.37), (124.80, 8.45),
            (124.75, 8.54),
        ],
    ),
    (
        "Allah",
        6,
        [
            (124.72, 6.22), (124.74, 6.38), (124.68, 6.52), (124.60, 6.66),
            (124.55, 6.84), (124.51, 7.03),
        ],
    ),
    (
        "Agus",
        6,
        [
            (124.29, 8.00), (124.24, 8.07), (124.21, 8.14), (124.19, 8.21),
        ],
    ),
]

# Major inland marshlands / peat swamps (lon, lat, radius_deg) for realistic wetland hexes
PH_MARSHLANDS: list[tuple[float, float, float]] = [
    (125.84, 8.28, 0.22),   # Agusan Marsh (Agusan del Sur)
    (124.72, 6.96, 0.22),   # Liguasan Marsh (Maguindanao / Cotabato)
    (120.84, 15.08, 0.12),  # Candaba Swamp (Pampanga / Bulacan)
]

# Provinces with notable dry-season savanna / pasture / scrub hills
PASTURE_PROVINCES = {"bukidnon", "masbate", "occidentalmindoro", "sarangani", "southcotabato"}
SCRUB_PROVINCES = {"ilocosnorte", "ilocossur", "zambales", "pangasinan", "cebu", "bohol", "siquijor", "negrosoriental"}

# Curated Philippine geographic features for cartographic map labels:
# (name, kind, lon, lat, axis_endpoints_or_None, tier, elev_m_or_None, note)
PH_GEO_FEATURES: list[tuple[str, str, float, float, tuple[tuple[float, float], tuple[float, float]] | None, int, int | None, str]] = [
    # --- Seas & Gulfs ---
    ("West Philippine Sea", "sea", 118.20, 15.00, None, 1, None, "Eastern margin of the South China Sea bordering Luzon and Palawan"),
    ("Philippine Sea", "sea", 126.20, 15.20, None, 1, None, "Western Pacific marginal sea east of Luzon and the Visayas"),
    ("Sulu Sea", "sea", 120.80, 8.80, None, 1, None, "Deep inland sea between Palawan, the Visayas, and the Sulu Archipelago"),
    ("Celebes Sea", "sea", 122.40, 5.10, None, 1, None, "Deep ocean basin south of Mindanao and the Sulu Archipelago"),
    ("Visayan Sea", "sea", 123.65, 11.40, None, 2, None, "Rich fishing grounds bounded by Masbate, Leyte, Cebu, Negros, and Panay"),
    ("Sibuyan Sea", "sea", 122.55, 12.60, None, 2, None, "Central archipelago sea separating Luzon from the Visayas"),
    ("Bohol Sea", "sea", 124.25, 9.20, None, 2, None, "Mindanao Sea between Bohol, Negros, Leyte, and northern Mindanao"),
    ("Manila Bay", "sea", 120.76, 14.52, None, 2, None, "Natural deep-water harbor fronting Metro Manila, Cavite, and Bataan"),
    ("Lingayen Gulf", "sea", 120.18, 16.22, None, 2, None, "Broad northwestern Luzon gulf off Pangasinan and La Union"),
    ("Lamon Bay", "sea", 122.05, 14.35, None, 2, None, "Pacific bay along the eastern coast of Quezon province"),
    ("Davao Gulf", "sea", 125.78, 6.72, None, 2, None, "Deep southern Mindanao gulf bordered by Davao City and Davao Oriental"),
    ("Moro Gulf", "sea", 123.50, 6.90, None, 2, None, "Largest gulf in the Philippines, off Zamboanga and Cotabato"),
    # --- Mountain Ranges ---
    ("Cordillera Central", "range", 120.95, 17.25, ((120.75, 16.45), (121.15, 18.10)), 1, None, "Highest and most extensive mountain system in northern Luzon"),
    ("Sierra Madre", "range", 122.12, 16.45, ((121.75, 15.20), (122.35, 17.85)), 1, None, "Longest mountain range in the Philippines, shielding eastern Luzon from typhoons"),
    ("Zambales Mountains", "range", 120.22, 15.42, ((120.12, 15.90), (120.32, 14.95)), 2, None, "Volcanic coastal range along western Central Luzon, home to Mt. Pinatubo"),
    ("Caraballo Mountains", "range", 121.05, 16.12, ((120.78, 16.18), (121.35, 16.05)), 2, None, "Central mountain knot linking Cordillera Central and the Sierra Madre"),
    ("Central Panay Range", "range", 122.28, 11.15, ((122.22, 10.65), (122.35, 11.65)), 2, None, "North-south spine of Panay Island separating Antique from Iloilo and Capiz"),
    ("Kitanglad Range", "range", 124.88, 8.13, ((124.70, 8.15), (125.08, 8.10)), 2, None, "ASEAN Heritage Park in Bukidnon with several of Mindanao's highest peaks"),
    ("Pantaron Range", "range", 125.35, 7.90, ((125.30, 7.35), (125.42, 8.45)), 2, None, "Central Mindanao spine and major watershed feeding the Pulangi and Davao rivers"),
    ("Diwata Mountains", "range", 126.05, 8.55, ((126.15, 7.80), (125.92, 9.25)), 2, None, "Eastern Mindanao coastal range along Surigao and Agusan"),
    ("Daguma Range", "range", 124.45, 6.42, ((124.20, 6.65), (124.72, 6.18)), 2, None, "Southwestern Mindanao range bordering Sultan Kudarat and South Cotabato"),
    # --- Major Peaks & Volcanoes ---
    ("Mt. Apo", "peak", 125.271, 6.987, None, 1, 2954, "Highest peak in the Philippines (Davao del Sur / Cotabato)"),
    ("Mt. Dulang-dulang", "peak", 124.922, 8.115, None, 2, 2938, "Second-highest peak in the Philippines (Kitanglad Range, Bukidnon)"),
    ("Mt. Pulag", "peak", 120.899, 16.597, None, 1, 2926, "Highest peak in Luzon, famed for its mossy forest and sea of clouds"),
    ("Mt. Kitanglad", "peak", 124.920, 8.190, None, 2, 2899, "Fourth-highest peak in the Philippines (Bukidnon)"),
    ("Mt. Halcon", "peak", 120.994, 13.261, None, 2, 2586, "Highest peak on Mindoro Island"),
    ("Mt. Kanlaon", "peak", 123.132, 10.412, None, 1, 2465, "Highest peak in the Visayas and active stratovolcano on Negros Island"),
    ("Mt. Mayon", "peak", 123.685, 13.257, None, 1, 2462, "Active stratovolcano in Albay famed for its near-perfect cone"),
    ("Mt. Banahaw", "peak", 121.492, 14.067, None, 2, 2170, "Sacred volcanic massif straddling Laguna and Quezon"),
    ("Mt. Mantalingajan", "peak", 117.674, 8.818, None, 2, 2085, "Highest peak on Palawan Island (Protected Landscape)"),
    ("Mt. Guiting-Guiting", "peak", 122.567, 12.415, None, 2, 2058, "Jagged peak of Sibuyan Island, Romblon"),
    ("Mt. Bulusan", "peak", 124.050, 12.770, None, 2, 1565, "Active volcano at the southern tip of the Bicol Peninsula (Sorsogon)"),
    ("Mt. Pinatubo", "peak", 120.350, 15.130, None, 2, 1486, "Active caldera volcano in the Zambales Mountains (1991 eruption)"),
    ("Mt. Makiling", "peak", 121.195, 14.136, None, 2, 1090, "Dormant volcano and ASEAN Heritage Park above Los Baños, Laguna"),
    # --- Lakes ---
    ("Laguna de Bay", "lake", 121.26, 14.36, None, 1, None, "Largest lake in the Philippines (~911 km²), southeast of Metro Manila"),
    ("Lake Lanao", "lake", 124.25, 7.88, None, 1, None, "Second-largest lake in the Philippines and ancient tectonic lake in Lanao del Sur"),
    ("Taal Lake", "lake", 121.00, 13.98, None, 1, None, "Volcanic caldera lake in Batangas enclosing Taal Volcano Island"),
    ("Lake Mainit", "lake", 125.52, 9.47, None, 2, None, "Deepest lake in the Philippines (Surigao del Norte / Agusan del Norte)"),
    ("Naujan Lake", "lake", 121.35, 13.17, None, 2, None, "Fifth-largest lake in the Philippines (Ramsar wetland, Oriental Mindoro)"),
    ("Lake Buluan", "lake", 124.83, 6.64, None, 2, None, "Freshwater lake in the Cotabato Basin (Maguindanao / Sultan Kudarat)"),
    ("Lake Sebu", "lake", 124.71, 6.22, None, 2, None, "Highland lake and cultural watershed of South Cotabato"),
    # --- Forests, Wetlands & Biosphere Reserves ---
    ("Northern Sierra Madre Forest", "forest", 122.25, 17.15, None, 1, None, "Largest protected rainforest in the Philippines (Isabela / Cagayan)"),
    ("Palawan Biosphere Reserve", "forest", 118.95, 9.85, None, 1, None, "UNESCO Biosphere Reserve spanning Palawan's intact tropical forests and reefs"),
    ("Agusan Marsh", "forest", 125.84, 8.28, None, 2, None, "Vast freshwater peat swamp and Ramsar wetland in Agusan del Sur"),
    ("Candaba Swamp", "forest", 120.84, 15.08, None, 2, None, "Seasonal floodplain wetland and migratory bird sanctuary in Pampanga"),
    ("Liguasan Marsh", "forest", 124.72, 6.96, None, 2, None, "Largest marshland in Mindanao, regulating the Rio Grande de Mindanao basin"),
    ("Mt. Hamiguitan Forest", "forest", 126.18, 6.74, None, 2, None, "UNESCO World Heritage pygmy bonsai forest in Davao Oriental"),
    ("Bataan Natural Park", "forest", 120.43, 14.70, None, 2, None, "Mountain rainforest reserve covering Mt. Natib on the Bataan Peninsula"),
    # --- Plains & Valleys ---
    ("Cagayan Valley", "plain", 121.72, 17.25, None, 1, None, "Broad fertile alluvial valley between the Cordillera and Sierra Madre"),
    ("Central Luzon Plain", "plain", 120.78, 15.48, None, 1, None, "Rice Granary of the Philippines, spanning Nueva Ecija, Tarlac, Pampanga, and Bulacan"),
    ("Bicol Plain", "plain", 123.30, 13.52, None, 2, None, "Fertile alluvial lowland of Camarines Sur and Albay along the Bicol River"),
    ("Cotabato Basin", "plain", 124.58, 7.12, None, 2, None, "Wide alluvial basin of central Mindanao drained by the Rio Grande de Mindanao"),
    ("Agusan Valley", "plain", 125.78, 8.62, None, 2, None, "Fertile eastern Mindanao river valley along the Agusan River"),
]

RIVER_LABEL_PRIORITY = {
    "Cagayan",
    "Rio Grande de Mindanao",
    "Agusan",
    "Pampanga",
    "Pasig",
    "Bicol",
    "Agno",
    "Chico",
    "Magat",
    "Abra",
    "Panay",
    "Jalaur",
    "Davao",
    "Cagayan de Oro",
}

PROV_ALIASES = {
    "mtprovince": "mountainprovince",
    "compostelavalley": "davaodeoro",
    "nationalcapitalregion": "metromanila",
    "ncr": "metromanila",
    "maguindanaodelnorte": "maguindanao",
    "maguindanaodelsur": "maguindanao",
    "davaooccidental": "davaodelsur",
    "cotabatonorthcotabato": "cotabato",
    "northcotabato": "cotabato",
    "westernsamar": "samar",
}


def read_psa_csv(path: Path) -> list[list[str]]:
    """Read a PSA CSV file handling UTF-8-SIG with CP1252 fallback for ñ/Ñ characters."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return list(csv.reader(text.splitlines()))


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"^\.+", "", s.strip())
    s = re.sub(r"\s+\d+/\s*$", "", s)
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return s


def norm_muni(s: str) -> str:
    """Normalize a municipality/city name stripping 'City of ', trailing 'City', and footnotes."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"^\.+", "", s.strip())
    s = re.sub(r"\s+[a-z]?\d+/\s*", " ", s)
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"^(city|municipality)\s+of\s+", "", s.strip(), flags=re.IGNORECASE)
    s = re.sub(r"\s*city$", "", s.strip(), flags=re.IGNORECASE)
    k = re.sub(r"[^a-z0-9]+", "", s.lower())
    return k


def split_gadm_camel(s: str) -> str:
    """Insert spaces in GADM2 CamelCase names like 'ButuanCity' -> 'Butuan City'."""
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = re.sub(r"\bdel(?=[A-Z])", "del ", s)
    return s.strip()


def classify_relief_ph(mean: float, std: float) -> int:
    """Tropical archipelago relief thresholds (tuned for Cordillera, Sierra Madre, Mt. Apo, etc.)."""
    if mean >= 1700:
        return PEAKS
    if std >= 200 or mean >= 1000:
        return MOUNTAINS
    if std >= 65 or mean >= 350:
        return HILLS
    return FLAT


def load_psa_land_stats() -> tuple[dict[str, float], dict[str, float]]:
    """Return (forestland_share, fishpond_share) by normalized province name from PSA Land Classification 2021."""
    path = PH_RAW / "psa_land/land_classification_region_province_2021.csv"
    forest_out: dict[str, float] = {}
    fishpond_out: dict[str, float] = {}
    if not path.exists():
        return forest_out, fishpond_out
    rows = read_psa_csv(path)
    for row in rows[1:]:
        if len(row) < 4:
            continue
        try:
            total = float(row[1])
            forest = float(row[3])
        except ValueError:
            continue
        if total <= 0:
            continue
        f_share = max(0.0, min(1.0, forest / total))
        try:
            fp_share = max(0.0, min(1.0, float(row[11]) / total)) if len(row) > 11 else 0.0
        except ValueError:
            fp_share = 0.0
        parts = re.split(r"\s+and\s+", row[0].strip(), flags=re.IGNORECASE)
        for part in parts:
            k = PROV_ALIASES.get(norm_name(part), norm_name(part))
            if k:
                forest_out[k] = f_share
                fishpond_out[k] = fp_share
    return forest_out, fishpond_out


def load_psa_muni_density() -> tuple[dict[tuple[str, str], tuple[float, float]], dict[str, tuple[float, float]]]:
    """Return ((prov_key, muni_key) -> (pop_2024, density_2024), muni_key -> (pop, dens)) from PSA 2024."""
    path = PH_RAW / "psa_census_2024/pop_area_density_2015_2024.csv"
    by_pm: dict[tuple[str, str], tuple[float, float]] = {}
    by_m: dict[str, tuple[float, float]] = {}
    if not path.exists():
        return by_pm, by_m
    cur_prov = ""
    in_ncr = False
    for row in read_psa_csv(path)[1:]:
        if len(row) < 8:
            continue
        loc = row[0]
        dots = len(loc) - len(loc.lstrip("."))
        if dots == 2:
            in_ncr = "national capital" in loc.lower() or "(ncr)" in loc.lower()
            cur_prov = "metromanila" if in_ncr else ""
            continue
        try:
            pop = float(row[3])
            dens = float(row[7])
        except ValueError:
            if dots == 4 and not in_ncr:
                pk = norm_name(loc)
                cur_prov = PROV_ALIASES.get(pk, pk)
            continue
        if dots == 4 and not in_ncr:
            pk = norm_name(loc)
            cur_prov = PROV_ALIASES.get(pk, pk)
            # Some HUCs appear at level 4 (e.g. City of Baguio, City of Cebu)
            if "city" in loc.lower():
                mk = norm_muni(loc)
                if mk:
                    by_m[mk] = (pop, dens)
                    if cur_prov:
                        by_pm[(cur_prov, mk)] = (pop, dens)
            continue
        if (dots == 4 and in_ncr) or dots >= 6:
            mk = norm_muni(loc)
            if mk:
                if cur_prov:
                    by_pm[(cur_prov, mk)] = (pop, dens)
                by_m.setdefault(mk, (pop, dens))
    return by_pm, by_m


def load_psa_urban_shares() -> tuple[dict[tuple[str, str], float], dict[str, float], dict[str, float]]:
    """Return (by_(prov, muni), by_muni, by_prov) urban share (0..1) from 2024 Census."""
    path = PH_RAW / "psa_census_2024/urban_pop_2024.csv"
    by_pm: dict[tuple[str, str], float] = {}
    by_m: dict[str, float] = {}
    by_p: dict[str, float] = {}
    if not path.exists():
        return by_pm, by_m, by_p
    cur_prov = ""
    in_ncr = False
    for row in read_psa_csv(path)[1:]:
        if len(row) < 4:
            continue
        loc = row[0]
        dots = len(loc) - len(loc.lstrip("."))
        if dots == 2:
            in_ncr = "national capital" in loc.lower() or "(ncr)" in loc.lower()
            cur_prov = "metromanila" if in_ncr else ""
            try:
                if in_ncr:
                    by_p["metromanila"] = max(0.0, min(1.0, float(row[3]) / 100.0))
            except ValueError:
                pass
            continue
        try:
            share = max(0.0, min(1.0, float(row[3]) / 100.0))
        except ValueError:
            if dots == 4 and not in_ncr:
                pk = norm_name(loc)
                cur_prov = PROV_ALIASES.get(pk, pk)
            continue
        if dots == 4 and not in_ncr:
            pk = norm_name(loc)
            pk = PROV_ALIASES.get(pk, pk)
            if "city" in loc.lower():
                mk = norm_muni(loc)
                if mk:
                    by_m[mk] = share
                    if cur_prov:
                        by_pm[(cur_prov, mk)] = share
            else:
                cur_prov = pk
                if pk:
                    by_p[pk] = share
        elif (dots == 4 and in_ncr) or dots >= 6:
            mk = norm_muni(loc)
            if mk:
                if cur_prov:
                    by_pm[(cur_prov, mk)] = share
                by_m.setdefault(mk, share)
    return by_pm, by_m, by_p


def ring_area_km2(proj: MapProjection, ring: list[tuple[float, float]]) -> float:
    pts = [proj(lon, lat) for lon, lat in ring]
    area = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def build() -> None:
    t0 = time.perf_counter()
    print(f"Building Philippines world.json (HEX_RADIUS_KM={HEX_RADIUS_KM:g})...")

    # 1. Load GADM ADM1 provinces (for projection bounds & province/region tables)
    with zipfile.ZipFile(PH_RAW / "gadm_boundaries/gadm41_PHL_1.json.zip") as z:
        gadm1 = json.loads(z.read(z.namelist()[0]))

    counties = []
    for feat in gadm1["features"]:
        raw_name = feat["properties"]["NAME_1"]
        name, geoid, reg_fips = PROVINCE_META[raw_name]
        rings = geojson_rings(feat["geometry"])
        counties.append({"geoid": geoid, "name": name, "state_fips": reg_fips, "raw_name": raw_name, "rings": rings})
    counties.sort(key=lambda c: c["geoid"])

    states = list(REGIONS)
    state_idx_by_fips = {s["fips"]: idx for idx, s in enumerate(states)}
    county_idx_by_raw = {c["raw_name"]: idx for idx, c in enumerate(counties)}

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

    # 3. Load GADM ADM2 (1,628 municipalities/cities + 19 waterbodies/lakes)
    with zipfile.ZipFile(PH_RAW / "gadm_boundaries/gadm41_PHL_2.json.zip") as z:
        gadm2 = json.loads(z.read(z.namelist()[0]))

    municipalities: list[dict] = []
    adm2_shapes: list[Shape] = []
    adm2_county: list[int] = []
    adm2_state: list[int] = []
    lake_shapes: list[Shape] = []

    for feat in gadm2["features"]:
        props = feat["properties"]
        t2 = props.get("TYPE_2") or ""
        rings = geojson_rings(feat["geometry"])
        if not rings:
            continue
        if t2 == "Waterbody":
            area = sum(ring_area_km2(proj, r) for r in rings)
            if area >= 4.0:
                lake_shapes.append(make_shape(rings))
            continue
        raw_prov = props["NAME_1"]
        c_idx = county_idx_by_raw[raw_prov]
        prov_name, _, reg_fips = PROVINCE_META[raw_prov]
        s_idx = state_idx_by_fips[reg_fips]
        raw_muni = props.get("NAME_2") or ""
        municipalities.append(
            {
                "gid": props.get("GID_2") or "",
                "cc": props.get("CC_2") or "",
                "name": split_gadm_camel(raw_muni),
                "raw_name": raw_muni,
                "province": prov_name,
                "county": c_idx,
                "state_fips": reg_fips,
                "type": props.get("ENGTYPE_2") or "",
            }
        )
        adm2_shapes.append(make_shape(rings))
        adm2_county.append(c_idx)
        adm2_state.append(s_idx)

    # Fine-grained spatial index (15 km buckets) keeps ~154k hex lookups fast
    adm2_idx = ShapeIndex(adm2_shapes, cell_km=15)

    # 4. Natural Earth: neighboring countries (per-polygon for fast ocean rejection) & non-PH 1:10m lakes
    def in_ph_window(rings_or_line, is_rings=True):
        pts = [p for r in rings_or_line for p in r] if is_rings else rings_or_line
        return any(112 <= lon <= 130 and 1 <= lat <= 25 for lon, lat in pts)

    def in_core_ph(rings):
        pts = [p for r in rings for p in r]
        return any(116.5 <= lon <= 126.8 and 4.5 <= lat <= 21.2 for lon, lat in pts)

    ne_dir = PH_RAW / "natural_earth/v5.1.2"
    foreign_shapes = []
    for feat in json.loads((ne_dir / "ne_50m_admin_0_countries.geojson").read_text())["features"]:
        iso = feat["properties"].get("ISO_A3") or feat["properties"].get("ADM0_A3") or ""
        if iso == "PHL":
            continue
        geom = feat["geometry"]
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"] if geom["type"] == "MultiPolygon" else []
        for poly in polys:
            rings = [[(round(x, 5), round(y, 5)) for x, y, *_ in ring] for ring in poly]
            if in_ph_window(rings, True):
                foreign_shapes.append(make_shape(rings))
    foreign_idx = ShapeIndex(foreign_shapes, cell_km=30)

    # Add Natural Earth 1:10m lakes outside core PH (core PH lakes come from higher-res GADM2 Waterbodies
    # so features like Taal Volcano Island are preserved via polygon holes)
    lakes_10m = ne_dir / "ne_10m_lakes.geojson"
    lakes_path = lakes_10m if lakes_10m.exists() else (ne_dir / "ne_50m_lakes.geojson")
    for feat in json.loads(lakes_path.read_text())["features"]:
        rings = geojson_rings(feat["geometry"])
        if in_ph_window(rings, True) and not in_core_ph(rings):
            lake_shapes.append(make_shape(rings))
    lake_idx = ShapeIndex(lake_shapes, cell_km=15)

    # 5. ETOPO1 elevation & bathymetry (prefer full-resolution stride=1 ~1.8 km)
    etopo_stride1 = PH_RAW / "etopo/etopo_phl_lat4_22_lon115_128_stride1.csv"
    etopo_path = etopo_stride1 if etopo_stride1.exists() else (PH_RAW / "etopo/etopo_phl_lat4_22_lon115_128_stride2.csv")
    etopo_res_label = "~1.8 km stride=1" if etopo_path == etopo_stride1 else "~3.7 km stride=2"
    print(f"  loading ETOPO1 ({etopo_path.name})...")

    stats_all = [[0, 0.0, 0.0] for _ in range(grid.size)]
    stats_land = [[0, 0.0, 0.0] for _ in range(grid.size)]
    samples_land: list[list[float]] = [[] for _ in range(grid.size)]
    below_sea = [0] * grid.size
    below_sum = [0.0] * grid.size

    with open(etopo_path, encoding="utf-8") as f:
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

    # 6. Single-pass 7-point ownership + county + municipality lookup
    offsets = [(0.0, 0.0)] + [
        (0.6 * HEX_RADIUS_KM * math.cos(math.radians(60 * k - 30)), 0.6 * HEX_RADIUS_KM * math.sin(math.radians(60 * k - 30)))
        for k in range(6)
    ]
    owner = [WATER] * grid.size
    water = [OCEAN] * grid.size
    county = [-1] * grid.size
    municipality = [-1] * grid.size
    county_samples: list[list[int]] = [[] for _ in range(grid.size)]

    for i in range(grid.size):
        cx, cy = grid.center(*grid.col_row(i))
        kinds = []
        muni_hits = []
        for dx, dy in offsets:
            x, y = cx + dx, cy + dy
            if lake_idx.locate(x, y) >= 0:
                kinds.append("lake")
                muni_hits.append(-1)
            elif (m := adm2_idx.locate(x, y)) >= 0:
                kinds.append(adm2_state[m])
                muni_hits.append(m)
            elif foreign_idx.locate(x, y) >= 0:
                kinds.append(FOREIGN)
                muni_hits.append(-1)
            else:
                kinds.append("sea")
                muni_hits.append(-1)

        land = [k for k in kinds if not isinstance(k, str)]
        ph_pts = [k for k in land if k >= 0]
        n_lake = kinds.count("lake")
        m_all, _ = mean_std(stats_all[i])

        # Major lakes stay LAKE rather than being swallowed by surrounding land
        is_lake = (kinds[0] == "lake") or (n_lake >= 3 and kinds[0] != "sea")
        # Keep small Philippine islands (Boracay, Camiguin, Mactan, Siquijor, Taal Volcano Island, etc.) crisp
        is_ph_island = (not is_lake) and (len(ph_pts) >= 3 or (isinstance(kinds[0], int) and kinds[0] >= 0))
        tidal = kinds[0] == "sea" and m_all is not None and (m_all <= -5 or sea_share(i) >= 0.7)

        if not is_lake and (is_ph_island or len(land) * 2 > len(kinds)) and not tidal:
            if ph_pts:
                s_own = majority(ph_pts, kinds[0] if isinstance(kinds[0], int) and kinds[0] >= 0 else ph_pts[0])
                owner[i] = s_own
                csmp = [adm2_county[m] if m >= 0 else -1 for m in muni_hits]
                county_samples[i] = csmp
                # Choose municipality & county consistent with owner region
                valid_m = [m for m in muni_hits if m >= 0 and adm2_state[m] == s_own]
                if not valid_m:
                    valid_m = [m for m in muni_hits if m >= 0]
                if valid_m:
                    m_best = muni_hits[0] if muni_hits[0] in valid_m else Counter(valid_m).most_common(1)[0][0]
                    municipality[i] = m_best
                    county[i] = adm2_county[m_best]
            else:
                owner[i] = FOREIGN
            water[i] = LAND
        else:
            wet = [k for k in kinds if isinstance(k, str)]
            above_sea = m_all is not None and m_all > 0
            lakeish = is_lake or ("lake" in wet and (above_sea or majority(wet, kinds[0] if kinds[0] in wet else "lake") == "lake"))
            water[i] = LAKE if lakeish else OCEAN

    # 7. Elevation, relief, coast/ocean
    elev = [0] * grid.size
    relief = [FLAT] * grid.size
    elev_max = [0] * grid.size
    elev_std = [0.0] * grid.size
    for i in range(grid.size):
        if water[i] == LAND:
            m, sd = mean_std(stats_land[i])
            if m is None:
                m, sd = mean_std(stats_all[i])
                m = None if m is None else max(m, 0.0)
            if m is None:
                continue
            elev[i] = round(m)
            elev_std[i] = sd or 0.0
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

    # 8. Land cover from PSA provincial forest/fishpond + PSA 2024 municipality density/urban share + ETOPO1 relief
    # Cover ids: 1 deciduous, 2 evergreen, 3 mixed, 4 scrub, 5 grass, 6 pasture, 7 cropland, 8 wetland, 9 urban
    forest_by_prov, fishpond_by_prov = load_psa_land_stats()
    dens_by_pm, dens_by_m = load_psa_muni_density()
    urban_by_pm, urban_by_m, urban_by_prov = load_psa_urban_shares()

    # Precompute per-municipality (pop_2024, density_2024, urban_share)
    muni_stats: list[tuple[float, float, float]] = []
    for m in municipalities:
        pk = PROV_ALIASES.get(norm_name(m["province"]), norm_name(m["province"]))
        mk = norm_muni(m["name"])
        pop, dens = dens_by_pm.get((pk, mk)) or dens_by_m.get(mk) or (0.0, 0.0)
        u_sh = urban_by_pm.get((pk, mk))
        if u_sh is None:
            u_sh = urban_by_m.get(mk)
        if u_sh is None:
            u_sh = urban_by_prov.get(pk, 0.35)
        muni_stats.append((pop, dens, u_sh))

    cover = [0] * grid.size
    pct = {k: [0] * grid.size for k in PCT}

    for i in range(grid.size):
        if owner[i] < 0:
            continue
        cidx = county[i]
        midx = municipality[i]
        pname = norm_name(counties[cidx]["name"]) if cidx >= 0 else ""
        is_ncr = states[owner[i]]["fips"] == "13"
        f_base = forest_by_prov.get(pname, 0.52)
        fp_base = fishpond_by_prov.get(pname, 0.0)
        pop_m, dens_m, u_muni = muni_stats[midx] if midx >= 0 else (0.0, 0.0, urban_by_prov.get(pname, 0.35))
        e = elev[i]
        sd = elev_std[i]
        rl = relief[i]
        cx, cy = grid.center(*grid.col_row(i))
        lon, lat = proj.inverse(cx, cy)

        near_water = any(water[j] in (COAST, LAKE) for j in grid.neighbours(i))
        in_marsh = any(math.hypot(lon - mlon, lat - mlat) <= mrad for mlon, mlat, mrad in PH_MARSHLANDS)

        # Urban classification grounded in real PSA 2024 municipality density & urban share
        is_urban = (
            is_ncr
            or (dens_m >= 2500 and u_muni >= 0.60 and e < 1600)
            or (dens_m >= 1500 and u_muni >= 0.80 and e <= 300)
            or (dens_m >= 1000 and u_muni >= 0.90 and e <= 120)
        )

        fr = cr = pa = gr = sh = wt = dev = 0

        if is_ncr:
            dev = min(88, max(68, round(58 + min(1.0, dens_m / 25000.0) * 30)))
            rem = 100 - dev
            fr = round(rem * 0.25)
            wt = 4 if near_water else 1
            wt = min(wt, rem - fr)
            cr = round((rem - fr - wt) * 0.25)
            gr = max(0, rem - fr - wt - cr)
            cid = 9  # urban
        elif is_urban:
            dev = min(76, max(32, round(28 + min(1.0, dens_m / 8000.0) * 36 + u_muni * 12)))
            rem = 100 - dev
            fr = round(rem * min(0.45, f_base * 0.55))
            wt = 6 if (near_water and e <= 15) else 2
            wt = min(wt, rem - fr)
            cr = round((rem - fr - wt) * 0.55)
            gr = max(0, rem - fr - wt - cr)
            cid = 9  # urban
        elif rl >= MOUNTAINS or e >= 600:
            fr = min(90, round(58 + f_base * 30))
            sh = max(3, round((100 - fr) * 0.35)) if (pname in SCRUB_PROVINCES or sd >= 240) else 0
            gr = max(4, round((100 - fr - sh) * 0.45))
            cr = max(0, 100 - fr - sh - gr - 2)
            dev = 2
            cid = 2 if e >= 900 else 3  # evergreen montane/pine vs tropical mixed forest
        elif in_marsh and e <= 80:
            wt = 52
            fr = round(22 * (0.6 + f_base * 0.5))
            cr = max(8, 100 - wt - fr - 8)
            gr = max(0, 100 - wt - fr - cr - 3)
            dev = max(0, 100 - wt - fr - cr - gr)
            cid = 8  # wetland
        elif rl == HILLS or e >= 200:
            if pname in PASTURE_PROVINCES and f_base < 0.62 and e < 550:
                pa = 36
                gr = 20
                fr = min(28, round(f_base * 42))
                cr = max(6, 100 - pa - gr - fr - 6)
                dev = max(0, 100 - pa - gr - fr - cr)
                cid = 6  # pasture
            elif pname in SCRUB_PROVINCES and f_base < 0.45:
                sh = 38
                fr = min(28, round(f_base * 55))
                cr = round((100 - sh - fr) * 0.45)
                gr = max(4, 100 - sh - fr - cr - 6)
                dev = max(0, 100 - sh - fr - cr - gr)
                cid = 4  # scrub
            else:
                fr = min(80, round(32 + f_base * 46))
                dev = min(24, round(u_muni * 16 + min(1.0, dens_m / 2000.0) * 10))
                rem = max(0, 100 - fr - dev)
                cr = round(rem * 0.48)
                gr = round(rem * 0.34)
                pa = max(0, rem - cr - gr)
                if fr >= cr and fr >= gr:
                    cid = 3 if e >= 350 else 1  # mixed vs tropical broadleaf forest
                elif gr >= cr:
                    cid = 5  # grassland
                else:
                    cid = 7  # upland cropland
        else:
            # Flat lowlands: coastal wetlands/mangroves, rice/coconut/sugar plains, or town periphery
            dev = min(28, round(u_muni * 18 + min(1.0, dens_m / 2000.0) * 12))
            coastal_wet = near_water and e <= 15 and (fp_base >= 0.008 or (e <= 6 and f_base >= 0.42))
            if coastal_wet:
                wt = min(54, round(36 + fp_base * 450))
                fr = min(28, round(f_base * 34))
                rem = max(0, 100 - wt - fr - dev)
                cr = round(rem * 0.65)
                gr = rem - cr
                cid = 8  # coastal wetland / mangrove / fishpond belt
            else:
                wt = 10 if (near_water and e <= 15) else 3
                fr = min(52, round(f_base * 44))
                rem = max(0, 100 - dev - fr - wt)
                if pname in PASTURE_PROVINCES and f_base < 0.50:
                    pa = round(rem * 0.45)
                    cr = round(rem * 0.35)
                    gr = rem - pa - cr
                else:
                    cr = round(rem * 0.68)
                    gr = round(rem * 0.20)
                    pa = rem - cr - gr
                if cr >= fr and cr >= pa and cr >= wt:
                    cid = 7  # cropland (rice/coconut/sugar plains)
                elif pa >= fr and pa >= wt:
                    cid = 6  # pasture
                elif wt >= fr:
                    cid = 8  # wetland
                else:
                    cid = 1  # tropical broadleaf forest

        pct["forest"][i] = fr
        pct["crop"][i] = cr
        pct["pasture"][i] = pa
        pct["grass"][i] = gr
        pct["shrub"][i] = sh
        pct["wetland"][i] = wt
        pct["developed"][i] = dev
        cover[i] = cid

    # 9. Rivers from Natural Earth 1:10m + curated major Philippine rivers
    def on_land(pt):
        c = grid.cell_at(*pt)
        return c >= 0 and water[c] == LAND

    by_river: dict[tuple[str, int], list] = {}
    riv_10m = ne_dir / "ne_10m_rivers_lake_centerlines.geojson"
    riv_path = riv_10m if riv_10m.exists() else (ne_dir / "ne_50m_rivers_lake_centerlines.geojson")
    for feat in json.loads(riv_path.read_text())["features"]:
        props = feat["properties"]
        rank = int(round(float(props.get("scalerank") or 6)))
        name = props.get("name") or ""
        for raw_line in geojson_lines(feat["geometry"]):
            if not in_ph_window(raw_line, False):
                continue
            in_core_ph_line = any(116.5 <= lon <= 126.8 and 5.0 <= lat <= 19.5 for lon, lat in raw_line)
            if in_core_ph_line:
                if name.lower() == "cagayan":
                    name, rank = "Cagayan", 4
                elif not name and any(124.0 <= lon <= 125.6 and 6.8 <= lat <= 8.6 for lon, lat in raw_line):
                    name, rank = "Rio Grande de Mindanao", 4
            line = [proj(lon, lat) for lon, lat in raw_line]
            if (props.get("featurecla") or "").startswith("Lake Centerline"):
                parts = [run for p in clip_polyline(line, width, height) for run in land_runs(p, on_land)]
            else:
                parts = clip_polyline(line, width, height)
            by_river.setdefault((name, rank), []).extend(parts)

    for rname, rrank, raw_line in PH_SUPPLEMENTAL_RIVERS:
        line = [proj(lon, lat) for lon, lat in raw_line]
        parts = clip_polyline(line, width, height)
        by_river.setdefault((rname, rrank), []).extend(parts)

    rivers = []
    for (name, rank), parts in by_river.items():
        for piece in join_lines(parts):
            if sum(math.dist(a, b) for a, b in zip(piece, piece[1:])) < 1.0:
                continue
            pts = simplify(piece, 1.0)
            if len(pts) >= 2:
                rivers.append({"name": name, "rank": rank, "pts": [round(v, 1) for p in pts for v in p]})

    # 10. Geographic feature labels (seas, mountain ranges, peaks, lakes, forests/wetlands, plains, major rivers)
    def upright_angle(dx: float, dy: float) -> float:
        ang = math.degrees(math.atan2(dy, dx))
        if ang > 90.0:
            ang -= 180.0
        elif ang < -90.0:
            ang += 180.0
        return round(max(-80.0, min(80.0, ang)), 1)

    features: list[dict] = []
    for fname, fkind, flon, flat, axis, ftier, felev, fnote in PH_GEO_FEATURES:
        fx, fy = proj(flon, flat)
        if axis is not None:
            ax0, ay0 = proj(*axis[0])
            ax1, ay1 = proj(*axis[1])
            ang = upright_angle(ax1 - ax0, ay1 - ay0)
        else:
            ang = 0.0
        features.append(
            {
                "name": fname,
                "kind": fkind,
                "x": round(fx, 1),
                "y": round(fy, 1),
                "angle": ang,
                "tier": ftier,
                "elev_m": felev,
                "note": fnote,
            }
        )

    # River labels: midpoint (x, y) and local tangent angle from the projected river polylines
    river_best: dict[str, tuple[float, int, list[tuple[float, float]]]] = {}
    for rv in rivers:
        rname = rv["name"]
        if not rname:
            continue
        p = rv["pts"]
        coords = [(p[k], p[k + 1]) for k in range(0, len(p), 2)]
        length = sum(math.dist(a, b) for a, b in zip(coords, coords[1:]))
        prev = river_best.get(rname)
        if prev is None or length > prev[0]:
            river_best[rname] = (length, rv["rank"], coords)

    ranked_rivers = sorted(river_best.items(), key=lambda kv: kv[1][0], reverse=True)
    selected_rivers = [
        (rname, info)
        for rname, info in ranked_rivers
        if rname in RIVER_LABEL_PRIORITY
    ]
    for rname, (rlen, rrank, coords) in selected_rivers:
        cum = [0.0]
        for a, b in zip(coords, coords[1:]):
            cum.append(cum[-1] + math.dist(a, b))
        half = cum[-1] * 0.5
        mid_x, mid_y = coords[len(coords) // 2]
        ang = 0.0
        for idx in range(len(coords) - 1):
            if cum[idx + 1] >= half:
                seg_len = cum[idx + 1] - cum[idx] or 1.0
                t = (half - cum[idx]) / seg_len
                x0, y0 = coords[idx]
                x1, y1 = coords[idx + 1]
                mid_x = x0 + t * (x1 - x0)
                mid_y = y0 + t * (y1 - y0)
                # Use a ~30 km window around midpoint for a smooth tangent angle
                i_lo = max(0, idx - 1)
                i_hi = min(len(coords) - 1, idx + 2)
                ang = upright_angle(coords[i_hi][0] - coords[i_lo][0], coords[i_hi][1] - coords[i_lo][1])
                break
        label_name = rname if ("River" in rname or "Rio " in rname) else f"{rname} River"
        features.append(
            {
                "name": label_name,
                "kind": "river",
                "x": round(mid_x, 1),
                "y": round(mid_y, 1),
                "angle": ang,
                "tier": 1 if rrank <= 4 else 2,
                "elev_m": None,
                "note": f"Major Philippine river (~{round(rlen)} km mapped length)",
            }
        )

    water_table = [
        {"id": LAND, "name": "Land", "rule": "Philippine archipelago land (GADM 4.1 ADM2) or neighboring countries (Natural Earth)."},
        {"id": OCEAN, "name": "Ocean", "color": "#1b4266", "rule": "Sea deeper than 200 m and not next to land."},
        {"id": COAST, "name": "Coast", "color": "#2d6d97", "rule": "Sea next to a land hex, or shallower than 200 m (ETOPO1)."},
        {"id": LAKE, "name": "Lake", "color": "#3a80b0", "rule": "Inland lakes (Laguna de Bay, Lake Lanao, Taal Lake, Lake Mainit, Naujan Lake, Lake Buluan, Bato, Buhi)."},
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
            "water": f"Philippines archipelago hexes ({HEX_RADIUS_KM:g} km radius) from GADM 4.1 ADM2 boundaries & waterbodies, Natural Earth 1:10m lakes, and NOAA ETOPO1 bathymetry.",
            "relief": f"From NOAA ETOPO1 ({etopo_res_label}) tuned for Philippine mountain ranges.",
            "cover": "Modelled per hex from PSA Land Classification (2021) forest & fishpond shares, 2024 Census municipality density & urbanization (GADM2), marshlands, and ETOPO1 relief.",
        },
    }

    ph_hexes = sum(1 for o in owner if o >= 0)
    foreign_hexes = sum(1 for o in owner if o == FOREIGN)
    lake_hexes = sum(1 for w in water if w == LAKE)
    elapsed = time.perf_counter() - t0
    print(f"  {ph_hexes} Philippine land hexes, {foreign_hexes} foreign land hexes, {lake_hexes} lake hexes, {len(rivers)} rivers, {len(features)} features ({elapsed:.1f}s)")

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
            "features": features,
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
            "municipality": municipality,
            "municipalities": municipalities,
            "states": [s["fips"] for s in states],
            "counties": [c["geoid"] for c in counties],
            "water": water,
            "cover": cover,
            "pct": pct,
        },
    )


if __name__ == "__main__":
    build()
