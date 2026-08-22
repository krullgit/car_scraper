#!/usr/bin/env python3
"""Periodic tracker for Audi Q4 e-tron, VW ID.4, VW ID.5 and Skoda Enyaq listings.

Stores full history in SQLite and publishes a public cars.json feed. No
equipment/km/price pre-filtering — the downstream LLM agent evaluates
raw_full_text itself.

Usage:
    python3 car_tracker.py               # run continuously (default: every 30 min)
    python3 car_tracker.py --once        # run once and exit (for cron/systemd timer)
    python3 car_tracker.py --interval N  # run every N seconds
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# --- Config ----------------------------------------------------------------

DB_PATH = Path(os.environ.get("CAR_DB_PATH", Path(__file__).resolve().parent / "cars.db"))
JSON_EXPORT = Path(__file__).resolve().parent / "cars_legacy.json"
JSON_EXPORT_FEED = Path(__file__).resolve().parent / "cars.json"
JSON_EXPORT_STATUS = Path(__file__).resolve().parent / "status.json"
PUBLIC_KEY = "ac25fe9c-38d4-45bb-98cf-1f95088db0e2"
API_BASE = "https://apps.autohausen.de/ahp6/api"
PAGE_SIZE = 100

MAKE_ID = 46
# Model families we track: Audi Q4 e-tron, VW ID.4, VW ID.5, Škoda Enyaq.
# Only a price ceiling is applied — no equipment/km pre-filtering.
# The downstream LLM agent judges raw_full_text itself.
MAKE_MODELS: dict[int, list[int]] = {
    11: [1102221, 1102353],   # Audi Q4 e-tron, Q4 Sportback e-tron
    46: [4602192],            # Škoda Enyaq (iV / 85 / Essence)
    52: [5202184, 5202335],   # VW ID.4, VW ID.5
}
MAX_PRICE = 31_000  # price ceiling in EUR for the four model families

# Telegram notifications (loaded from secrets.json / env, never hard-coded)
import secrets as _secrets_mod
TELEGRAM_TOKEN = _secrets_mod.get_secret("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _secrets_mod.get_secret("TELEGRAM_CHAT_ID")

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

# --- Database --------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS cars (
    vehicleid       INTEGER PRIMARY KEY,
    make            INTEGER,
    model           INTEGER,
    shortdescription TEXT,
    customerprice   REAL,
    price           REAL,
    listprice       REAL,
    monthlypayment  REAL,
    registrationdate TEXT,
    kilometers      INTEGER,
    fuel            INTEGER,
    fuel_name       TEXT,
    power           INTEGER,
    body            INTEGER,
    body_name       TEXT,
    transmission    INTEGER,
    transmission_name TEXT,
    seatcover       INTEGER,
    seatcover_name  TEXT,
    numowners       INTEGER,
    emissionco2     REAL,
    emissionsbadge  INTEGER,
    emissionsgroup  INTEGER,
    batteryrange    INTEGER,
    dealerid        INTEGER,
    offertypecode   INTEGER,
    category        INTEGER,
    numimages       INTEGER,
    images          TEXT,
    envkv           TEXT,
    financing       TEXT,
    leasingbusiness TEXT,
    leasingprivate  TEXT,
    first_seen      TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen       TEXT NOT NULL DEFAULT (datetime('now')),
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS price_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicleid       INTEGER NOT NULL,
    customerprice   REAL,
    price           REAL,
    monthlypayment  REAL,
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (vehicleid) REFERENCES cars(vehicleid)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT,
    finished_at     TEXT,
    cars_total      INTEGER,
    cars_active     INTEGER,
    new_cars        INTEGER,
    removed_cars    INTEGER,
    price_changes   INTEGER,
    duration_ms     INTEGER,
    status          TEXT
);

CREATE INDEX IF NOT EXISTS idx_cars_active ON cars(is_active);
CREATE INDEX IF NOT EXISTS idx_cars_price ON cars(customerprice);
CREATE INDEX IF NOT EXISTS idx_cars_dealer ON cars(dealerid);
CREATE INDEX IF NOT EXISTS idx_price_history_vehicle ON price_history(vehicleid);
CREATE INDEX IF NOT EXISTS idx_price_history_date ON price_history(recorded_at);
"""


