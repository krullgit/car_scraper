#!/usr/bin/env python3
"""Tests for the battery-certificate vision analysis.

Regression case (from the requirements):
    A VW ID.5 listing with an AVILOO battery certificate image showing
    SoH 96,5 %, 74/77 kWh, 78.251 km, and 23.07.2026 must be recognized as a
    battery certificate and yield soh_percent = 96.5 — nothing invented that
    is not actually visible on the image.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import certificate


class _FakeBackend:
    """Vision backend that returns pre-scripted lines (deterministic test)."""

    name = "fake"

    def __init__(self, lines):
        self._lines = lines

    def read_text(self, image_bytes):
        return [(l, 0.99) for l in self._lines]


AVILOO_LINES = [
    "AVILOO Batteriezertifikat",
    "Fahrzeug: VW ID.5",
    "Gesundheitszustand (SOH): 96,5 %",
    "Energie: 74 kWh / 77 kWh",
    "Kilometerstand: 78.251 km",
    "Datum: 23.07.2026",
]


class TestCertificateClassification(unittest.TestCase):
    def test_aviloo_image_is_classified_as_battery_certificate(self):
        cls = certificate.classify_image_text(AVILOO_LINES)
        self.assertEqual(cls, "battery_certificate")

    def test_marketing_image_is_not_battery_certificate(self):
        cls = certificate.classify_image_text([
            "Inzahlungnahme aller Fahrzeuge",
            "Leistungsstarke Wartung",
            "Top-Konditionen",
            "Kfz-Versicherung",
        ])
        self.assertIn(cls, ("marketing", "other"))

    def test_car_photo_without_text_is_other(self):
        cls = certificate.classify_image_text([])
        self.assertEqual(cls, "other")


class TestCertificateExtraction(unittest.TestCase):
    def _analyze(self):
        return certificate.analyze_image(
            b"x",
            "https://cdn.example/imgs/12.jpg",
            12,
            backend=_FakeBackend(AVILOO_LINES),
        )

    def test_regression_aviloo_soh_965(self):
        r = self._analyze()
        self.assertTrue(r["certificate_detected"])
        self.assertEqual(r["values"]["soh_percent"], 96.5)
        self.assertEqual(r["values"]["certificate_provider"], "AVILOO")
        self.assertEqual(r["values"]["measured_capacity_kwh"], 74)
        self.assertEqual(r["values"]["nominal_capacity_kwh"], 77)
        self.assertEqual(r["values"]["test_mileage_km"], 78251)
        self.assertEqual(r["values"]["test_date"], "2026-07-23")
        self.assertEqual(r["source_image_url"], "https://cdn.example/imgs/12.jpg")
        self.assertEqual(r["source_image_index"], 12)

    def test_evidence_is_stored_for_traceability(self):
        r = self._analyze()
        ev = r["evidence"]
        self.assertIn("soh_percent", ev)
        self.assertIn("96,5", ev["soh_percent"])
        self.assertIn("78.251", ev["test_mileage"])

    def test_no_values_are_invented(self):
        # Only SOH visible -> only SOH extracted, nothing else guessed.
        backend = _FakeBackend([
            "AVILOO Batteriezertifikat",
            "Gesundheitszustand (SOH): 96,5 %",
        ])
        r = certificate.analyze_image(b"x", "u", 3, backend=backend)
        self.assertTrue(r["certificate_detected"])
        self.assertEqual(r["values"].get("soh_percent"), 96.5)
        self.assertNotIn("test_mileage_km", r["values"])
        self.assertNotIn("test_date", r["values"])

    def test_never_guess_soh_from_range_or_model(self):
        # Text with plenty of car data but NO certificate SOH must not yield soh.
        backend = _FakeBackend([
            "VW ID.5 Pro 82 kWh Batterie",
            "Reichweite 520 km (WLTP)",
            "Kilometerstand: 78.251 km",
            "Erstzulassung 03/2023",
            "Kaufpreis 29.950 EUR",
        ])
        lines = [l for l, _ in backend.read_text(b"")]
        cls = certificate.classify_image_text(lines)
        self.assertNotEqual(cls, "battery_certificate")
        r = certificate.analyze_image(b"x", "u", 0, backend=backend)
        self.assertFalse(r["certificate_detected"])

    def test_result_contains_empty_cert_list_when_none_found(self):
        backend = _FakeBackend(["Ein schones Auto vor dem Haus"])
        r = certificate.analyze_image(b"x", "u", 1, backend=backend)
        self.assertFalse(r["certificate_detected"])


if __name__ == "__main__":
    unittest.main(verbosity=2)