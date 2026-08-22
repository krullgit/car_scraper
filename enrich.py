#!/usr/bin/env python3
"""Scrape detail pages to extract full equipment list for each car."""

import asyncio
import re
import sqlite3
from pathlib import Path

from playwright.async_api import async_playwright

DB_PATH = Path(__file__).resolve().parent / "cars.db"

FEATURE_PATTERNS = {
    "ACC": re.compile(r"(?:Automatische\s+Distanzregelung|Adaptive\s+Cruise)",
                      re.IGNORECASE),
    "Anhängerkupplung": re.compile(r"(?:Anhängerkupplung\s*(?:abnehmbar|schwenkbar)?|AHK\s*(?:abnehmbar|schwenkbar)?|abnehmbare\s+Anhängerkupplung|schwenkbare\s+Anhängerkupplung)",
                                  re.IGNORECASE),
    "Rückfahrkamera": re.compile(r"(?:Rückfahrkamera|Rear\s*View|Rückfahr.*kamera|Area\s*View|360°.*Kamera|RFK)",
                                 re.IGNORECASE),
    "Automatik": re.compile(r"(?:Automatikgetriebe|Automatic|DSG|S.?tronic|Doppelkupplungs?|7-Gang-Automatik)",
                            re.IGNORECASE),
}


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


