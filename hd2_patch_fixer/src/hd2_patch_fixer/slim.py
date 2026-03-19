import os
import struct
from dataclasses import dataclass, field

import lz4.block


CONTINUE = 0x04
START = 0x02
COMPRESSED = 0x03

LEGACY = 3
BUNDLED = 2
DSAR = 1

package_contents = {}
bundle_offsets = {}
game_data_folder = ""


def read_int(file_obj):
    return int.from_bytes(file_obj.read(4), "little")


def read_long(file_obj):
    return int.from_bytes(file_obj.read(8), "little")


def to_int(byte_data):
    return int.from_bytes(byte_data, "little")


@dataclass
class BundleEntry:
    start_offset: int = 0
    bundle_index: int = 0
    original_archive_offset: int = 0


@dataclass
class Package:
    size: int = 0
    name: str = ""
    entries: list[BundleEntry] = field(default_factory=list)


def slim_init(file_path: str):
    global game_data_folder
    game_data_folder = file_path
    if is_slim_version():
        init_bundle_mapping()


def is_slim_version():
    return not os.path.exists(os.path.join(game_data_folder, "9ba626afa44a3aa3"))


def decompress_dsar(file_path):
    with open(file_path, "rb") as bundle:
        bundle.seek(8)
        num_chunks = read_int(bundle)
        data = []
        for i in range(num_chunks):
            bundle.seek(0x20 + i * 0x20)
            uncompressed_offset = read_long(bundle)
            compressed_offset = read_long(bundle)
            uncompressed_size = read_int(bundle)
            compressed_size = read_int(bundle)
            compression_type = int.from_bytes(bundle.read(1), "little")
            chunk_type = int.from_bytes(bundle.read(1), "little")

            bundle.seek(compressed_offset)
            temp_data = bundle.read(compressed_size)
            if compression_type == COMPRESSED:
                temp_data = lz4.block.decompress(
                    temp_data,
                    uncompressed_size=uncompressed_size,
                )
            data.append(temp_data)
    return b"".join(data)


def get_resource_from_bundle(bundle_path: str, resource_file_offset: int):
    bundle_name = os.path.basename(bundle_path)
    chunk_num = bundle_offsets[bundle_name][resource_file_offset]

    with open(bundle_path, "rb") as bundle:
        bundle.seek(8)
        num_chunks = read_int(bundle)
        data = []

        while True:
            bundle.seek(0x20 + 0x20 * chunk_num)
            values = struct.unpack("<QQIIBB6x", bundle.read(0x20))
            (
                _uncompressed_offset,
                compressed_offset,
                uncompressed_size,
                compressed_size,
                compression_type,
                chunk_type,
            ) = values

            if chunk_type & START and data:
                return b"".join(data)

            bundle.seek(compressed_offset)
            temp_data = bundle.read(compressed_size)
            if compression_type == COMPRESSED:
                temp_data = lz4.block.decompress(
                    temp_data,
                    uncompressed_size=uncompressed_size,
                )
            data.append(temp_data)

            if chunk_num == num_chunks - 1:
                return b"".join(data)

            chunk_num += 1


def init_bundle_mapping():
    bundle_contents = decompress_dsar(os.path.join(game_data_folder, "bundles.nxa"))
    num_packages = to_int(bundle_contents[0x10:0x14])

    global package_contents
    global bundle_offsets
    package_contents = {}
    bundle_offsets = {}

    for filename in os.listdir(game_data_folder):
        full_path = os.path.join(game_data_folder, filename)
        if os.path.isdir(full_path):
            continue
        if ".patch" in filename:
            continue
        if os.path.splitext(filename)[1] not in ["", ".stream", ".nxa", ".gpu_resources"]:
            continue
        bundle_name = os.path.basename(filename)
        bundle_offsets[bundle_name] = {}
        with open(full_path, "rb") as bundle:
            bundle.seek(8)
            num_chunks = read_int(bundle)
            bundle.seek(0x20)
            offsets = struct.unpack(f"<{'Q24x' * num_chunks}", bundle.read(0x20 * num_chunks))
            for index, offset in enumerate(offsets):
                bundle_offsets[bundle_name][offset] = index

    for n in range(num_packages):
        bundle_location = 0x18 + n * 0x18
        bundle_size = to_int(bundle_contents[bundle_location:bundle_location + 8])
        name_offset = to_int(bundle_contents[bundle_location + 8:bundle_location + 12])

        name_end = name_offset
        while bundle_contents[name_end] != 0:
            name_end += 1
        name = bundle_contents[name_offset:name_end].decode()

        items_count = to_int(bundle_contents[bundle_location + 12:bundle_location + 16])
        items_offset = to_int(bundle_contents[bundle_location + 16:bundle_location + 20])

        package = package_contents.setdefault(name, Package(size=bundle_size, name=name))
        for i in range(items_count):
            offset = items_offset + 0x10 * i
            original_archive_offset = to_int(bundle_contents[offset:offset + 8])
            uncompressed_bundle_offset = to_int(bundle_contents[offset + 8:offset + 12])
            bundle_index = bundle_contents[offset + 0x0F]
            package.entries.append(
                BundleEntry(
                    start_offset=uncompressed_bundle_offset,
                    bundle_index=bundle_index,
                    original_archive_offset=original_archive_offset,
                )
            )


