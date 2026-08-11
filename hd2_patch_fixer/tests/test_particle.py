import struct
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from hd2_patch_fixer.archive import StreamToc, TocEntry, build_entry_from_source, rebuild_entry_payload
from hd2_patch_fixer.constants import ParticleID
from hd2_patch_fixer.particle import ParticleMigrationError, migrate_particle_effect


def _u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def _build_particle_payload(version, legacy_marker=None):
    """Build the smallest well-formed synthetic payload for migration tests."""

    header_size = 80 if version >= 0x6F else 72
    variable_count = 1
    system_size = 660
    emitter_offset = 260
    visualizer_offset = 400
    system_offset = header_size + variable_count * 16
    tail = b"unparsed-particle-tail"
    payload = bytearray(system_offset + system_size)

    struct.pack_into("<I", payload, 0, version)
    struct.pack_into("<ff", payload, 4, 1.25, 2.5)
    struct.pack_into("<II", payload, 20, variable_count, 1)
    struct.pack_into("<I", payload, header_size, 0x12345678)
    struct.pack_into("<fff", payload, header_size + 4, 3.0, 4.0, 5.0)

    struct.pack_into("<I", payload, system_offset + 76, 0)
    # The community reader constructs a scipy Rotation from these matrix rows.
    # Use a real identity transform so its reference migration can parse this
    # synthetic payload as well.
    struct.pack_into("<fff", payload, system_offset + 120, 1.0, 0.0, 0.0)
    struct.pack_into("<fff", payload, system_offset + 136, 0.0, 1.0, 0.0)
    struct.pack_into("<fff", payload, system_offset + 152, 0.0, 0.0, 1.0)
    struct.pack_into("<I", payload, system_offset + 240, emitter_offset)
    struct.pack_into("<I", payload, system_offset + 252, visualizer_offset)
    struct.pack_into("<I", payload, system_offset + 256, system_size)

    if legacy_marker is not None:
        struct.pack_into("<I", payload, system_offset + emitter_offset - 16, legacy_marker)
        if legacy_marker == 8:
            marker_offset = system_offset + emitter_offset + 16
            payload[marker_offset:marker_offset + 8] = b"\x08\x00\x00\x00\x00\x00\x00\x00"
            struct.pack_into("<I", payload, marker_offset + 8, 0x60)

    visualizer_start = system_offset + visualizer_offset
    struct.pack_into("<I", payload, visualizer_start, 0)  # billboard
    struct.pack_into("<I", payload, visualizer_start + 20, 1)
    struct.pack_into("<II", payload, visualizer_start + 24, 7, 17)
    return bytes(payload) + tail


class ParticleMigrationTests(unittest.TestCase):
    def test_current_payload_is_returned_byte_for_byte(self):
        source = _build_particle_payload(0x73)

        migrated, mode = migrate_particle_effect(source)

        self.assertEqual(mode, "particle-current")
        self.assertEqual(migrated, source)

    def test_072_updates_version_and_visualizer_component_format(self):
        source = _build_particle_payload(0x72)

        migrated, mode = migrate_particle_effect(source)

        system_offset = 80 + 16
        self.assertEqual(mode, "particle-migrated-0x72-to-0x73")
        self.assertEqual(len(migrated), len(source))
        self.assertEqual(_u32(migrated, 0), 0x73)
        self.assertEqual(_u32(migrated, system_offset + 400 + 28), 21)
        self.assertTrue(migrated.endswith(b"unparsed-particle-tail"))

    def test_06e_inserts_current_header_and_legacy_emitter_marker(self):
        source = _build_particle_payload(0x6E, legacy_marker=0)

        migrated, mode = migrate_particle_effect(source)

        system_offset = 80 + 16
        self.assertEqual(mode, "particle-migrated-0x6E-to-0x73")
        self.assertEqual(len(migrated), len(source) + 12)
        self.assertEqual(_u32(migrated, 0), 0x73)
        self.assertEqual(_u32(migrated, system_offset + 252), 404)
        self.assertEqual(_u32(migrated, system_offset + 256), 664)
        self.assertEqual(_u32(migrated, system_offset + 404 + 28), 21)
        self.assertTrue(migrated.endswith(b"unparsed-particle-tail"))

    def test_06d_ports_the_community_updater_long_legacy_path(self):
        source = _build_particle_payload(0x6D, legacy_marker=8)

        migrated, mode = migrate_particle_effect(source)

        system_offset = 80 + 16
        marker_offset = system_offset + 260 + 20
        self.assertEqual(mode, "particle-migrated-0x6D-to-0x73")
        self.assertEqual(len(migrated), len(source) + 24)
        self.assertEqual(_u32(migrated, system_offset + 252), 416)
        self.assertEqual(_u32(migrated, system_offset + 256), 676)
        self.assertEqual(_u32(migrated, marker_offset + 8), 0x70)
        self.assertEqual(_u32(migrated, marker_offset + 12), 0x74)
        self.assertEqual(_u32(migrated, marker_offset + 16), 0x78)
        self.assertEqual(_u32(migrated, system_offset + 416 + 28), 21)

    def test_unknown_version_is_refused_without_serializing(self):
        with self.assertRaises(ParticleMigrationError):
            migrate_particle_effect(struct.pack("<I", 0x99) + b"payload")

    def test_archive_rebuild_uses_migrator_and_preserves_sidecar_payloads(self):
        entry = TocEntry()
        entry.type_id = ParticleID
        entry.toc_data = _build_particle_payload(0x72)
        entry.gpu_data = b"gpu-bytes"
        entry.stream_data = b"stream-bytes"

        toc_data, gpu_data, stream_data, mode = rebuild_entry_payload(entry)

        self.assertEqual(mode, "particle-migrated-0x72-to-0x73")
        self.assertEqual(_u32(toc_data, 0), 0x73)
        self.assertEqual(gpu_data, b"gpu-bytes")
        self.assertEqual(stream_data, b"stream-bytes")

    def test_normal_archive_pipeline_selects_particle_migration(self):
        entry = TocEntry()
        entry.type_id = ParticleID
        entry.toc_data = _build_particle_payload(0x71)

        rebuilt, mode = build_entry_from_source(
            entry,
            raw_fallback_for_unsupported=True,
            default_archive=StreamToc(),
        )

        self.assertEqual(mode, "particle-migrated-0x71-to-0x73")
        self.assertEqual(_u32(rebuilt.toc_data, 0), 0x73)


if __name__ == "__main__":
    unittest.main()
