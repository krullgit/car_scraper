#!/usr/bin/env python3
"""Battery-certificate detection & structured extraction from vehicle images.

This is a SEPARATE processing step on top of the raw scraper. It never
invents values: every extracted field is read from the visible text of the
image (via a vision/OCR backend). Provenance (source_image_url +
source_image_index + evidence) is stored for every finding.

Deliberately no SoH estimation from range/SoC/age/mileage/capacity — a value
is only produced when it is actually readable on a certificate image.
"""

import io
import json
import re
from typing import Any, Optional

import requests

# Images whose OCR text matches any of these are rejected as NOT being a
# battery certificate. These are dealership/brand marketing phrases.
_MARKETING_MARKERS = (
    "inzahlung", "leasing", "finanzierung", "gewerbekunden", "werkstatt",
    "versicherung", "zufriedene kunden", "ihr wunschfahrzeug", "privatkunden",
    "beratung", "probefahrt", "service-paket", "servicepaket", "wartung",
    "kfz-versicherung", "top-konditionen", "marken", "gebrauchtwagen-zentrum",
    "gebrauchtwagen zentrum", "fahren sie grün", "leasingrate", "kaufpreis",
    "finanzierungsrate", "monatl. rate",
)

# Phrases that positively identify a battery health certificate.
_CERTIFICATE_MARKERS = (
    "gesundheitszustand", "batteriezustand", "state of health", "soh",
    "zellspannung", "zellspannungen", "batterie-zertifikat", "batteriezertifikat",
    "hybrid-zertifikat", "hybridzertifikat", "aviloo", "batteriegesundheit",
    "capacity", "ausgezeichneter gesundheitszustand", "health certificate",
    "battery diagnostics", "batteriediagnose", "batterie-zertifiziert",
)

# Certificate providers we can detect from text.
_PROVIDERS = ("AVILOO", "TUEV", "TÜV", "DEKRA", "GTÜ", "Batterie-Check", "Batteriecheck")


def _norm(text: str) -> str:
    """Normalize OCR noise for matching: lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def classify_image_text(lines: list[str]) -> str:
    """Classify an image from its reading lines.

    Returns one of: "battery_certificate", "marketing", "document", "other".
    This is a deliberately cheap heuristic — it distinguishes documents with
    battery-health wording from marketing/financing material. It does NOT
    extract any vehicle values.
    """
    joined = _norm(" ".join(lines))
    if not joined:
        return "other"

    cert_hits = sum(1 for m in _CERTIFICATE_MARKERS if m in joined)
    marketing_hits = sum(1 for m in _MARKETING_MARKERS if m in joined)

    # Must have a strong battery-health signal to be trusted.
    if cert_hits >= 2 or ("aviloo" in joined and ("soh" in joined or "gesundheitszustand" in joined)):
        if marketing_hits >= 2 and cert_hits < 3:
            return "marketing"
        return "battery_certificate"
    # A battery-health label + a percent reading on a document-like image.
    if "soh" in joined and re.search(r"\d{1,3}[.,]\d{1,2}\s*%", joined) and "marke" in joined:
        return "battery_certificate"
    if cert_hits == 1 and "zertifikat" in joined:
        return "document"  # generic certificate, not battery-health
    if cert_hits >= 1:
        return "document"
    if marketing_hits >= 1:
        return "marketing"
    return "other"


# --- Extraction (values only from visible text) --------------------------

def _num(s: str) -> Optional[float]:
    s = s.strip().replace("\u00a0", " ").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _extract_percent(text: str) -> Optional[float]:
    """Find '96,5 %' style SOH next to Gesundheitszustand/SOH markers.

    Supports both inline ("Gesundheitszustand (SOH): 96,5 %") and column
    layouts on real AVILOO certificates ("...GESUNDHEITSZUSTAND (SOH)...
    ERGEBNISSE ... 94,3 %")."""
    full = _norm(text)
    for m in re.finditer(r"(gesundheitszustand|state\s*of\s*health|soh|batteriezustand)", full):
        seg = full[m.start():m.start() + 80]
        mnum = re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%", seg)
        if mnum:
            return _num(mnum.group(1))
    # Column layout: a percent on the line(s) after ERGEBNISSE/GESUNDHEITSZUSTAND.
    for marker in ("ergebnisse", "gesundheitszustand"):
        m2 = re.search(re.escape(marker) + r"[^\d]{0,40}?(\d{1,3}(?:[.,]\d{1,2})?)\s*%", full)
        if m2:
            return _num(m2.group(1))
    # fallback: a bare '96,5 %' on a certificate-like line
    mnum = re.search(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%", full)
    if mnum:
        return _num(mnum.group(1))
    return None


def _extract_capacity(text: str) -> dict:
    """Find '74 kWh / 77 kWh' pairs, incl. AVILOO '73kWh|77kWh' layout."""
    full = _norm(text)
    m = re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:kwh|kwh?\b)", full)
    if not m:
        return {}
    first = _num(m.group(1))
    rest = full[m.end():]
    m2 = re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:kwh|kwh?\b)", rest)
    second = _num(m2.group(1)) if m2 else None
    if second is not None and abs(first - second) >= 1:
        return {"measured_capacity_kwh": min(first, second), "nominal_capacity_kwh": max(first, second)}
    return {"measured_capacity_kwh": first}


