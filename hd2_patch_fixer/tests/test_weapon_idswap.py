import struct
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from hd2_patch_fixer.archive import (  # noqa: E402
    StreamToc,
    TocEntry,
    infer_weapon_idswap_mappings,
    is_static_unit,
    migrate_static_unit_idswap_unit,
    migrate_weapon_idswap_unit,
    validate_unit_dependencies,
)
from hd2_patch_fixer.constants import UnitID  # noqa: E402


LEGACY_UNIT_VERSION = 0xA4CD34
CURRENT_UNIT_VERSION = 0xA4CD36

TARGET_UNIT_ID = 0x11C27D3BABB38956
SOURCE_UNIT_ID = 0x2152D5147B0AC418
BONES_ID = 0x1111222233334444
STATE_MACHINE_ID = 0x5555666677778888
COMPOSITE_ID = 0x9999AAAABBBBCCCC


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _build_unit_payload(
    *,
    version: int,
    bones_ref: int = 0,
    state_machine_ref: int = 0,
    composite_ref: int = 0,
    marker: bytes = b"patch-unit-data",
    format_ids: tuple[int, ...] = (0x1A, 0x10, 0x1D),
) -> bytes:
    """Make a compact Unit header with the legacy stream-layout table.

    The updater from the community patcher reads the Unit version at ``+0x2C``
    and, for legacy Units, walks the layout-list pointer at ``+0x5C``.  This
    fixture keeps all other bytes intentionally distinctive so the migration
    test catches an accidental full Unit/LOD/source rebuild.
    """

    layout_list_offset = 0x80
    first_item_offset = layout_list_offset + 8 + 8
    payload = bytearray(0x220)

    struct.pack_into("<Q", payload, 0x08, bones_ref)
    struct.pack_into("<Q", payload, 0x10, composite_ref)
    struct.pack_into("<Q", payload, 0x20, state_machine_ref)
    # Header flag 2 plus a separate high 32-bit Unit version.
    struct.pack_into("<Q", payload, 0x28, (version << 32) | 2)
    struct.pack_into("<I", payload, 0x30, 0x80)  # lod-group offset
    struct.pack_into("<I", payload, 0x34, 0x90)  # next section offset
    struct.pack_into("<I", payload, 0x5C, layout_list_offset)
    struct.pack_into("<I", payload, 0x60, len(payload) - 8)  # ending offset

    struct.pack_into("<I", payload, layout_list_offset, 1)
    struct.pack_into("<I", payload, layout_list_offset + 4, 8)
    for index in range(16):
        item_offset = first_item_offset + index * 20
        struct.pack_into("<I", payload, item_offset, 0x70000000 + index)
        format_id = format_ids[index] if index < len(format_ids) else 0
        struct.pack_into("<I", payload, item_offset + 4, format_id)
        payload[item_offset + 8:item_offset + 20] = bytes([0xA0 + index]) * 12

    marker_offset = first_item_offset + 16 * 20
    payload[marker_offset:marker_offset + len(marker)] = marker
    return bytes(payload)


def _unit_entry(
    file_id: int,
    *,
    version: int = LEGACY_UNIT_VERSION,
    bones_ref: int = 0,
    state_machine_ref: int = 0,
    composite_ref: int = 0,
    marker: bytes = b"patch-unit-data",
    format_ids: tuple[int, ...] = (0x1A, 0x10, 0x1D),
) -> TocEntry:
    entry = TocEntry()
    entry.file_id = file_id
    entry.type_id = UnitID
    entry.toc_data = _build_unit_payload(
        version=version,
        bones_ref=bones_ref,
        state_machine_ref=state_machine_ref,
        composite_ref=composite_ref,
        marker=marker,
        format_ids=format_ids,
    )
    entry.gpu_data = b"custom-gpu-payload"
    entry.stream_data = b"custom-stream-payload"
    return entry


