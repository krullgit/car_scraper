#!/usr/bin/env python3
"""Build the public cars.json feed (+ status.json) for the LLM scheduler.

This module only collects raw data — it does NOT normalize or semantically
interpret equipment. The downstream LLM agent evaluates raw_full_text itself.

Per-vehicle fields follow the agreed public schema:
    vehicleid, title, price_eur, kilometers, registrationdate, power_kw, url,
    dealer_name, raw_full_text  (+ additional structured API fields)

Usage:
    python3 feed.py                # write cars.json next to this script
    python3 feed.py --out path     # write to a custom path
"""

import argparse
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import config


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _iso_ts(value: str) -> str:
    """Normalize 'YYYY-MM-DD HH:MM:SS' (UTC from SQLite) to ISO with offset."""
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.isoformat(timespec="seconds")


def _city_from_address(address: str) -> str:
    """Extract the city from a 'street · 12345 City' address string."""
    if not address:
        return ""
    m = re.search(r"(\d{5})\s+([A-Za-zäöüÄÖÜß\- ]+)$", address.strip())
    if m:
        return m.group(2).strip()
    return ""


def build_vehicles(conn: sqlite3.Connection) -> list[dict]:
    """Build the vehicle list. One broken vehicle must never break the export."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT c.*, e.raw_text as raw_text, e.raw_full_text as raw_full_text,
               e.dealer_name as dealer_name, e.dealer_address as dealer_address,
               e.scraped_at as scraped_at
        FROM cars c
        LEFT JOIN car_equipment e ON c.vehicleid = e.vehicleid
        WHERE c.is_active = 1
        ORDER BY c.customerprice ASC
    """).fetchall()

    vehicles: list[dict] = []
    for row in rows:
        try:
            r = dict(row)
            vid = int(r["vehicleid"])
            history = conn.execute(
                "SELECT recorded_at, customerprice FROM price_history "
                "WHERE vehicleid=? ORDER BY recorded_at DESC",
                (vid,),
            ).fetchall()

            dealer_name = (r["dealer_name"] or "").strip() or config.DEALER_NAMES.get(r["dealerid"], "")
            city = _city_from_address(r["dealer_address"] or "") or config.DEALER_CITY.get(r["dealerid"], "")
            reg = (r["registrationdate"] or "")[:7]

            vehicle: dict[str, Any] = {
                "vehicleid": str(vid),
                "title": r["shortdescription"] or "",
                "price_eur": round(float(r["customerprice"] or 0)),
                "kilometers": r["kilometers"],
                "registrationdate": reg or None,
                "power_kw": r["power"],
                "url": f"{config.BASE_URL}/{vid}",
                "dealer_name": dealer_name or None,
            }
            # Easy structured values that the API already provides (no text
            # interpretation required). None-values are dropped.
            extra = {
                "id": str(vid),
                "make": r["make"],
                "model": r["model"],
                "dealer_id": r["dealerid"],
                "local_code": r.get("localcode"),
                "fuel": config.FUEL_MAP.get(r["fuel"]),
                "transmission": config.TRANSMISSION_MAP.get(r["transmission"]),
                "body": config.BODY_MAP.get(r["body"]),
                "num_owners": r["numowners"],
                "co2_gkm": r["emissionco2"] if r["emissionco2"] else None,
                "battery_range_km": r["batteryrange"] if r["batteryrange"] else None,
                "num_images": r["numimages"],
                "location": city or None,
                "first_seen": _iso_ts(r["first_seen"]),
                "last_seen": _iso_ts(r["last_seen"]),
                "scraped_at": _iso_ts(r["scraped_at"]) if r["scraped_at"] else None,
            }
            vehicle.update({k: v for k, v in extra.items() if v not in (None, 0)})

            if history:
                vehicle["price_history"] = [
                    {"recorded_at": _iso_ts(h["recorded_at"]), "price_eur": round(float(h["customerprice"] or 0))}
                    for h in history
                    if h["customerprice"] is not None
                ]

            vehicle["raw_text"] = (r["raw_text"] or "").strip()
            # Unmodified document.body.innerText — kept as-is, never filtered.
            vehicle["raw_full_text"] = r["raw_full_text"] or ""

            vehicles.append(vehicle)
        except Exception as e:
            print(f"  WARN: skipping vehicle {row.get('vehicleid')}: {e}", file=__import__("sys").stderr)
    return vehicles


def build_feed(conn: sqlite3.Connection) -> dict:
    vehicles = build_vehicles(conn)
    return {
        "generated_at": _now_iso(),
        "source": config.SOURCE,
        "vehicle_count": len(vehicles),
        "vehicles": vehicles,
    }


def build_status(feed: dict) -> dict:
    """Small status file so the scheduler can see the scraper is alive."""
    return {
        "last_successful_scrape": feed["generated_at"],
        "vehicle_count": feed["vehicle_count"],
        "status": "ok",
    }


def write_json(data: dict, out_path: Path) -> None:
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build cars.json feed")
    parser.add_argument("--out", type=str, default=str(config.FEED_FILE),
                        help="Output path for the feed file")
    args = parser.parse_args()

    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        feed = build_feed(conn)
    finally:
        conn.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(feed, out)
    print(f"Feed written: {out} ({len(feed['vehicles'])} vehicles)")


if __name__ == "__main__":
    main()