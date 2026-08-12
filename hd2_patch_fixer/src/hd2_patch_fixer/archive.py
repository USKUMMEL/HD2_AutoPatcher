import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from math import ceil
from pathlib import Path
import zipfile

from .audio import AUDIO_TYPE_IDS, inspect_audio_collection, inspect_audio_entry
from .community_audio_adapter import (
    CommunityAudioAdapterError,
    migrate_audio_patch_with_community_engine,
)
from .constants import (
    BaseArchiveHexID,
    BoneID,
    CompositeUnitID,
    MaterialID,
    ParticleID,
    StateMachineID,
    TexID,
    UnitID,
    TYPE_NAME_MAP,
    WwiseBankID,
    WwiseMetaDataID,
)
from .memory_stream import MemoryStream
from .particle import migrate_particle_effect
from .parsers import (
    StingrayBones,
    StingrayMaterial,
    StingrayStateMachine,
    StingrayTexture,
    StingrayUnit,
)
from .slim import get_package_toc, is_slim_version, load_package, slim_init


#region Module Helpers And Constants
def log_message(log, message):
    if log is not None:
        log(message)


SUPPORTED_REBUILD_TYPES = {
    TexID,
    MaterialID,
    BoneID,
    ParticleID,
    StateMachineID,
}

OLD_UNIT_VERSION = 10800437

VERTEX_FORMAT_NAME_BY_VERSION = {
    "new": {
        0: "float",
        1: "vec2_float",
        2: "vec3_float",
        3: "vec4_float",
        4: "rgba_r8g8b8a8",
        21: "uint32",
        22: "vec2_uint32",
        23: "vec3_uint32",
        24: "vec4_uint32",
        25: "int8",
        26: "vec2_int8",
        27: "vec3_int8",
        28: "vec4_int8",
        29: "vec4_1010102",
        30: "unk_normal",
        32: "float16",
        33: "vec2_half",
        34: "vec3_half",
        35: "vec4_half",
    },
    "old": {
        0: "float",
        1: "vec2_float",
        2: "vec3_float",
        3: "vec4_float",
        4: "rgba_r8g8b8a8",
        17: "uint32",
        18: "vec2_uint32",
        19: "vec3_uint32",
        20: "vec4_uint32",
        21: "int8",
        22: "vec2_int8",
        23: "vec3_int8",
        24: "vec4_int8",
        25: "vec4_1010102",
        26: "unk_normal",
        28: "float16",
        29: "vec2_half",
        30: "vec3_half",
        31: "vec4_half",
    },
}

VERTEX_FORMAT_ID_BY_VERSION = {
    version: {name: format_id for format_id, name in format_map.items()}
    for version, format_map in VERTEX_FORMAT_NAME_BY_VERSION.items()
}

VERTEX_FORMAT_SIZE_BY_NAME = {
    "float": 4,
    "vec2_float": 8,
    "vec3_float": 12,
    "vec4_float": 16,
    "rgba_r8g8b8a8": 4,
    "uint32": 4,
    "vec2_uint32": 8,
    "vec3_uint32": 12,
    "vec4_uint32": 16,
    "int8": 1,
    "vec2_int8": 2,
    "vec3_int8": 3,
    "vec4_int8": 4,
    "vec4_1010102": 4,
    "unk_normal": 4,
    "float16": 2,
    "vec2_half": 4,
    "vec3_half": 6,
    "vec4_half": 8,
}


def is_old_unit_version(unit_version: int):
    return 0 < unit_version <= OLD_UNIT_VERSION


def vertex_format_version_key(unit_version: int):
    return "old" if is_old_unit_version(unit_version) else "new"


def get_vertex_format_name(format_id: int, unit_version: int):
    preferred_version = vertex_format_version_key(unit_version)
    fallback_version = "new" if preferred_version == "old" else "old"
    format_name = VERTEX_FORMAT_NAME_BY_VERSION[preferred_version].get(format_id)
    if format_name is None:
        format_name = VERTEX_FORMAT_NAME_BY_VERSION[fallback_version].get(format_id)
    if format_name is None:
        raise ValueError(
            f"Unsupported Unit vertex format {format_id} for unit version {unit_version or 'unknown'}"
        )
    return format_name


def get_vertex_format_id(format_name: str, unit_version: int):
    format_id = VERTEX_FORMAT_ID_BY_VERSION[vertex_format_version_key(unit_version)].get(format_name)
    if format_id is None:
        raise ValueError(
            f"Unsupported Unit vertex format name {format_name} for unit version {unit_version or 'unknown'}"
        )
    return format_id

IDSWAP_PATCH_SECTION_OVERRIDES = (
    "bone_info",
    "stream_info",
    "mesh_info",
    "materials",
    "customization_info",
    "connecting_bone_hash",
)

# A source archive is an optional hint for true ID-swap geometry.  Weapon
# patches also contain target-only scaffolding Units, so a weak resemblance is
# not enough to safely borrow its LOD group from the source model.
IDSWAP_SOURCE_MATCH_MIN_SCORE = 60

# ``10800438`` is the Unit layout used by the current game build.  The
# community updater's +4 vertex-format migration applies when crossing this
# boundary; it is intentionally a byte-level schema migration, not a model
# conversion.
UNIT_IDSWAP_CURRENT_UNIT_VERSION = 0xA4CD36

# Backwards-compatible internal alias.  The migration is now used for both
# rigged weapon swaps and static armor/helmet swaps.
WEAPON_IDSWAP_CURRENT_UNIT_VERSION = UNIT_IDSWAP_CURRENT_UNIT_VERSION

# Unit header offsets which point to data after the LOD group.  Static
# armor/helmet migration replaces the raw LOD group from the current target
# Unit; when its size changes, only these real offsets are shifted.  Do not
# update the neighbouring header-data fields just because they happen to look
# like integers (the community script does that, but it can corrupt metadata).
UNIT_SECTION_OFFSET_POSITIONS = (
    0x34,  # transform_info
    0x38,  # light_list
    0x3C,  # pre_light_list
    0x40,  # wwise_callback
    0x4C,  # customization_info
    0x50,  # unk_header_1
    0x54,  # connecting_bone_hash
    0x58,  # bone_info
    0x5C,  # stream_info
    0x60,  # ending offset
    0x64,  # mesh_info
    0x70,  # materials
)
#endregion Module Helpers And Constants


#region Binary Data Models
@dataclass
class TocFileType:
    type_id: int = 0
    num_files: int = 0
    unk1: int = 0
    unk2: int = 16
    unk3: int = 64

    def serialize(self, toc_file: MemoryStream):
        self.unk1 = toc_file.uint64(self.unk1)
        self.type_id = toc_file.uint64(self.type_id)
        self.num_files = toc_file.uint64(self.num_files)
        self.unk2 = toc_file.uint32(self.unk2)
        self.unk3 = toc_file.uint32(self.unk3)
        return self


@dataclass(frozen=True)
class UnitVertexComponent:
    RECORD_SIZE = 20
    type_id: int = 0
    format_id: int = 0
    index: int = 0
    unknown: int = 0
    unit_version: int = 0

    def serialize(self, stream: MemoryStream):
        return UnitVertexComponent(
            type_id=stream.uint32(self.type_id),
            format_id=stream.uint32(self.format_id),
            index=stream.uint32(self.index),
            unknown=stream.uint64(self.unknown),
            unit_version=self.unit_version,
        )

    @property
    def key(self):
        return (self.type_id, self.format_id, self.index)

    @property
    def format_name(self):
        return get_vertex_format_name(self.format_id, self.unit_version)

    @property
    def semantic_key(self):
        return (self.type_id, self.format_name, self.index)

    @property
    def size(self):
        return VERTEX_FORMAT_SIZE_BY_NAME[self.format_name]

    def converted_for_version(self, unit_version: int):
        return UnitVertexComponent(
            type_id=self.type_id,
            format_id=get_vertex_format_id(self.format_name, unit_version),
            index=self.index,
            unknown=self.unknown,
            unit_version=unit_version,
        )


@dataclass(frozen=True)
class UnitFingerprint:
    file_id: int
    bones_ref: int
    composite_ref: int
    state_machine_ref: int
    mesh_ids: tuple[int, ...]
    lod_indices: tuple[int, ...]
    material_ids: tuple[int, ...]
    stream_layouts: tuple[tuple[tuple[int, int, int], ...], ...]
    section_sizes: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class UnitSimilarity:
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ProbableIdSwap:
    source_id: int
    source_name: str
    source_score: int
    target_score: int
    source_reasons: tuple[str, ...]
    target_reasons: tuple[str, ...]


@dataclass(frozen=True)
class WeaponIdSwapMapping:
    """One high-confidence source Unit inferred from a weapon patch Unit."""

    target_unit_id: int
    source_unit_id: int
    source_entry: object
    source_name: str
    source_archive_path: str


@dataclass
class UnitStreamInfo:
    components: list[UnitVertexComponent]
    component_info_id: int = 0
    vertex_buffer_id: int = 0
    vertex_buffer_unk1: int = 0
    num_vertices: int = 0
    vertex_stride: int = 0
    vertex_buffer_unk2: int = 0
    vertex_buffer_unk3: int = 0
    index_buffer_id: int = 0
    index_buffer_unk1: int = 0
    num_indices: int = 0
    index_buffer_type: int = 0
    index_buffer_unk2: int = 0
    index_buffer_unk3: int = 0
    vertex_buffer_offset: int = 0
    vertex_buffer_size: int = 0
    index_buffer_offset: int = 0
    index_buffer_size: int = 0
    ending_bytes: bytes = b"\x00" * 16

    @classmethod
    def parse(cls, stream: MemoryStream, unit_version: int = 0):
        component_info_id = stream.uint64(0)
        component_area_offset = stream.tell()
        stream.seek(component_area_offset + 320)
        num_components = stream.uint64(0)
        vertex_buffer_id = stream.uint64(0)
        vertex_buffer_unk1 = stream.uint64(0)
        num_vertices = stream.uint32(0)
        vertex_stride = stream.uint32(0)
        vertex_buffer_unk2 = stream.uint64(0)
        vertex_buffer_unk3 = stream.uint64(0)
        index_buffer_id = stream.uint64(0)
        index_buffer_unk1 = stream.uint64(0)
        num_indices = stream.uint32(0)
        index_buffer_type = stream.uint32(0)
        index_buffer_unk2 = stream.uint64(0)
        index_buffer_unk3 = stream.uint64(0)
        vertex_buffer_offset = stream.uint32(0)
        vertex_buffer_size = stream.uint32(0)
        index_buffer_offset = stream.uint32(0)
        index_buffer_size = stream.uint32(0)
        ending_bytes = bytes(stream.bytes(bytearray(16), 16))

        record_end = ceil(float(stream.tell()) / 16) * 16
        stream.seek(component_area_offset)
        components = []
        for _ in range(num_components):
            component = UnitVertexComponent(unit_version=unit_version).serialize(stream)
            components.append(component)
        stream.seek(record_end)

        return cls(
            components=components,
            component_info_id=component_info_id,
            vertex_buffer_id=vertex_buffer_id,
            vertex_buffer_unk1=vertex_buffer_unk1,
            num_vertices=num_vertices,
            vertex_stride=vertex_stride,
            vertex_buffer_unk2=vertex_buffer_unk2,
            vertex_buffer_unk3=vertex_buffer_unk3,
            index_buffer_id=index_buffer_id,
            index_buffer_unk1=index_buffer_unk1,
            num_indices=num_indices,
            index_buffer_type=index_buffer_type,
            index_buffer_unk2=index_buffer_unk2,
            index_buffer_unk3=index_buffer_unk3,
            vertex_buffer_offset=vertex_buffer_offset,
            vertex_buffer_size=vertex_buffer_size,
            index_buffer_offset=index_buffer_offset,
            index_buffer_size=index_buffer_size,
            ending_bytes=ending_bytes,
        )

    def write(self, stream: MemoryStream):
        stream.uint64(self.component_info_id)
        for component in self.components:
            stream.uint32(component.type_id)
            stream.uint32(component.format_id)
            stream.uint32(component.index)
            stream.uint64(component.unknown)
        component_bytes = len(self.components) * UnitVertexComponent.RECORD_SIZE
        if component_bytes > 320:
            raise ValueError("Unit stream contains too many vertex components")
        stream.write(b"\x00" * (320 - component_bytes))
        stream.uint64(len(self.components))
        stream.uint64(self.vertex_buffer_id)
        stream.uint64(self.vertex_buffer_unk1)
        stream.uint32(self.num_vertices)
        stream.uint32(self.vertex_stride)
        stream.uint64(self.vertex_buffer_unk2)
        stream.uint64(self.vertex_buffer_unk3)
        stream.uint64(self.index_buffer_id)
        stream.uint64(self.index_buffer_unk1)
        stream.uint32(self.num_indices)
        stream.uint32(self.index_buffer_type)
        stream.uint64(self.index_buffer_unk2)
        stream.uint64(self.index_buffer_unk3)
        stream.uint32(self.vertex_buffer_offset)
        stream.uint32(self.vertex_buffer_size)
        stream.uint32(self.index_buffer_offset)
        stream.uint32(self.index_buffer_size)
        stream.write(self.ending_bytes[:16].ljust(16, b"\x00"))
        aligned_end = ceil(float(stream.tell()) / 16) * 16
        stream.seek(aligned_end)


