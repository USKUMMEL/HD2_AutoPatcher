"""Named Helldivers 2 archive catalog used by the optional source picker."""

from __future__ import annotations

import json
import os
import re
import struct
from pathlib import Path
from urllib.request import urlopen


ARCHIVE_LIST_URL = (
    "https://raw.githubusercontent.com/Boxofbiscuits97/"
    "HD2SDK-CommunityEdition/main/hashlists/archivehashes.json"
)
ARCHIVE_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")


def catalog_cache_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "HD2 Patch Fixer" / "archivehashes.json"


def parse_archive_catalog(raw_data) -> dict[str, str]:
    """Flatten the Community SDK's category -> ID -> name JSON structure."""
    if not isinstance(raw_data, dict):
        raise ValueError("Archive catalog must be a JSON object.")
    return {
        archive_id.lower(): f"{category}: {name}"
        for category, entries in raw_data.items()
        if isinstance(category, str) and isinstance(entries, dict)
        for archive_id, name in entries.items()
        if isinstance(archive_id, str)
        and isinstance(name, str)
        and ARCHIVE_ID_PATTERN.fullmatch(archive_id.lower())
    }


def read_cached_archive_catalog() -> dict[str, str]:
    try:
        return parse_archive_catalog(
            json.loads(catalog_cache_path().read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def load_archive_catalog(timeout: int = 8) -> dict[str, str]:
    """Fetch a current catalog, falling back to the local cache offline."""
    cached = read_cached_archive_catalog()
    try:
        with urlopen(ARCHIVE_LIST_URL, timeout=timeout) as response:
            raw_data = json.loads(response.read().decode("utf-8"))
        catalog = parse_archive_catalog(raw_data)
        cache_path = catalog_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(raw_data, ensure_ascii=True), encoding="utf-8")
        return catalog
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return cached


def installed_archive_ids(game_data_folder: str) -> set[str]:
    """Return archive IDs present in legacy files or a Slim bundle database."""
    try:
        game_path = Path(game_data_folder)
        archive_ids = {
            candidate.name.lower()
            for candidate in game_path.iterdir()
            if candidate.is_file() and ARCHIVE_ID_PATTERN.fullmatch(candidate.name.lower())
        }
        bundle_database = game_path / "bundle_database.data"
        if bundle_database.is_file():
            data = bundle_database.read_bytes()
            if len(data) >= 8:
                package_count = struct.unpack_from("<I", data, 4)[0]
                for index in range(package_count):
                    offset = 0x10 + (0x33 * index)
                    raw_name = data[offset:offset + 0x33]
                    archive_id = (
                        raw_name.decode(errors="ignore")
                        .split("\x17", 1)[0]
                        .rstrip("\x00")
                        .lower()
                    )
                    if ARCHIVE_ID_PATTERN.fullmatch(archive_id):
                        archive_ids.add(archive_id)
        return archive_ids
    except OSError:
        return set()