def _ser(v: Any) -> Any:
    """Serialize value for JSON storage — keep None as None."""
    if v is None:
        return None
    return v


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# --- API ----------------------------------------------------------------

def _base_filters(make: int, model_ids: list[int]) -> dict[str, Any]:
    return {
        "typeextendedcode": [2, 4],
        "make": [make],
        "model": model_ids,
        "customerprice": [None, MAX_PRICE],
    }


def fetch_all_cars() -> list[dict]:
    """Fetch all matching cars with full pagination.

    The target model families span 3 makes (Audi 11, Skoda 46, VW 52), so we
    query each make separately and merge by vehicleid. No price, kilometer or
    dealer filter — the full available stock of the four model lines is kept."""
    result: dict[int, dict] = {}
    for make, model_ids in MAKE_MODELS.items():
        filt = _base_filters(make, model_ids)

        # Get total count first
        resp = requests.post(f"{API_BASE}/count", json={
            "filter": filt,
            "publicKey": PUBLIC_KEY,
        }, timeout=30)
        resp.raise_for_status()
        total = resp.json().get("meta", {}).get("total", 0)

        offset = 0
        while offset < total:
            resp = requests.post(f"{API_BASE}/list", json={
                "filter": filt,
                "orderBy": "priceAsc",
                "offset": offset,
                "limit": PAGE_SIZE,
                "publicKey": PUBLIC_KEY,
            }, timeout=30)
            resp.raise_for_status()
            batch = resp.json().get("data", [])
            for car in batch:
                result[car["vehicleid"]] = car
            offset += len(batch)
            if len(batch) < PAGE_SIZE:
                break

    return list(result.values())


# --- DB operations -------------------------------------------------------------

CAR_COLUMNS = [
    "vehicleid", "make", "model", "shortdescription", "customerprice",
    "price", "listprice", "monthlypayment", "registrationdate",
    "kilometers", "fuel", "fuel_name", "power", "body", "body_name",
    "transmission", "transmission_name", "seatcover", "seatcover_name",
    "numowners", "emissionco2", "emissionsbadge", "emissionsgroup",
    "batteryrange", "dealerid", "offertypecode", "category",
    "numimages", "images", "envkv", "financing", "leasingbusiness",
    "leasingprivate",
]

CAR_COLUMNS_STR = ", ".join(CAR_COLUMNS)
PLACEHOLDERS = ", ".join(["?" for _ in CAR_COLUMNS])

UPDATES = ", ".join(
    f"{col}=excluded.{col}"
    for col in CAR_COLUMNS
    if col not in ("vehicleid",)
) + ", last_seen=datetime('now'), is_active=1"