def _build_static_unit_payload(
    *,
    version: int,
    lod_data: bytes,
    transform_marker: bytes,
    format_ids: tuple[int, ...] = (0x1A, 0x10, 0x1D),
) -> bytes:
    """Build a zero-reference Unit whose LOD size can change independently."""
    lod_start = 0x80
    transform_start = lod_start + len(lod_data)
    stream_start = transform_start + len(transform_marker)
    layout_record_start = stream_start + 16
    ending = layout_record_start + 16 * 20 + 24
    payload = bytearray(ending + 8)

    struct.pack_into("<Q", payload, 0x28, (version << 32) | 2)
    struct.pack_into("<I", payload, 0x30, lod_start)
    struct.pack_into("<I", payload, 0x34, transform_start)
    struct.pack_into("<I", payload, 0x5C, stream_start)
    struct.pack_into("<I", payload, 0x60, ending)
    payload[lod_start:transform_start] = lod_data
    payload[transform_start:stream_start] = transform_marker

    struct.pack_into("<I", payload, stream_start, 1)
    struct.pack_into("<I", payload, stream_start + 4, 8)
    for index in range(16):
        item_offset = layout_record_start + index * 20
        struct.pack_into("<I", payload, item_offset, 0x70000000 + index)
        format_id = format_ids[index] if index < len(format_ids) else 0
        struct.pack_into("<I", payload, item_offset + 4, format_id)
        payload[item_offset + 8:item_offset + 20] = bytes([0xC0 + index]) * 12
    payload[layout_record_start + 16 * 20:ending] = b"custom-static-tail-data"[: ending - (layout_record_start + 16 * 20)]
    return bytes(payload)


def _static_unit_entry(file_id: int, *, version: int, lod_data: bytes, transform_marker: bytes):
    entry = TocEntry()
    entry.file_id = file_id
    entry.type_id = UnitID
    entry.toc_data = _build_static_unit_payload(
        version=version,
        lod_data=lod_data,
        transform_marker=transform_marker,
    )
    entry.gpu_data = b"custom-static-gpu"
    entry.stream_data = b"custom-static-stream"
    return entry


def _archive(*entries: TocEntry) -> StreamToc:
    archive = StreamToc()
    for entry in entries:
        archive.add_entry(entry)
    return archive