def get_resources_from_bundle(bundle_path: str, start_offset: int, size: int):
    current_size = 0
    resources = []
    while current_size < size:
        resource = get_resource_from_bundle(bundle_path, start_offset + current_size)
        current_size += len(resource)
        resources.append(resource)
    return resources


def reconstruct_package_from_bundles(package_name: str):
    package_name = os.path.basename(package_name)
    package = package_contents.get(package_name)
    if package is None:
        return bytearray()

    package_data = bytearray(package.size)
    for index, item in enumerate(package.entries):
        if index + 1 < len(package.entries):
            item_size = (
                package.entries[index + 1].original_archive_offset - item.original_archive_offset
            )
        else:
            item_size = package.size - item.original_archive_offset

        bundle_path = os.path.join(game_data_folder, f"bundles.{item.bundle_index:02d}.nxa")
        resources = get_resources_from_bundle(bundle_path, item.start_offset, item_size)
        combined_data = b"".join(resources)
        start = item.original_archive_offset
        package_data[start:start + len(combined_data)] = combined_data
    return package_data


def get_package_toc(package_name: str):
    package_name = os.path.basename(package_name)
    full_path = os.path.join(game_data_folder, package_name)

    if os.path.exists(full_path):
        with open(full_path, "rb") as file_obj:
            magic = int.from_bytes(file_obj.read(4), "little")
            package_type = DSAR if magic == 1380012868 else LEGACY
    else:
        package_type = BUNDLED

    if package_type == BUNDLED:
        package = package_contents.get(package_name)
        if package is None or not package.entries:
            return bytearray()
        bundle_path = os.path.join(game_data_folder, f"bundles.{package.entries[0].bundle_index:02d}.nxa")
        return get_resource_from_bundle(bundle_path, package.entries[0].start_offset)

    if package_type == DSAR:
        return get_resource_from_bundle(full_path, 0x00)

    with open(full_path, "rb") as package_file:
        header = package_file.read(12)
        magic, num_types, num_files = struct.unpack("<III", header)
        if magic != 4026531857:
            return bytearray()
        package_file.seek(0)
        return package_file.read(72 + num_types * 32 + num_files * 80)


def load_package(package_path: str):
    if not os.path.dirname(package_path):
        package_path = os.path.join(game_data_folder, package_path)

    if os.path.exists(package_path):
        with open(package_path, "rb") as file_obj:
            magic = int.from_bytes(file_obj.read(4), "little")
            package_type = DSAR if magic == 1380012868 else LEGACY
    else:
        package_type = BUNDLED

    toc_data = bytearray()
    gpu_data = bytearray()
    stream_data = bytearray()

    if package_type == BUNDLED:
        content = reconstruct_package_from_bundles(package_path)
        if content:
            toc_data = content
        content = reconstruct_package_from_bundles(f"{package_path}.gpu_resources")
        if content:
            gpu_data = content
        content = reconstruct_package_from_bundles(f"{package_path}.stream")
        if content:
            stream_data = content
    elif package_type == DSAR:
        toc_data = decompress_dsar(package_path)
        gpu_path = package_path + ".gpu_resources"
        stream_path = package_path + ".stream"
        if os.path.exists(gpu_path):
            gpu_data = decompress_dsar(gpu_path)
        if os.path.exists(stream_path):
            stream_data = decompress_dsar(stream_path)
    else:
        with open(package_path, "rb") as file_obj:
            toc_data = file_obj.read()
        gpu_path = package_path + ".gpu_resources"
        stream_path = package_path + ".stream"
        if os.path.exists(gpu_path):
            with open(gpu_path, "rb") as file_obj:
                gpu_data = file_obj.read()
        if os.path.exists(stream_path):
            with open(stream_path, "rb") as file_obj:
                stream_data = file_obj.read()

    return toc_data, gpu_data, stream_data
