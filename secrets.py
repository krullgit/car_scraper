#!/usr/bin/env python3
"""Load secrets from environment variables or from a local secrets.json file.

secrets.json is gitignored and never pushed to any repository. Environment
variables take precedence:
    TELEGRAM_TOKEN
    TELEGRAM_CHAT_ID
"""

import json
import os
from pathlib import Path

SECRETS_FILE = Path(__file__).resolve().parent / "secrets.json"


def _load() -> dict:
    secrets = {}
    if SECRETS_FILE.exists():
        try:
            with open(SECRETS_FILE, encoding="utf-8") as f:
                secrets.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return secrets


_FILE_SECRETS = _load()


def get_secret(name: str, default: str = "") -> str:
    return os.environ.get(name) or _FILE_SECRETS.get(name) or default