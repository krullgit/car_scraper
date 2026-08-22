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


def looks_like_document(image_bytes: bytes) -> bool:
    """Cheap, PRE-OCR heuristic to reject obvious car photos.

    A battery certificate / document is typically a bright-ish image with high
    text density: many local intensity changes (edges), relatively uniform
    background and fairly small size compared to a photo. We only use this as a
    pre-filter that avoids scanning photos with OCR — actual certificate
    detection still happens via the vision/OCR backend afterwards.

    Returns True when the image plausibly contains dense text (document-like),
    False for likely photos. This is a pure image-statistics filter; it never
    extracts or interprets any value."""
    try:
        from PIL import Image, ImageFilter, ImageOps
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception:
        return True  # can't tell -> let OCR decide

    w, h = img.size
    if w < 20 or h < 20:
        return False

    # Downscale large images to keep the statistics cheap (~160px wide).
    scale = min(1.0, 200.0 / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    # Brightness: certificates are usually light documents with dark text.
    stats = img.getbbox()  # not used; keep simple
    histogram = img.histogram()
    total = sum(histogram)
    light_pixels = sum(histogram[180:256])
    dark_pixels = sum(histogram[0:60])
    light_ratio = light_pixels / total if total else 0
    dark_ratio = dark_pixels / total if total else 0

    # Edge density: text creates many thin high-contrast edges.
    edges = img.filter(ImageFilter.FIND_EDGES)
    edge_hist = edges.histogram()
    edge_total = sum(edge_hist)
    strong_edges = sum(edge_hist[40:256])
    edge_ratio = strong_edges / edge_total if edge_total else 0

    # A photo usually has large smooth areas; a document has spare white space
    # between tightly packed text lines. A battery certificate is virtually
    # always a light document: bright canvas (>25%) with a fair amount of thin
    # high-contrast edges (>6%). We keep images that plausibly contain text and
    # reject dark smooth photos — this roughly halves the OCR load.
    if light_ratio > 0.25 and edge_ratio > 0.06:
        return True
    # Exception: extremely edge-heavy images (dense screenshots/documents).
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

    candidates = []
    urls = list(src)
    if urls:
        # fork is the Linux default and does not re-import the main module
        # (avoiding the "No such file: <stdin>" spawn problem).
        try:
            mp_ctx = mp.get_context("fork")
        except ValueError:
            mp_ctx = mp.get_context("spawn")
        with mp_ctx.Pool(processes=min(workers, len(urls))) as pool:
            for spec_url, lines in zip(urls, pool.imap_unordered(_stage1_worker, urls, chunksize=1)):
                if isinstance(lines, str):
                    report.errors.append(f"image {spec_url[:60]}: {lines}")
                    continue
                if lines is None:
                    continue  # rejected by the cheap document pre-filter
                spec = src[spec_url]
                report.scanned_count += 1
                classification = certificate.classify_image_text([l for l, _ in lines])
                if classification == "battery_certificate":
                    candidates.append((spec, lines))
                    if stop_at_first:
                        pool.terminate()
                        break

    # Stage 2: full-size, structured extraction for certificate candidates.
    for spec, _lines in candidates:
        report.analyzed_count += 1
        try:
            full_content = _fetch(spec.m or spec.s or spec.url)
            result = certificate.analyze_image(full_content, spec.url, spec.index, backend=backend)
            if result.get("certificate_detected"):
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