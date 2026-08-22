#!/usr/bin/env python3
"""Scraper for Skoda Octavia < 25k EUR from Volkswagen Automobile Berlin."""

import requests
from typing import Any

PUBLIC_KEY = "ac25fe9c-38d4-45bb-98cf-1f95088db0e2"
API_BASE = "https://apps.autohausen.de/ahp6/api"

# Verified Berlin-area dealer IDs
BERLIN_DEALER_IDS = [545, 546, 558, 21824, 22617, 22631, 22673, 22676, 25894]

FUEL_MAP = {
    3: "Benzin", 4: "Diesel", 5: "Hybrid", 13: "Plug-in",
    9: "Ethanol", 11: "Erdgas", 14: "Wasserstoff",
}
TRANSMISSION_MAP = {1: "Manuell", 2: "Automatik", 3: "Halbautomatik"}
BODY_MAP = {
    1: "Limousine", 3: "Kombi", 4: "Coupé", 5: "Cabrio",
    6: "Kombi", 9: "SUV", 13: "Van", 14: "Pickup",
    15: "Transporter", 19: "Sportwagen", 25: "Crossover",
    26: "Offroader", 43: "Sonderaufbau", 100: "Sonstige",
}


def _base_filters() -> dict[str, Any]:
    return {
        "typeextendedcode": [2, 4],
        "make": [46],
        "model": [4600998],
        "customerprice": [None, 30000],
        "registrationdate": [None, None],
        "kilometers": [None, None],
        "power": [None, None],
        "dealerid": BERLIN_DEALER_IDS,
    }


def fetch_page(offset: int = 0, limit: int = 100) -> list[dict]:
    payload = {
        "filter": _base_filters(),
        "orderBy": "priceAsc",
        "offset": offset,
        "limit": limit,
        "publicKey": PUBLIC_KEY,
    }
    resp = requests.post(f"{API_BASE}/list", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def get_total() -> int:
    resp = requests.post(f"{API_BASE}/count", json={
        "filter": _base_filters(),
        "publicKey": PUBLIC_KEY,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json().get("meta", {}).get("total", 0)


def format_car(car: dict) -> str:
    price = float(car.get("customerprice", 0))
    km = car.get("kilometers", 0)
    reg = car.get("registrationdate", "?")[:10]
    fuel = FUEL_MAP.get(car.get("fuel", 0), str(car.get("fuel", "?")))
    power = car.get("power", "?")
    trans = TRANSMISSION_MAP.get(car.get("transmission", 0), "?")
    body = BODY_MAP.get(car.get("body", 0), str(car.get("body", "?")))
    desc = car.get("shortdescription", "?")
    dealer_id = car.get("dealerid", "?")

    return (
        f"{price:>9,.2f}€ | {km:>7,d}km | {reg} | {power:>3}kW | "
        f"{fuel:<7} | {trans:12} | {body:<12} | Dealer:{dealer_id} | {desc}"
    )


def main() -> None:
    print("=" * 140)
    print("  Skoda Octavia < 25.000 € | Volkswagen Automobile Berlin (Umkreis Berlin)")
    print("=" * 140)

    total = get_total()
    print(f"\n  Total matches: {total}\n")

    PAGE_SIZE = 100
    all_cars: list[dict] = []
    offset = 0

    while offset < total:
        page_num = offset // PAGE_SIZE + 1
        print(f"  Loading page {page_num} (offset={offset})...", end=" ", flush=True)
        batch = fetch_page(offset=offset, limit=PAGE_SIZE)
        all_cars.extend(batch)
        print(f"got {len(batch)} cars")
        offset += len(batch)
        if len(batch) < PAGE_SIZE:
            break

    pages_loaded = (offset + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"\n  Loaded {len(all_cars)} cars across {pages_loaded} page(s) — pagination complete.")
    print("-" * 140)

    for car in all_cars:
        print(format_car(car))

    print("-" * 140)
    print(f"  Total: {len(all_cars)} Skoda Octavia under 25.000 €")
    print("=" * 140)

    if all_cars:
        prices = [float(c["customerprice"]) for c in all_cars]
        kms = [c["kilometers"] for c in all_cars]
        print(f"\n  Price range: {min(prices):,.2f}€ - {max(prices):,.2f}€ (avg: {sum(prices)/len(prices):,.2f}€)")
        print(f"  Km range:    {min(kms):,d} - {max(kms):,d} (avg: {sum(kms)/len(kms):,.0f})")


if __name__ == "__main__":
    main()
