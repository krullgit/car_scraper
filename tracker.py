#!/usr/bin/env python3
"""Periodic tracker for Audi Q4 e-tron, VW ID.4, VW ID.5 and Skoda Enyaq listings.

Stores full history in SQLite and publishes a public cars.json feed. No
equipment/km/price pre-filtering (besides the price ceiling) — the downstream
LLM agent evaluates raw_full_text itself.

Usage:
    python3 tracker.py               # run continuously (default: every 30 min)
    python3 tracker.py --once        # run once and exit (for cron/systemd timer)
    python3 tracker.py --interval N  # run every N seconds
"""

import argparse
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import api
import config
import db
import feed
import notify


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_subprocess(name: str, args: list[str], timeout: int) -> None:
    """Run a helper script and print its output, without failing the run."""
    try:
        proc = subprocess.run(
            ["python3", str(config.SCRIPT_DIR / args[0])] + args[1:],
            capture_output=True, timeout=timeout,
        )
    except Exception as e:
        print(f"  WARN: {name} failed: {e}", file=sys.stderr)
        return
    if proc.stdout:
        print(proc.stdout.decode(errors="replace").rstrip())
    if proc.returncode != 0 and proc.stderr:
        print(f"  WARN: {name} exit {proc.returncode}: {proc.stderr.decode(errors='replace').strip()[:500]}",
              file=sys.stderr)


def run_scrape(conn: sqlite3.Connection) -> None:
    started = time.monotonic()
    started_iso = _now_iso()
    status = "ok"

    print(f"[{started_iso}] Fetching cars from API...")
    try:
        cars = api.fetch_all_cars()
    except Exception as e:
        print(f"[{started_iso}] ERROR fetching: {e}", file=sys.stderr)
        conn.execute("""
            INSERT INTO scrape_runs (started_at, finished_at, status)
            VALUES (?, ?, ?)
        """, (started_iso, _now_iso(), f"fetch_error: {str(e)[:200]}"))
        conn.commit()
        return

    print(f"[{started_iso}] Got {len(cars)} cars. Updating database...")
    stats = db.update_db(conn, cars)

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

    # Export JSON (legacy local website format)
    try:
        db.export_legacy_json(conn)
    except Exception as e:
        print(f"  WARN: JSON export failed: {e}", file=sys.stderr)

    # Enrich new cars with detail page data (fills raw_text/raw_full_text).
    # Only cars that were never enriched (or miss raw data) are scraped.
    _run_subprocess("enrich", ["enrich.py"], timeout=10800)

    # Build the public split feed (cars_index.json + cars/<id>.json) for the agent
    try:
        feed_summary, index = feed.write_split_feed(conn)
        feed.write_json(feed.build_status(feed_summary, conn), config.STATUS_FILE)
        print(f"  Feed: {feed_summary['vehicle_count']} vehicles "
              f"({len(index)} index entries, status.json written)")
    except Exception as e:
        print(f"  WARN: feed build failed: {e}", file=sys.stderr)

    # Publish the feed to GitHub
    _run_subprocess("publish", ["publish.py"], timeout=120)

    if stats["new_ids"]:
        notify.notify_new_cars(conn, stats["new_ids"])

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
    parser.add_argument("--db", type=str, default=str(config.DB_PATH),
                        help="Path to SQLite database")
    args = parser.parse_args()

    db_path = Path(args.db)
    print(f"Database: {db_path}")
    print(f"Interval: {'once' if args.once else f'{args.interval}s'}")
    print(f"Export:   {config.JSON_EXPORT}")
    print()

    conn = db.get_db()

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