def car_to_row(car: dict) -> dict[str, Any]:
    images_json = json.dumps(car.get("images", []), ensure_ascii=False)
    envkv_json = json.dumps(_ser(car.get("envkv")), ensure_ascii=False)
    financing_json = json.dumps(_ser(car.get("financing")), ensure_ascii=False)
    lb_json = json.dumps(_ser(car.get("leasingbusiness")), ensure_ascii=False)
    lp_json = json.dumps(_ser(car.get("leasingprivate")), ensure_ascii=False)

    fuel_code = car.get("fuel", 0)
    body_code = car.get("body", 0)
    trans_code = car.get("transmission", 0)
    seat_code = car.get("seatcover", 0)

    return {
        "vehicleid": car["vehicleid"],
        "make": car.get("make"),
        "model": car.get("model"),
        "shortdescription": car.get("shortdescription"),
        "customerprice": float(car.get("customerprice", 0)),
        "price": float(car.get("price", 0)),
        "listprice": float(car.get("listprice", 0)) if car.get("listprice") else None,
        "monthlypayment": float(car.get("monthlypayment", 0)) if car.get("monthlypayment") else None,
        "registrationdate": car.get("registrationdate"),
        "kilometers": car.get("kilometers"),
        "fuel": fuel_code,
        "fuel_name": FUEL_MAP.get(fuel_code),
        "power": car.get("power"),
        "body": body_code,
        "body_name": BODY_MAP.get(body_code),
        "transmission": trans_code,
        "transmission_name": TRANSMISSION_MAP.get(trans_code),
        "seatcover": seat_code,
        "seatcover_name": SEAT_MAP.get(seat_code),
        "numowners": car.get("numowners"),
        "emissionco2": _ser(car.get("emissionco2")),
        "emissionsbadge": car.get("emissionsbadge"),
        "emissionsgroup": car.get("emissionsgroup"),
        "batteryrange": car.get("batteryrange"),
        "dealerid": car.get("dealerid"),
        "offertypecode": car.get("offertypecode"),
        "category": car.get("category"),
        "numimages": car.get("numimages"),
        "images": images_json,
        "envkv": envkv_json,
        "financing": financing_json,
        "leasingbusiness": lb_json,
        "leasingprivate": lp_json,
    }


def _update_db_v2(conn: sqlite3.Connection, cars: list[dict]) -> dict:
    """Upsert cars with proper price change detection."""
    cursor = conn.cursor()

    existing_ids = {
        row[0] for row in cursor.execute("SELECT vehicleid FROM cars WHERE is_active=1").fetchall()
    }
    incoming_ids = {c["vehicleid"] for c in cars}

    new_ids = incoming_ids - existing_ids
    gone_ids = existing_ids - incoming_ids
    price_changes = 0

    # Detect price changes BEFORE updating
    for car in cars:
        if car["vehicleid"] in existing_ids:
            prev = cursor.execute(
                "SELECT customerprice, price, monthlypayment FROM cars WHERE vehicleid=?",
                (car["vehicleid"],),
            ).fetchone()
            if prev:
                new_cp = float(car.get("customerprice", 0))
                new_p = float(car.get("price", 0))
                new_mp = float(car.get("monthlypayment", 0) or 0)

                old_cp = prev["customerprice"] or 0
                old_p = prev["price"] or 0
                old_mp = prev["monthlypayment"] or 0

                if abs(new_cp - old_cp) > 0.01 or abs(new_p - old_p) > 0.01 or abs(new_mp - old_mp) > 0.01:
                    cursor.execute("""
                        INSERT INTO price_history (vehicleid, customerprice, price, monthlypayment)
                        VALUES (?, ?, ?, ?)
                    """, (car["vehicleid"], old_cp, old_p, old_mp))
                    price_changes += 1

    # Upsert all incoming cars
    for car in cars:
        row = car_to_row(car)
        values = [row[col] for col in CAR_COLUMNS]
        cursor.execute(f"""
            INSERT INTO cars ({CAR_COLUMNS_STR})
            VALUES ({PLACEHOLDERS})
            ON CONFLICT(vehicleid) DO UPDATE SET {UPDATES}
        """, values)

    # Mark gone cars as inactive
    if gone_ids:
        cursor.execute(
            f"UPDATE cars SET is_active=0, last_seen='{datetime.now(timezone.utc).isoformat(timespec='seconds')}' "
            f"WHERE vehicleid IN ({','.join('?' for _ in gone_ids)})",
            list(gone_ids),
        )

    conn.commit()

    return {
        "new_cars": len(new_ids),
        "removed_cars": len(gone_ids),
        "price_changes": price_changes,
        "total_incoming": len(cars),
        "total_active": len(incoming_ids),
        "new_ids": new_ids,
        "gone_ids": gone_ids,
    }


# --- Export -------------------------------------------------------------

