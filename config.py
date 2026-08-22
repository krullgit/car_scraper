#!/usr/bin/env python3
"""Central configuration for the car scraper.

All paths, API credentials (non-secret), model filters, price ceiling and
lookup maps live here so the modules share a single source of truth.
Secrets (Telegram token etc.) are loaded by secrets.py and never land here.
"""

import os
from pathlib import Path

# --- Paths ---------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("CAR_DB_PATH", SCRIPT_DIR / "cars.db"))
JSON_EXPORT = SCRIPT_DIR / "cars_legacy.json"
FEED_FILE = SCRIPT_DIR / "cars.json"
STATUS_FILE = SCRIPT_DIR / "status.json"
SECRETS_FILE = SCRIPT_DIR / "secrets.json"

# --- Autohausen API ------------------------------------------------------

# Public API key for reading the dealer-group listing feed (not a secret).
PUBLIC_KEY = "ac25fe9c-38d4-45bb-98cf-1f95088db0e2"
API_BASE = "https://apps.autohausen.de/ahp6/api"
PAGE_SIZE = 100

# --- Target model families ------------------------------------------------

# Audi Q4 e-tron, Škoda Enyaq, VW ID.4, VW ID.5 — queried per make.
# Only a price ceiling is applied; no equipment/km pre-filtering.
# The downstream LLM agent judges raw_full_text itself.
MAKE_MODELS: dict[int, list[int]] = {
    11: [1102221, 1102353],   # Audi Q4 e-tron, Q4 Sportback e-tron
    46: [4602192],            # Škoda Enyaq (iV / 85 / Essence)
    52: [5202184, 5202335],   # VW ID.4, VW ID.5
}
MAX_PRICE = 31_000  # price ceiling in EUR

# --- Feed / URLs ----------------------------------------------------------

SOURCE = "volkswagen-automobile-berlin"
BASE_URL = "https://www.volkswagen-automobile-berlin.de/gebrauchtwagen/fahrzeugsuche"

# --- GitHub publishing ----------------------------------------------------

DEFAULT_OWNER = "krullgit"
DEFAULT_REPO = "car_scraper"
GIT_AUTHOR = "car-feed-bot"
GIT_EMAIL = "car-feed-bot@users.noreply.github.com"

# --- Lookup maps ----------------------------------------------------------

FUEL_MAP: dict[int, str] = {
    3: "Benzin", 4: "Diesel", 5: "Hybrid", 13: "Plug-in",
    9: "Ethanol", 11: "Erdgas", 14: "Wasserstoff",
}
TRANSMISSION_MAP: dict[int, str] = {
    1: "Manuell", 2: "Automatik", 3: "Halbautomatik",
}
BODY_MAP: dict[int, str] = {
    1: "Limousine", 3: "Kombi", 4: "Coupé", 5: "Cabrio",
    6: "Kombi", 9: "SUV", 13: "Van", 14: "Pickup",
    15: "Transporter", 19: "Sportwagen", 25: "Crossover",
    26: "Offroader", 43: "Sonderaufbau", 100: "Sonstige",
}
SEAT_MAP: dict[int, str] = {
    1: "Stoff", 2: "Teilleder", 3: "Vollleder", 4: "Velours",
    5: "Alcantara", 6: "Kunstleder", 7: "Sonstige", 8: "Stoff/Leder Mix",
}

# Dealer-name/city fallback used when the detail scrape has no dealer data yet.
DEALER_NAMES: dict[int, str] = {
    545: "Skoda Automobile Berlin Spandau",
    546: "Skoda Automobile Berlin Charlottenburg",
    558: "Skoda Automobile Berlin Tempelhof",
    21824: "Skoda Automobile Berlin Zehlendorf",
    22617: "Skoda Automobile Potsdam",
    22631: "Skoda Automobile Berlin Marzahn",
    22673: "Audi Berlin",
    22676: "Audi Berlin",
    25894: "VGRB Berlin Weißensee",
}
DEALER_CITY: dict[int, str] = {
    545: "Berlin", 546: "Berlin", 558: "Berlin", 21824: "Berlin",
    22617: "Potsdam", 22631: "Berlin", 22673: "Berlin", 22676: "Berlin",
    25894: "Berlin",
}