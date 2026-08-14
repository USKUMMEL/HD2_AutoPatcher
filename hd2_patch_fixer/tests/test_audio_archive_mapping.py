import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import hd2_patch_fixer.archive as archive_module
from hd2_patch_fixer.archive import (
    GameArchiveIndex,
    SearchToc,
    StreamToc,
    TocEntry,
    migrate_patch_audio_from_current_game_archives,
    map_patch_wwise_banks_to_game_archives,
)
from hd2_patch_fixer.constants import WwiseBankID, WwiseDepID, WwiseStreamID
from hd2_patch_fixer.community_audio_adapter import CommunityAudioAdapterError


ARCHIVE_MAGIC = 4026531857


def make_toc_index_data(entries):
    """Build the header/table bytes that ``SearchToc`` needs for a test."""

    type_ids = sorted({type_id for _file_id, type_id in entries})
    data = bytearray(
        struct.pack(
            "<IIII56s",
            ARCHIVE_MAGIC,
            len(type_ids),
            len(entries),
            0,
            b"\x00" * 56,
        )
    )
    for type_id in type_ids:
        data.extend(struct.pack("<QQQII", 0, type_id, 0, 16, 64))
    for file_id, type_id in entries:
        entry = bytearray(80)
        struct.pack_into("<QQ", entry, 0, file_id, type_id)
        data.extend(entry)
    return bytes(data)


