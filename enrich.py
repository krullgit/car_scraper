#!/usr/bin/env python3
"""Collect the unmodified detail-page text for each car.

This module is a pure data collector: it stores the complete body.innerText
(raw_full_text) of the detail page. It does NOT extract or interpret features,
location, dealer names etc. — that is the downstream LLM agent's job.
"""

import asyncio
import sqlite3

from playwright.async_api import async_playwright

import config

DB_PATH = config.DB_PATH


def init_db() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS car_equipment (
            vehicleid INTEGER PRIMARY KEY,
            full_text TEXT,
            summary TEXT,
            acc INTEGER DEFAULT 0,
            ahk INTEGER DEFAULT 0,
            rfk INTEGER DEFAULT 0,
            automatic INTEGER DEFAULT 0,
            dealer_address TEXT,
            dealer_phone TEXT,
            dealer_name TEXT,
            raw_text TEXT,
            raw_full_text TEXT,
            scraped_at TEXT,
            FOREIGN KEY (vehicleid) REFERENCES cars(vehicleid)
        )
    """)
    for col in ("summary", "automatic", "dealer_address", "dealer_phone", "dealer_name", "raw_text", "raw_full_text"):
        try:
            conn.execute(f"ALTER TABLE car_equipment ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def store_equipment(vehicleid: int, raw_full_text: str, raw_text: str = "") -> None:
    """Store the collected raw page text. Semantic fields are left as 0/empty —
    they exist only for schema backwards compatibility and are never filled."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO car_equipment (vehicleid, full_text, summary, acc, ahk, rfk,
                                   automatic, dealer_address, dealer_phone,
                                   dealer_name, raw_text, raw_full_text, scraped_at)
        VALUES (?, ?, ?, 0, 0, 0, 0, '', '', '', ?, ?, datetime('now'))
        ON CONFLICT(vehicleid) DO UPDATE SET
            raw_text=excluded.raw_text,
            raw_full_text=excluded.raw_full_text,
            scraped_at=excluded.scraped_at
    """, (vehicleid, raw_text, raw_full_text))
    conn.commit()
    conn.close()


async def scrape_detail_page(vehicleid: int, browser, car_info: dict = None) -> str:
    """Return the unmodified document.body.innerText of the detail page."""
    page = await browser.new_page()
    try:
        url = f"https://www.volkswagen-automobile-berlin.de/gebrauchtwagen/fahrzeugsuche/{vehicleid}"
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        # The detail view is rendered by JS after the initial load. Wait until a
        # marker that only exists on the fully-rendered detail page appears.
        try:
            await page.wait_for_selector("text=Vorbesitzer", timeout=25000)
        except Exception:
            pass  # some pages omit that field — just scrape whatever is there
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

        # raw_full_text = the unmodified, complete body.innerText. Menu, footer
        # and any other text is intentionally kept — nothing is interpretive.
        return await page.evaluate("document.body.innerText")
    finally:
        await page.close()


async def main_async():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Only cars that were never enriched or are missing raw_full_text are
    # processed — re-scraping all cars on every tracker run would be too slow.
    rows = conn.execute("""
        SELECT c.*, e.summary as has_summary
        FROM cars c
        LEFT JOIN car_equipment e ON c.vehicleid = e.vehicleid
        WHERE c.is_active = 1 AND (e.vehicleid IS NULL OR e.raw_text IS NULL OR e.raw_text = ''
              OR e.raw_full_text IS NULL OR e.raw_full_text = '')
    """).fetchall()
    conn.close()

    if not rows:
        print("All cars already enriched.")
        return

    print(f"Enriching {len(rows)} cars...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for row in rows:
            vid = row["vehicleid"]
            desc = (row["shortdescription"] or "")[:50]
            print(f"  {vid} — {desc}...", end=" ", flush=True)
            try:
                raw_full_text = await scrape_detail_page(vid, browser)
                store_equipment(vid, raw_full_text)
                print(f"✓ {len(raw_full_text)} Zeichen")
            except Exception as e:
                print(f"✗ {e}")
        await browser.close()

    print("Done.")


def main():
    init_db()
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
