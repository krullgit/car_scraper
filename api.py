#!/usr/bin/env python3
"""Autohausen API client: fetch the full stock of the tracked model families."""

from typing import Any

import requests

import config


def _base_filters(make: int, model_ids: list[int]) -> dict[str, Any]:
    return {
        "typeextendedcode": [2, 4],
        "make": [make],
        "model": model_ids,
        "customerprice": [None, config.MAX_PRICE],
    }


def _count(filt: dict[str, Any]) -> int:
    resp = requests.post(f"{config.API_BASE}/count", json={
        "filter": filt,
        "publicKey": config.PUBLIC_KEY,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json().get("meta", {}).get("total", 0)


def _page(filt: dict[str, Any], offset: int, limit: int) -> list[dict]:
    resp = requests.post(f"{config.API_BASE}/list", json={
        "filter": filt,
        "orderBy": "priceAsc",
        "offset": offset,
        "limit": limit,
        "publicKey": config.PUBLIC_KEY,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def fetch_all_cars() -> list[dict]:
    """Fetch all matching cars with full pagination.

    The target model families span 3 makes (Audi 11, Skoda 46, VW 52), so we
    query each make separately and merge by vehicleid. Only a price ceiling is
    applied — the full available stock of the four model lines is kept."""
    result: dict[int, dict] = {}
    for make, model_ids in config.MAKE_MODELS.items():
        filt = _base_filters(make, model_ids)

        total = _count(filt)
        offset = 0
        while offset < total:
            batch = _page(filt, offset, config.PAGE_SIZE)
            for car in batch:
                result[car["vehicleid"]] = car
            offset += len(batch)
            if len(batch) < config.PAGE_SIZE:
                break

    return list(result.values())