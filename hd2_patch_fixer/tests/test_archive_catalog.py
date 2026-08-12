import struct
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from hd2_patch_fixer.archive_catalog import (  # noqa: E402
    installed_archive_ids,
    parse_archive_catalog,
)


class ArchiveCatalogTests(unittest.TestCase):
    def test_catalog_is_flattened_and_normalized(self):
        catalog = parse_archive_catalog({
            "Armor": {"AABBCCDDEEFF0011": "AC-1 Body"},
            "Ignored": {"not-an-archive": "Nope"},
        })
        self.assertEqual(catalog, {"aabbccddeeff0011": "Armor: AC-1 Body"})

    def test_slim_bundle_database_archive_ids_are_discovered(self):
        archive_id = b"aabbccddeeff0011"
        with tempfile.TemporaryDirectory() as directory:
            bundle_database = bytearray(0x10 + 0x33)
            struct.pack_into("<I", bundle_database, 4, 1)
            bundle_database[0x10:0x10 + len(archive_id)] = archive_id
            (Path(directory) / "bundle_database.data").write_bytes(bundle_database)
            self.assertEqual(installed_archive_ids(directory), {archive_id.decode()})


if __name__ == "__main__":
    unittest.main()
