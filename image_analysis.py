#!/usr/bin/env python3
"""Run battery-certificate vision analysis over a vehicle's images.

Two-stage pipeline (cost optimization):
  Stage 1 (cheap):  fetch the small "xs" thumbnail, run the vision backend on
                    it, classify as battery_certificate / marketing / document.
  Stage 2 (detail): ONLY for images that Stage 1 flagged as a likely
                    battery certificate, fetch the full-size URL and run the
                    structured extraction (more text detail, higher accuracy).

The vision backend (vision.read_text) is abstracted — OCR or Ollama-vision.
No semantic heuristic for vehicle values lives here: certificate.py extracts
only actual visible text, and provenance is always attached.
"""

import json
import io
import multiprocessing as mp
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

import certificate
import config
import vision


@dataclass
class ImageSpec:
    url: str
    index: int
    xs: str = ""
    s: str = ""
    m: str = ""

    @classmethod
    def from_api_obj(cls, obj: dict, index: int) -> "ImageSpec":
        return cls(
            url=obj.get("m") or obj.get("s") or obj.get("xs") or "",
            index=index,
            xs=obj.get("xs", ""),
            s=obj.get("s", ""),
            m=obj.get("m", ""),
        )


def _fetch(url: str, timeout: int = 25) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def looksha256(image_bytes: bytes) -> str:
    """Scale-normalized hash of the top header band.

    Verified empirically: real AVILOO certificates have a nearly identical top
    band. We normalize size first (max width ~160, keep aspect) and take the
    top ~28% as the 'header' so a thumbnail and the full-size image hash the
    same — this lets the template learned from a full cert match its thumbnail.
    """
    import hashlib
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        w, h = img.size
        scale = 160.0 / w
        img = img.resize((160, max(1, int(h * scale))))
        header_h = max(1, int(img.size[1] * 0.28))
        header = img.crop((0, 0, img.size[0], header_h)).resize((32, 16))
        return hashlib.sha1(header.tobytes()).hexdigest()
    except Exception:
        return ""


# Registered header hashes of confirmed battery certificates. They are learned
# at runtime (see load_certificate_templates) and persisted across runs.
_HEADER_TEMPLATES: set = set()
_TEMPLATES_LOADED = False


def load_certificate_templates(db_path) -> None:
    """Load the top-band header hashes of previously confirmed certificates
    from the database so the header pre-filter is effective from the start.
    Runs at most once per process."""
    global _TEMPLATES_LOADED
    if _TEMPLATES_LOADED:
        return
    _TEMPLATES_LOADED = True
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT c.images, e.image_analysis_json "
            "FROM car_equipment e JOIN cars c ON c.vehicleid=e.vehicleid "
            "WHERE e.image_analysis_json LIKE '%certificate_provider%'"
        ).fetchall()
        conn.close()
    except Exception:
        return
    # Learn header templates from the xs thumbnail of every confirmed
    # certificate, because stage-1 classifies thumbnails. Thumb and full image
    # are cropped differently, so the template must match what stage-1 sees.
    # Loading all known variants keeps header recall high without any fallback.
    for row in rows:
        try:
            certs = json.loads(row["image_analysis_json"])["image_analysis"]["battery_certificates"]
            images = json.loads(row["images"]) if row["images"] else []
        except (json.JSONDecodeError, TypeError):
            continue
        for cert in certs:
            idx = cert.get("source_image_index")
            if idx is None or idx >= len(images):
                continue
            obj = images[idx]
            thumb_url = obj.get("xs") or obj.get("s") if isinstance(obj, dict) else None
            if not thumb_url:
                continue
            try:
                register_header_template(_fetch(thumb_url, timeout=20))
            except Exception:
                continue
    if _HEADER_TEMPLATES:
        print(f"  Header-Templates geladen: {len(_HEADER_TEMPLATES)}", flush=True)


def register_header_template(image_bytes: bytes) -> None:
    """Register the top-band hash of a confirmed certificate as a template."""
    h = looksha256(image_bytes)
    if h:
        _HEADER_TEMPLATES.add(h)


def has_certificate_header(image_bytes: bytes) -> bool:
    """True when the image's top band matches a known certificate template."""
    if not _HEADER_TEMPLATES:
        return True  # no templates yet -> fall back to generic document filter
    return looksha256(image_bytes) in _HEADER_TEMPLATES


