#!/usr/bin/env python3
"""Vision backfill: analyze images for cars that lack image_analysis yet."""
import json
import sqlite3
import sys

sys.path.insert(0, "/home/matthes/Documents/car_craper")
import config
import image_analysis


def main():
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT c.vehicleid, c.shortdescription, c.images
        FROM cars c
        LEFT JOIN car_equipment e ON c.vehicleid = e.vehicleid
        WHERE c.is_active = 1
          AND (e.image_analysis_json IS NULL OR e.image_analysis_json = '')
        ORDER BY c.customerprice ASC
    """).fetchall()

    print(f"Vision-Backfill: {len(rows)} Fahrzeuge fehlen noch", flush=True)
    for row in rows:
        vid = row["vehicleid"]
        desc = (row["shortdescription"] or "")[:45]
        print(f"  {vid} — {desc}...", end=" ", flush=True)
        try:
            imgs = []
            if row["images"]:
                parsed = json.loads(row["images"])
                if isinstance(parsed, list):
                    imgs = parsed
            report = image_analysis.analyze_vehicle_images(imgs, workers=4)
            analysis = {"image_analysis": {"battery_certificates": report.battery_certificates}}
            if not report.battery_certificates and report.errors:
                analysis["image_analysis"]["errors"] = report.errors[:10]
            ia_json = json.dumps(analysis, ensure_ascii=False)
            existing = conn.execute(
                "SELECT raw_full_text FROM car_equipment WHERE vehicleid=?", (vid,)
            ).fetchone()
            raw_full_text = existing[0] if existing and existing[0] else ""
            conn.execute("""
                INSERT INTO car_equipment (vehicleid, full_text, summary, acc, ahk, rfk,
                    automatic, dealer_address, dealer_phone, dealer_name, raw_text,
                    raw_full_text, image_analysis_json, scraped_at)
                VALUES (?, '', '', 0, 0, 0, 0, '', '', '', '', ?, ?, datetime('now'))
                ON CONFLICT(vehicleid) DO UPDATE SET
                    raw_full_text=excluded.raw_full_text,
                    image_analysis_json=excluded.image_analysis_json,
                    scraped_at=excluded.scraped_at
            """, (vid, raw_full_text, ia_json))
            conn.commit()
            print(f"✓ {len(report.battery_certificates)} Zertifikate ({report.scanned_count} Bilder)")
        except Exception as e:
            conn.rollback()
            print(f"✗ {e}")
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()