@dataclass
class UnitStreamSection:
    stream_infos: list[UnitStreamInfo]
    stream_unk_ids: list[int]
    stream_unk2: int = 0

    @classmethod
    def parse(cls, data: bytes, unit_version: int = 0):
        stream = MemoryStream(data)
        num_streams = stream.uint32(0)
        offsets = [stream.uint32(0) for _ in range(num_streams)]
        stream_unk_ids = [stream.uint32(0) for _ in range(num_streams)]
        stream_unk2 = stream.uint32(0)
        stream_infos = []
        for offset in offsets:
            stream.seek(offset)
            stream_infos.append(UnitStreamInfo.parse(stream, unit_version=unit_version))
        return cls(stream_infos=stream_infos, stream_unk_ids=stream_unk_ids, stream_unk2=stream_unk2)

    def build(self):
        stream = MemoryStream(io_mode="write")
        stream.uint32(len(self.stream_infos))
        offset_positions = [stream.tell() + (index * 4) for index in range(len(self.stream_infos))]
        for _ in self.stream_infos:
            stream.uint32(0)
        for value in self.stream_unk_ids:
            stream.uint32(value)
        stream.uint32(self.stream_unk2)

        record_offsets = []
        for info in self.stream_infos:
            record_offsets.append(stream.tell())
            info.write(stream)

        end_location = stream.tell()
        for position, offset in zip(offset_positions, record_offsets):
            stream.seek(position)
            stream.uint32(offset)
        stream.seek(end_location)
        return bytes(stream.data)


@dataclass
class UnitMeshSectionInfo:
    material_index: int = 0
    vertex_offset: int = 0
    num_vertices: int = 0
    index_offset: int = 0
    num_indices: int = 0
    group_index: int = 0

    @classmethod
    def parse(cls, stream: MemoryStream):
        return cls(
            material_index=stream.uint32(0),
            vertex_offset=stream.uint32(0),
            num_vertices=stream.uint32(0),
            index_offset=stream.uint32(0),
            num_indices=stream.uint32(0),
            group_index=stream.uint32(0),
        )

    def write(self, stream: MemoryStream):
        stream.uint32(self.material_index)
        stream.uint32(self.vertex_offset)
        stream.uint32(self.num_vertices)
        stream.uint32(self.index_offset)
        stream.uint32(self.num_indices)
        stream.uint32(self.group_index)


@dataclass
class UnitMeshInfo:
    mesh_id: int = 0
    lod_index: int = -1
    stream_index: int = 0
    transform_index: int = 0
    unk1: int = 0
    unk2: bytes = b"\x00" * 32
    unk3: int = 0
    unk4: int = 0
    unk6: bytes = b"\x00" * 40
    unk8: int = 0
    material_ids: list[int] = None
    sections: list[UnitMeshSectionInfo] = None

    def __post_init__(self):
        if self.material_ids is None:
            self.material_ids = []
        if self.sections is None:
            self.sections = []

    @classmethod
    def parse(cls, stream: MemoryStream):
        unk1 = stream.uint64(0)
        unk2 = bytes(stream.bytes(bytearray(32), 32))
        mesh_id = stream.uint32(0)
        unk3 = stream.uint32(0)
        transform_index = stream.uint32(0)
        unk4 = stream.uint32(0)
        lod_index = stream.int32(0)
        stream_index = stream.uint32(0)
        unk6 = bytes(stream.bytes(bytearray(40), 40))
        num_materials = stream.uint32(0)
        material_offset = stream.uint32(0)
        unk8 = stream.uint64(0)
        num_sections = stream.uint32(0)
        sections_offset = stream.uint32(0)
        material_ids = [stream.uint32(0) for _ in range(num_materials)]
        sections = [UnitMeshSectionInfo.parse(stream) for _ in range(num_sections)]
        return cls(
            mesh_id=mesh_id,
            lod_index=lod_index,
            stream_index=stream_index,
            transform_index=transform_index,
            unk1=unk1,
            unk2=unk2,
            unk3=unk3,
            unk4=unk4,
            unk6=unk6,
            unk8=unk8,
            material_ids=material_ids,
            sections=sections,
        )

    def write(self, stream: MemoryStream):
        record_start = stream.tell()
        stream.uint64(self.unk1)
        stream.write(self.unk2[:32].ljust(32, b"\x00"))
        stream.uint32(self.mesh_id)
        stream.uint32(self.unk3)
        stream.uint32(self.transform_index)
        stream.uint32(self.unk4)
        stream.int32(self.lod_index)
        stream.uint32(self.stream_index)
        stream.write(self.unk6[:40].ljust(40, b"\x00"))
        num_materials = len(self.material_ids)
        num_sections = len(self.sections)
        stream.uint32(num_materials)
        material_offset_location = stream.tell()
        stream.uint32(0)
        stream.uint64(self.unk8)
        stream.uint32(num_sections)
        sections_offset_location = stream.tell()
        stream.uint32(0)

        material_offset = stream.tell() - record_start
        for material_id in self.material_ids:
            stream.uint32(material_id)
        sections_offset = stream.tell() - record_start
        for section in self.sections:
            section.write(stream)

        record_end = stream.tell()
        stream.seek(material_offset_location)
        stream.uint32(material_offset)
        stream.seek(sections_offset_location)
        stream.uint32(sections_offset)
        stream.seek(record_end)


@dataclass
class UnitMeshSection:
    mesh_infos: list[UnitMeshInfo]
    mesh_unk_ids: list[int]

    @classmethod
    def parse(cls, data: bytes):
        stream = MemoryStream(data)
        num_meshes = stream.uint32(0)
        offsets = [stream.uint32(0) for _ in range(num_meshes)]
        mesh_unk_ids = [stream.uint32(0) for _ in range(num_meshes)]
        mesh_infos = []
        for offset in offsets:
            stream.seek(offset)
            mesh_infos.append(UnitMeshInfo.parse(stream))
        return cls(mesh_infos=mesh_infos, mesh_unk_ids=mesh_unk_ids)

    def build(self):
        stream = MemoryStream(io_mode="write")
        stream.uint32(len(self.mesh_infos))
        offset_positions = [stream.tell() + (index * 4) for index in range(len(self.mesh_infos))]
        for _ in self.mesh_infos:
            stream.uint32(0)
        for value in self.mesh_unk_ids:
            stream.uint32(value)

        record_offsets = []
        for info in self.mesh_infos:
            record_offsets.append(stream.tell())
            info.write(stream)

        end_location = stream.tell()
        for position, offset in zip(offset_positions, record_offsets):
            stream.seek(position)
            stream.uint32(offset)
        stream.seek(end_location)
        return bytes(stream.data)
#endregion Binary Data Models


#region Archive Containers And Indexing
class TocEntry:
    def __init__(self):
        self.file_id = 0
        self.type_id = 0
        self.toc_data_offset = 0
        self.unknown1 = 0
        self.gpu_resource_offset = 0
        self.unknown2 = 0
        self.toc_data_size = 0
        self.gpu_resource_size = 0
        self.entry_index = 0
        self.stream_size = 0
        self.stream_offset = 0
        self.unknown3 = 16
        self.unknown4 = 64
        self.toc_data = b""
        self.gpu_data = b""
        self.stream_data = b""

    def serialize(self, toc_file: MemoryStream, index=0):
        self.file_id = toc_file.uint64(self.file_id)
        self.type_id = toc_file.uint64(self.type_id)
        self.toc_data_offset = toc_file.uint64(self.toc_data_offset)
        self.stream_offset = toc_file.uint64(self.stream_offset)
        self.gpu_resource_offset = toc_file.uint64(self.gpu_resource_offset)
        self.unknown1 = toc_file.uint64(self.unknown1)
        self.unknown2 = toc_file.uint64(self.unknown2)
        self.toc_data_size = toc_file.uint32(len(self.toc_data))
        self.stream_size = toc_file.uint32(len(self.stream_data))
        self.gpu_resource_size = toc_file.uint32(len(self.gpu_data))
        self.unknown3 = toc_file.uint32(self.unknown3)
        self.unknown4 = toc_file.uint32(self.unknown4)
        self.entry_index = toc_file.uint32(index)
        return self

    def serialize_data(self, toc_file: MemoryStream, gpu_file: MemoryStream, stream_file: MemoryStream):
        if toc_file.is_reading():
            toc_file.seek(self.toc_data_offset)
            self.toc_data = bytearray(self.toc_data_size)
        else:
            self.toc_data_offset = toc_file.tell()
        self.toc_data = toc_file.bytes(self.toc_data)

        if gpu_file.is_writing():
            self.gpu_resource_offset = ceil(float(gpu_file.tell()) / 64) * 64
        if self.gpu_resource_size > 0:
            gpu_file.seek(self.gpu_resource_offset)
            if gpu_file.is_reading():
                self.gpu_data = bytearray(self.gpu_resource_size)
            self.gpu_data = gpu_file.bytes(self.gpu_data)

        if stream_file.is_writing():
            self.stream_offset = ceil(float(stream_file.tell()) / 64) * 64
        if self.stream_size > 0:
            stream_file.seek(self.stream_offset)
            if stream_file.is_reading():
                self.stream_data = bytearray(self.stream_size)
            self.stream_data = stream_file.bytes(self.stream_data)

    def clone(self):
        return copy.deepcopy(self)


class StreamToc:
    def __init__(self):
        self.magic = 0
        self.num_types = 0
        self.num_files = 0
        self.unknown = 0
        self.unk4_data = bytearray(56)
        self.toc_types = []
        self.toc_entries = []
        self.toc_dict = {}
        self.path = ""
        self.name = ""

    def update_types(self):
        self.toc_types = [
            TocFileType(type_id=type_id, num_files=len(entries))
            for type_id, entries in self.toc_dict.items()
        ]

    def update_path(self, path):
        self.path = path
        self.name = Path(path).name

    def serialize(self, serialize_data=True):
        if self.toc_file.is_writing():
            self.update_types()

        if len(self.toc_file.data) == 0 and self.toc_file.is_reading():
            return False

        self.magic = self.toc_file.uint32(self.magic)
        if self.magic != 4026531857:
            return False

        self.num_types = self.toc_file.uint32(len(self.toc_types))
        if self.toc_file.is_reading():
            self.num_files = self.toc_file.uint32(len(self.toc_entries))
        else:
            self.num_files = self.toc_file.uint32(
                sum(len(entries) for entries in self.toc_dict.values())
            )
        self.unknown = self.toc_file.uint32(self.unknown)
        self.unk4_data = self.toc_file.bytes(self.unk4_data, 56)

        if self.toc_file.is_reading():
            self.toc_types = [TocFileType() for _ in range(self.num_types)]
            self.toc_entries = [TocEntry() for _ in range(self.num_files)]

        self.toc_types = [entry.serialize(self.toc_file) for entry in self.toc_types]
        toc_entry_start = self.toc_file.tell()

        if self.toc_file.is_reading():
            self.toc_entries = [entry.serialize(self.toc_file) for entry in self.toc_entries]
            for entry in self.toc_entries:
                self.toc_dict.setdefault(entry.type_id, {})[entry.file_id] = entry
        else:
            index = 1
            for toc_type in self.toc_types:
                for entry in self.toc_dict[toc_type.type_id].values():
                    entry.serialize(self.toc_file, index)
                    index += 1

        if serialize_data:
            for entries in self.toc_dict.values():
                for entry in entries.values():
                    entry.serialize_data(self.toc_file, self.gpu_file, self.stream_file)

        if self.toc_file.is_writing():
            self.toc_file.seek(toc_entry_start)
            index = 1
            for toc_type in self.toc_types:
                for entry in self.toc_dict[toc_type.type_id].values():
                    entry.serialize(self.toc_file, index)
                    index += 1
        return True

    def from_file(self, path, serialize_data=True):
        self.update_path(path)
        toc_data, gpu_data, stream_data = load_package(path)
        self.toc_file = MemoryStream(toc_data)
        self.gpu_file = MemoryStream(gpu_data)
        self.stream_file = MemoryStream(stream_data)
        return self.serialize(serialize_data)

    def to_file(self, path=None):
        self.toc_file = MemoryStream(io_mode="write")
        self.gpu_file = MemoryStream(io_mode="write")
        self.stream_file = MemoryStream(io_mode="write")
        self.serialize()
        if path is None:
            path = self.path
        num_entries = sum(len(entries) for entries in self.toc_dict.values())
        min_size = 256 * num_entries
        if len(self.toc_file.data) < min_size:
            self.toc_file.data.extend(bytearray(min_size - len(self.toc_file.data)))

        with open(path, "w+b") as file_obj:
            file_obj.write(bytes(self.toc_file.data))
        with open(path + ".gpu_resources", "w+b") as file_obj:
            file_obj.write(bytes(self.gpu_file.data))
        with open(path + ".stream", "w+b") as file_obj:
            file_obj.write(bytes(self.stream_file.data))

    def get_entry(self, file_id, type_id):
        return self.toc_dict.get(int(type_id), {}).get(int(file_id))

    def add_entry(self, new_entry, override=False):
        existing = self.get_entry(new_entry.file_id, new_entry.type_id)
        if existing is not None and not override:
            raise ValueError("Entry with same ID already exists")
        self.toc_dict.setdefault(new_entry.type_id, {})[new_entry.file_id] = new_entry
        self.update_types()

    def clear_entries(self):
        self.toc_types = []
        self.toc_entries = []
        self.toc_dict = {}

    def entry_counts(self):
        return {type_id: len(entries) for type_id, entries in self.toc_dict.items()}


