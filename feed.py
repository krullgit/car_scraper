#!/usr/bin/env python3
"""Build the public split feed for the LLM scheduler.

Outputs:
  - cars_index.json        small index (id, model, url, price, km, detail_file)
  - cars/<id>.json         one full detail file per vehicle (raw_full_text, ...)
  - status.json            scraper status / freshness

This module only transports raw source data — it does NOT normalize or
semantically interpret anything. The downstream LLM agent is the only part
that judges equipment, location, features, etc.

Per-vehicle core schema (in cars/<id>.json):
    id, url, scraped_at, source, raw_full_text, source_fields, image_analysis

`source_fields` is the unmodified original API JSON (1:1 from the source).
No location derivation, no feature heuristics, no dealer-name normalization.

Usage:
    python3 feed.py          # write the split feed next to this script
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
    """Build the full per-vehicle detail list. One broken vehicle must never
    break the export."""
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


def _model_name(vehicle: dict) -> str:
    """Display model name for the index (from the source model id, no inference)."""
    model_id = (vehicle.get("source_fields") or {}).get("model")
    return config.MODEL_NAMES.get(model_id, f"model-{model_id}")


def _to_num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except (ValueError, TypeError):
        return v


def build_index(vehicles: list[dict]) -> list[dict]:
    """Small index: one lightweight entry per vehicle, pointing to its detail
    file. Lets a consumer load only the listing it actually wants."""
    index = []
    for v in vehicles:
        sf = v.get("source_fields") or {}
        entry = {
            "id": v["id"],
            "model": _model_name(v),
            "url": v["url"],
            "price": _to_num(sf.get("customerprice")),
            "km": _to_num(sf.get("kilometers")),
            "source": v.get("source"),
            "detail_file": f"cars/{v['id']}.json",
        }
        index.append(entry)
    return index


def write_split_feed(conn: sqlite3.Connection) -> tuple[dict, list[dict]]:
    """Write the split feed: cars_index.json + cars/<id>.json per vehicle.

    Returns (feed_summary, index). The legacy full cars.json is no longer
    produced — the split structure is the only output."""
    vehicles = build_vehicles(conn)
    index = build_index(vehicles)

    # Small index file.
    write_json({
        "generated_at": _now_iso(),
        "source": config.SOURCE,
        "vehicle_count": len(vehicles),
        "vehicles": index,
    }, config.INDEX_FILE)

    # One detail file per vehicle (full raw_full_text + source_fields + analysis).
    config.CARS_DIR.mkdir(parents=True, exist_ok=True)
    for v in vehicles:
        write_json(v, config.CARS_DIR / f"{v['id']}.json")

    return (
        {"generated_at": _now_iso(), "source": config.SOURCE,
         "vehicle_count": len(vehicles)},
        index,
    )


def build_status(feed: dict, conn: sqlite3.Connection | None = None) -> dict:
    """Status file so the scheduler can tell data freshness from process liveness.

    Crucial distinction:
      - last_successful_scrape = the last scrape that completed with status 'ok'
        (real data freshness from scrape_runs). NOT the current wall-clock time.
      - last_attempt_at = the last time the tracker tried to run.
      - generated_at   = when this status.json was produced.
      - age_minutes    = how old the last successful scrape is.
      - status         = 'ok' / 'stale' / 'error' based on the last run.
    """
    import sqlite3 as _sqlite3

    now = _now_iso()
    last_attempt = now
    last_success = None
    status = "ok"
    error = None

    own_conn = conn is None
    if own_conn:
        conn = _sqlite3.connect(str(config.DB_PATH))

    try:
        rows = conn.execute(
            "SELECT finished_at, status FROM scrape_runs ORDER BY id DESC LIMIT 5"
        ).fetchall()
        if not rows:
            status = "stale"
        else:
            last_attempt = rows[0][0] or last_attempt
            last_run_status = rows[0][1] or ""
            if last_run_status != "ok":
                last_success = next(
                    (r[0] for r in rows if r[1] == "ok"), None
                )
                # transient network/DNS timeouts -> 'stale'; hard errors -> 'error'
                transient = ("timeout", "connection", "name resolution",
                             "max retries", "dns", "ssl")
                if any(t in last_run_status.lower() for t in transient) \
                        and last_success is not None:
                    status = "stale"
                else:
                    status = "error"
                error = last_run_status if last_run_status != "ok" else None
            else:
                last_success = rows[0][0]

            # If the tracker couldn't reach data at all, surface staleness.
            if last_success is None:
                status = "stale"
    except Exception as e:
        status = "error"
        error = f"status_db_error: {e}"
    finally:
        if own_conn:
            conn.close()

    result = {
        "status": status,
        "generated_at": now,
        "last_attempt_at": last_attempt,
        "vehicle_count": feed["vehicle_count"],
    }
    if last_success:
        result["last_successful_scrape"] = last_success
        try:
            age = (
                __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                - __import__("datetime").datetime.fromisoformat(last_success)
            )
            result["age_minutes"] = max(0, int(age.total_seconds() // 60))
        except (ValueError, TypeError):
            pass
    if error:
        result["error"] = error[:300]
    return result


def write_json(data: dict, out_path: Path) -> None:
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the split feed (index + per-vehicle files)")
    args = parser.parse_args()

    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        summary, index = write_split_feed(conn)
    finally:
        conn.close()

    print(f"Split feed written: {summary['vehicle_count']} vehicles "
          f"({len(index)} index entries)")


if __name__ == "__main__":
    main()