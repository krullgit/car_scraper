#!/usr/bin/env python3
"""Benchmark: parallel vision pipeline on one vehicle."""
import json
import sqlite3
import time
import sys

sys.path.insert(0, "/home/matthes/Documents/car_craper")
import image_analysis

vehicle_id = int(sys.argv[1]) if len(sys.argv) > 1 else 8652667
workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4

conn = sqlite3.connect("/home/matthes/Documents/car_craper/cars.db")
row = conn.execute("SELECT images FROM cars WHERE vehicleid=?", (vehicle_id,)).fetchone()
imgs = json.loads(row[0])
conn.close()

t0 = time.time()
report = image_analysis.analyze_vehicle_images(imgs, workers=workers)
dt = time.time() - t0
print(f"{len(imgs)} Bilder, {report.scanned_count} gescannt, {dt:.1f}s | Zertifikate: {len(report.battery_certificates)}")
for c in report.battery_certificates:
    print(f"  SoH={c.get('soh_percent')} kWh={c.get('measured_capacity_kwh')}/{c.get('nominal_capacity_kwh')} "
          f"km={c.get('test_mileage_km')} date={c.get('test_date')} model={c.get('vehicle_model')} "
          f"rating={str(c.get('certificate_rating'))[:30]}")