class SearchToc:
    def __init__(self):
        self.toc_entries = {}
        self.path = ""
        self.name = ""

    def has_entry(self, file_id, type_id):
        return int(file_id) in self.toc_entries.get(int(type_id), set())

    def update_path(self, path):
        self.path = path
        self.name = Path(path).name

    def from_slim_file(self, path):
        self.update_path(path)
        data = get_package_toc(path)
        if not data:
            return False
        magic, num_types, num_files = struct.unpack_from("<III", data, offset=0)
        if magic != 4026531857:
            return False
        offset = 72 + (num_types << 5)
        for _ in range(num_files):
            file_id, type_id, _toc_data_offset = struct.unpack_from("<QQQ", data, offset=offset)
            self.toc_entries.setdefault(int(type_id), set()).add(int(file_id))
            offset += 80
        return True

    def from_file(self, path):
        self.update_path(path)
        with open(path, "rb") as file_obj:
            header = file_obj.read(12)
            magic, num_types, num_files = struct.unpack("<III", header)
            if magic != 4026531857:
                return False
            offset = 72 + (num_types << 5)
            file_obj.seek(offset)
            entry_blob = file_obj.read(80 * num_files)
        for index in range(num_files):
            file_id, type_id = struct.unpack_from("<QQ", entry_blob, offset=index * 80)
            self.toc_entries.setdefault(int(type_id), set()).add(int(file_id))
        return True


class GameArchiveIndex:
    def __init__(self, game_data_folder: str):
        self.game_data_folder = game_data_folder
        self.search_archives = []
        self.archive_cache = {}
        self.unit_fingerprint_cache = {}

    def build(self):
        if self.search_archives:
            return

        game_path = Path(self.game_data_folder)
        if is_slim_version():
            bundle_database_path = game_path / "bundle_database.data"
            with open(bundle_database_path, "rb") as file_obj:
                data = file_obj.read()
            num_packages = int.from_bytes(data[4:8], "little")
            for index in range(num_packages):
                offset = 0x10 + 0x33 * index
                name = data[offset:offset + 0x33].decode(errors="ignore").split("\x17")[0]
                if not name:
                    continue
                search_toc = SearchToc()
                full_path = str(game_path / name)
                if search_toc.from_slim_file(full_path):
                    self.search_archives.append(search_toc)
        else:
            for root, _dirs, files in os.walk(game_path):
                for name in files:
                    if Path(name).suffix != "":
                        continue
                    full_path = os.path.join(root, name)
                    search_toc = SearchToc()
                    if search_toc.from_file(full_path):
                        self.search_archives.append(search_toc)

    def find_archive_path(self, file_id: int, type_id: int):
        self.build()
        for archive in self.search_archives:
            if archive.has_entry(file_id, type_id):
                return archive.path
        return None

    def find_archive_paths(self, file_ids, type_id: int):
        """Map resource IDs to every base-game archive that contains them.

        ``find_archive_path`` is appropriate for a single dependency lookup.
        Audio imports commonly contain several Wwise Banks, however, so doing
        one full archive scan per Bank is needlessly expensive.  This method
        builds the lightweight TOC index once, then walks every archive once
        to find all requested IDs.  It only examines archive TOC headers and
        never attempts to parse Wwise/HIRC payloads.

        The returned tuples intentionally retain every match.  A Bank normally
        belongs to one base-game archive, but reporting duplicates is safer
        than silently selecting an arbitrary archive when a game build has
        overlapping resources.
        """

        requested_ids = tuple(sorted({int(file_id) for file_id in file_ids}))
        matches = {file_id: [] for file_id in requested_ids}
        if not requested_ids:
            return {}

        self.build()
        requested_set = set(requested_ids)
        for archive in self.search_archives:
            available_ids = archive.toc_entries.get(int(type_id), set())
            for file_id in requested_set.intersection(available_ids):
                matches[file_id].append(archive.path)

        return {
            file_id: tuple(matches[file_id])
            for file_id in requested_ids
        }

    def map_wwise_bank_ids_to_archive_paths(self, bank_ids):
        """Return ``{bank_id: (game_archive_path, ...)}`` for Wwise Banks.

        ``SearchToc`` already knows how to read both legacy archives and the
        slim/bundled TOC path, so this works for either game layout after the
        caller has initialized ``slim`` for the selected game-data folder.
        """

        return self.find_archive_paths(bank_ids, WwiseBankID)

    def load_archive(self, archive_path: str):
        archive = self.archive_cache.get(archive_path)
        if archive is not None:
            return archive
        archive = StreamToc()
        if not archive.from_file(str(archive_path)):
            return None
        self.archive_cache[archive_path] = archive
        return archive

    def iter_entries(self, type_id: int):
        self.build()
        seen = set()
        for search_archive in self.search_archives:
            entry_ids = sorted(search_archive.toc_entries.get(int(type_id), set()))
            if not entry_ids:
                continue
            archive = self.load_archive(search_archive.path)
            if archive is None:
                continue
            for file_id in entry_ids:
                if file_id in seen:
                    continue
                entry = archive.get_entry(file_id, type_id)
                if entry is None:
                    continue
                seen.add(file_id)
                yield entry, f"game archive {Path(search_archive.path).name}"


def map_patch_wwise_banks_to_game_archives(
    patch: StreamToc,
    archive_index: GameArchiveIndex,
):
    """Map every Wwise Bank included by ``patch`` to base-game archive paths.

    This deliberately treats a patched Bank's archive ``file_id`` as the
    lookup key.  That is the relationship used by the community audio tool
    and is stable independently of the Bank's version-sensitive inner Wwise
    hierarchy.  Missing IDs are returned with an empty tuple so a caller can
    show a useful diagnostic or keep a standalone Bank without guessing a
    destination.
    """

    bank_ids = patch.toc_dict.get(WwiseBankID, {}).keys()
    return archive_index.map_wwise_bank_ids_to_archive_paths(bank_ids)


def migrate_patch_audio_from_current_game_archives(
    *,
    game_data_folder: str,
    broken_patch_path: str,
    broken_patch: StreamToc,
    archive_index: GameArchiveIndex,
    log=None,
):
    """Run the community Wwise merge and return its audio entries.

    This is deliberately an *aggressive* compatibility mode.  A Wwise Bank
    produced by the community audio modder often contains all media from the
    game version on which the mod was authored, so there is no reliable way to
    distinguish a deliberate replacement from a vanilla sound that changed
    between game versions without the author's original baseline/manifest.
    The caller must opt in through the GUI before invoking this function.

    Entries for Banks which cannot be mapped to a current archive are not sent
    to the community engine; the normal raw-preserve path keeps them intact.
    """

    bank_paths = map_patch_wwise_banks_to_game_archives(broken_patch, archive_index)
    selected_paths = []
    for bank_id, candidates in bank_paths.items():
        if not candidates:
            log_message(
                log,
                f"COMMUNITY AUDIO: No current game archive found for Bank {bank_id}; preserving it unchanged.",
            )
            continue
        if len(candidates) > 1:
            log_message(
                log,
                f"COMMUNITY AUDIO: Bank {bank_id} appears in multiple archives; using {Path(candidates[0]).name}.",
            )
        selected_paths.append(candidates[0])

    if not selected_paths:
        return {}

    # The community engine produces a complete temporary patch.  Read it with
    # our own archive writer, then merge only its audio entries into the final
    # fixed patch; Unit/texture/etc. data remains under this tool's control.
    with tempfile.TemporaryDirectory(prefix="hd2_audio_migration_") as temp_dir:
        migrated_patch_path = Path(temp_dir) / "community_audio.patch_0"
        try:
            result = migrate_audio_patch_with_community_engine(
                base_archive_paths=selected_paths,
                patch_path=broken_patch_path,
                output_patch_path=migrated_patch_path,
                game_data_folder=game_data_folder,
                log=log,
            )
        except CommunityAudioAdapterError as exc:
            log_message(
                log,
                f"COMMUNITY AUDIO: Semantic migration failed; preserving original audio. {exc}",
            )
            return {}

        migrated_patch = StreamToc()
        if not migrated_patch.from_file(str(result.output_path)):
            log_message(
                log,
                "COMMUNITY AUDIO: Generated patch could not be read; preserving original audio.",
            )
            return {}

        migrated_entries = {}
        for type_id in AUDIO_TYPE_IDS:
            for entry in migrated_patch.toc_dict.get(type_id, {}).values():
                # The community writer does not understand Wwise Metadata; it
                # is intentionally retained from the original patch instead.
                if type_id == WwiseMetaDataID:
                    continue
                migrated_entries[(int(type_id), int(entry.file_id))] = entry.clone()

        log_message(
            log,
            "COMMUNITY AUDIO: Migrated "
            f"{len(result.modified_bank_ids)} Bank(s), {len(result.modified_stream_ids)} Stream(s); "
            f"using {len(result.base_archives)} current game archive(s).",
        )
        return migrated_entries
#endregion Archive Containers And Indexing


#region Archive And Package Utilities
def build_patch_template(default_archive: StreamToc, output_path: str):
    patch = StreamToc()
    patch.magic = default_archive.magic
    patch.unknown = default_archive.unknown
    patch.unk4_data = bytearray(default_archive.unk4_data)
    patch.clear_entries()
    patch.update_path(output_path)
    return patch


def normalize_archive_selection(selected_path: str):
    lower_path = selected_path.lower()
    for suffix in (".gpu_resources", ".stream"):
        if lower_path.endswith(suffix):
            return selected_path[: -len(suffix)]
    return selected_path