def export_json(conn: sqlite3.Connection) -> None:
    """Export active cars as JSON for website consumption."""
    rows = conn.execute("""
        SELECT * FROM cars
        WHERE is_active = 1
        ORDER BY customerprice ASC
    """).fetchall()

    cars_list = []
    for r in rows:
        car = dict(r)
        # Parse JSON fields
        for field in ("images", "envkv", "financing", "leasingbusiness", "leasingprivate"):
            try:
                car[field] = json.loads(car[field]) if car[field] else None
            except (json.JSONDecodeError, TypeError):
                pass
        cars_list.append(car)

    with open(JSON_EXPORT, "w") as f:
        json.dump({"exported_at": datetime.now(timezone.utc).isoformat(), "cars": cars_list},
                  f, ensure_ascii=False, indent=2)


# --- Main loop -------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def send_telegram(message: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def notify_new_cars(conn, new_ids: set[int]) -> None:
    if not new_ids:
        return
    conn.execute("CREATE TABLE IF NOT EXISTS car_notified (vehicleid INTEGER PRIMARY KEY)")
    already = {r[0] for r in conn.execute(
        f"SELECT vehicleid FROM car_notified WHERE vehicleid IN ({','.join('?' for _ in new_ids)})",
        list(new_ids),
    ).fetchall()}
    to_notify = new_ids - already
    if not to_notify:
        return
    rows = conn.execute(
        f"SELECT shortdescription, customerprice, kilometers, registrationdate, dealerid, vehicleid "
        f"FROM cars WHERE vehicleid IN ({','.join('?' for _ in to_notify)}) "
        f"ORDER BY customerprice",
        list(to_notify),
    ).fetchall()
    for r in rows:
        price = f"{r['customerprice']:,.0f}€".replace(",", ".")
        km = f"{r['kilometers']:,d}".replace(",", ".")
        reg = (r["registrationdate"] or "")[:7]
        dname = {546: "Charlottenburg", 25894: "Weißensee", 558: "Tempelhof",
                 22617: "Potsdam", 545: "Spandau"}.get(r["dealerid"], f"Dealer {r['dealerid']}")
        vid = r["vehicleid"]
        url = f"https://www.volkswagen-automobile-berlin.de/gebrauchtwagen/fahrzeugsuche/{vid}"
        msg = (
            f"🆕 <b>Neues E-Fahrzeug!</b>\n"
            f"{r['shortdescription']}\n"
            f"💰 {price} · {km}km · EZ {reg}\n"
            f"📍 {dname}\n"
            f"<a href=\"{url}\">Zum Angebot</a>\n"
            f"🖥 <a href=\"http://192.168.0.13:8080\">Trader ansehen</a>"
        )
        send_telegram(msg)
        conn.execute("INSERT OR IGNORE INTO car_notified (vehicleid) VALUES (?)", (vid,))
    conn.commit()