class WeaponIdSwapTests(unittest.TestCase):
    def test_auto_mapping_accepts_one_unique_exact_ref_signature(self):
        source = _unit_entry(
            SOURCE_UNIT_ID,
            version=CURRENT_UNIT_VERSION,
            bones_ref=BONES_ID,
            state_machine_ref=STATE_MACHINE_ID,
            composite_ref=COMPOSITE_ID,
        )
        target = _unit_entry(
            TARGET_UNIT_ID,
            bones_ref=BONES_ID,
            state_machine_ref=STATE_MACHINE_ID,
            composite_ref=COMPOSITE_ID,
        )
        # A copied source Unit can be present in the mod too.  It is not a
        # swap mapping and must not become a target->itself UI row.
        same_id_source_copy = _unit_entry(
            SOURCE_UNIT_ID,
            bones_ref=BONES_ID,
            state_machine_ref=STATE_MACHINE_ID,
            composite_ref=COMPOSITE_ID,
        )
        zero_ref_scaffold = _unit_entry(0x2222)
        near_match = _unit_entry(
            0x3333,
            bones_ref=BONES_ID,
            state_machine_ref=STATE_MACHINE_ID + 1,
            composite_ref=COMPOSITE_ID,
        )

        mappings = infer_weapon_idswap_mappings(
            _archive(target, same_id_source_copy, zero_ref_scaffold, near_match),
            _archive(source),
        )

        self.assertEqual(len(mappings), 1)
        mapping = mappings[0]
        self.assertEqual(mapping.target_unit_id, TARGET_UNIT_ID)
        self.assertEqual(mapping.source_unit_id, SOURCE_UNIT_ID)
        self.assertEqual(mapping.source_entry.file_id, SOURCE_UNIT_ID)
        # The UI needs these provenance fields, but their exact text/path is
        # intentionally an implementation detail for in-memory test archives.
        self.assertTrue(hasattr(mapping, "source_name"))
        self.assertTrue(hasattr(mapping, "source_archive_path"))

    def test_auto_mapping_rejects_partial_and_ambiguous_ref_signatures(self):
        target = _unit_entry(
            TARGET_UNIT_ID,
            bones_ref=BONES_ID,
            state_machine_ref=STATE_MACHINE_ID,
        )
        partial_source = _unit_entry(
            SOURCE_UNIT_ID,
            version=CURRENT_UNIT_VERSION,
            bones_ref=BONES_ID,
            state_machine_ref=0,
        )
        self.assertFalse(
            infer_weapon_idswap_mappings(_archive(target), _archive(partial_source))
        )

        first_source = _unit_entry(
            SOURCE_UNIT_ID,
            version=CURRENT_UNIT_VERSION,
            bones_ref=BONES_ID,
            state_machine_ref=STATE_MACHINE_ID,
        )
        second_source = _unit_entry(
            SOURCE_UNIT_ID + 1,
            version=CURRENT_UNIT_VERSION,
            bones_ref=BONES_ID,
            state_machine_ref=STATE_MACHINE_ID,
        )
        self.assertFalse(
            infer_weapon_idswap_mappings(_archive(target), _archive(first_source, second_source))
        )

    def test_surgical_migration_changes_only_version_and_legacy_format_ids(self):
        source = _unit_entry(
            SOURCE_UNIT_ID,
            version=CURRENT_UNIT_VERSION,
            bones_ref=BONES_ID,
            state_machine_ref=STATE_MACHINE_ID,
            marker=b"source-lod-and-animation-layout-must-not-be-copied",
            format_ids=(0x99, 0x98, 0x97),
        )
        target = _unit_entry(
            TARGET_UNIT_ID,
            version=LEGACY_UNIT_VERSION,
            bones_ref=BONES_ID,
            state_machine_ref=STATE_MACHINE_ID,
            marker=b"target-custom-lod-and-animation-layout-must-survive",
            format_ids=(0x1A, 0x10, 0x1D),
        )
        original_toc = bytes(target.toc_data)

        migrated = migrate_weapon_idswap_unit(target, source)

        expected_toc = bytearray(original_toc)
        struct.pack_into("<I", expected_toc, 0x2C, CURRENT_UNIT_VERSION)
        first_item_offset = 0x80 + 8 + 8
        struct.pack_into("<I", expected_toc, first_item_offset + 4, 0x1E)
        struct.pack_into("<I", expected_toc, first_item_offset + 20 + 4, 0x10)
        struct.pack_into("<I", expected_toc, first_item_offset + 40 + 4, 0x21)

        self.assertIsNot(migrated, target)
        self.assertEqual(migrated.file_id, TARGET_UNIT_ID)
        self.assertEqual(migrated.type_id, UnitID)
        self.assertEqual(migrated.toc_data, bytes(expected_toc))
        self.assertEqual(migrated.gpu_data, target.gpu_data)
        self.assertEqual(migrated.stream_data, target.stream_data)
        self.assertEqual(_u32(migrated.toc_data, 0x2C), CURRENT_UNIT_VERSION)
        self.assertEqual(_u32(migrated.toc_data, first_item_offset + 4), 0x1E)
        self.assertEqual(_u32(migrated.toc_data, first_item_offset + 20 + 4), 0x10)
        self.assertEqual(_u32(migrated.toc_data, first_item_offset + 40 + 4), 0x21)
        self.assertIn(b"target-custom-lod-and-animation-layout-must-survive", migrated.toc_data)
        self.assertNotIn(b"source-lod-and-animation-layout-must-not-be-copied", migrated.toc_data)

    def test_current_unit_migration_is_a_byte_preserving_clone(self):
        source = _unit_entry(SOURCE_UNIT_ID, version=CURRENT_UNIT_VERSION)
        target = _unit_entry(
            TARGET_UNIT_ID,
            version=CURRENT_UNIT_VERSION,
            marker=b"already-current-custom-animation",
        )

        migrated = migrate_weapon_idswap_unit(target, source)

        self.assertIsNot(migrated, target)
        self.assertEqual(migrated.toc_data, target.toc_data)
        self.assertEqual(migrated.gpu_data, target.gpu_data)
        self.assertEqual(migrated.stream_data, target.stream_data)

    def test_unknown_unit_schema_transition_is_refused(self):
        source = _unit_entry(SOURCE_UNIT_ID, version=CURRENT_UNIT_VERSION + 1)
        target = _unit_entry(TARGET_UNIT_ID, version=CURRENT_UNIT_VERSION)

        with self.assertRaisesRegex(ValueError, "schema update is unknown"):
            migrate_weapon_idswap_unit(target, source)

    def test_static_armor_or_helmet_migration_uses_current_target_lod_only(self):
        target_id = 0xABCD
        patch = _static_unit_entry(
            target_id,
            version=LEGACY_UNIT_VERSION,
            lod_data=b"PATCH-LOD-KEEP-NO-BYTES",
            transform_marker=b"PATCH-CUSTOM-TRANSFORM",
        )
        current_target = _static_unit_entry(
            target_id,
            version=CURRENT_UNIT_VERSION,
            lod_data=b"CURRENT-TARGET-LOD-IS-LONGER",
            transform_marker=b"CURRENT-TRANSFORM-MUST-NOT-BE-COPIED",
        )
        original = bytes(patch.toc_data)

        migrated = migrate_static_unit_idswap_unit(patch, current_target)

        self.assertTrue(is_static_unit(patch))
        self.assertEqual(migrated.file_id, target_id)
        self.assertEqual(_u32(migrated.toc_data, 0x2C), CURRENT_UNIT_VERSION)
        self.assertIn(b"CURRENT-TARGET-LOD-IS-LONGER", migrated.toc_data)
        self.assertNotIn(b"PATCH-LOD-KEEP-NO-BYTES", migrated.toc_data)
        self.assertIn(b"PATCH-CUSTOM-TRANSFORM", migrated.toc_data)
        self.assertNotIn(b"CURRENT-TRANSFORM-MUST-NOT-BE-COPIED", migrated.toc_data)
        self.assertEqual(migrated.gpu_data, patch.gpu_data)
        self.assertEqual(migrated.stream_data, patch.stream_data)

        # The LOD grew, so offsets after it move by exactly the same amount.
        size_delta = len(b"CURRENT-TARGET-LOD-IS-LONGER") - len(b"PATCH-LOD-KEEP-NO-BYTES")
        self.assertEqual(_u32(migrated.toc_data, 0x34), _u32(original, 0x34) + size_delta)
        self.assertEqual(_u32(migrated.toc_data, 0x5C), _u32(original, 0x5C) + size_delta)
        self.assertEqual(_u32(migrated.toc_data, 0x60), _u32(original, 0x60) + size_delta)

        # Legacy vertex-format IDs move by +4, but no other patch sections
        # are reconstructed or sourced from the target Unit.
        first_component_format = _u32(migrated.toc_data, _u32(migrated.toc_data, 0x5C) + 16 + 4)
        self.assertEqual(first_component_format, 0x1E)

    def test_current_game_dangling_reference_is_not_a_patch_validation_error(self):
        """A current Unit can carry a non-resource self StateMachine ref."""
        target_id = 0x503C0E01FBAFA86A
        patch_unit = _unit_entry(
            target_id,
            state_machine_ref=target_id,
        )
        current_game_unit = _unit_entry(
            target_id,
            version=CURRENT_UNIT_VERSION,
            state_machine_ref=target_id,
        )

        unresolved = validate_unit_dependencies(
            _archive(patch_unit),
            default_archive=_archive(current_game_unit),
        )

        self.assertEqual(unresolved, [])


if __name__ == "__main__":
    unittest.main()
