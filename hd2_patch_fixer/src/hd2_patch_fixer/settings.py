"""Small AppData-backed preferences store for the desktop application."""

from __future__ import annotations

import json
import os
from pathlib import Path


def app_data_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "HD2 Patch Fixer"


def preferences_path() -> Path:
    return app_data_directory() / "settings.json"


def load_preferences() -> dict:
    try:
        raw_data = json.loads(preferences_path().read_text(encoding="utf-8"))
        return raw_data if isinstance(raw_data, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def save_preferences(data: dict) -> None:
    """Atomically save user-only settings without failing the UI on disk errors."""
    try:
        path = preferences_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary_path.replace(path)
    except OSError:
        pass
