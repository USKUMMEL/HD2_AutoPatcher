import copy
import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from math import ceil
from pathlib import Path
import zipfile

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
)
from .memory_stream import MemoryStream
from .parsers import (
    StingrayBones,
    StingrayMaterial,
    StingrayParticles,
    StingrayStateMachine,
    StingrayTexture,
)
from .slim import get_package_toc, is_slim_version, load_package, slim_init


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
        asset = StingrayParticles()
        asset.serialize(MemoryStream(entry.toc_data))
        toc = MemoryStream(entry.toc_data, io_mode="write")
        asset.serialize(toc)
        return bytes(toc.data), b"", b"", "rebuilt"

    if entry.type_id == StateMachineID:
        asset = StingrayStateMachine()
        asset.serialize(MemoryStream(entry.toc_data))
        toc = MemoryStream(io_mode="write")
        asset.serialize(toc)
        return bytes(toc.data), b"", b"", "rebuilt"

    return bytes(entry.toc_data), bytes(entry.gpu_data), bytes(entry.stream_data), "raw"


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


def normalize_unit_entry_from_source(
    entry,
    default_archive: StreamToc,
    archive_index: GameArchiveIndex | None = None,
    log=None,
):
    source_entry, source_name = resolve_unit_source_entry(
        entry.file_id,
        default_archive,
        archive_index=archive_index,
    )
    if source_entry is None:
        return False
    if len(entry.toc_data) < 88 or len(source_entry.toc_data) < 88:
        return False

    normalized = bytearray(entry.toc_data)
    source_toc = bytes(source_entry.toc_data)
    normalized[40:88] = source_toc[40:88]
    entry.toc_data = bytes(normalized)
    log_message(log, f"NORMALIZED Unit structural segment 40..88 from {source_name}: {entry.file_id}")
    return True


def build_entry_from_source(
    entry,
    raw_fallback_for_unsupported: bool,
    default_archive: StreamToc,
    archive_index: GameArchiveIndex | None = None,
    log=None,
):
    mode = "raw"
    if entry.type_id in SUPPORTED_REBUILD_TYPES:
        toc_data, gpu_data, stream_data, mode = rebuild_entry_payload(entry)
        new_entry = entry.clone()
        new_entry.toc_data = toc_data
        new_entry.gpu_data = gpu_data
        new_entry.stream_data = stream_data
    elif raw_fallback_for_unsupported:
        new_entry = entry.clone()
        mode = "raw-fallback"
        if entry.type_id == UnitID:
            normalize_unit_entry_from_source(
                new_entry,
                default_archive,
                archive_index=archive_index,
                log=log,
            )
    else:
        return None, None
    return new_entry, mode


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

            source_entry, source_name = resolve_dependency_entry(
                ref_id,
                type_id,
                broken_patch,
                default_archive,
                archive_index=archive_index,
            )
            if source_entry is None:
                unresolved.append((unit_entry.file_id, label, ref_id))
                log_message(log, f"UNRESOLVED dependency for unit {unit_entry.file_id}: {label} {ref_id}")
                continue

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


def validate_unit_dependencies(fixed_patch: StreamToc):
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
            if fixed_patch.get_entry(ref_id, type_id) is None:
                unresolved.append((unit_entry.file_id, label, ref_id))
    return unresolved


def create_fixed_patch(
    game_data_folder: str,
    broken_patch_path: str,
    export_dir: str,
    keep_type_ids: set[int],
    keep_unknown_types: bool = True,
    raw_fallback_for_unsupported: bool = False,
    auto_include_unit_dependencies: bool = True,
    output_patch_path: str | None = None,
    log=None,
):
    broken_patch_path = normalize_archive_selection(broken_patch_path)
    if not Path(game_data_folder).is_dir():
        raise ValueError("Game data folder is invalid.")
    if not Path(broken_patch_path).is_file():
        raise ValueError("Broken patch file does not exist.")
    if not Path(export_dir).is_dir():
        raise ValueError("Export folder is invalid.")

    slim_init(game_data_folder)
    archive_index = GameArchiveIndex(game_data_folder) if auto_include_unit_dependencies else None

    default_archive_path = str(Path(game_data_folder) / BaseArchiveHexID)
    log_message(log, f"Loading default archive: {default_archive_path}")
    default_archive = StreamToc()
    if not default_archive.from_file(default_archive_path):
        raise ValueError("Failed to load default archive from the selected game folder.")

    log_message(log, f"Loading broken patch: {broken_patch_path}")
    broken_patch = StreamToc()
    if not broken_patch.from_file(broken_patch_path):
        raise ValueError("Failed to load the selected broken patch.")

    patch_index = detect_patch_index_from_name(broken_patch_path)
    output_path = output_patch_path or resolve_output_path(export_dir, patch_index)
    fixed_patch = build_patch_template(default_archive, output_path)

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
            new_entry, mode = build_entry_from_source(
                entry,
                raw_fallback_for_unsupported=raw_fallback_for_unsupported,
                default_archive=default_archive,
                archive_index=archive_index,
                log=log,
            )
            if new_entry is None:
                skipped_entries += 1
                log_message(log, f"Skipping unsupported type without raw fallback: {label} entry {entry.file_id}")
                continue

            fixed_patch.add_entry(new_entry, override=True)
            kept_entries += 1
            copied_counts[label] = copied_counts.get(label, 0) + 1
            log_message(log, f"{mode.upper()} {label}: {entry.file_id}")

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

    unresolved_after_build = validate_unit_dependencies(fixed_patch)
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
    }


def create_fixed_mod_archive(
    game_data_folder: str,
    input_archive_path: str,
    output_zip_path: str,
    keep_type_ids: set[int],
    keep_unknown_types: bool = True,
    raw_fallback_for_unsupported: bool = False,
    auto_include_unit_dependencies: bool = True,
    log=None,
):
    if not Path(game_data_folder).is_dir():
        raise ValueError("Game data folder is invalid.")
    if not Path(input_archive_path).is_file():
        raise ValueError("Compressed mod file does not exist.")
    if Path(output_zip_path).suffix.lower() != ".zip":
        raise ValueError("Export file must be a .zip file.")

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

        for patch_path in patch_paths:
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
                output_patch_path=patch_path,
                log=log,
            )
            fixed_patch_results.append(
                {
                    "relative_path": str(relative_path).replace("\\", "/"),
                    "output_path": result["output_path"],
                    "kept_entries": result["kept_entries"],
                    "skipped_entries": result["skipped_entries"],
                }
            )

        log_message(log, f"Creating fixed compressed mod zip: {output_zip_path}")
        create_zip_from_directory(extract_dir, output_zip_path)

    return {
        "output_path": output_zip_path,
        "fixed_patch_count": len(fixed_patch_results),
        "patch_results": fixed_patch_results,
        "incomplete_patch_groups": incomplete_groups,
    }
