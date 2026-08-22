#!/usr/bin/env python3
"""Regression tests: the scraper must never derive structured fields from text.

Key case (previous bug): an offer must NOT become `location = Berlin` just
because the page text or dealer network mentions Berlin/Adlershof while other
parts of the source point to Frankfurt. The scraper only transports raw data;
no semantic interpretation, no location logic, no feature heuristics.
"""

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import feed


class TestNoLocationDerivation(unittest.TestCase):
    def test_source_with_berlin_text_does_not_produce_location_field(self):
        """An offer whose raw_full_text mentions Berlin must not yield a
        structured `location` field — location may only come from source_fields
        verbatim, never from text analysis."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db_schema())

        api_payload = {
            "vehicleid": 48066,
            "make": 11,
            "model": 1102221,
            "shortdescription": "Q4 e-tron 35 Frankfurt/Adlershof example",
            "customerprice": 29450,
            "kilometers": 86186,
            "registrationdate": "2023-06-14",
            "power": 125,
            "dealerid": 23551,
            "localcode": "LOC-FRA-01",
            "images": [],
        }
        conn.execute(
            "INSERT INTO cars (vehicleid, make, model, shortdescription, "
            "customerprice, kilometers, registrationdate, power, dealerid, "
            "api_json, first_seen, last_seen, is_active) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                48066, 11, 1102221, api_payload["shortdescription"],
                29450, 86186, "2023-06-14", 125, 23551,
                json.dumps(api_payload, ensure_ascii=False),
                "2026-08-22 10:00:00", "2026-08-22 10:00:00",
            ),
        )
        # The scraped page text is Berlin-heavy, but no interpretation allowed.
        conn.execute(
            "INSERT INTO car_equipment (vehicleid, raw_full_text, scraped_at) "
            "VALUES (?,?,datetime('now'))",
            (
                48066,
                "Home\nGebrauchtwagen\nQ4 e-tron 35 Frankfurt/Adlershof example\n"
                "Kaufpreis\n29.450 €\nDaten\nErstzulassung\n06/2023\n"
                "Standort Adlershof\nRudower Chaussee 47\n12489 Berlin\n"
                "Anbieter\nFrankfurt Motor GmbH\nFrankfurter Str. 1\n60311 Frankfurt\n"
                "Impressum\nDatenschutz\n",
            ),
        )

        vehicles = feed.build_vehicles(conn)
        conn.close()

        self.assertEqual(len(vehicles), 1)
        v = vehicles[0]

        # Core: no derived fields.
        self.assertNotIn("location", v)
        self.assertNotIn("acc", v)
        self.assertNotIn("seat_heating", v)
        self.assertNotIn("fuel_name", v)

        # source_fields transport the original API payload verbatim.
        self.assertIn("source_fields", v)
        self.assertEqual(v["source_fields"]["dealerid"], 23551)
        self.assertEqual(v["source_fields"]["shortdescription"],
                         api_payload["shortdescription"])
        # The API says dealerid 23551; the text mentions Berlin AND Frankfurt.
        # We must not have synthesized a location preferring one of them.
        self.assertNotIn("location", v)
        self.assertTrue(v["raw_full_text"])
        self.assertIn("Frankfurt", v["raw_full_text"])
        self.assertIn("Berlin", v["raw_full_text"])


class TestSourceFieldsVerbatim(unittest.TestCase):
    def test_source_fields_are_the_original_api_json(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db_schema())

        payload = {
            "vehicleid": 8720718,
            "make": 11,
            "model": 1102221,
            "shortdescription": "Q4 e-tron 35 Virtual+/Navi+/GRA",
            "customerprice": 23417.0,
            "kilometers": 31224,
            "registrationdate": "2023-05-20",
            "power": 125,
            "dealerid": 23551,
            "localcode": "SOME-CODE",
        }
        conn.execute(
            "INSERT INTO cars (vehicleid, make, model, shortdescription, "
            "customerprice, kilometers, registrationdate, power, dealerid, "
            "api_json, first_seen, last_seen, is_active) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                8720718, 11, 1102221, payload["shortdescription"],
                23417.0, 31224, "2023-05-20", 125, 23551,
                json.dumps(payload, ensure_ascii=False),
                "2026-08-22 10:00:00", "2026-08-22 10:00:00",
            ),
        )
        conn.execute(
            "INSERT INTO car_equipment (vehicleid, raw_full_text, scraped_at) "
            "VALUES (?,?,datetime('now'))",
            (8720718, "Unmodified full page text.\nKaufpreis\n…",),
        )

        vehicles = feed.build_vehicles(conn)
        conn.close()

        self.assertEqual(len(vehicles), 1)
        sf = vehicles[0]["source_fields"]
        self.assertEqual(sf["registrationdate"], "2023-05-20")
        self.assertEqual(sf["customerprice"], 23417.0)
        # Values are copied, not mapped/translated by our code.
        self.assertNotIn("fuel_name", sf.get("fuel", {}),
                         "raw source must not contain synthesized names")


def db_schema() -> str:
    return """
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
        power           INTEGER,
        body            INTEGER,
        transmission    INTEGER,
        numowners       INTEGER,
        emissionco2     REAL,
        batteryrange    INTEGER,
        dealerid        INTEGER,
        numimages       INTEGER,
        localcode       TEXT,
        api_json        TEXT,
        first_seen      TEXT,
        last_seen       TEXT,
        is_active       INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS car_equipment (
        vehicleid       INTEGER PRIMARY KEY,
        raw_full_text   TEXT,
        image_analysis_json TEXT,
        scraped_at      TEXT
    );
    CREATE TABLE IF NOT EXISTS price_history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicleid       INTEGER,
        customerprice   REAL,
        recorded_at     TEXT
    );
    """


if __name__ == "__main__":
    unittest.main(verbosity=2)