def store_equipment(vehicleid: int, full_text: str, features: dict[str, bool],
                    summary: str, dealer_address: str = "", dealer_phone: str = "",
                    dealer_name: str = "", raw_text: str = "",
                    raw_full_text: str = "") -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO car_equipment (vehicleid, full_text, acc, ahk, rfk, automatic,
                                   summary, dealer_address, dealer_phone, dealer_name,
                                   raw_text, raw_full_text, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(vehicleid) DO UPDATE SET
            full_text=excluded.full_text, acc=excluded.acc, ahk=excluded.ahk,
            rfk=excluded.rfk, automatic=excluded.automatic, summary=excluded.summary,
            dealer_address=excluded.dealer_address, dealer_phone=excluded.dealer_phone,
            dealer_name=excluded.dealer_name, raw_text=excluded.raw_text,
            raw_full_text=excluded.raw_full_text, scraped_at=excluded.scraped_at
    """, (vehicleid, full_text,
          int(features.get("ACC", False)),
          int(features.get("Anhängerkupplung", False)),
          int(features.get("Rückfahrkamera", False)),
          int(features.get("Automatik", False)),
          summary, dealer_address, dealer_phone, dealer_name, raw_text, raw_full_text))
    conn.commit()
    conn.close()


def extract_features(text: str) -> dict[str, bool]:
    result = {}
    for name, pattern in FEATURE_PATTERNS.items():
        match = pattern.search(text)
        result[name] = bool(match)
    # "Vorbereitung" contexts mean the feature is only prepared, not installed.
    # Check globally (not just ±30 chars) so prep is never flagged as installed.
    prep = re.search(
        r"Vorbereitung(?:\s+für)?\s+(?:für\s+)?(?:eine\s+)?Anhängerkupplung"
        r"|Anhängerkupplung(?:\s+für)?\s+Vorbereitung",
        text, re.IGNORECASE,
    )
    if prep:
        result["Anhängerkupplung"] = False
    return result


def extract_daten_section(text: str) -> str:
    """Extract the 'Daten' block (Erstzulassung, km, Kraftstoff, Vorbesitzer, etc.)
    from the page text. It sits between the Kaufpreis/Finanzierung blocks and the
    Ausstattung section. Returns the section as a compact multi-line string."""
    lines = text.split("\n")
    stripped = [l.strip() for l in lines]
    start = None
    for i, l in enumerate(stripped):
        if l.lower() in ("daten", "fahrzeugdaten") and i + 1 < len(stripped) \
                and stripped[i + 1]:
            start = i
            break
    if start is None:
        return ""
    # Skip the first few lines that may be labels of the previous block (Kaufpreis etc.)
    out = []
    for l in stripped[start + 1:start + 60]:
        low = l.lower()
        if low in ("ausstattung", "highlights"):
            break
        if low in ("fahrzeug anfragen", "impressum", "datenschutz"):
            break
        if not l:
            continue
        if low in ("kaufpreis", "finanzierung", "finanzierungsbeispiel", "daten",
                   "monatl. rate", "laufzeit", "schlussrate", "anzahlung",
                   "netto darlehensbetrag", "sollzins gebunden p.a.",
                   "effektiver jahreszins", "brutto darlehensbetrag",
                   "kreditschutzbrief", "ein angebot der"):
            continue
        # Skip pure price/currency lines that belong to financing (e.g. 17.740,00 €)
        if re.match(r"^[\d.,\s]+€?$", l) and re.search(r"\d", l) \
                and not re.match(r"^\d{1,2}$", l):
            continue
        out.append(l)
    return "\n".join(out)


def _is_footer_marker(l: str) -> bool:
    s = l.strip()
    low = s.lower()
    if low in ("zurück zur suche", "fahrzeug anfragen", "impressum", "datenschutz",
               "weitere informationen", "cookie policy",
               "datenschutzhinweis für kunden und interessenten", "compliance",
               "barrierefreiheitserklärung", "eu data act", "cookies",
               "alle akzeptieren", "notwendige akzeptieren",
               "individuelle einstellungen", "cookie einstellungen"):
        return True
    if low.startswith(("copyright", "eine gesellschaft der", "barrierefreiheits-menü",
                       "um unsere webseite für sie optimal", "weitere informationen",
                       "die volkswagen automobile")):
        return True
    if re.match(r"^(\*|²|¹|³|\*\*\*|©)", s):
        return True
    return False


def extract_raw_text(text: str, title: str = "") -> str:
    """Extract the complete vehicle-relevant text from the detail page.

    The page's body.innerText starts with the search listing and continues with
    the detail view (title, Kaufpreis, Daten, Ausstattung, Anbieter/Kontakte).
    The detail view starts at the vehicle title (its last occurrence in the
    document, optionally brand-prefixed). We include that line and everything
    after it until dealer boilerplate (footnotes, Zurück zur Suche, footer).

    If the title is not found, we anchor on a block that only exists on the
    detail page (Kaufpreis / Finanzierungsbeispiel / Erstzulassung / Ausstattung)
    and include the lines right above it. Falls back to the full page text."""
    lines = text.split("\n")
    stripped = [l.strip() for l in lines]

    start = None
    if title:
        candidates = {title, f"Škoda {title}", f"Skoda {title}", f"VW {title}",
                      f"Volkswagen {title}"}
        cand_low = {c.lower() for c in candidates if c}
        for i, l in enumerate(stripped):
            if l and l.lower() in cand_low:
                start = i  # keep last occurrence (detail view comes after listing)

    if start is not None:
        lines = [l.rstrip() for l in lines[start:]]
    else:
        head = None
        for anchor in ("Kaufpreis", "Finanzierungsbeispiel", "Erstzulassung", "Ausstattung"):
            for i, l in enumerate(stripped):
                if l.lower() == anchor.lower():
                    start = i
                    break
            if start is not None:
                head = lines[:start]
                break
        if start is not None and head is not None:
            keep = []
            for l in reversed(head):
                s = l.strip()
                if not s:
                    if keep:
                        break
                    continue
                low = s.lower()
                if low in ("home", "neuwagen", "gebrauchtwagen", "werkstatt", "standorte",
                           "kontakt", "filter", "preis", "marke", "suche starten",
                           "suche zurücksetzen", "sortieren nach", "finanzierungsrate",
                           "monatl. rate", "laufzeit", "privat-leasingrate", "privatleasing",
                           "zum angebot", "verkaufen sie ihr fahrzeug online!",
                           "jetzt den fahrzeugwert kostenlos ermitteln.", "jetzt bewerten",
                           "fahrzeug in zahlung geben."):
                    continue
                # A price/currency line in the head belongs to the search listing above.
                if re.match(r"^[\d.,\s]+€?\s*$", s) and re.search(r"\d", s):
                    continue
                keep.append(s)
                if len(keep) >= 2:
                    break
            keep.reverse()
            lines = keep + [l.rstrip() for l in lines[start:]]
        else:
            lines = [l.rstrip() for l in lines]

    out2 = []
    for l in lines:
        s = l.rstrip()
        if not s.strip():
            continue
        if _is_footer_marker(s):
            break
        out2.append(s)
    return "\n".join(out2).strip()


def extract_dealer_name(text: str) -> str:
    """Extract the dealer/standort name from the Anbieter/Kontakte block.

    Structure: "Kontakte\\nBetrieb Tempelhof\\nOberlandstraße 40-41..."
    or "Anbieter\\nVGRB GmbH\\nHansastraße 202...". Returns the first line
    that is a plausible name."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower() not in ("anbieter", "kontakte"):
            continue
        for l in lines[i + 1:i + 6]:
            s = l.strip()
            if not s:
                continue
            if re.match(r"^\d{4,5}$", s) or re.match(r"^\d{5}\s", s):
                continue
            if re.match(r"^[\d\s/\-()+]*$", s):
                continue
            if re.match(r"^[A-Za-zäöüÄÖÜß]+\s\d", s):  # street-like
                continue
            low = s.lower()
            if low.startswith(("*", "²", "³", "betrieb info")) or low.startswith("weitere informationen"):
                continue
            return s
    return ""


def extract_dealer_info(text: str) -> tuple[str, str]:
    """Extract dealer address and phone from the Anbieter/Kontakte section.

    Page structure variants:
      "Anbieter\\nVGRB GmbH\\nHansastraße 202\\n13088 Berlin\\n..."
      "Kontakte\\nBetrieb Tempelhof\\nOberlandstraße 40-41\\n12099 Berlin\\n030 / 8908 1055"
    Returns (address, phone)."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower() not in ("anbieter", "kontakte"):
            continue
        rest = [l.strip() for l in lines[i + 1:i + 10] if l.strip()]
        street = None
        city = None
        phone = None
        for l in rest:
            if re.match(r"^\d{5}\s", l):
                city = l
            elif re.match(r"^[\d\s/\-()+]*$", l) and re.search(r"\d", l):
                phone = l
            elif street is None and re.match(r"^[A-Za-zäöüÄÖÜß][\wöäüÖÄÜß.\- ]+\d+", l) \
                    and not l.startswith(("*", "Betrieb", "GmbH", "Gmbh", "Autohaus")):
                street = l
        if street and city and re.match(r"^\d{5}\s", city):
            return f"{street} · {city}", phone or ""
    return "", ""


async def scrape_detail_page(vehicleid: int, browser, car_info: dict = None) -> tuple[str, str, dict[str, bool], str, str, str, str]:
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

        body_text = await page.evaluate("document.body.innerText")

        # raw_full_text = the unmodified, complete body.innerText of the detail
        # page. Menu, footer and any other text is intentionally kept — the
        # downstream LLM agent judges the equipment itself. Nothing is removed.
        raw_full_text = body_text

        raw_text = extract_raw_text(body_text, car_info.get("shortdescription", "") if car_info else "")

        # Extract the Ausstattung section
        ausstattung_text = ""
        lines = body_text.split("\n")
        in_ausstattung = False
        for line in lines:
            stripped = line.strip()
            if stripped.lower() in ("ausstattung", "highlights"):
                in_ausstattung = True
                continue
            if in_ausstattung:
                if stripped.lower() in ("angaben zum hersteller", "anbieter", "fahrzeug anfragen",
                                        "impressum", "datenschutz", "finanzierung",
                                        "privatleasing", "kaufpreis", "daten"):
                    break
                if stripped:
                    ausstattung_text += stripped + "\n"

        features = extract_features(ausstattung_text)

        # Trust the API's transmission field for the gearbox flag
        if car_info and car_info.get("transmission_name"):
            features["Automatik"] = "automatik" in car_info["transmission_name"].lower()

        dealer_address, dealer_phone = extract_dealer_info(body_text)
        dealer_name = extract_dealer_name(body_text)
        daten_section = extract_daten_section(body_text)

        # Build summary text for copying
        summary_parts = []
        if car_info:
            summary_parts.append(car_info.get("shortdescription", ""))
            price = float(car_info.get("customerprice", 0) or 0)
            km = car_info.get("kilometers", 0)
            reg = (car_info.get("registrationdate", "") or "")[:7]
            summary_parts.append(f"{price:,.0f}€ · {km:,d}km · EZ {reg}".replace(",", "."))
            summary_parts.append(f"{car_info.get('power','?')}kW · {car_info.get('fuel_name','?')} · {car_info.get('transmission_name','?')}")
        if features:
            feat_list = [k for k, v in features.items() if v]
            if feat_list:
                summary_parts.append(" · ".join(feat_list))
        if dealer_address or dealer_phone:
            summary_parts.append(f"📍 {dealer_address}{(' · ' + dealer_phone) if dealer_phone else ''}")
        if daten_section:
            summary_parts.append("\nDaten:\n" + daten_section)
        if ausstattung_text:
            summary_parts.append("\nAusstattung:\n" + ausstattung_text.strip())

        summary = "\n".join(summary_parts)
        return raw_text, raw_full_text, ausstattung_text.strip(), features, summary, dealer_address, dealer_phone, dealer_name
    finally:
        await page.close()


async def main_async():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Get all cars that haven't been scraped yet (or have no equipment data).
# Only cars that were never enriched or are missing raw_text are processed —
# re-scraping all cars on every tracker run would be far too slow.
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
                car_info = {
                    "shortdescription": row["shortdescription"],
                    "customerprice": row["customerprice"],
                    "kilometers": row["kilometers"],
                    "registrationdate": row["registrationdate"],
                    "power": row["power"],
                    "fuel_name": row["fuel_name"],
                    "transmission_name": row["transmission_name"],
                }
                raw_text, raw_full_text, full_text, features, summary, dealer_address, dealer_phone, dealer_name = await scrape_detail_page(vid, browser, car_info)
                store_equipment(vid, full_text, features, summary, dealer_address, dealer_phone,
                                dealer_name, raw_text, raw_full_text)
                found = [k for k, v in features.items() if v]
                addr = f" | 📍{dealer_address}" if dealer_address else ""
                print(f"✓ {' | '.join(found) if found else '(none)'}{addr}")
            except Exception as e:
                print(f"✗ {e}")
        await browser.close()

    print("Done.")


def main():
    init_db()
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