def run_scrape(conn: sqlite3.Connection) -> None:
    started = time.monotonic()
    started_iso = _now_iso()
    status = "ok"

    print(f"[{started_iso}] Fetching cars from API...")
    try:
        cars = fetch_all_cars()
    except Exception as e:
        print(f"[{started_iso}] ERROR fetching: {e}", file=sys.stderr)
        conn.execute("""
            INSERT INTO scrape_runs (started_at, finished_at, status)
            VALUES (?, ?, ?)
        """, (started_iso, _now_iso(), f"fetch_error: {str(e)[:200]}"))
        conn.commit()
        return

    print(f"[{started_iso}] Got {len(cars)} cars. Updating database...")
    stats = _update_db_v2(conn, cars)

    finished_iso = _now_iso()
    duration = int((time.monotonic() - started) * 1000)

    conn.execute("""
        INSERT INTO scrape_runs
            (started_at, finished_at, cars_total, cars_active,
             new_cars, removed_cars, price_changes, duration_ms, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (started_iso, finished_iso, stats["total_incoming"], stats["total_active"],
          stats["new_cars"], stats["removed_cars"], stats["price_changes"],
          duration, status))
    conn.commit()

    # Print summary
    emoji = "🆕" if stats["new_cars"] else "💰" if stats["price_changes"] else "✅"
    print(
        f"  {emoji} {stats['total_active']} active | "
        f"+{stats['new_cars']} new | "
        f"-{stats['removed_cars']} gone | "
        f"~{stats['price_changes']} price changes | "
        f"{duration}ms"
    )

    # Export JSON (local website format)
    try:
        export_json(conn)
    except Exception as e:
        print(f"  WARN: JSON export failed: {e}", file=sys.stderr)

    # Enrich new cars with detail page data (fills raw_text for the feed).
    # Only cars that were never enriched (or are missing raw_text) are scraped,
    # so this is cheap unless there is something new.
    try:
        import subprocess
        enrich_res = subprocess.run(
            ["python3", str(Path(__file__).resolve().parent / "enrich.py")],
            capture_output=True, timeout=1800,
        )
        if enrich_res.stdout:
            last = [l for l in enrich_res.stdout.decode(errors="replace").splitlines() if l.strip()][-3:]
            print("  Enrich: " + " | ".join(last))
        if enrich_res.returncode != 0 and enrich_res.stderr:
            print(f"  WARN: enrich exit {enrich_res.returncode}: {enrich_res.stderr.decode(errors='replace').strip()[:300]}",
                  file=sys.stderr)
    except Exception as e:
        print(f"  WARN: enrichment failed: {e}", file=sys.stderr)

    # Build and publish the public cars.json feed (raw_full_text for ChatGPT)
    try:
        import feed
        feed_f = feed.build_feed(conn)
        feed.write_feed(feed_f, JSON_EXPORT_FEED)
        feed.write_json(feed.build_status(feed_f), JSON_EXPORT_STATUS)
        print(f"  Feed: {len(feed_f['vehicles'])} vehicles (status.json written)")
    except Exception as e:
        print(f"  WARN: feed build failed: {e}", file=sys.stderr)

    try:
        import subprocess
        pub = subprocess.run(
            ["python3", str(Path(__file__).resolve().parent / "publish_feed.py")],
            capture_output=True, timeout=120,
        )
        if pub.stdout:
            print(pub.stdout.decode(errors="replace").rstrip())
        if pub.returncode != 0 and pub.stderr:
            print(f"  WARN: publish_feed exit {pub.returncode}: {pub.stderr.decode(errors='replace').strip()[:500]}",
                  file=sys.stderr)
    except Exception as e:
        print(f"  WARN: publish_feed failed: {e}", file=sys.stderr)

    if stats["new_ids"]:
        notify_new_cars(conn, stats["new_ids"])

    # Show new/removed cars
    if stats["new_ids"]:
        placeholders = ",".join("?" for _ in stats["new_ids"])
        new_rows = conn.execute(
            f"SELECT shortdescription, customerprice FROM cars WHERE vehicleid IN ({placeholders})",
            list(stats["new_ids"]),
        ).fetchall()
        for r in new_rows:
            print(f"  NEW: {r['customerprice']:,.2f}€ | {r['shortdescription']}")

    if stats["gone_ids"]:
        placeholders = ",".join("?" for _ in stats["gone_ids"])
        gone_rows = conn.execute(
            f"SELECT shortdescription, customerprice FROM cars WHERE vehicleid IN ({placeholders})",
            list(stats["gone_ids"]),
        ).fetchall()
        for r in gone_rows:
            print(f"  GONE: {r['customerprice']:,.2f}€ | {r['shortdescription']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Track Audi Q4 e-tron, VW ID.4/ID.5 and Skoda Enyaq listings")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=1800,
                        help="Seconds between runs (default: 1800 = 30 min)")
    parser.add_argument("--db", type=str, default=str(DB_PATH),
                        help="Path to SQLite database")
    args = parser.parse_args()

    db_path = Path(args.db)
    print(f"Database: {db_path}")
    print(f"Interval: {'once' if args.once else f'{args.interval}s'}")
    print(f"Export:   {JSON_EXPORT}")
    print()

    conn = get_db()

    try:
        while True:
            run_scrape(conn)
            if args.once:
                break
            print(f"  Sleeping {args.interval}s... (Ctrl+C to stop)")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
