#!/usr/bin/env python3
"""Vision backend abstraction for image analysis.

Two backends are supported:
  - "ocr": local ONNX OCR (rapidocr) — cheap, offline, default.
  - "ollama": a vision-capable Ollama model (e.g. qwen2.5vl / llava).

The backend is chosen once per process via get_backend(). All callers use the
abstract `read_text(image_bytes)` interface so the rest of the pipeline never
cares which engine produced the text.
"""

import os
import threading
from typing import Optional

DEFAULT_BACKEND = os.environ.get("VISION_BACKEND", "ocr").strip().lower()
OLLAMA_MODEL = os.environ.get("VISION_OLLAMA_MODEL", "qwen2.5vl:7b").strip()


class BaseVisionBackend:
    """Interface: return a list of (text, confidence) reading results."""

    name: str = "base"

    def read_text(self, image_bytes: bytes) -> list[tuple[str, float]]:
        raise NotImplementedError


class OcrBackend(BaseVisionBackend):
    """Local ONNX OCR — cheap and private. Reads multilingual text."""

    name = "ocr"

    def __init__(self) -> None:
        self._engine = None

    def _get_engine(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            self._engine = RapidOCR()
        return self._engine

    def read_text(self, image_bytes: bytes) -> list[tuple[str, float]]:
        engine = self._get_engine()
        result, _elapse = engine(image_bytes)
        if not result:
            return []
        out = []
        for box, text, score in result:
            out.append((str(text), float(score) if score else 0.0))
        return out


class OllamaVisionBackend(BaseVisionBackend):
    """Vision-capable Ollama model (qwen2.5vl / llava style)."""

    name = "ollama"

    def __init__(self, model: str = OLLAMA_MODEL) -> None:
        self.model = model
        self._base = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    def read_text(self, image_bytes: bytes) -> list[tuple[str, float]]:
        import base64
        import requests

        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": "Read all text in this image. Return the raw text lines.",
                "images": [b64],
            }],
            "stream": False,
        }
        resp = requests.post(f"{self._base}/api/chat", json=payload, timeout=120)
        resp.raise_for_status()
        text = resp.json().get("message", {}).get("content", "")
        return [(line.strip(), 1.0) for line in text.splitlines() if line.strip()]


_backend_lock = threading.Lock()
_backend_cache: dict[str, BaseVisionBackend] = {}


def get_backend(name: str = "") -> BaseVisionBackend:
    """Return a cached backend instance, selected by name (or env default)."""
    selected = (name or DEFAULT_BACKEND).lower()
    with _backend_lock:
        if selected in _backend_cache:
            return _backend_cache[selected]
        if selected == "ollama":
            backend = OllamaVisionBackend()
        else:
            backend = OcrBackend()
        _backend_cache[selected] = backend
        return backend


def read_text(image_bytes: bytes, backend: Optional[BaseVisionBackend] = None) -> list[tuple[str, float]]:
    """Convenience: read text from image bytes using a backend."""
    backend = backend or get_backend()
    return backend.read_text(image_bytes)