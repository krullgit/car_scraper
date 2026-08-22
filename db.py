#!/usr/bin/env python3
"""SQLite persistence: schema, connection, upsert with price history."""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config

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
    api_json        TEXT,
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


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    # Migration for existing databases that predate api_json.
    try:
        conn.execute("ALTER TABLE cars ADD COLUMN api_json TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return conn


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _ser(v: Any) -> Any:
    """Serialize value for JSON storage — keep None as None."""
    return v


CAR_COLUMNS = [
    "vehicleid", "make", "model", "shortdescription", "customerprice",
    "price", "listprice", "monthlypayment", "registrationdate",
    "kilometers", "fuel", "fuel_name", "power", "body", "body_name",
    "transmission", "transmission_name", "seatcover", "seatcover_name",
    "numowners", "emissionco2", "emissionsbadge", "emissionsgroup",
    "batteryrange", "dealerid", "offertypecode", "category",
    "numimages", "images", "envkv", "financing", "leasingbusiness",
    "leasingprivate", "api_json",
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
        "fuel_name": config.FUEL_MAP.get(fuel_code),
        "power": car.get("power"),
        "body": body_code,
        "body_name": config.BODY_MAP.get(body_code),
        "transmission": trans_code,
        "transmission_name": config.TRANSMISSION_MAP.get(trans_code),
        "seatcover": seat_code,
        "seatcover_name": config.SEAT_MAP.get(seat_code),
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
        "api_json": json.dumps(car, ensure_ascii=False, default=_ser),
    }


def update_db(conn: sqlite3.Connection, cars: list[dict]) -> dict:
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


# ---------------------------------------------------------------------------
# Export (legacy local format)
# ---------------------------------------------------------------------------

def export_legacy_json(conn: sqlite3.Connection) -> None:
    """Export active cars as JSON for the local website format."""
    rows = conn.execute("""
        SELECT * FROM cars
        WHERE is_active = 1
        ORDER BY customerprice ASC
    """).fetchall()

    cars_list = []
    for r in rows:
        car = dict(r)
        for field in ("images", "envkv", "financing", "leasingbusiness", "leasingprivate"):
            try:
                car[field] = json.loads(car[field]) if car[field] else None
            except (json.JSONDecodeError, TypeError):
                pass
        cars_list.append(car)

    with open(config.JSON_EXPORT, "w") as f:
        json.dump({"exported_at": datetime.now(timezone.utc).isoformat(), "cars": cars_list},
                  f, ensure_ascii=False, indent=2)