def looks_like_document(image_bytes: bytes) -> bool:
    """Cheap, PRE-OCR heuristic to reject obvious car photos.

    A battery certificate / document is typically a bright-ish image with high
    text density. AVILOO certificates are consistently 800x600 (~600 KB JPEG is
    roughly 150-210 KB) with a bright canvas and many thin edges. We use this
    as a hard pre-filter: only images that plausibly match a document signature
    reach the OCR stage. Actual certificate detection still happens via OCR.

    Returns True when the image plausibly contains dense text (document-like),
    False for likely photos. Pure image-statistics — it never extracts or
    interprets any value."""
    try:
        import struct
        from PIL import Image, ImageFilter
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception:
        return True  # can't tell -> let OCR decide

    w, h = img.size
    if w < 20 or h < 20:
        return False

    # AVILOO certificates are 800x600. Favor document-like aspect ratios
    # (roughly A4/4:3/portrait with dense text), reject ultra-wide photos.
    ratio = w / h
    if ratio > 2.2 or ratio < 0.45:
        return False

    # Image byte size as a weak signal: a text-heavy JPEG is larger than a
    # flat photo and smaller than a very high-res photo.
    size_kb = len(image_bytes) / 1024
    if size_kb < 8:
        return True  # tiny thumbnail — can't infer; let OCR decide

    # Bright canvas + edge density (same as before).
    scale = min(1.0, 200.0 / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    histogram = img.histogram()
    total = sum(histogram)
    light_ratio = sum(histogram[180:256]) / total if total else 0
    edges = img.filter(ImageFilter.FIND_EDGES)
    edge_hist = edges.histogram()
    edge_total = sum(edge_hist)
    edge_ratio = sum(edge_hist[40:256]) / edge_total if edge_total else 0

    if light_ratio > 0.25 and edge_ratio > 0.06:
        return True
    if edge_ratio > 0.30:
        return True
    return False


@dataclass
class ImageAnalysisReport:
    battery_certificates: list = field(default_factory=list)
    analyzed_count: int = 0
    scanned_count: int = 0
    errors: list = field(default_factory=list)


def _stage1_worker(spec_url: str):
    """OCR one thumbnail in its OWN process (each process has its own ONNX
    engine, so OCR inference actually runs in parallel across workers).
    Returns a list of (text, confidence) tuples, a string error message, or
    None when the image is rejected by the cheap document pre-filter."""
    try:
        content = _fetch(spec_url)
        if not looks_like_document(content):
            return None  # cheap prefilter: skip OCR on likely photos
        lines = vision.read_text(content)  # uses process-local backend
        return [(t, c) for t, c in lines]
    except Exception as e:
        return f"ERR: {e}"


def analyze_vehicle_images(
    images: list,
    max_images: Optional[int] = None,
    backend=None,
    fetch_thumb: bool = True,
    sleep: float = 0.0,
    stop_at_first: bool = True,
    workers: int = 4,
) -> ImageAnalysisReport:
    """Analyze the images of one vehicle.

    images: list of API image dicts (each with xs/s/m URLs) or strings.
    Returns an ImageAnalysisReport with battery_certificates entries that
    carry source_image_url + source_image_index + evidence.

    Cost optimization:
      Stage 1 classifies cheap thumbnails (xs). To avoid ONNX-engine lock
      serialization, thumbnails are OCR'd in a multiprocessing pool — every
      worker process owns its own engine, so reads run in parallel.
      Only images flagged as battery certificates are fetched at full size
      (stage 2) for structured extraction. `stop_at_first` stops after the
      first certificate is found — most listings carry a single AVILOO image.
    """
    import concurrent.futures as futures

    report = ImageAnalysisReport()
    specs: list[ImageSpec] = []
    for i, obj in enumerate(images):
        if isinstance(obj, str):
            specs.append(ImageSpec(url=obj, index=i, xs=obj, s=obj, m=obj))
        elif isinstance(obj, dict):
            specs.append(ImageSpec.from_api_obj(obj, i))
        else:
            continue
        if max_images is not None and len(specs) >= max_images:
            break

    # Stage 1: parallel thumbnail classification (process-local backends).
    backend = backend or vision.get_backend()
    src = {}
    for spec in specs:
        if spec.url:
            src[spec.xs or spec.s or spec.url] = spec

    # Pre-load known certificate header templates (learned in earlier runs).
    load_certificate_templates(config.DB_PATH)

    candidates = []
    urls = list(src)

    def _ocr_urls(url_batch):
        """OCR a batch of thumbnails in parallel; returns candidate (spec, lines)."""
        found = []
        if not url_batch:
            return found
        try:
            mp_ctx = mp.get_context("fork")
        except ValueError:
            mp_ctx = mp.get_context("spawn")
        with mp_ctx.Pool(processes=min(workers, len(url_batch))) as pool:
            try:
                for spec_url, lines in zip(url_batch, pool.imap_unordered(_stage1_worker, url_batch, chunksize=1)):
                    if isinstance(lines, str):
                        report.errors.append(f"image {spec_url[:60]}: {lines}")
                        continue
                    if lines is None:
                        continue  # rejected by the cheap document pre-filter
                    spec = src[spec_url]
                    report.scanned_count += 1
                    classification = certificate.classify_image_text([l for l, _ in lines])
                    if classification == "battery_certificate":
                        found.append((spec, lines))
                        if stop_at_first:
                            break
            finally:
                pool.terminate()
        return found

    if urls:
        # Only classify thumbnails whose header matches a known certificate
        # template. This keeps OCR to ~1 image per listing (certificates have
        # a consistent header); unknown/other document types are skipped.
        header_urls = []
        try:
            for u in urls:
                content = _fetch(src[u].xs or src[u].s or u, timeout=15)
                if has_certificate_header(content):
                    header_urls.append(u)
        except Exception:
            header_urls = urls  # header fetch failed -> be permissive

        candidates = _ocr_urls(header_urls)

    # Stage 2: full-size, structured extraction for certificate candidates.
    for spec, _lines in candidates:
        report.analyzed_count += 1
        try:
            full_content = _fetch(spec.m or spec.s or spec.url)
            result = certificate.analyze_image(full_content, spec.url, spec.index, backend=backend)
            if result.get("certificate_detected"):
                # Learn the header template for future runs.
                try:
                    register_header_template(_fetch(spec.xs or spec.s or spec.url))
                except Exception:
                    pass
                entry = {
                    "source_image_url": spec.url,
                    "source_image_index": spec.index,
                }
                entry.update(result.get("values", {}))
                ev = result.get("evidence", {})
                if ev:
                    entry["evidence"] = ev
                report.battery_certificates.append(entry)
                if stop_at_first:
                    break
            if sleep:
                time.sleep(sleep)
        except Exception as e:
            report.errors.append(f"img{spec.index}: {e}")

    return report