class AudioArchiveMappingTests(unittest.TestCase):
    def test_legacy_game_archives_are_bulk_mapped_by_bank_id(self):
        bank_id = 101
        second_bank_id = 202
        with tempfile.TemporaryDirectory() as temp_dir:
            game_dir = Path(temp_dir)
            first_archive = game_dir / "aa11"
            second_archive = game_dir / "bb22"
            first_archive.write_bytes(make_toc_index_data([(bank_id, WwiseBankID)]))
            second_archive.write_bytes(
                make_toc_index_data(
                    [
                        (second_bank_id, WwiseBankID),
                        (bank_id, WwiseDepID),
                    ]
                )
            )
            # Patches must not be used as base-game destinations.
            (game_dir / "aa11.patch_0").write_bytes(
                make_toc_index_data([(999, WwiseBankID)])
            )

            with patch.object(archive_module, "is_slim_version", return_value=False):
                index = GameArchiveIndex(str(game_dir))
                matches = index.map_wwise_bank_ids_to_archive_paths(
                    [second_bank_id, bank_id, 404]
                )

        self.assertEqual(matches[bank_id], (str(first_archive),))
        self.assertEqual(matches[second_bank_id], (str(second_archive),))
        self.assertEqual(matches[404], ())

    def test_slim_bundle_database_uses_the_same_bank_mapping_interface(self):
        bank_id = 303
        package_name = "cc33"
        toc_data = make_toc_index_data([(bank_id, WwiseBankID)])
        with tempfile.TemporaryDirectory() as temp_dir:
            game_dir = Path(temp_dir)
            bundle_database = bytearray(0x10 + 0x33)
            struct.pack_into("<I", bundle_database, 4, 1)
            name_bytes = package_name.encode("ascii")
            bundle_database[0x10:0x10 + len(name_bytes)] = name_bytes
            bundle_database[0x10 + len(name_bytes)] = 0x17
            (game_dir / "bundle_database.data").write_bytes(bundle_database)

            with (
                patch.object(archive_module, "is_slim_version", return_value=True),
                patch.object(archive_module, "get_package_toc", return_value=toc_data) as get_toc,
            ):
                index = GameArchiveIndex(str(game_dir))
                matches = index.map_wwise_bank_ids_to_archive_paths([bank_id, 505])

        expected_path = str(game_dir / package_name)
        self.assertEqual(matches[bank_id], (expected_path,))
        self.assertEqual(matches[505], ())
        get_toc.assert_called_once_with(expected_path)

    def test_patch_helper_preserves_duplicate_destinations_for_the_caller(self):
        bank_id = 606
        first = SearchToc()
        first.update_path("first_game_archive")
        first.toc_entries = {WwiseBankID: {bank_id}}
        second = SearchToc()
        second.update_path("second_game_archive")
        second.toc_entries = {WwiseBankID: {bank_id}}

        index = GameArchiveIndex("unused")
        index.search_archives = [first, second]

        patch_archive = StreamToc()
        bank_entry = TocEntry()
        bank_entry.file_id = bank_id
        bank_entry.type_id = WwiseBankID
        patch_archive.add_entry(bank_entry)

        matches = map_patch_wwise_banks_to_game_archives(patch_archive, index)

        self.assertEqual(
            matches,
            {bank_id: ("first_game_archive", "second_game_archive")},
        )

    def test_semantic_audio_output_replaces_only_audio_entries(self):
        bank_id = 707
        stream_id = 808
        patch_archive = StreamToc()
        bank = TocEntry()
        bank.file_id = bank_id
        bank.type_id = WwiseBankID
        bank.toc_data = b"old-bank"
        patch_archive.add_entry(bank)

        def write_community_output(**kwargs):
            output_path = Path(kwargs["output_patch_path"])
            generated = StreamToc()
            generated.magic = ARCHIVE_MAGIC
            generated.unknown = 0
            generated.unk4_data = bytearray(56)
            generated.update_path(str(output_path))
            for file_id, type_id, toc_data, stream_data in (
                (bank_id, WwiseBankID, b"new-bank", b""),
                (bank_id, WwiseDepID, b"new-dep", b""),
                (stream_id, WwiseStreamID, b"new-stream-header", b"new-wem"),
            ):
                entry = TocEntry()
                entry.file_id = file_id
                entry.type_id = type_id
                entry.toc_data = toc_data
                entry.stream_data = stream_data
                generated.add_entry(entry)
            generated.to_file(str(output_path))
            return SimpleNamespace(
                output_path=output_path,
                base_archives=(Path("current_game_archive"),),
                modified_bank_ids=(bank_id,),
                modified_stream_ids=(stream_id,),
            )

        with (
            patch.object(
                archive_module,
                "map_patch_wwise_banks_to_game_archives",
                return_value={bank_id: ("current_game_archive",)},
            ),
            patch.object(
                archive_module,
                "migrate_audio_patch_with_community_engine",
                side_effect=write_community_output,
            ),
        ):
            entries = migrate_patch_audio_from_current_game_archives(
                game_data_folder="unused",
                broken_patch_path="original.patch_0",
                broken_patch=patch_archive,
                archive_index=GameArchiveIndex("unused"),
            )

        self.assertEqual(entries[(WwiseBankID, bank_id)].toc_data, b"new-bank")
        self.assertEqual(entries[(WwiseDepID, bank_id)].toc_data, b"new-dep")
        self.assertEqual(entries[(WwiseStreamID, stream_id)].stream_data, b"new-wem")

    def test_mapped_audio_failure_does_not_silently_export_raw_audio(self):
        bank_id = 909
        patch_archive = StreamToc()
        bank = TocEntry()
        bank.file_id = bank_id
        bank.type_id = WwiseBankID
        bank.toc_data = b"old-bank"
        patch_archive.add_entry(bank)

        with (
            patch.object(
                archive_module,
                "map_patch_wwise_banks_to_game_archives",
                return_value={bank_id: ("current_game_archive",)},
            ),
            patch.object(
                archive_module,
                "migrate_audio_patch_with_community_engine",
                side_effect=CommunityAudioAdapterError("synthetic merge failure"),
            ),
        ):
            with self.assertRaisesRegex(
                CommunityAudioAdapterError,
                "unfixed audio cannot be exported",
            ):
                migrate_patch_audio_from_current_game_archives(
                    game_data_folder="unused",
                    broken_patch_path="original.patch_0",
                    broken_patch=patch_archive,
                    archive_index=GameArchiveIndex("unused"),
                )


if __name__ == "__main__":
    unittest.main()
