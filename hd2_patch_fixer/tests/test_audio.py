import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from hd2_patch_fixer.audio import (
    BANK_VERSION_KEY,
    WWISE_RESOURCE_MAGIC,
    inspect_audio_collection,
    inspect_audio_entry,
)
from hd2_patch_fixer.archive import StreamToc, TocEntry, build_entry_from_source
from hd2_patch_fixer.constants import WwiseBankID, WwiseDepID, WwiseStreamID


def make_entry(file_id, type_id, toc_data=b"", stream_data=b""):
    return SimpleNamespace(
        file_id=file_id,
        type_id=type_id,
        toc_data=toc_data,
        stream_data=stream_data,
    )


def make_chunk(tag, payload):
    return tag.encode("ascii") + struct.pack("<I", len(payload)) + payload


class AudioValidationTests(unittest.TestCase):
    def test_valid_community_style_bank_stream_and_dep_are_grouped(self):
        bank_id = 123456
        bank_payload = b"".join(
            (
                make_chunk("BKHD", struct.pack("<I", 154 ^ BANK_VERSION_KEY)),
                make_chunk("HIRC", struct.pack("<I", 0)),
            )
        )
        bank_toc = (
            WWISE_RESOURCE_MAGIC
            + struct.pack("<I", len(bank_payload))
            + struct.pack("<Q", bank_id)
            + bank_payload
        )
        stream_data = b"wem-data"
        stream_toc = WWISE_RESOURCE_MAGIC + b"\x00\x00\x00\x00" + struct.pack("<Q", len(stream_data))
        dep_toc = struct.pack("<II", 0, len(b"soundbanks/test.bnk")) + b"soundbanks/test.bnk"

        bank = make_entry(bank_id, WwiseBankID, toc_data=bank_toc)
        stream = make_entry(789, WwiseStreamID, toc_data=stream_toc, stream_data=stream_data)
        dep = make_entry(bank_id, WwiseDepID, toc_data=dep_toc)

        inspection = inspect_audio_entry(bank)
        self.assertTrue(inspection.valid)
        self.assertEqual(inspection.bank_version, 154)
        self.assertEqual(inspection.hirc_entry_count, 0)

        report = inspect_audio_collection(
            {
                WwiseBankID: {bank_id: bank},
                WwiseDepID: {bank_id: dep},
                WwiseStreamID: {789: stream},
            }
        )
        self.assertEqual(report.stream_count, 1)
        self.assertEqual(len(report.invalid_entries), 0)
        self.assertEqual(len(report.groups), 1)
        self.assertTrue(report.groups[0].has_bank)
        self.assertTrue(report.groups[0].has_dep)
        self.assertFalse(report.groups[0].notes)

    def test_malformed_stream_is_reported_without_attempting_rewrite(self):
        stream = make_entry(
            7,
            WwiseStreamID,
            toc_data=WWISE_RESOURCE_MAGIC + b"\x00\x00\x00\x00" + struct.pack("<Q", 99),
            stream_data=b"short",
        )

        inspection = inspect_audio_entry(stream)
        self.assertFalse(inspection.valid)
        self.assertIn("declares 99", inspection.notes[0])

    def test_legacy_12_byte_stream_record_from_community_writer_is_accepted(self):
        stream_data = b"legacy-wem"
        stream = make_entry(
            8,
            WwiseStreamID,
            toc_data=WWISE_RESOURCE_MAGIC + b"\x00\x00\x00\x00" + struct.pack("<I", len(stream_data)),
            stream_data=stream_data,
        )

        inspection = inspect_audio_entry(stream)
        self.assertTrue(inspection.valid)
        self.assertEqual(inspection.declared_stream_size, len(stream_data))

    def test_archive_safe_preserve_mode_keeps_audio_bytes_exactly(self):
        bank_id = 44
        bank_payload = make_chunk("BKHD", struct.pack("<I", 154 ^ BANK_VERSION_KEY))
        entry = TocEntry()
        entry.file_id = bank_id
        entry.type_id = WwiseBankID
        entry.toc_data = (
            WWISE_RESOURCE_MAGIC
            + struct.pack("<I", len(bank_payload))
            + struct.pack("<Q", bank_id)
            + bank_payload
        )
        entry.gpu_data = b"opaque-gpu-payload"
        entry.stream_data = b"opaque-stream-payload"

        rebuilt, mode = build_entry_from_source(
            entry,
            raw_fallback_for_unsupported=False,
            default_archive=StreamToc(),
        )

        self.assertEqual(mode, "audio-validated-preserve")
        self.assertEqual(rebuilt.toc_data, entry.toc_data)
        self.assertEqual(rebuilt.gpu_data, entry.gpu_data)
        self.assertEqual(rebuilt.stream_data, entry.stream_data)


if __name__ == "__main__":
    unittest.main()
