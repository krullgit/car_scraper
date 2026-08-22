#!/usr/bin/env python3
"""Build the public cars.json feed (+ status.json) for the LLM scheduler.

This module only transports raw source data — it does NOT normalize or
semantically interpret anything. The downstream LLM agent is the only part
that judges equipment, location, features, etc.

Per-vehicle core schema:
    id, url, scraped_at, source, raw_full_text, source_fields

`source_fields` is the unmodified original API JSON (1:1 from the source).
No location derivation, no feature heuristics, no dealer-name normalization.

Usage:
    python3 feed.py                # write cars.json next to this script
    python3 feed.py --out path     # write to a custom path
"""

import argparse
import json
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


def build_vehicles(conn: sqlite3.Connection) -> list[dict]:
    """Build the vehicle list. One broken vehicle must never break the export."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT c.*, e.raw_full_text as stored_raw_full_text,
               e.image_analysis_json as stored_image_analysis,
               e.scraped_at as equip_scraped_at
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

            # source_fields: the original API response, untouched.
            source_fields = {}
            api_json = (r.get("api_json") or "").strip()
            if api_json:
                try:
                    parsed = json.loads(api_json)
                    if isinstance(parsed, dict):
                        source_fields = parsed
                except (json.JSONDecodeError, TypeError):
                    source_fields = {}

            vehicle: dict[str, Any] = {
                "id": str(vid),
                "url": f"{config.BASE_URL}/{vid}",
                "scraped_at": _iso_ts(r["equip_scraped_at"]) if r["equip_scraped_at"] else _now_iso(),
                "source": config.SOURCE,
                # Unmodified document.body.innerText — kept as-is, never filtered.
                "raw_full_text": r["stored_raw_full_text"] or "",
                "source_fields": source_fields,
            }

            # Vision analysis results, separated from the source data.
            ia_raw = (r.get("stored_image_analysis") or "").strip()
            if ia_raw:
                try:
                    vehicle["image_analysis"] = json.loads(ia_raw).get("image_analysis",
                                        {"battery_certificates": []})
                except (json.JSONDecodeError, TypeError):
                    vehicle["image_analysis"] = {"battery_certificates": []}
            else:
                vehicle["image_analysis"] = {"battery_certificates": []}

            # Transport-only records (not interpretations): scrape bookkeeping.
            vehicle["first_seen"] = _iso_ts(r["first_seen"])
            vehicle["last_seen"] = _iso_ts(r["last_seen"])

            history = conn.execute(
                "SELECT recorded_at, customerprice FROM price_history "
                "WHERE vehicleid=? ORDER BY recorded_at DESC",
                (vid,),
            ).fetchall()
            if history:
                vehicle["price_history"] = [
                    {"recorded_at": _iso_ts(h["recorded_at"]),
                     "price_eur": round(float(h["customerprice"] or 0))}
                    for h in history
                    if h["customerprice"] is not None
                ]

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