def find_7z_executable():
    for candidate in (
        shutil.which("7z"),
        shutil.which("7zz"),
        r"C:\Windows\System32\7z.exe",
        r"C:\Program Files\7-Zip\7z.exe",
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def extract_archive_file(input_archive_path: str, extract_dir: str):
    suffix = Path(input_archive_path).suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(input_archive_path, "r") as archive_file:
            archive_file.extractall(extract_dir)
        return

    if suffix not in {".7z", ".rar"}:
        raise ValueError("Unsupported archive format. Please choose a .zip, .7z, or .rar file.")

    tool_path = find_7z_executable()
    if tool_path is None:
        raise ValueError("7-Zip is required to open .7z or .rar files, but 7z.exe was not found.")

    result = subprocess.run(
        [tool_path, "x", "-y", f"-o{extract_dir}", input_archive_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "Unknown extraction error."
        raise ValueError(f"Failed to extract compressed mod archive.\n{details}")


def create_zip_from_directory(source_dir: str, output_zip_path: str):
    source_path = Path(source_dir)
    output_path = Path(output_zip_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
        for file_path in sorted(source_path.rglob("*")):
            if file_path.is_file():
                archive_file.write(file_path, arcname=file_path.relative_to(source_path))


def find_patch_groups(root_dir: str):
    grouped_paths = {}
    for file_path in Path(root_dir).rglob("*"):
        if not file_path.is_file():
            continue
        normalized_path = Path(normalize_archive_selection(str(file_path)))
        if ".patch_" not in normalized_path.name:
            continue
        grouped_paths.setdefault(normalized_path, set()).add(file_path)

    patch_paths = []
    incomplete_groups = []
    for normalized_path, members in sorted(grouped_paths.items(), key=lambda item: str(item[0]).lower()):
        if normalized_path.is_file():
            patch_paths.append(str(normalized_path))
        else:
            incomplete_groups.append(
                {
                    "base_path": str(normalized_path),
                    "members": sorted(str(member) for member in members),
                }
            )
    return patch_paths, incomplete_groups


def resolve_output_path(export_dir: str, patch_index: int = 0):
    return str(Path(export_dir) / f"{BaseArchiveHexID}.patch_{patch_index}")


def detect_patch_index_from_name(patch_path: str):
    name = Path(patch_path).name
    marker = ".patch_"
    if marker not in name:
        return 0
    suffix = name.split(marker, 1)[1]
    digits = []
    for char in suffix:
        if char.isdigit():
            digits.append(char)
        else:
            break
    if not digits:
        return 0
    return int("".join(digits))


def rebuild_entry_payload(entry):
    if entry.type_id == TexID:
        asset = StingrayTexture()
        asset.serialize(
            MemoryStream(entry.toc_data),
            MemoryStream(entry.gpu_data),
            MemoryStream(entry.stream_data),
        )
        toc = MemoryStream(io_mode="write")
        gpu = MemoryStream(io_mode="write")
        stream = MemoryStream(io_mode="write")
        asset.serialize(toc, gpu, stream)
        return bytes(toc.data), bytes(gpu.data), bytes(stream.data), "rebuilt"

    if entry.type_id == MaterialID:
        asset = StingrayMaterial()
        asset.serialize(MemoryStream(entry.toc_data))
        toc = MemoryStream(io_mode="write")
        asset.serialize(toc)
        return bytes(toc.data), bytes(entry.gpu_data), b"", "rebuilt"

    if entry.type_id == BoneID:
        asset = StingrayBones()
        asset.serialize(MemoryStream(entry.toc_data))
        toc = MemoryStream(entry.toc_data, io_mode="write")
        asset.serialize(toc)
        return bytes(toc.data), b"", b"", "rebuilt"

    if entry.type_id == ParticleID:
        # Particle effects need a version-aware migration.  Preserve their
        # stream/GPU data byte-for-byte; the community updater only changes
        # the effect payload stored in the package TOC.
        toc_data, mode = migrate_particle_effect(entry.toc_data)
        return toc_data, bytes(entry.gpu_data), bytes(entry.stream_data), mode

    if entry.type_id == StateMachineID:
        asset = StingrayStateMachine()
        asset.serialize(MemoryStream(entry.toc_data))
        toc = MemoryStream(io_mode="write")
        asset.serialize(toc)
        return bytes(toc.data), b"", b"", "rebuilt"

    return bytes(entry.toc_data), bytes(entry.gpu_data), bytes(entry.stream_data), "raw"
#endregion Archive And Package Utilities


#region Unit Analysis And ID Swap Matching
def parse_unit_refs(entry):
    stream = MemoryStream(entry.toc_data)
    unk_ref1 = stream.uint64(0)
    bones_ref = stream.uint64(0)
    composite_ref = stream.uint64(0)
    unk_ref2 = stream.uint64(0)
    state_machine_ref = stream.uint64(0)
    header1 = stream.uint64(0)
    return {
        "unk_ref1": unk_ref1,
        "bones_ref": bones_ref,
        "composite_ref": composite_ref,
        "unk_ref2": unk_ref2,
        "state_machine_ref": state_machine_ref,
        "header1": header1,
    }


def weapon_unit_ref_signature(entry):
    """Return the stable rig signature used to identify a swapped weapon.

    The Community SDK creates an ID-swap by cloning a source Unit, changing
    its file ID, and then writing custom mesh data.  The target file ID is
    therefore not a useful way to find the source.  The Unit's Bone and State
    Machine references survive that operation and provide a much stronger
    identity than mesh/LOD similarity.
    """
    if entry.type_id != UnitID or len(entry.toc_data) < 48:
        return None
    refs = parse_unit_refs(entry)
    if not refs["bones_ref"] or not refs["state_machine_ref"]:
        return None
    return (
        int(refs["bones_ref"]),
        int(refs["composite_ref"]),
        int(refs["state_machine_ref"]),
    )


def _matching_weapon_source_units(source_archive, signature, target_unit_id):
    return [
        entry
        for entry in source_archive.toc_dict.get(UnitID, {}).values()
        if int(entry.file_id) != int(target_unit_id)
        and weapon_unit_ref_signature(entry) == signature
    ]


def infer_weapon_idswap_mappings(patch, source):
    """Infer only unambiguous weapon swap mappings.

    ``source`` can be a loaded :class:`StreamToc` for a focused/manual lookup
    or a :class:`GameArchiveIndex` for automatic discovery across the selected
    game data folder.  A mapping is returned only when exactly one *different*
    source Unit has the patch Unit's exact ``(Bones, Composite, StateMachine)``
    signature.  Zero-reference helper Units and same-ID Units are deliberately
    ignored; using fuzzy mesh matching for those was the source of unsafe
    weapon repairs.
    """
    patch_units = list(patch.toc_dict.get(UnitID, {}).values())
    target_signatures = {
        int(entry.file_id): weapon_unit_ref_signature(entry)
        for entry in patch_units
    }
    target_signatures = {
        target_id: signature
        for target_id, signature in target_signatures.items()
        if signature is not None
    }
    if not target_signatures:
        return []

    if isinstance(source, StreamToc):
        mappings = []
        source_name = source.name or "source archive"
        source_path = source.path or ""
        for target_id, signature in target_signatures.items():
            candidates = _matching_weapon_source_units(source, signature, target_id)
            if len(candidates) == 1:
                candidate = candidates[0]
                mappings.append(
                    WeaponIdSwapMapping(
                        target_unit_id=target_id,
                        source_unit_id=int(candidate.file_id),
                        source_entry=candidate,
                        source_name=source_name,
                        source_archive_path=source_path,
                    )
                )
        return mappings

    if not isinstance(source, GameArchiveIndex):
        raise TypeError("Weapon ID-swap inference needs a StreamToc or GameArchiveIndex.")

    # Index all requested Bone/StateMachine references in one TOC-header pass.
    # This is substantially cheaper than a complete archive scan for every
    # Unit in a multi-part weapon patch.
    source.build()
    requested_bones = {signature[0] for signature in target_signatures.values()}
    requested_state_machines = {signature[2] for signature in target_signatures.values()}
    bone_paths = {resource_id: set() for resource_id in requested_bones}
    state_machine_paths = {resource_id: set() for resource_id in requested_state_machines}
    for search_archive in source.search_archives:
        available_bones = search_archive.toc_entries.get(BoneID, set())
        available_state_machines = search_archive.toc_entries.get(StateMachineID, set())
        for resource_id in requested_bones.intersection(available_bones):
            bone_paths[resource_id].add(search_archive.path)
        for resource_id in requested_state_machines.intersection(available_state_machines):
            state_machine_paths[resource_id].add(search_archive.path)

    mappings = []
    for target_id, signature in target_signatures.items():
        candidate_paths = sorted(
            bone_paths[signature[0]].intersection(state_machine_paths[signature[2]])
        )
        candidates = []
        for archive_path in candidate_paths:
            archive = source.load_archive(archive_path)
            if archive is None:
                continue
            for entry in _matching_weapon_source_units(archive, signature, target_id):
                candidates.append((entry, archive_path))

        # Multiple archive copies of an identical Unit can exist.  They are
        # still safe when their source ID and payload are identical; otherwise
        # leave the target unresolved instead of choosing arbitrarily.
        by_source_id = {}
        ambiguous = False
        for entry, archive_path in candidates:
            existing = by_source_id.get(int(entry.file_id))
            if existing is None:
                by_source_id[int(entry.file_id)] = (entry, archive_path)
            elif bytes(existing[0].toc_data) != bytes(entry.toc_data):
                ambiguous = True
        if ambiguous or len(by_source_id) != 1:
            continue
        source_entry, source_path = next(iter(by_source_id.values()))
        mappings.append(
            WeaponIdSwapMapping(
                target_unit_id=target_id,
                source_unit_id=int(source_entry.file_id),
                source_entry=source_entry,
                source_name=f"game archive {Path(source_path).name}",
                source_archive_path=str(source_path),
            )
        )
    return mappings


def migrate_weapon_idswap_unit(entry, source_entry):
    """Apply the safe, surgical Unit schema update for a custom Unit swap.

    No Unit section is parsed and reserialized.  In particular this preserves
    the mod's custom transform/bone information, LOD data, materials, mesh
    data, GPU payload, stream payload, and all external animation references.
    Only the Unit version and the legacy stream-layout format IDs can change.
    """
    if entry.type_id != UnitID or source_entry.type_id != UnitID:
        raise ValueError("Weapon ID-swap migration requires Unit entries.")
    if len(entry.toc_data) < 0x60 or len(source_entry.toc_data) < 0x30:
        raise ValueError("Unit payload is too small for weapon ID-swap migration.")

    patch_data = bytearray(entry.toc_data)
    source_data = bytes(source_entry.toc_data)
    patch_version = struct.unpack_from("<I", patch_data, 0x2C)[0]
    source_version = struct.unpack_from("<I", source_data, 0x2C)[0]

    if patch_version != source_version and not (
        patch_version < WEAPON_IDSWAP_CURRENT_UNIT_VERSION
        and source_version == WEAPON_IDSWAP_CURRENT_UNIT_VERSION
    ):
        raise ValueError(
            "Weapon Unit schema update is unknown for "
            f"{patch_version} -> {source_version}; refusing to alter custom rig data."
        )

    if patch_version < WEAPON_IDSWAP_CURRENT_UNIT_VERSION:
        layout_list_offset = struct.unpack_from("<I", patch_data, 0x5C)[0]
        if layout_list_offset + 4 > len(patch_data):
            raise ValueError("Weapon Unit has an invalid stream-layout offset.")
        layout_count = struct.unpack_from("<I", patch_data, layout_list_offset)[0]
        offsets_start = layout_list_offset + 4
        if offsets_start + layout_count * 4 > len(patch_data):
            raise ValueError("Weapon Unit has truncated stream-layout offsets.")
        for layout_index in range(layout_count):
            layout_offset = struct.unpack_from(
                "<I", patch_data, offsets_start + layout_index * 4
            )[0]
            items_start = layout_list_offset + layout_offset + 8
            if items_start + 16 * UnitVertexComponent.RECORD_SIZE > len(patch_data):
                raise ValueError("Weapon Unit has truncated stream-layout items.")
            for item_index in range(16):
                format_offset = (
                    items_start
                    + item_index * UnitVertexComponent.RECORD_SIZE
                    + 4
                )
                format_id = struct.unpack_from("<I", patch_data, format_offset)[0]
                if format_id > 16:
                    struct.pack_into("<I", patch_data, format_offset, format_id + 4)

    struct.pack_into("<I", patch_data, 0x2C, source_version)
    migrated = entry.clone()
    migrated.toc_data = bytes(patch_data)
    return migrated


def is_static_unit(entry):
    """Whether a Unit has no external rig references.

    Armor and helmet swaps created by the Community SDK are commonly static:
    their Bone, Composite, and State Machine references are all zero.  They
    need a different repair from weapon swaps: the current *target* Unit's
    LOD group is authoritative, while all custom mesh/bone/GPU payload remains
    owned by the patch.
    """
    if entry.type_id != UnitID or len(entry.toc_data) < 48:
        return False
    refs = parse_unit_refs(entry)
    return not (
        refs["bones_ref"]
        or refs["composite_ref"]
        or refs["state_machine_ref"]
    )


def _unit_lod_range(unit_data: bytes):
    """Return the exact raw ``lod_group_list`` byte range in a Unit payload."""
    if len(unit_data) < 0x38:
        raise ValueError("Unit payload is too small for LOD migration.")
    lod_start, next_section_start = struct.unpack_from("<II", unit_data, 0x30)
    if (
        lod_start < 0x80
        or next_section_start < lod_start
        or next_section_start > len(unit_data)
    ):
        raise ValueError("Unit has invalid LOD group offsets.")
    return lod_start, next_section_start


def migrate_static_unit_idswap_unit(entry, target_entry):
    """Migrate a static armor/helmet Unit without a manual source archive.

    The Community Unit Patcher resolves static armor and helmet Units by their
    *target* file ID.  Reproduce that safe part of its update here: take the
    current target's raw LOD group, update offsets if its size changed, and
    apply the byte-level Unit schema conversion.  The patch's transforms,
    customization, bone info, mesh info, materials, GPU data, and stream data
    are never rebuilt or copied from the game.
    """
    if not is_static_unit(entry):
        raise ValueError("Static Unit migration requires a zero-reference Unit entry.")
    if target_entry.type_id != UnitID:
        raise ValueError("Static Unit migration requires a current target Unit entry.")

    # This validates the version transition and preserves every patch payload
    # byte except the version and legacy vertex-format IDs.
    migrated = migrate_weapon_idswap_unit(entry, target_entry)
    patch_data = bytearray(migrated.toc_data)
    target_data = bytes(target_entry.toc_data)
    patch_lod_start, patch_lod_end = _unit_lod_range(patch_data)
    target_lod_start, target_lod_end = _unit_lod_range(target_data)
    target_lod = target_data[target_lod_start:target_lod_end]
    size_difference = len(target_lod) - (patch_lod_end - patch_lod_start)

    # A slice replacement deliberately keeps every trailing custom section in
    # its original byte form.  Header offsets are adjusted afterwards instead
    # of parsing/re-serializing the Unit.
    patch_data[patch_lod_start:patch_lod_end] = target_lod
    if size_difference:
        for offset_position in UNIT_SECTION_OFFSET_POSITIONS:
            if offset_position + 4 > len(patch_data):
                raise ValueError("Static Unit has a truncated header offset table.")
            section_offset = struct.unpack_from("<I", patch_data, offset_position)[0]
            if section_offset >= patch_lod_end:
                adjusted_offset = section_offset + size_difference
                if adjusted_offset < 0 or adjusted_offset > len(patch_data):
                    raise ValueError("Static Unit LOD update produced an invalid section offset.")
                struct.pack_into("<I", patch_data, offset_position, adjusted_offset)

    migrated.toc_data = bytes(patch_data)
    return migrated


def is_probable_static_idswap_patch(broken_patch: StreamToc):
    units = list(broken_patch.toc_dict.get(UnitID, {}).values())
    if len(units) < 2:
        return False
    for unit_entry in units:
        refs = parse_unit_refs(unit_entry)
        if refs["bones_ref"] != 0:
            return False
        if refs["composite_ref"] != 0:
            return False
        if refs["state_machine_ref"] != 0:
            return False
        if refs["header1"] != 4294967298:
            return False
    return True


def resolve_unit_source_entry(
    unit_id: int,
    default_archive: StreamToc,
    archive_index: GameArchiveIndex | None = None,
):
    default_entry = default_archive.get_entry(unit_id, UnitID)
    if default_entry is not None:
        return default_entry, "default archive"
    if archive_index is not None:
        archive_path = archive_index.find_archive_path(unit_id, UnitID)
        if archive_path is not None:
            archive = StreamToc()
            if archive.from_file(str(archive_path)):
                entry = archive.get_entry(unit_id, UnitID)
                if entry is not None:
                    return entry, f"game archive {Path(archive_path).name}"
    return None, None


def extract_unit_stream_sections(unit: StingrayUnit):
    stream_blob = unit.section_blobs.get("stream_info", b"")
    if not stream_blob:
        return None
    return UnitStreamSection.parse(stream_blob, unit_version=unit.unit_version)


def extract_unit_mesh_sections(unit: StingrayUnit):
    mesh_blob = unit.section_blobs.get("mesh_info", b"")
    if not mesh_blob:
        return None
    return UnitMeshSection.parse(mesh_blob)


def get_unit_fingerprint(entry):
    unit = StingrayUnit()
    unit.serialize(MemoryStream(entry.toc_data))

    mesh_sections = extract_unit_mesh_sections(unit)
    stream_sections = extract_unit_stream_sections(unit)

    mesh_ids = ()
    lod_indices = ()
    material_ids = ()
    if mesh_sections is not None:
        mesh_ids = tuple(mesh.mesh_id for mesh in mesh_sections.mesh_infos)
        lod_indices = tuple(mesh.lod_index for mesh in mesh_sections.mesh_infos)
        material_ids = tuple(
            material_id
            for mesh in mesh_sections.mesh_infos
            for material_id in mesh.material_ids
        )

    stream_layouts = ()
    if stream_sections is not None:
        stream_layouts = tuple(
            tuple(component.semantic_key for component in info.components)
            for info in stream_sections.stream_infos
        )

    section_sizes = tuple(
        (name, len(unit.section_blobs.get(name, b"")))
        for name in StingrayUnit.SECTION_ORDER
    )

    return UnitFingerprint(
        file_id=int(entry.file_id),
        bones_ref=int(unit.bones_ref),
        composite_ref=int(unit.composite_ref),
        state_machine_ref=int(unit.state_machine_ref),
        mesh_ids=mesh_ids,
        lod_indices=lod_indices,
        material_ids=material_ids,
        stream_layouts=stream_layouts,
        section_sizes=section_sizes,
    )


def score_unit_similarity(base: UnitFingerprint, candidate: UnitFingerprint):
    score = 0
    reasons = []

    if base.mesh_ids and candidate.mesh_ids:
        if base.mesh_ids == candidate.mesh_ids:
            score += 40
            reasons.append("mesh_ids exact")
        elif set(base.mesh_ids) == set(candidate.mesh_ids):
            score += 28
            reasons.append("mesh_ids set")
        elif len(set(base.mesh_ids) & set(candidate.mesh_ids)) > 0:
            overlap = len(set(base.mesh_ids) & set(candidate.mesh_ids))
            score += min(18, overlap * 4)
            reasons.append(f"mesh_ids overlap {overlap}")
        if len(base.mesh_ids) == len(candidate.mesh_ids):
            score += 8
            reasons.append("mesh_count")

    if base.stream_layouts and candidate.stream_layouts:
        if base.stream_layouts == candidate.stream_layouts:
            score += 28
            reasons.append("stream_layouts exact")
        else:
            matching_layouts = sum(
                1
                for left, right in zip(base.stream_layouts, candidate.stream_layouts)
                if left == right
            )
            if matching_layouts:
                score += min(18, matching_layouts * 6)
                reasons.append(f"stream_layouts overlap {matching_layouts}")
        if len(base.stream_layouts) == len(candidate.stream_layouts):
            score += 6
            reasons.append("stream_count")

    if base.material_ids and candidate.material_ids:
        if base.material_ids == candidate.material_ids:
            score += 16
            reasons.append("material_ids exact")
        elif set(base.material_ids) == set(candidate.material_ids):
            score += 12
            reasons.append("material_ids set")
        elif len(set(base.material_ids) & set(candidate.material_ids)) > 0:
            overlap = len(set(base.material_ids) & set(candidate.material_ids))
            score += min(8, overlap * 2)
            reasons.append(f"material_ids overlap {overlap}")

    if base.lod_indices and candidate.lod_indices and base.lod_indices == candidate.lod_indices:
        score += 8
        reasons.append("lod_indices")

    for label, left_value, right_value in (
        ("bones_ref", base.bones_ref, candidate.bones_ref),
        ("composite_ref", base.composite_ref, candidate.composite_ref),
        ("state_machine_ref", base.state_machine_ref, candidate.state_machine_ref),
    ):
        if left_value != 0 and left_value == right_value:
            score += 8
            reasons.append(label)

    matching_sections = sum(
        1 for left, right in zip(base.section_sizes, candidate.section_sizes)
        if left[1] != 0 and left[1] == right[1]
    )
    if matching_sections:
        score += min(12, matching_sections * 2)
        reasons.append(f"section_sizes {matching_sections}")

    return UnitSimilarity(score=score, reasons=tuple(reasons))


def get_cached_unit_fingerprint(entry, archive_index: GameArchiveIndex | None = None):
    if archive_index is None:
        return get_unit_fingerprint(entry)
    cached = archive_index.unit_fingerprint_cache.get(int(entry.file_id))
    if cached is not None:
        return cached
    fingerprint = get_unit_fingerprint(entry)
    archive_index.unit_fingerprint_cache[int(entry.file_id)] = fingerprint
    return fingerprint


def resolve_archive_input_path(game_data_folder: str, archive_input: str):
    archive_input = archive_input.strip()
    if not archive_input:
        return None

    candidate = Path(archive_input)
    if candidate.anchor:
        return normalize_archive_selection(str(candidate))
    return normalize_archive_selection(str(Path(game_data_folder) / archive_input))


def resolve_archive_input_paths(game_data_folder: str, archive_input: str):
    archive_paths = []
    for token in archive_input.split(","):
        token = token.strip()
        if not token:
            continue
        resolved = resolve_archive_input_path(game_data_folder, token)
        if resolved is not None:
            archive_paths.append(resolved)
    return archive_paths


def match_unit_to_source_archive(
    entry,
    source_archive: StreamToc,
    source_fingerprints: dict[int, UnitFingerprint] | None = None,
):
    if entry.type_id != UnitID:
        return None

    if source_fingerprints is None:
        source_fingerprints = {
            int(candidate.file_id): get_unit_fingerprint(candidate)
            for candidate in source_archive.toc_dict.get(UnitID, {}).values()
        }

    base_fingerprint = get_unit_fingerprint(entry)
    best_entry = None
    best_similarity = None

    for candidate in source_archive.toc_dict.get(UnitID, {}).values():
        similarity = score_unit_similarity(
            base_fingerprint,
            source_fingerprints[int(candidate.file_id)],
        )
        if best_similarity is None or similarity.score > best_similarity.score:
            best_entry = candidate
            best_similarity = similarity

    if best_entry is None or best_similarity is None or best_similarity.score <= 0:
        return None
    return best_entry, best_similarity


def match_unit_to_source_archives(
    entry,
    source_archives: list[dict],
):
    best_match = None
    for source_info in source_archives:
        match = match_unit_to_source_archive(
            entry,
            source_info["archive"],
            source_fingerprints=source_info["fingerprints"],
        )
        if match is None:
            continue
        matched_entry, similarity = match
        if best_match is None or similarity.score > best_match["similarity"].score:
            best_match = {
                "entry": matched_entry,
                "similarity": similarity,
                "name": source_info["name"],
                "path": source_info["path"],
            }
    return best_match


def detect_probable_id_swap(
    entry,
    default_archive: StreamToc,
    archive_index: GameArchiveIndex | None = None,
):
    # Disabled for now.
    #
    # The first implementation scanned and loaded too much of the game data in order to
    # score every Unit candidate, which is not safe to run by default on real user machines.
    # We'll redesign this with a metadata-first approach before re-enabling automatic
    # ID swap source inference.
    return None
#endregion Unit Analysis And ID Swap Matching


#region Unit Repair And Rebuild
def default_component_bytes(component: UnitVertexComponent):
    if component.type_id == 5 and component.format_name == "rgba_r8g8b8a8":
        return b"\xFF\xFF\xFF\xFF"
    if component.type_id == 7 and component.format_name == "vec4_half":
        return b"\x00\x3C\x00\x00\x00\x00\x00\x00"
    return b"\x00" * component.size


def convert_unit_stream_section_version(
    unit: StingrayUnit,
    target_unit_version: int,
):
    stream_blob = unit.section_blobs.get("stream_info", b"")
    if not stream_blob or unit.unit_version == target_unit_version:
        return False

    stream_sections = UnitStreamSection.parse(stream_blob, unit_version=unit.unit_version)
    changed = False
    for info in stream_sections.stream_infos:
        converted_components = []
        for component in info.components:
            converted_component = component.converted_for_version(target_unit_version)
            if converted_component.key != component.key:
                changed = True
            converted_components.append(converted_component)
        if changed:
            info.components = converted_components
            info.vertex_stride = sum(component.size for component in converted_components)

    if not changed:
        return False

    unit.section_blobs["stream_info"] = stream_sections.build()
    return True


def rebuild_vertex_buffer_for_layout(
    old_buffer: bytes,
    old_info: UnitStreamInfo,
    target_components: list[UnitVertexComponent],
):
    old_stride = old_info.vertex_stride
    if old_stride <= 0:
        raise ValueError("Unit stream has invalid vertex stride")
    expected_size = old_info.num_vertices * old_stride
    if len(old_buffer) < expected_size:
        raise ValueError("Unit vertex buffer is smaller than expected")

    old_component_offsets = {}
    running_offset = 0
    for component in old_info.components:
        old_component_offsets[component.semantic_key] = (running_offset, component.size)
        running_offset += component.size
    if running_offset != old_info.vertex_stride:
        raise ValueError("Unit stream component sizes do not match vertex stride")

    new_parts = []
    for vertex_index in range(old_info.num_vertices):
        vertex_start = vertex_index * old_stride
        for component in target_components:
            component_info = old_component_offsets.get(component.semantic_key)
            if component_info is None:
                if component.type_id in {0, 1, 6, 7}:
                    raise ValueError(
                        f"Missing critical vertex component {component.semantic_key} in stream layout"
                    )
                new_parts.append(default_component_bytes(component))
                continue
            component_offset, component_size = component_info
            new_parts.append(
                old_buffer[
                    vertex_start + component_offset: vertex_start + component_offset + component_size
                ]
            )
    return b"".join(new_parts)


def repair_unit_stream_layout_from_source(
    entry,
    source_entry,
    log=None,
):
    source_unit = StingrayUnit()
    source_unit.serialize(MemoryStream(source_entry.toc_data))
    entry_unit = StingrayUnit()
    entry_unit.serialize(MemoryStream(entry.toc_data))

    if convert_unit_stream_section_version(entry_unit, source_unit.unit_version):
        toc = MemoryStream(io_mode="write")
        entry_unit.serialize(toc)
        entry.toc_data = bytes(toc.data)
        log_message(
            log,
            f"CONVERTED Unit stream component formats to version {source_unit.unit_version}: {entry.file_id}",
        )

        entry_unit = StingrayUnit()
        entry_unit.serialize(MemoryStream(entry.toc_data))

    source_streams = extract_unit_stream_sections(source_unit)
    entry_streams = extract_unit_stream_sections(entry_unit)
    if source_streams is None or entry_streams is None:
        return False
    if len(source_streams.stream_infos) != len(entry_streams.stream_infos):
        return False

    new_gpu = bytearray()
    changed = False
    rebuilt_infos = []

    for stream_index, (source_info, entry_info) in enumerate(zip(source_streams.stream_infos, entry_streams.stream_infos)):
        old_vertex_end = entry_info.vertex_buffer_offset + entry_info.vertex_buffer_size
        old_index_end = entry_info.index_buffer_offset + entry_info.index_buffer_size
        if old_vertex_end > len(entry.gpu_data) or old_index_end > len(entry.gpu_data):
            return False

        old_vertex_buffer = bytes(entry.gpu_data[entry_info.vertex_buffer_offset:old_vertex_end])
        old_index_buffer = bytes(entry.gpu_data[entry_info.index_buffer_offset:old_index_end])

        source_unit_version = source_unit.unit_version
        source_layout = [component.semantic_key for component in source_info.components]
        entry_layout = [component.semantic_key for component in entry_info.components]
        extra_entry_components = [
            component.converted_for_version(source_unit_version)
            for component in entry_info.components
            if component.semantic_key not in source_layout
        ]
        target_components = [
            component.converted_for_version(source_unit_version)
            for component in source_info.components
        ] + extra_entry_components
        target_layout = [component.semantic_key for component in target_components]
        target_raw_layout = [component.key for component in target_components]
        entry_raw_layout = [component.key for component in entry_info.components]

        if target_layout != entry_layout:
            rebuilt_vertex_buffer = rebuild_vertex_buffer_for_layout(
                old_vertex_buffer,
                entry_info,
                target_components,
            )
            changed = True
            log_message(
                log,
                f"REPAIRED Unit stream layout {entry.file_id} stream {stream_index}: {entry_layout} -> {target_layout}",
            )
        else:
            rebuilt_vertex_buffer = old_vertex_buffer
            if target_raw_layout != entry_raw_layout:
                changed = True
                log_message(
                    log,
                    f"UPDATED Unit stream component format IDs {entry.file_id} stream {stream_index}: {entry_raw_layout} -> {target_raw_layout}",
                )

        new_info = copy.deepcopy(entry_info)
        new_info.components = list(target_components)
        new_info.num_vertices = entry_info.num_vertices
        new_info.num_indices = entry_info.num_indices
        new_info.vertex_stride = sum(component.size for component in target_components)
        new_info.vertex_buffer_offset = len(new_gpu)
        new_info.vertex_buffer_size = len(rebuilt_vertex_buffer)
        new_gpu.extend(rebuilt_vertex_buffer)
        new_info.index_buffer_offset = len(new_gpu)
        new_info.index_buffer_size = len(old_index_buffer)
        new_gpu.extend(old_index_buffer)
        rebuilt_infos.append(new_info)

    if not changed:
        return False

    rebuilt_stream_section = copy.deepcopy(entry_streams)
    rebuilt_stream_section.stream_infos = rebuilt_infos
    if len(source_streams.stream_unk_ids) == len(rebuilt_stream_section.stream_unk_ids):
        rebuilt_stream_section.stream_unk_ids = list(source_streams.stream_unk_ids)

    entry_unit.section_blobs["stream_info"] = rebuilt_stream_section.build()
    toc = MemoryStream(io_mode="write")
    entry_unit.serialize(toc)
    entry.toc_data = bytes(toc.data)
    entry.gpu_data = bytes(new_gpu)
    return True


def repair_unit_mesh_order_from_source(
    entry,
    source_entry,
    log=None,
):
    source_unit = StingrayUnit()
    source_unit.serialize(MemoryStream(source_entry.toc_data))
    entry_unit = StingrayUnit()
    entry_unit.serialize(MemoryStream(entry.toc_data))

    source_meshes = extract_unit_mesh_sections(source_unit)
    entry_meshes = extract_unit_mesh_sections(entry_unit)
    if source_meshes is None or entry_meshes is None:
        return False
    if len(source_meshes.mesh_infos) != len(entry_meshes.mesh_infos):
        return False

    source_ids = [mesh.mesh_id for mesh in source_meshes.mesh_infos]
    entry_by_id = {mesh.mesh_id: mesh for mesh in entry_meshes.mesh_infos}
    if set(source_ids) != set(entry_by_id):
        return False

    changed = False
    reordered_infos = []
    for source_mesh in source_meshes.mesh_infos:
        entry_mesh = copy.deepcopy(entry_by_id[source_mesh.mesh_id])
        if (
            entry_mesh.transform_index != source_mesh.transform_index
            or entry_mesh.stream_index != source_mesh.stream_index
            or entry_mesh.lod_index != source_mesh.lod_index
        ):
            changed = True
        entry_mesh.transform_index = source_mesh.transform_index
        entry_mesh.stream_index = source_mesh.stream_index
        entry_mesh.lod_index = source_mesh.lod_index
        reordered_infos.append(entry_mesh)

    if [mesh.mesh_id for mesh in entry_meshes.mesh_infos] != source_ids:
        changed = True

    if not changed:
        return False

    entry_meshes.mesh_infos = reordered_infos
    entry_meshes.mesh_unk_ids = [mesh.mesh_id for mesh in reordered_infos]
    entry_unit.section_blobs["mesh_info"] = entry_meshes.build()
    toc = MemoryStream(io_mode="write")
    entry_unit.serialize(toc)
    entry.toc_data = bytes(toc.data)
    log_message(log, f"REORDERED Unit mesh info from source layout: {entry.file_id}")
    return True


def repair_unit_lod_group_from_source(
    entry,
    source_entry,
    log=None,
):
    source_unit = StingrayUnit()
    source_unit.serialize(MemoryStream(source_entry.toc_data))
    entry_unit = StingrayUnit()
    entry_unit.serialize(MemoryStream(entry.toc_data))

    source_blob = source_unit.section_blobs.get("lod_group_list", b"")
    entry_blob = entry_unit.section_blobs.get("lod_group_list", b"")
    if not source_blob or source_blob == entry_blob:
        return False

    entry_unit.section_blobs["lod_group_list"] = bytes(source_blob)
    toc = MemoryStream(io_mode="write")
    entry_unit.serialize(toc)
    entry.toc_data = bytes(toc.data)
    log_message(log, f"REPLACED Unit lod_group_list from source: {entry.file_id}")
    return True


def repair_unit_material_bindings_from_source(
    entry,
    source_entry,
    log=None,
):
    """Refresh vanilla material *references* without touching mod geometry.

    Some same-ID Unit patches contain custom GPU/mesh data but no Material
    entries of their own.  A game update can change the short material ID used
    by a subset of an otherwise unchanged mesh list.  Keeping the old mapping
    makes the Unit load with no renderable material, even though its mesh and
    sidecars are intact.  This is deliberately strict: every mesh identity and
    structural index must agree with the current Unit before its material IDs
    and compact mapping table are refreshed.
    """
    source_unit = StingrayUnit()
    source_unit.serialize(MemoryStream(source_entry.toc_data))
    entry_unit = StingrayUnit()
    entry_unit.serialize(MemoryStream(entry.toc_data))

    source_meshes = extract_unit_mesh_sections(source_unit)
    entry_meshes = extract_unit_mesh_sections(entry_unit)
    source_materials = source_unit.section_blobs.get("materials", b"")
    entry_materials = entry_unit.section_blobs.get("materials", b"")
    if (
        source_meshes is None
        or entry_meshes is None
        or len(source_materials) < 4
        or len(entry_materials) < 4
        or len(source_meshes.mesh_infos) != len(entry_meshes.mesh_infos)
    ):
        return False

    for source_mesh, entry_mesh in zip(source_meshes.mesh_infos, entry_meshes.mesh_infos):
        if (
            source_mesh.mesh_id != entry_mesh.mesh_id
            or source_mesh.lod_index != entry_mesh.lod_index
            or source_mesh.stream_index != entry_mesh.stream_index
            or source_mesh.transform_index != entry_mesh.transform_index
            or len(source_mesh.material_ids) != len(entry_mesh.material_ids)
        ):
            return False

    changed = False
    for source_mesh, entry_mesh in zip(source_meshes.mesh_infos, entry_meshes.mesh_infos):
        if source_mesh.material_ids != entry_mesh.material_ids:
            entry_mesh.material_ids = list(source_mesh.material_ids)
            changed = True

    if not changed:
        return False

    # Replace only the compact SectionID -> MaterialID table.  The source mesh
    # identities above prove every now-referenced SectionID belongs to the
    # current vanilla Unit.  All other Unit sections and GPU/stream sidecars
    # remain the patch's bytes.
    entry_unit.section_blobs["mesh_info"] = entry_meshes.build()
    entry_unit.section_blobs["materials"] = bytes(source_materials)
    toc = MemoryStream(io_mode="write")
    entry_unit.serialize(toc)
    entry.toc_data = bytes(toc.data)
    log_message(log, f"REFRESHED vanilla material bindings for Unit {entry.file_id}")
    return True


def normalize_unit_entry_from_source(
    entry,
    default_archive: StreamToc,
    archive_index: GameArchiveIndex | None = None,
    log=None,
    header_only: bool = False,
    refresh_material_bindings: bool = False,
):
    probable_id_swap = detect_probable_id_swap(
        entry,
        default_archive,
        archive_index=archive_index,
    )
    source_entry, source_name = resolve_unit_source_entry(
        entry.file_id,
        default_archive,
        archive_index=archive_index,
    )
    if source_entry is None:
        return False
    if len(entry.toc_data) < 88 or len(source_entry.toc_data) < 88:
        return False

    source_unit = StingrayUnit()
    source_unit.serialize(MemoryStream(source_entry.toc_data))

    repaired_layout = False
    if header_only:
        log_message(
            log,
            f"Using header-only Unit normalization for {entry.file_id}: preserving mesh/lod/material ordering.",
        )
    elif probable_id_swap is not None:
        log_message(
            log,
            "PROBABLE ID SWAP detected for "
            f"{entry.file_id}: probable source {probable_id_swap.source_id} from "
            f"{probable_id_swap.source_name} "
            f"(score {probable_id_swap.source_score} vs target {probable_id_swap.target_score})",
        )
        if probable_id_swap.source_reasons:
            log_message(
                log,
                f"ID swap source match reasons for {entry.file_id}: {', '.join(probable_id_swap.source_reasons)}",
            )
        if probable_id_swap.target_reasons:
            log_message(
                log,
                f"Target match reasons for {entry.file_id}: {', '.join(probable_id_swap.target_reasons)}",
            )
        log_message(
            log,
            f"Using ID swap safe mode for {entry.file_id}: preserving patch geometry layout and skipping target LOD/mesh coercion.",
        )
    else:
        try:
            repaired_layout = repair_unit_stream_layout_from_source(entry, source_entry, log=log)
        except Exception as exc:
            log_message(log, f"FAILED Unit stream layout repair for {entry.file_id}: {exc}")
        try:
            repaired_layout = repair_unit_mesh_order_from_source(entry, source_entry, log=log) or repaired_layout
        except Exception as exc:
            log_message(log, f"FAILED Unit mesh order repair for {entry.file_id}: {exc}")
        try:
            repaired_layout = repair_unit_lod_group_from_source(entry, source_entry, log=log) or repaired_layout
        except Exception as exc:
            log_message(log, f"FAILED Unit LOD group repair for {entry.file_id}: {exc}")
        if refresh_material_bindings:
            try:
                repaired_layout = repair_unit_material_bindings_from_source(
                    entry,
                    source_entry,
                    log=log,
                ) or repaired_layout
            except Exception as exc:
                log_message(log, f"FAILED Unit material-binding refresh for {entry.file_id}: {exc}")

    entry_unit = StingrayUnit()
    entry_unit.serialize(MemoryStream(entry.toc_data))
    old_header = entry_unit.header_data_1
    # The header contains a 32-bit Unit flag followed by the 32-bit format
    # version.  The community updater replaces only the version at +0x2C;
    # preserve the patch's flag so custom Unit metadata is not overwritten.
    entry_unit.header_data_1 = (
        (source_unit.header_data_1 & 0xFFFFFFFF00000000)
        | (old_header & 0xFFFFFFFF)
    )
    toc = MemoryStream(io_mode="write")
    entry_unit.serialize(toc)
    entry.toc_data = bytes(toc.data)
    log_message(
        log,
        f"NORMALIZED Unit header_data_1 from {source_name}: {entry.file_id} ({old_header} -> {entry_unit.header_data_1})",
    )
    return repaired_layout or True


def rebuild_idswap_unit_from_source_archive(
    entry,
    source_entry,
    source_name: str,
    replace_lod_group: bool = True,
    log=None,
):
    """Apply the community Unit updater's surgical ID-swap migration.

    Swapped weapons often have a different Unit ID from the current-game
    source Unit.  Rebuilding a full ``StingrayUnit`` from that source mixes
    its transform/bone data with the patch's GPU buffers and can crash the
    game.  The community updater instead changes only the format version,
    legacy vertex-format IDs, and LOD-group blob, retaining every other byte
    from the patch Unit.  Do the same here.
    """
    patch_data = bytearray(entry.toc_data)
    source_data = bytes(source_entry.toc_data)
    if len(patch_data) < 0x60 or len(source_data) < 0x38:
        raise ValueError("Unit payload is too small for ID swap migration")

    source_version = struct.unpack_from("<I", source_data, 0x2C)[0]
    patch_version = struct.unpack_from("<I", patch_data, 0x2C)[0]
    if patch_version < 0xA4CD36:
        layout_list_offset = struct.unpack_from("<I", patch_data, 0x5C)[0]
        if layout_list_offset + 4 > len(patch_data):
            raise ValueError("Patch Unit has invalid stream-layout offset")
        layout_count = struct.unpack_from("<I", patch_data, layout_list_offset)[0]
        offsets_start = layout_list_offset + 4
        if offsets_start + layout_count * 4 > len(patch_data):
            raise ValueError("Patch Unit has truncated stream-layout offsets")
        for index in range(layout_count):
            layout_offset = struct.unpack_from("<I", patch_data, offsets_start + index * 4)[0]
            item_start = layout_list_offset + layout_offset + 8
            if item_start + 16 * 20 > len(patch_data):
                raise ValueError("Patch Unit has truncated stream-layout items")
            for item_index in range(16):
                format_offset = item_start + item_index * 20 + 4
                item_format = struct.unpack_from("<I", patch_data, format_offset)[0]
                if item_format > 16:
                    struct.pack_into("<I", patch_data, format_offset, item_format + 4)

    if replace_lod_group:
        source_lod_offset, source_joint_offset = struct.unpack_from("<II", source_data, 0x30)
        if not (0 <= source_lod_offset <= source_joint_offset <= len(source_data)):
            raise ValueError("Source Unit has invalid LOD-group bounds")
        source_lod_data = source_data[source_lod_offset:source_joint_offset]

        patch_lod_offset, patch_joint_offset = struct.unpack_from("<II", patch_data, 0x30)
        if not (0 <= patch_lod_offset <= patch_joint_offset <= len(patch_data)):
            raise ValueError("Patch Unit has invalid LOD-group bounds")
        size_difference = len(source_lod_data) - (patch_joint_offset - patch_lod_offset)

        # The 16 section offsets begin at +0x34.  Adjust every section after
        # the LOD group exactly as the community updater does before replacing
        # bytes.
        for index in range(16):
            offset_position = 0x34 + index * 4
            if offset_position + 4 > len(patch_data):
                break
            offset = struct.unpack_from("<I", patch_data, offset_position)[0]
            if offset != 0 and offset > patch_lod_offset:
                struct.pack_into("<I", patch_data, offset_position, offset + size_difference)

        patch_data[patch_lod_offset:patch_joint_offset] = source_lod_data
    struct.pack_into("<I", patch_data, 0x2C, source_version)

    new_entry = entry.clone()
    new_entry.toc_data = bytes(patch_data)
    log_message(
        log,
        f"IDSWAP community-compatible Unit migration for {entry.file_id} from "
        f"{source_name} entry {source_entry.file_id}; preserved patch GPU/stream and "
        f"{'non-LOD Unit bytes' if replace_lod_group else 'the patch LOD group and other Unit bytes'}",
    )
    return new_entry, "idswap-source"


def build_entry_from_source(
    entry,
    raw_fallback_for_unsupported: bool,
    default_archive: StreamToc,
    archive_index: GameArchiveIndex | None = None,
    log=None,
    unit_header_only: bool = False,
    unit_passthrough: bool = False,
    refresh_unit_material_bindings: bool = False,
):
    mode = "raw"
    if unit_passthrough and entry.type_id == UnitID:
        return entry.clone(), "raw-preserve"

    if entry.type_id in AUDIO_TYPE_IDS:
        # Wwise hierarchy records are version-sensitive.  Validate only their
        # stable outer envelopes, then retain the author's payload verbatim
        # while StreamToc writes fresh archive offsets and sizes.
        inspection = inspect_audio_entry(entry)
        if not inspection.valid:
            log_message(
                log,
                f"AUDIO WARNING {inspection.kind} {entry.file_id}: "
                f"{'; '.join(inspection.notes)}; preserving original bytes.",
            )
            return entry.clone(), "audio-unvalidated-preserve"
        return entry.clone(), "audio-validated-preserve"

    if entry.type_id in {BoneID, StateMachineID}:
        # Unit migrations do not change Bones or State Machine payloads.  The
        # parsers for these opaque runtime resources can normalize padding and
        # offset fields while serializing (Railgun's State Machine changed
        # from 796 to 804 bytes), which can make a perfectly present model
        # appear invisible or lose its runtime animation.  Keep authored bytes
        # exactly as supplied; StreamToc still writes fresh package offsets.
        return entry.clone(), "raw-preserve"

    if entry.type_id in SUPPORTED_REBUILD_TYPES:
        try:
            toc_data, gpu_data, stream_data, mode = rebuild_entry_payload(entry)
            new_entry = entry.clone()
            new_entry.toc_data = toc_data
            new_entry.gpu_data = gpu_data
            new_entry.stream_data = stream_data
        except Exception as exc:
            if not raw_fallback_for_unsupported:
                raise
            new_entry = entry.clone()
            mode = "raw-fallback"
            log_message(
                log,
                f"REBUILD FAILED for {TYPE_NAME_MAP.get(entry.type_id, entry.type_id)} {entry.file_id}: {exc}",
            )
            if entry.type_id == UnitID:
                normalize_unit_entry_from_source(
                    new_entry,
                    default_archive,
                    archive_index=archive_index,
                    log=log,
                    header_only=unit_header_only,
                    refresh_material_bindings=refresh_unit_material_bindings,
                )
    elif raw_fallback_for_unsupported:
        new_entry = entry.clone()
        mode = "raw-fallback"
        if entry.type_id == UnitID:
            normalize_unit_entry_from_source(
                new_entry,
                default_archive,
                archive_index=archive_index,
                log=log,
                header_only=unit_header_only,
                refresh_material_bindings=refresh_unit_material_bindings,
            )
    else:
        return None, None
    return new_entry, mode
#endregion Unit Repair And Rebuild


#region Dependency Resolution
def resolve_dependency_entry(
    file_id,
    type_id,
    broken_patch: StreamToc,
    default_archive: StreamToc,
    archive_index: GameArchiveIndex | None = None,
):
    entry = broken_patch.get_entry(file_id, type_id)
    if entry is not None:
        return entry, "broken patch"
    entry = default_archive.get_entry(file_id, type_id)
    if entry is not None:
        return entry, "default archive"
    if archive_index is not None:
        archive_path = archive_index.find_archive_path(file_id, type_id)
        if archive_path is not None:
            archive = StreamToc()
            if archive.from_file(str(archive_path)):
                entry = archive.get_entry(file_id, type_id)
                if entry is not None:
                    return entry, f"game archive {Path(archive_path).name}"
    return None, None


def is_inherited_current_unit_reference(
    unit_entry,
    ref_name: str,
    ref_id: int,
    default_archive: StreamToc,
    archive_index: GameArchiveIndex | None = None,
):
    """Whether a missing Unit reference already exists in the current game.

    Some shipped Units contain a nonzero reference which has no standalone
    resource of the expected type.  Railgun's auxiliary Unit is one example:
    its current-game Unit has the same self StateMachine reference, while no
    State Machine resource with that ID exists.  Treating that inherited
    vanilla layout as a hard patch error makes a valid patch impossible to
    export.  This intentionally permits it only when the current Unit with
    the *same file ID* contains the exact same reference.
    """
    current_unit, source_name = resolve_unit_source_entry(
        unit_entry.file_id,
        default_archive,
        archive_index=archive_index,
    )
    if current_unit is None:
        return False, None
    current_refs = parse_unit_refs(current_unit)
    return current_refs.get(ref_name) == ref_id, source_name


def add_unit_dependencies(
    fixed_patch: StreamToc,
    broken_patch: StreamToc,
    default_archive: StreamToc,
    archive_index: GameArchiveIndex | None,
    raw_fallback_for_unsupported: bool,
    copied_counts: dict,
    log=None,
):
    unresolved = []
    units = list(fixed_patch.toc_dict.get(UnitID, {}).values())
    dependency_specs = [
        ("bones_ref", BoneID, "Bones"),
        ("state_machine_ref", StateMachineID, "State Machine"),
        ("composite_ref", CompositeUnitID, "Composite Unit"),
    ]

    for unit_entry in units:
        refs = parse_unit_refs(unit_entry)
        for ref_name, type_id, label in dependency_specs:
            ref_id = refs[ref_name]
            if ref_id == 0:
                continue
            if fixed_patch.get_entry(ref_id, type_id) is not None:
                continue

            inherited, inherited_source = is_inherited_current_unit_reference(
                unit_entry,
                ref_name,
                ref_id,
                default_archive,
                archive_index=archive_index,
            )
            if inherited:
                log_message(
                    log,
                    f"INHERITED current-game {label} reference for unit {unit_entry.file_id}: "
                    f"{ref_id} from {inherited_source}; leaving it external.",
                )
                continue

            source_entry = broken_patch.get_entry(ref_id, type_id)
            if source_entry is None:
                external_entry, external_name = resolve_dependency_entry(
                    ref_id,
                    type_id,
                    StreamToc(),
                    default_archive,
                    archive_index=archive_index,
                )
                if external_entry is None:
                    unresolved.append((unit_entry.file_id, label, ref_id))
                    log_message(log, f"UNRESOLVED dependency for unit {unit_entry.file_id}: {label} {ref_id}")
                else:
                    log_message(
                        log,
                        f"LEAVING external {label} dependency unresolved in patch for unit {unit_entry.file_id}: "
                        f"{ref_id} from {external_name}",
                    )
                continue

            source_name = "broken patch"

            new_entry, mode = build_entry_from_source(
                source_entry,
                raw_fallback_for_unsupported=raw_fallback_for_unsupported,
                default_archive=default_archive,
                archive_index=archive_index,
                log=log,
            )
            if new_entry is None:
                unresolved.append((unit_entry.file_id, label, ref_id))
                log_message(log, f"SKIPPED dependency without fallback for unit {unit_entry.file_id}: {label} {ref_id}")
                continue

            fixed_patch.add_entry(new_entry, override=True)
            copied_counts[label] = copied_counts.get(label, 0) + 1
            log_message(log, f"AUTO {mode.upper()} {label}: {ref_id} from {source_name} for unit {unit_entry.file_id}")

    return unresolved


def validate_unit_dependencies(
    fixed_patch: StreamToc,
    default_archive: StreamToc | None = None,
    archive_index: GameArchiveIndex | None = None,
):
    unresolved = []
    dependency_specs = [
        ("bones_ref", BoneID, "Bones"),
        ("state_machine_ref", StateMachineID, "State Machine"),
        ("composite_ref", CompositeUnitID, "Composite Unit"),
    ]
    for unit_entry in fixed_patch.toc_dict.get(UnitID, {}).values():
        refs = parse_unit_refs(unit_entry)
        for ref_name, type_id, label in dependency_specs:
            ref_id = refs[ref_name]
            if ref_id == 0:
                continue
            if fixed_patch.get_entry(ref_id, type_id) is not None:
                continue
            if default_archive is not None and default_archive.get_entry(ref_id, type_id) is not None:
                continue
            if archive_index is not None and archive_index.find_archive_path(ref_id, type_id) is not None:
                continue
            if default_archive is not None:
                inherited, _source_name = is_inherited_current_unit_reference(
                    unit_entry,
                    ref_name,
                    ref_id,
                    default_archive,
                    archive_index=archive_index,
                )
                if inherited:
                    continue
            unresolved.append((unit_entry.file_id, label, ref_id))
    return unresolved
#endregion Dependency Resolution


#region Public Fix Workflows
def create_fixed_patch(
    game_data_folder: str,
    broken_patch_path: str,
    export_dir: str,
    keep_type_ids: set[int],
    keep_unknown_types: bool = True,
    raw_fallback_for_unsupported: bool = True,
    auto_include_unit_dependencies: bool = True,
    migrate_audio: bool = False,
    weapon_swap_mode: bool = True,
    output_patch_path: str | None = None,
    idswap_source_archive: str | None = None,
    log=None,
    slim_initialized: bool = False,
):
    broken_patch_path = normalize_archive_selection(broken_patch_path)
    if not Path(game_data_folder).is_dir():
        raise ValueError("Game data folder is invalid.")
    if not Path(broken_patch_path).is_file():
        raise ValueError("Broken patch file does not exist.")
    if not Path(export_dir).is_dir():
        raise ValueError("Export folder is invalid.")

    # A compressed mod can process independent patch files in parallel.  Its
    # parent initializes the Slim bundle map once before creating workers;
    # rebuilding that global read-only map inside every worker is wasteful.
    if not slim_initialized:
        slim_init(game_data_folder)
    archive_index = (
        GameArchiveIndex(game_data_folder)
        if auto_include_unit_dependencies or migrate_audio or weapon_swap_mode
        else None
    )

    default_archive_path = str(Path(game_data_folder) / BaseArchiveHexID)
    log_message(log, f"Loading default archive: {default_archive_path}")
    default_archive = StreamToc()
    if not default_archive.from_file(default_archive_path):
        raise ValueError("Failed to load default archive from the selected game folder.")

    log_message(log, f"Loading broken patch: {broken_patch_path}")
    broken_patch = StreamToc()
    if not broken_patch.from_file(broken_patch_path):
        raise ValueError("Failed to load the selected broken patch.")

    # If a patch ships Material resources, its material choices are authored
    # content and must never be replaced by a vanilla lookup.  A patch with no
    # Material entries can safely refresh only verified same-ID Unit bindings
    # when the game moved a vanilla material SectionID between versions.
    refresh_unit_material_bindings = not bool(broken_patch.toc_dict.get(MaterialID))

    idswap_source_archives = []
    idswap_source_matches = {}
    if idswap_source_archive:
        idswap_source_paths = resolve_archive_input_paths(game_data_folder, idswap_source_archive)
        if not idswap_source_paths:
            raise ValueError("No valid ID swap source archive IDs were provided.")
        for idswap_source_path in idswap_source_paths:
            source_name = f"game archive {Path(idswap_source_path).name}"
            log_message(log, f"Loading ID swap source archive: {idswap_source_path}")
            source_archive = StreamToc()
            if not source_archive.from_file(str(idswap_source_path)):
                raise ValueError(
                    f"Failed to load the selected ID swap source archive: {idswap_source_path}"
                )
            idswap_source_archives.append(
                {
                    "path": idswap_source_path,
                    "name": source_name,
                    "archive": source_archive,
                    "fingerprints": {
                        int(candidate.file_id): get_unit_fingerprint(candidate)
                        for candidate in source_archive.toc_dict.get(UnitID, {}).values()
                    },
                }
            )

    # Rigged weapon swaps need their source Unit only to obtain the current
    # schema version.  The mod's rig, LOD, mesh, GPU, and stream data stays in
    # the patch.  Static armor/helmet swaps are handled separately below by
    # resolving their current *target* Unit by ID.
    weapon_mappings = []
    if weapon_swap_mode and archive_index is not None:
        mapping_candidates = infer_weapon_idswap_mappings(broken_patch, archive_index)
        # A manually supplied archive remains useful as an advanced hint when
        # a mod references data which is not visible in the normal game index.
        for source_info in idswap_source_archives:
            mapping_candidates.extend(
                infer_weapon_idswap_mappings(broken_patch, source_info["archive"])
            )

        candidates_by_target = {}
        for mapping in mapping_candidates:
            candidates_by_target.setdefault(mapping.target_unit_id, []).append(mapping)
        for target_id, candidates in sorted(candidates_by_target.items()):
            unique_sources = {
                (candidate.source_unit_id, bytes(candidate.source_entry.toc_data))
                for candidate in candidates
            }
            if len(unique_sources) != 1:
                log_message(
                    log,
                    f"UNIT IDSWAP ambiguous rig source for Unit {target_id}; leaving it out of automatic migration.",
                )
                continue
            chosen = sorted(
                candidates,
                key=lambda candidate: (candidate.source_archive_path, candidate.source_unit_id),
            )[0]
            weapon_mappings.append(chosen)
            log_message(
                log,
                f"UNIT IDSWAP verified rigged Unit {chosen.target_unit_id} -> {chosen.source_unit_id} "
                f"from {chosen.source_name}; preserving patch rig, LOD, and GPU data.",
            )

    weapon_mapping_by_target = {
        mapping.target_unit_id: mapping for mapping in weapon_mappings
    }

    # Static armor and helmet Units normally have no Bone/Composite/State
    # Machine references, so a weapon-style source lookup is impossible.  The
    # Community Unit Patcher instead resolves the current Unit with the same
    # target file ID and refreshes only its LOD group.  This needs no manual
    # archive input and never copies mesh/bone/GPU data from the game.
    static_target_entries = {}
    if weapon_swap_mode:
        for unit_entry in broken_patch.toc_dict.get(UnitID, {}).values():
            target_id = int(unit_entry.file_id)
            if target_id in weapon_mapping_by_target or not is_static_unit(unit_entry):
                continue
            target_entry, target_name = resolve_unit_source_entry(
                target_id,
                default_archive,
                archive_index=archive_index,
            )
            if target_entry is None:
                log_message(
                    log,
                    f"STATIC UNIT target {target_id} is not present in current game data; preserving it without a target LOD refresh.",
                )
                continue
            # Static migration reads only the target Unit TOC bytes.  Retain a
            # tiny schema-only entry instead of several large base-game GPU
            # buffers for a multi-piece armor patch.
            schema_entry = TocEntry()
            schema_entry.file_id = int(target_entry.file_id)
            schema_entry.type_id = int(target_entry.type_id)
            schema_entry.toc_data = bytes(target_entry.toc_data)
            static_target_entries[target_id] = {
                "entry": schema_entry,
                "name": target_name,
            }
            log_message(
                log,
                f"STATIC UNIT target verified for {target_id} from {target_name}; "
                "will preserve patch mesh/bone/GPU data and refresh only target LOD/schema.",
            )

    # Keep a supplied archive as a per-Unit advanced fallback.  It must not
    # disable automatic migration for other Units in a mixed weapon/armor mod.
    if idswap_source_archives:
        for unit_entry in broken_patch.toc_dict.get(UnitID, {}).values():
            target_id = int(unit_entry.file_id)
            if target_id in weapon_mapping_by_target or target_id in static_target_entries:
                continue
            match = match_unit_to_source_archives(
                unit_entry,
                idswap_source_archives,
            )
            if match is None:
                log_message(log, f"IDSWAP source match not found for Unit {unit_entry.file_id}")
                continue
            matched_entry = match["entry"]
            matched_similarity = match["similarity"]
            matched_source_name = match["name"]
            if matched_similarity.score < IDSWAP_SOURCE_MATCH_MIN_SCORE:
                log_message(
                    log,
                    f"IDSWAP source hint ignored for Unit {unit_entry.file_id}: "
                    f"best source entry {matched_entry.file_id} scored "
                    f"{matched_similarity.score} (< {IDSWAP_SOURCE_MATCH_MIN_SCORE}); "
                    "using the current game Unit with the same ID instead.",
                )
                continue
            idswap_source_matches[int(unit_entry.file_id)] = {
                "entry": matched_entry,
                "name": matched_source_name,
            }
            log_message(
                log,
                f"IDSWAP source matched for Unit {unit_entry.file_id}: "
                f"{matched_entry.file_id} from {matched_source_name} "
                f"(score {matched_similarity.score}; {', '.join(matched_similarity.reasons)})",
            )

    # Static patches used to be passed through completely because their source
    # Unit cannot be inferred from zero references.  They are now handled by
    # ``static_target_entries`` above, using the current target Unit instead.
    unit_passthrough_mode = False
    unit_header_only_mode = False
    full_passthrough_mode = False

    patch_index = detect_patch_index_from_name(broken_patch_path)
    output_path = output_patch_path or resolve_output_path(export_dir, patch_index)
    fixed_patch = build_patch_template(default_archive, output_path)

    migrated_audio_entries = {}
    if migrate_audio and WwiseBankID in keep_type_ids and archive_index is not None:
        log_message(
            log,
            "COMMUNITY AUDIO: Aggressive semantic migration enabled. "
            "This can reapply old vanilla Wwise media if the mod was built on an older game version.",
        )
        migrated_audio_entries = migrate_patch_audio_from_current_game_archives(
            game_data_folder=game_data_folder,
            broken_patch_path=broken_patch_path,
            broken_patch=broken_patch,
            archive_index=archive_index,
            log=log,
        )

    kept_entries = 0
    skipped_entries = 0
    copied_counts = {}

    for type_id, entries in broken_patch.toc_dict.items():
        should_keep = type_id in keep_type_ids or (keep_unknown_types and type_id not in TYPE_NAME_MAP)
        label = TYPE_NAME_MAP.get(type_id, f"Unknown ({type_id})")
        if not should_keep:
            skipped_entries += len(entries)
            log_message(log, f"Skipping {label}: {len(entries)} entries")
            continue

        for entry in entries.values():
            if full_passthrough_mode:
                new_entry, mode = entry.clone(), "raw-preserve"
            elif entry.type_id == UnitID and int(entry.file_id) in weapon_mapping_by_target:
                source_mapping = weapon_mapping_by_target[int(entry.file_id)]
                new_entry = migrate_weapon_idswap_unit(entry, source_mapping.source_entry)
                mode = "rigged-unit-idswap-surgical"
            elif entry.type_id == UnitID and int(entry.file_id) in static_target_entries:
                static_target = static_target_entries[int(entry.file_id)]
                try:
                    new_entry = migrate_static_unit_idswap_unit(
                        entry,
                        static_target["entry"],
                    )
                    mode = "static-unit-target-lod-surgical"
                except Exception as exc:
                    # The patch is more valuable intact than a guessed
                    # normalization.  Leave unusual static Units untouched
                    # and give the user a clear log entry instead.
                    new_entry = entry.clone()
                    mode = "static-unit-raw-fallback"
                    log_message(
                        log,
                        f"STATIC UNIT migration failed for {entry.file_id} from {static_target['name']}: {exc}; preserving patch bytes.",
                    )
            elif entry.type_id == UnitID and int(entry.file_id) in idswap_source_matches:
                # Manual source selection is now surgical too.  Rebuilding a
                # source Unit can overwrite custom armor/weapon transforms,
                # bone information, and LOD metadata.
                new_entry = migrate_weapon_idswap_unit(
                    entry,
                    idswap_source_matches[int(entry.file_id)]["entry"],
                )
                mode = "manual-unit-idswap-surgical"
            else:
                new_entry, mode = build_entry_from_source(
                    entry,
                    raw_fallback_for_unsupported=raw_fallback_for_unsupported,
                    default_archive=default_archive,
                    archive_index=archive_index,
                    log=log,
                    unit_header_only=unit_header_only_mode,
                    unit_passthrough=unit_passthrough_mode,
                    refresh_unit_material_bindings=refresh_unit_material_bindings,
                )
            if new_entry is None:
                skipped_entries += 1
                log_message(log, f"Skipping unsupported type without raw fallback: {label} entry {entry.file_id}")
                continue

            fixed_patch.add_entry(new_entry, override=True)
            kept_entries += 1
            copied_counts[label] = copied_counts.get(label, 0) + 1
            log_message(log, f"{mode.upper()} {label}: {entry.file_id}")

    # Add the complete Bank/Dep/Stream set generated by the community engine
    # after ordinary copying.  This includes dependencies which were implicit
    # in the original mod but are required by the current game archive.
    for (type_id, file_id), entry in migrated_audio_entries.items():
        should_keep = type_id in keep_type_ids or (
            keep_unknown_types and type_id not in TYPE_NAME_MAP
        )
        if not should_keep:
            continue
        existing = fixed_patch.get_entry(file_id, type_id)
        fixed_patch.add_entry(entry.clone(), override=True)
        label = TYPE_NAME_MAP.get(type_id, f"Unknown ({type_id})")
        if existing is None:
            kept_entries += 1
            copied_counts[label] = copied_counts.get(label, 0) + 1
        log_message(log, f"AUDIO-COMMUNITY-MIGRATED {label}: {file_id}")

    audio_report = inspect_audio_collection(fixed_patch.toc_dict)
    for line in audio_report.log_lines():
        log_message(log, line)

    dependency_issues = []
    if auto_include_unit_dependencies and UnitID in fixed_patch.toc_dict:
        dependency_issues = add_unit_dependencies(
            fixed_patch,
            broken_patch,
            default_archive,
            archive_index=archive_index,
            raw_fallback_for_unsupported=raw_fallback_for_unsupported,
            copied_counts=copied_counts,
            log=log,
        )

    unresolved_after_build = validate_unit_dependencies(
        fixed_patch,
        default_archive=default_archive,
        archive_index=archive_index,
    )
    if unresolved_after_build:
        lines = [
            f"Unit {unit_id} is missing {label} dependency {ref_id}"
            for unit_id, label, ref_id in unresolved_after_build
        ]
        raise ValueError(
            "Patch structure validation failed. Missing unit dependencies:\n"
            + "\n".join(lines)
        )

    log_message(log, f"Writing fixed patch: {output_path}")
    fixed_patch.to_file(output_path)

    return {
        "output_path": output_path,
        "kept_entries": kept_entries,
        "skipped_entries": skipped_entries,
        "copied_counts": copied_counts,
        "source_counts": broken_patch.entry_counts(),
        "dependency_issues_resolved_or_seen": dependency_issues,
        "unit_idswap_mappings": [
            {
                "target_unit_id": mapping.target_unit_id,
                "source_unit_id": mapping.source_unit_id,
                "source_archive_path": mapping.source_archive_path,
                "mode": "rigged-source",
            }
            for mapping in weapon_mappings
        ] + [
            {
                "target_unit_id": target_id,
                "source_unit_id": target_id,
                "source_archive_path": source["name"],
                "mode": "static-target",
            }
            for target_id, source in sorted(static_target_entries.items())
        ],
        # Kept for integrations written against the previous weapon-only
        # result shape.  New callers should use ``unit_idswap_mappings``.
        "weapon_idswap_mappings": [
            {
                "target_unit_id": mapping.target_unit_id,
                "source_unit_id": mapping.source_unit_id,
                "source_archive_path": mapping.source_archive_path,
            }
            for mapping in weapon_mappings
        ],
    }


def create_fixed_mod_archive(
    game_data_folder: str,
    input_archive_path: str,
    output_zip_path: str,
    keep_type_ids: set[int],
    keep_unknown_types: bool = True,
    raw_fallback_for_unsupported: bool = True,
    auto_include_unit_dependencies: bool = True,
    migrate_audio: bool = False,
    weapon_swap_mode: bool = True,
    idswap_source_archive: str | None = None,
    log=None,
    max_workers: int = 1,
):
    if not Path(game_data_folder).is_dir():
        raise ValueError("Game data folder is invalid.")
    if not Path(input_archive_path).is_file():
        raise ValueError("Compressed mod file does not exist.")
    if Path(output_zip_path).suffix.lower() != ".zip":
        raise ValueError("Export file must be a .zip file.")
    if not isinstance(max_workers, int) or max_workers < 1:
        raise ValueError("Parallel patch count must be a positive integer.")

    # Slim package metadata is global in the community reader.  Build it once
    # before worker threads begin; all patch workers only read it afterwards.
    slim_init(game_data_folder)

    with tempfile.TemporaryDirectory(prefix="hd2_patch_fixer_") as temp_dir:
        extract_dir = str(Path(temp_dir) / "mod")
        Path(extract_dir).mkdir(parents=True, exist_ok=True)

        log_message(log, f"Extracting compressed mod: {input_archive_path}")
        extract_archive_file(input_archive_path, extract_dir)

        patch_paths, incomplete_groups = find_patch_groups(extract_dir)
        for group in incomplete_groups:
            log_message(log, f"SKIP incomplete patch group without base file: {group['base_path']}")

        if not patch_paths:
            raise ValueError("No patch files were found inside the compressed mod archive.")

        fixed_patch_results = []
        log_message(log, f"Found {len(patch_paths)} patch file(s) inside compressed mod archive.")

        worker_count = min(max_workers, len(patch_paths))
        if worker_count > 1:
            log_message(log, f"Processing up to {worker_count} patch files in parallel.")

        def fix_one_patch(patch_path):
            relative_path = Path(patch_path).relative_to(extract_dir)
            log_message(log, f"Fixing patch inside archive: {relative_path}")
            result = create_fixed_patch(
                game_data_folder=game_data_folder,
                broken_patch_path=patch_path,
                export_dir=str(Path(patch_path).parent),
                keep_type_ids=keep_type_ids,
                keep_unknown_types=keep_unknown_types,
                raw_fallback_for_unsupported=raw_fallback_for_unsupported,
                auto_include_unit_dependencies=auto_include_unit_dependencies,
                migrate_audio=migrate_audio,
                weapon_swap_mode=weapon_swap_mode,
                output_patch_path=patch_path,
                idswap_source_archive=idswap_source_archive,
                log=log,
                slim_initialized=True,
            )
            return {
                "relative_path": str(relative_path).replace("\\", "/"),
                "output_path": result["output_path"],
                "kept_entries": result["kept_entries"],
                "skipped_entries": result["skipped_entries"],
            }

        if worker_count == 1:
            for patch_path in patch_paths:
                fixed_patch_results.append(fix_one_patch(patch_path))
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(fix_one_patch, patch_path) for patch_path in patch_paths]
                for future in as_completed(futures):
                    fixed_patch_results.append(future.result())

        fixed_patch_results.sort(key=lambda result: result["relative_path"])

        log_message(log, f"Creating fixed compressed mod zip: {output_zip_path}")
        create_zip_from_directory(extract_dir, output_zip_path)

    return {
        "output_path": output_zip_path,
        "fixed_patch_count": len(fixed_patch_results),
        "patch_results": fixed_patch_results,
        "incomplete_patch_groups": incomplete_groups,
    }
#endregion Public Fix Workflows
