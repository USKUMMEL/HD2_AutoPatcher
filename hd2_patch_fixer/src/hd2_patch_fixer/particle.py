"""Safe particle-effect payload migration.

Helldivers 2 particle effects are stored as opaque TOC payloads.  The
community particle updater migrates the supported legacy versions to 0x73,
but its object serializer necessarily rewrites sections it does not
understand.  This module ports only the required binary edits and retains all
other bytes exactly as they appeared in the mod payload.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


CURRENT_PARTICLE_EFFECT_VERSION = 0x73
VALID_PARTICLE_EFFECT_VERSIONS = frozenset((0x73, 0x72, 0x71, 0x6F, 0x6E, 0x6D))

_LEGACY_HEADER_SIZE = 72
_CURRENT_HEADER_SIZE = 80
_PARTICLE_SYSTEM_HEADER_SIZE = 260
_NON_RENDERING_OFFSET = 76
_EMITTER_OFFSET_FIELD = 240
_VISUALIZER_OFFSET_FIELD = 252
_SYSTEM_SIZE_FIELD = 256
_VISUALIZER_COMPONENT_SIZE = 20
_LEGACY_EMITTER_MARKER = b"\x08\x00\x00\x00\x00\x00\x00\x00"

# visualizer type: (bytes between the type field and data, data byte count)
_VISUALIZER_LAYOUTS = {
    0: (16, 240),  # billboard
    1: (0, 256),   # light
    2: (24, 224),  # mesh
    3: (16, 232),
    4: (12, 244),
}


class ParticleMigrationError(ValueError):
    """Raised when a payload is not a supported, safely parseable particle effect."""


@dataclass(frozen=True)
class ParticleSystemLayout:
    """The stable offsets needed for the legacy community migration."""

    offset: int
    non_rendering: int
    emitter_offset: int
    visualizer_offset: int
    size: int


def _require_range(data: bytes | bytearray, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ParticleMigrationError(
            f"Particle payload is truncated or has an invalid {label} range "
            f"(offset={offset}, size={size}, payload={len(data)})."
        )


def _read_u32(data: bytes | bytearray, offset: int, label: str) -> int:
    _require_range(data, offset, 4, label)
    return struct.unpack_from("<I", data, offset)[0]


def _write_u32(data: bytearray, offset: int, value: int, label: str) -> None:
    _require_range(data, offset, 4, label)
    if not 0 <= value <= 0xFFFFFFFF:
        raise ParticleMigrationError(f"Particle {label} value is outside uint32 range: {value}.")
    struct.pack_into("<I", data, offset, value)


def _read_particle_system_layouts(data: bytes | bytearray, header_size: int) -> list[ParticleSystemLayout]:
    _require_range(data, 0, 28, "particle header")
    num_variables = _read_u32(data, 20, "variable count")
    num_systems = _read_u32(data, 24, "system count")

    variable_data_size = num_variables * 16
    _require_range(data, header_size, variable_data_size, "particle variable table")
    offset = header_size + variable_data_size
    systems = []

    for index in range(num_systems):
        _require_range(data, offset, _PARTICLE_SYSTEM_HEADER_SIZE, f"particle system {index}")
        non_rendering = _read_u32(data, offset + _NON_RENDERING_OFFSET, f"particle system {index} render flag")
        emitter_offset = _read_u32(data, offset + _EMITTER_OFFSET_FIELD, f"particle system {index} emitter offset")
        visualizer_offset = _read_u32(
            data,
            offset + _VISUALIZER_OFFSET_FIELD,
            f"particle system {index} visualizer offset",
        )
        size = _read_u32(data, offset + _SYSTEM_SIZE_FIELD, f"particle system {index} size")

        if size < _PARTICLE_SYSTEM_HEADER_SIZE:
            raise ParticleMigrationError(
                f"Particle system {index} has an invalid size ({size}); expected at least "
                f"{_PARTICLE_SYSTEM_HEADER_SIZE}."
            )
        _require_range(data, offset, size, f"particle system {index}")
        if emitter_offset > size or visualizer_offset > size:
            raise ParticleMigrationError(
                f"Particle system {index} has offsets outside its size "
                f"(emitter={emitter_offset}, visualizer={visualizer_offset}, size={size})."
            )

        systems.append(
            ParticleSystemLayout(
                offset=offset,
                non_rendering=non_rendering,
                emitter_offset=emitter_offset,
                visualizer_offset=visualizer_offset,
                size=size,
            )
        )
        offset += size

    return systems


def _upgrade_visualizer_component_formats(
    data: bytearray,
    systems: list[ParticleSystemLayout],
) -> None:
    """Apply the 0x73 visualizer component-format renumbering in place."""

    for system_index, system in enumerate(systems):
        if system.non_rendering != 0 or system.visualizer_offset == system.size:
            continue
        if system.visualizer_offset < _PARTICLE_SYSTEM_HEADER_SIZE:
            raise ParticleMigrationError(
                f"Particle system {system_index} visualizer starts inside its header."
            )

        visualizer_start = system.offset + system.visualizer_offset
        _require_range(data, visualizer_start, 4, f"particle system {system_index} visualizer")
        visualizer_type = _read_u32(
            data,
            visualizer_start,
            f"particle system {system_index} visualizer type",
        )
        layout = _VISUALIZER_LAYOUTS.get(visualizer_type)
        if layout is None:
            raise ParticleMigrationError(
                f"Particle system {system_index} uses unsupported visualizer type {visualizer_type}."
            )

        prefix_size, data_size = layout
        component_data_start = visualizer_start + 4 + prefix_size
        component_data_end = component_data_start + data_size
        system_end = system.offset + system.size
        if component_data_end > system_end:
            raise ParticleMigrationError(
                f"Particle system {system_index} visualizer extends past its declared size."
            )

        component_count = _read_u32(
            data,
            component_data_start,
            f"particle system {system_index} visualizer component count",
        )
        components_start = component_data_start + 4
        available_component_bytes = component_data_end - components_start
        if component_count > available_component_bytes // _VISUALIZER_COMPONENT_SIZE:
            raise ParticleMigrationError(
                f"Particle system {system_index} has an invalid visualizer component count "
                f"({component_count})."
            )

        for component_index in range(component_count):
            component_offset = components_start + component_index * _VISUALIZER_COMPONENT_SIZE
            format_offset = component_offset + 4
            component_format = _read_u32(
                data,
                format_offset,
                f"particle system {system_index} component {component_index} format",
            )
            if component_format > 16:
                _write_u32(
                    data,
                    format_offset,
                    component_format + 4,
                    f"particle system {system_index} component {component_index} format",
                )


def _upgrade_legacy_emitter_layout(data: bytearray, systems: list[ParticleSystemLayout]) -> None:
    """Port the pre-0x71 emitter insertion performed by the community updater."""

    accumulated_shift = 0
    for system_index, original_system in enumerate(systems):
        if original_system.non_rendering != 0:
            continue

        system_start = original_system.offset + accumulated_shift
        emitter_offset = _read_u32(
            data,
            system_start + _EMITTER_OFFSET_FIELD,
            f"legacy particle system {system_index} emitter offset",
        )
        visualizer_offset = _read_u32(
            data,
            system_start + _VISUALIZER_OFFSET_FIELD,
            f"legacy particle system {system_index} visualizer offset",
        )
        system_size = _read_u32(
            data,
            system_start + _SYSTEM_SIZE_FIELD,
            f"legacy particle system {system_index} size",
        )
        if emitter_offset < 16 or emitter_offset + 8 > system_size:
            raise ParticleMigrationError(
                f"Legacy particle system {system_index} has an invalid emitter offset "
                f"({emitter_offset}) for size {system_size}."
            )
        if visualizer_offset > system_size:
            raise ParticleMigrationError(
                f"Legacy particle system {system_index} visualizer offset is outside its size."
            )

        marker_value = _read_u32(
            data,
            system_start + emitter_offset - 16,
            f"legacy particle system {system_index} emitter marker",
        )
        insertion_offset = system_start + emitter_offset + 8
        _require_range(data, insertion_offset, 0, f"legacy particle system {system_index} emitter insertion")
        data[insertion_offset:insertion_offset] = b"\xFF\xFF\xFF\xFF"

        if marker_value == 8:
            _write_u32(
                data,
                system_start + _VISUALIZER_OFFSET_FIELD,
                visualizer_offset + 16,
                f"legacy particle system {system_index} visualizer offset",
            )
            _write_u32(
                data,
                system_start + _SYSTEM_SIZE_FIELD,
                system_size + 16,
                f"legacy particle system {system_index} size",
            )

            # The community updater deliberately searches using the pre-update
            # visualizer offset after inserting the four-byte emitter marker.
            # Keeping that exact range preserves its two legacy upgrade paths.
            marker_start = system_start + emitter_offset
            marker_end = system_start + visualizer_offset
            replacement_offset = data.find(_LEGACY_EMITTER_MARKER, marker_start, marker_end)
            if replacement_offset == -1:
                raise ParticleMigrationError(
                    f"Legacy particle system {system_index} is missing the expected emitter marker."
                )
            replacement_value = _read_u32(
                data,
                replacement_offset + 8,
                f"legacy particle system {system_index} emitter replacement value",
            )
            if replacement_value > 0xFFFFFFFF - 0x18:
                raise ParticleMigrationError(
                    f"Legacy particle system {system_index} emitter replacement value overflows uint32."
                )
            additions = struct.pack(
                "<III",
                replacement_value + 0x10,
                replacement_value + 0x14,
                replacement_value + 0x18,
            )
            data[replacement_offset + 8:replacement_offset + 8] = additions
            accumulated_shift += 16
        else:
            _write_u32(
                data,
                system_start + _VISUALIZER_OFFSET_FIELD,
                visualizer_offset + 4,
                f"legacy particle system {system_index} visualizer offset",
            )
            _write_u32(
                data,
                system_start + _SYSTEM_SIZE_FIELD,
                system_size + 4,
                f"legacy particle system {system_index} size",
            )
            accumulated_shift += 4


def migrate_particle_effect(payload: bytes | bytearray) -> tuple[bytes, str]:
    """Migrate a supported particle effect payload to version ``0x73``.

    The returned payload preserves untouched bytes, including unknown component
    data.  Invalid or unrecognised payloads raise :class:`ParticleMigrationError`
    so the archive layer can use its existing raw-payload fallback rather than
    writing a partly converted patch.
    """

    raw_payload = bytes(payload)
    version = _read_u32(raw_payload, 0, "particle version")
    if version not in VALID_PARTICLE_EFFECT_VERSIONS:
        raise ParticleMigrationError(
            f"Unsupported particle effect version 0x{version:02X}; preserving the raw payload is safer."
        )
    if version == CURRENT_PARTICLE_EFFECT_VERSION:
        return raw_payload, "particle-current"

    data = bytearray(raw_payload)
    if version < 0x6F:
        _require_range(data, 0, _LEGACY_HEADER_SIZE, "legacy particle header")
        data[_LEGACY_HEADER_SIZE:_LEGACY_HEADER_SIZE] = b"\x00" * 8

    _write_u32(data, 0, CURRENT_PARTICLE_EFFECT_VERSION, "particle version")
    systems = _read_particle_system_layouts(data, _CURRENT_HEADER_SIZE)
    _upgrade_visualizer_component_formats(data, systems)
    if version < 0x71:
        _upgrade_legacy_emitter_layout(data, systems)

    return bytes(data), f"particle-migrated-0x{version:02X}-to-0x{CURRENT_PARTICLE_EFFECT_VERSION:02X}"