def _extract_mileage(text: str) -> Optional[int]:
    full = _norm(text)
    m = re.search(r"(kilometerstand|laufleistung|fahrleistung|km\s*stand)[:\s]*([\d.\xa0\s]{4,12})\s*k?m", full)
    if m:
        raw = m.group(2).replace(" ", "").replace("\u00a0", "")
        try:
            return int(raw.replace(".", ""))
        except ValueError:
            return None
    m = re.search(r"([\d]{1,3}(?:\.\d{3}){1,4})\s*k?m", full)
    return int(m.group(1).replace(".", "")) if m else None


def _extract_date(text: str) -> Optional[str]:
    """Find a date like 23.07.2026, 2026-07-23 or AVILOO 17.03.26,08:32."""
    full = _norm(text)
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", full)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})", full)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # AVILOO column layout: 'DATUM UND UHRZEIT: ... 17.03.26,08:32'
    for marker in ("datum", "datum und uhrzeit"):
        idx = full.find(marker)
        if idx >= 0:
            seg = full[idx:idx + 80]
            m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2})", seg)
            if m:
                yy = int(m.group(3))
                year = 2000 + yy if yy < 80 else 1900 + yy
                return f"{year}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def _detect_provider(text: str) -> Optional[str]:
    for p in _PROVIDERS:
        if p.lower() in text.lower():
            return "TÜV" if p == "TUEV" else p
    return None


_KNOWN_BRANDS = ("vw", "volkswagen", "audi", "skoda", "skoda", "seat", "cupra", "porsche",
                 "hyundai", "kia", "tesla", "bmw", "mercedes", "renault", "peugeot", "opel",
                 "ford", "nissan", "toyota")


def _detect_brand(text: str) -> Optional[str]:
    full = _norm(text)
    m = re.search(r"\bmarke\s*[:|\s]\s*([a-zäöüß\- ]{1,40})", full)
    if not m:
        return None
    val = m.group(1).strip()
    # take the first known brand token within the captured tail
    for token in _KNOWN_BRANDS:
        pat = re.search(rf"^{token}\b" if False else rf"\b{re.escape(token)}\b", val)
        if pat:
            return token
    return val.split()[0][:24] if val else None


def _detect_model(text: str) -> Optional[str]:
    m = re.search(r"modell\s*[:|\s]\s*([A-Za-z0-9äöüÄÖÜß\- ]{1,30})", _norm(text))
    if not m:
        return None
    raw = m.group(1).strip()
    # stop at the first word that looks like a new field label
    raw = re.split(r"\s+(gesundheitszustand|soh|kilometerstand|energie|ergebnis|batterie|datum|marke|zustand)", raw)[0].strip()
    # strip kWh-size tail and trailing junk (OCR merges '77kWh' into model)
    raw = re.sub(r"[-\s]?\d{1,3}kwh.*$", "", raw, flags=re.IGNORECASE).strip(" -")
    raw = re.sub(r"\s*\|.*$", "", raw).strip(" -")
    return raw or None


def _detect_rating(text: str) -> Optional[str]:
    # AVILOO: BEWERTUNG <rating text>; also 'Ausgezeichneter Gesundheitszustand'
    full = _norm(text)
    for marker in ("bewertung", "zusammenfassung"):
        idx = full.find(marker)
        if idx >= 0:
            seg = full[idx:idx + 60]
            # strip trailing boilerplate after rating text
            for cut in ("basierend auf", "diese batterie", "die antriebsbatterie"):
                ci = seg.find(cut)
                if ci >= 0:
                    seg = seg[:ci]
            val = re.sub(r"\s+", " ", seg.replace(marker, "").strip(" :.")).strip()
            if val:
                return val
    # fallback: rating phrases anywhere
    for phrase in ("ausgezeichneter gesundheitszustand", "guter gesundheitszustand",
                   "durchschnittlicher gesundheitszustand", "keine auffalligkeiten"):
        if phrase in full:
            return phrase.upper()
    return None


# --- Public API -----------------------------------------------------------

def analyze_image(image_bytes: bytes, source_image_url: str, source_image_index: int,
                  backend=None) -> dict[str, Any]:
    """Analyze one image. Returns a result dict with `certificate_detected`.

    Never invents values: every field in the result is backed by evidence
    extracted from the visible text (which is stored under `evidence`)."""
    import vision

    lines = vision.read_text(image_bytes, backend)
    body_raw = "\n".join(l for l, _ in lines)
    full_normalized = _norm(body_raw)
    joined = full_normalized

    classification = classify_image_text([l for l, _ in lines])
    payload: dict[str, Any] = {
        "source_image_url": source_image_url,
        "source_image_index": source_image_index,
        "certificate_detected": classification == "battery_certificate",
        "classification": classification,
    }
    if not payload["certificate_detected"]:
        # Still keep evidence of what was read, for traceability.
        if classification != "other" and body_raw.strip():
            payload["evidence"] = {"raw_text": body_raw.strip()[:3000]}
        return payload

    evidence: dict[str, str] = {}
    values: dict[str, Any] = {}

    provider = _detect_provider(joined)
    if provider:
        values["certificate_provider"] = provider
        evidence["certificate_provider"] = next(
            (l for l in (line.strip() for line in body_raw.splitlines()) if provider.lower() in l.lower()),
            provider,
        )

    brand = _detect_brand(full_normalized)
    if brand:
        values["vehicle_brand"] = brand
        evidence["vehicle_brand"] = next(
            (l for l in (line.strip() for line in body_raw.splitlines())
             if re.search(r"marke", l, re.IGNORECASE)), "")
    model = _detect_model(full_normalized)
    if model:
        values["vehicle_model"] = model
        evidence["vehicle_model"] = next(
            (l for l in (line.strip() for line in body_raw.splitlines())
             if re.search(r"modell", l, re.IGNORECASE)), "")

    soh = _extract_percent(body_raw)
    if soh is not None:
        values["soh_percent"] = soh
        evidence["soh_percent"] = next(
            (l for l in (line.strip() for line in body_raw.splitlines())
             if re.search(r"\d{1,3}(?:[.,]\d{1,2})?\s*%", l)), "")
    if soh is not None:
        rating = _detect_rating(full_normalized)
        if rating:
            values["certificate_rating"] = rating
            evidence["certificate_rating"] = next(
                (l for l in (line.strip() for line in body_raw.splitlines())
                 if re.search(r"gesundheitszustand|auffalligkeit", l, re.IGNORECASE)), "")

    cap = _extract_capacity(body_raw)
    if "measured_capacity_kwh" in cap:
        values.update(cap)
        evidence["capacity"] = next(
            (l for l in (line.strip() for line in body_raw.splitlines())
             if re.search(r"kwh", l, re.IGNORECASE)), "",
        )

    mileage = _extract_mileage(body_raw)
    if mileage is not None:
        values["test_mileage_km"] = mileage
        evidence["test_mileage"] = next(
            (l for l in (line.strip() for line in body_raw.splitlines())
             if re.search(r"kilometerstand|laufleistung|km\s*stand", l, re.IGNORECASE)), "",
        )

    date = _extract_date(body_raw)
    if date:
        values["test_date"] = date
        evidence["test_date"] = next(
            (l for l in (line.strip() for line in body_raw.splitlines()) if re.search(r"\d{1,2}\.\d{1,2}\.\d{2,4}", l)),
            "",
        )

    payload["values"] = values
    payload["evidence"] = evidence
    return payload


def strip_none_values(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove explicit None entries so output only contains real values."""
    values = payload.get("values", {})
    payload["values"] = {k: v for k, v in values.items() if v is not None}
    return payload