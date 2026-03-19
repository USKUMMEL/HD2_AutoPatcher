from .memory_stream import MemoryStream


class StingrayMipmapInfo:
    def __init__(self):
        self.start = 0
        self.bytes_left = 0
        self.height = 0
        self.width = 0

    def serialize(self, toc):
        self.start = toc.uint32(self.start)
        self.bytes_left = toc.uint32(self.bytes_left)
        self.height = toc.uint16(self.height)
        self.width = toc.uint16(self.width)
        return self


class StingrayTexture:
    def __init__(self):
        self.unk_id = 0
        self.unk1 = 0
        self.unk2 = 0
        self.mipmap_info = []
        self.dds_header = bytearray(148)
        self.raw_tex = b""
        self.format = ""
        self.width = 0
        self.height = 0
        self.num_mipmaps = 0
        self.array_size = 0

    def serialize(self, toc: MemoryStream, gpu: MemoryStream, stream: MemoryStream):
        if toc.is_writing():
            self.unk1 = 0
            self.unk2 = 0xFFFFFFFF
            self.mipmap_info = [StingrayMipmapInfo() for _ in range(15)]

        self.unk_id = toc.uint32(self.unk_id)
        self.unk1 = toc.uint32(self.unk1)
        self.unk2 = toc.uint32(self.unk2)
        if toc.is_reading():
            self.mipmap_info = [StingrayMipmapInfo() for _ in range(15)]
        self.mipmap_info = [mipmap.serialize(toc) for mipmap in self.mipmap_info]
        self.dds_header = toc.bytes(self.dds_header, 148)
        self.parse_dds_header()

        if toc.is_writing():
            gpu.bytes(self.raw_tex)
        elif len(stream.data) > 0:
            self.raw_tex = stream.data
        else:
            self.raw_tex = gpu.data

    def parse_dds_header(self):
        dds = MemoryStream(self.dds_header, io_mode="read")
        dds.seek(84)
        header = dds.read(4)
        if header != b"DX10":
            raise ValueError(f"DDS must use dx10 extended header. Got: {header}")
        dds.seek(12)
        self.height = dds.uint32(0)
        self.width = dds.uint32(0)
        dds.seek(28)
        self.num_mipmaps = dds.uint32(0)
        dds.seek(128)
        self.format = dxgi_format(dds.uint32(0))
        dds.seek(140)
        self.array_size = dds.uint32(0)


def dxgi_format(value):
    formats = {
        71: "BC1_UNORM",
        74: "BC2_UNORM",
        77: "BC3_UNORM",
        80: "BC4_UNORM",
        83: "BC5_UNORM",
        95: "BC6H_UF16",
        98: "BC7_UNORM",
        99: "BC7_UNORM_SRGB",
    }
    return formats.get(value, "UNKNOWN")


class ShaderVariable:
    def __init__(self):
        self.klass = 0
        self.klass_name = ""
        self.elements = 0
        self.variable_id = 0
        self.offset = 0
        self.element_stride = 0
        self.values = []


class StingrayMaterial:
    def __init__(self):
        self.undat1 = bytearray()
        self.end_offset = 0
        self.undat2 = 0
        self.parent_material_id = 0
        self.undat3 = bytearray()
        self.num_textures = 0
        self.undat4 = bytearray()
        self.num_variables = 0
        self.undat5 = bytearray()
        self.variable_data_size = 0
        self.undat6 = bytearray()
        self.tex_unks = []
        self.tex_ids = []
        self.shader_variables = []
        self.remaining_data = bytearray()

    def serialize(self, stream: MemoryStream):
        self.undat1 = stream.bytes(self.undat1, 12)
        self.end_offset = stream.uint32(self.end_offset)
        self.undat2 = stream.uint64(self.undat2)
        self.parent_material_id = stream.uint64(self.parent_material_id)
        self.undat3 = stream.bytes(self.undat3, 32)
        self.num_textures = stream.uint32(self.num_textures)
        self.undat4 = stream.bytes(self.undat4, 36)
        self.num_variables = stream.uint32(self.num_variables)
        self.undat5 = stream.bytes(self.undat5, 12)
        self.variable_data_size = stream.uint32(self.variable_data_size)
        self.undat6 = stream.bytes(self.undat6, 12)

        if stream.is_reading():
            self.tex_unks = [0 for _ in range(self.num_textures)]
            self.tex_ids = [0 for _ in range(self.num_textures)]
            self.shader_variables = [ShaderVariable() for _ in range(self.num_variables)]

        self.tex_unks = [stream.uint32(value) for value in self.tex_unks]
        self.tex_ids = [stream.uint64(value) for value in self.tex_ids]

        for variable in self.shader_variables:
            variable.klass = stream.uint32(variable.klass)
            variable.elements = stream.uint32(variable.elements)
            variable.variable_id = stream.uint32(variable.variable_id)
            variable.offset = stream.uint32(variable.offset)
            variable.element_stride = stream.uint32(variable.element_stride)
            if stream.is_reading():
                variable.values = [0 for _ in range(variable.klass + 1)]

        value_location = stream.location
        if stream.is_reading():
            self.remaining_data = stream.bytes(self.remaining_data, len(stream.data) - stream.tell())
        else:
            self.remaining_data = stream.bytes(self.remaining_data)
        stream.location = value_location

        for variable in self.shader_variables:
            old_location = stream.location
            stream.location = stream.location + variable.offset
            for index in range(len(variable.values)):
                variable.values[index] = stream.float32(variable.values[index])
            stream.location = old_location


class StingrayBones:
    def __init__(self):
        self.num_names = 0
        self.num_lod_levels = 0
        self.unk_array1 = []
        self.bone_hashes = []
        self.lod_levels = []
        self.names = []

    def serialize(self, stream: MemoryStream):
        self.num_names = stream.uint32(self.num_names)
        self.num_lod_levels = stream.uint32(self.num_lod_levels)
        if stream.is_reading():
            self.unk_array1 = [0 for _ in range(self.num_lod_levels)]
            self.bone_hashes = [0 for _ in range(self.num_names)]
            self.lod_levels = [0 for _ in range(self.num_lod_levels)]
        self.unk_array1 = [stream.float32(value) for value in self.unk_array1]
        self.bone_hashes = [stream.uint32(value) for value in self.bone_hashes]
        if not stream.is_reading():
            self.lod_levels = [self.num_names] * self.num_lod_levels
        self.lod_levels = [stream.uint32(value) for value in self.lod_levels]
        if stream.is_reading():
            data = stream.read().split(b"\x00")
            self.names = [item.decode() for item in data]
            if self.names and self.names[-1] == "":
                self.names.pop()
        else:
            data = b""
            for name in self.names:
                data += name.encode() + b"\x00"
            stream.write(data)
        return self


class StingrayParticles:
    def __init__(self):
        self.magic = 0
        self.min_lifetime = 0
        self.max_lifetime = 0
        self.unk1 = 0
        self.unk2 = 0
        self.num_variables = 0
        self.num_particle_systems = 0
        self.particle_variable_hashes = []
        self.particle_variable_positions = []
        self.particle_systems = []

    def serialize(self, stream: MemoryStream):
        self.magic = stream.uint32(self.magic)
        self.min_lifetime = stream.float32(self.min_lifetime)
        self.max_lifetime = stream.float32(self.max_lifetime)
        self.unk1 = stream.uint32(self.unk1)
        self.unk2 = stream.uint32(self.unk2)
        self.num_variables = stream.uint32(self.num_variables)
        self.num_particle_systems = stream.uint32(self.num_particle_systems)
        stream.seek(stream.tell() + 44)
        if stream.is_reading():
            self.particle_variable_hashes = [0 for _ in range(self.num_variables)]
            self.particle_variable_positions = [[0, 0, 0] for _ in range(self.num_variables)]
            self.particle_systems = [ParticleSystem() for _ in range(self.num_particle_systems)]

        self.particle_variable_hashes = [stream.uint32(value) for value in self.particle_variable_hashes]
        self.particle_variable_positions = [stream.vec3_float(value) for value in self.particle_variable_positions]
        for system in self.particle_systems:
            system.serialize(stream)


class ParticleSystem:
    def __init__(self):
        self.max_num_particles = 0
        self.num_components = 0
        self.unk2 = 0
        self.component_bit_flags = []
        self.unk3 = 0
        self.unk4 = 0
        self.unk5 = 0
        self.unk6 = 0
        self.type1 = 0
        self.type2 = 0
        self.rotation = ParticleRotation()
        self.unknown = []
        self.unk7 = 0
        self.component_list_offset = 0
        self.unk8 = 0
        self.component_list_size = 0
        self.unk9 = 0
        self.unk10 = 0
        self.offset3 = 0
        self.particle_system_size = 0
        self.component_list = ComponentList()

    def serialize(self, stream: MemoryStream):
        start_offset = stream.tell()
        self.max_num_particles = stream.uint32(self.max_num_particles)
        self.num_components = stream.uint32(self.num_components)
        self.unk2 = stream.uint32(self.unk2)
        if stream.is_reading():
            self.component_bit_flags = [0 for _ in range(self.num_components)]
        self.component_bit_flags = [stream.uint32(flag) for flag in self.component_bit_flags]
        stream.seek(stream.tell() + (64 - 4 * self.num_components))
        self.unk3 = stream.uint32(self.unk3)
        self.unk4 = stream.uint32(self.unk4)
        stream.seek(stream.tell() + 8)
        self.unk5 = stream.uint32(self.unk5)
        stream.seek(stream.tell() + 4)
        self.unk6 = stream.uint32(self.unk6)
        stream.seek(stream.tell() + 4)
        self.type1 = stream.uint32(self.type1)
        self.type2 = stream.uint32(self.type2)
        stream.seek(stream.tell() + 4)
        self.rotation.serialize(stream)
        if stream.is_reading():
            self.unknown = [0 for _ in range(11)]
        self.unknown = [stream.float32(value) for value in self.unknown]
        self.unk7 = stream.uint32(self.unk7)
        self.component_list_offset = stream.uint32(self.component_list_offset)
        self.unk8 = stream.uint32(self.unk8)
        self.component_list_size = stream.uint32(self.component_list_size)
        self.unk9 = stream.uint32(self.unk9)
        self.unk10 = stream.uint32(self.unk10)
        self.offset3 = stream.uint32(self.offset3)
        self.particle_system_size = stream.uint32(self.particle_system_size)
        stream.seek(start_offset + self.component_list_offset)
        if self.unk3 == 0xFFFFFFFF:
            stream.seek(start_offset + self.particle_system_size)
            return
        self.component_list.serialize(self, stream)
        stream.seek(start_offset + self.particle_system_size)


class ParticleRotation:
    def __init__(self):
        self.x_row = [0 for _ in range(3)]
        self.y_row = [0 for _ in range(3)]
        self.z_row = [0 for _ in range(3)]
        self.unk = [0 for _ in range(16)]

    def serialize(self, stream: MemoryStream):
        self.x_row = [stream.float32(value) for value in self.x_row]
        stream.seek(stream.tell() + 4)
        self.y_row = [stream.float32(value) for value in self.y_row]
        stream.seek(stream.tell() + 4)
        self.z_row = [stream.float32(value) for value in self.z_row]
        stream.seek(stream.tell() + 4)
        self.unk = [stream.uint8(value) for value in self.unk]


class ComponentList:
    def __init__(self):
        self.component_list = []

    def serialize(self, particle_system: ParticleSystem, stream: MemoryStream):
        size = particle_system.component_list_size - particle_system.component_list_offset
        if stream.is_reading():
            self.component_list = [0 for _ in range(size)]
        self.component_list = [stream.uint8(component) for component in self.component_list]


class StingrayStateMachine:
    def __init__(self):
        self.animation_ids = set()
        self.layer_count = 0
        self.layer_data_offset = 0
        self.animation_events_count = 0
        self.animation_events_offset = 0
        self.animation_vars_count = 0
        self.animation_vars_offset = 0
        self.blend_mask_count = 0
        self.blend_mask_offset = 0
        self.unk = 0
        self.unk_data_00_offset = 0
        self.unk_data_00_size = 0
        self.unk_data_01_offset = 0
        self.unk_data_01_size = 0
        self.unk_data_02_offset = 0
        self.unk_data_02_size = 0
        self.pre_blend_mask_data = bytearray()
        self.layers = []
        self.blend_masks = []
        self.blend_mask_offsets = []
        self.unk_data_03_size = 0
        self.unk_data_03_offset = 0
        self.unk_data_00 = None
        self.unk_data_01 = bytearray()
        self.unk_data_02 = bytearray()
        self.unk_data_03 = None
        self.ragdolls = []
        self.ragdoll_count = 0
        self.ragdoll_offset = 0

    def load(self, stream):
        offset_start = stream.tell()
        self.unk = stream.uint32(self.unk)
        self.layer_count = stream.uint32(self.layer_count)
        self.layer_data_offset = stream.uint32(self.layer_data_offset)
        self.animation_events_count = stream.uint32(self.animation_events_count)
        self.animation_events_offset = stream.uint32(self.animation_events_offset)
        self.animation_vars_count = stream.uint32(self.animation_vars_count)
        self.animation_vars_offset = stream.uint32(self.animation_vars_offset)
        self.blend_mask_count = stream.uint32(self.blend_mask_count)
        self.blend_mask_offset = stream.uint32(self.blend_mask_offset)
        self.unk_data_00_size = stream.uint32(self.unk_data_00_size)
        self.unk_data_00_offset = stream.uint32(self.unk_data_00_offset)
        self.unk_data_01_size = stream.uint32(self.unk_data_01_size)
        self.unk_data_01_offset = stream.uint32(self.unk_data_01_offset)
        self.unk_data_02_size = stream.uint32(self.unk_data_02_size)
        self.unk_data_02_offset = stream.uint32(self.unk_data_02_offset)
        self.unk_data_03_size = stream.uint32(self.unk_data_03_size)
        self.unk_data_03_offset = stream.uint32(self.unk_data_03_offset)
        self.ragdoll_count = stream.uint32(self.ragdoll_count)
        self.ragdoll_offset = stream.uint32(self.ragdoll_offset)

        if self.blend_mask_offset != 0:
            self.pre_blend_mask_data = stream.read(self.blend_mask_offset - (stream.tell() - offset_start))
        elif self.unk_data_00_offset != 0:
            self.pre_blend_mask_data = stream.read(self.unk_data_00_offset - (stream.tell() - offset_start))
        elif self.unk_data_01_offset != 0:
            self.pre_blend_mask_data = stream.read(self.unk_data_01_offset - (stream.tell() - offset_start))
        elif self.unk_data_02_offset != 0:
            self.pre_blend_mask_data = stream.read(self.unk_data_02_offset - (stream.tell() - offset_start))
        elif self.unk_data_03_offset != 0:
            self.pre_blend_mask_data = stream.read(self.unk_data_03_offset - (stream.tell() - offset_start))
        elif self.ragdoll_offset != 0:
            self.pre_blend_mask_data = stream.read(self.ragdoll_offset - (stream.tell() - offset_start))

        stream.seek(offset_start + self.layer_data_offset)
        self.layer_count = stream.uint32(self.layer_count)
        layer_offsets = [stream.uint32(0) for _ in range(self.layer_count)]
        self.layers = []
        for offset in layer_offsets:
            stream.seek(offset_start + self.layer_data_offset + offset)
            layer = Layer()
            layer.load(stream)
            self.layers.append(layer)

        if self.blend_mask_count > 0:
            stream.seek(offset_start + self.blend_mask_offset)
            self.blend_mask_count = stream.uint32(self.blend_mask_count)
            self.blend_mask_offsets = [stream.uint32(0) for _ in range(self.blend_mask_count)]
            self.blend_masks = []
            for offset in self.blend_mask_offsets:
                stream.seek(offset_start + self.blend_mask_offset + offset)
                blend_mask = BlendMask()
                blend_mask.load(stream)
                self.blend_masks.append(blend_mask)

        if self.unk_data_00_size > 0:
            stream.seek(offset_start + self.unk_data_00_offset)
            item = Unk00Item()
            item.load(stream)
            self.unk_data_00 = item

        if self.unk_data_01_size > 0:
            stream.seek(offset_start + self.unk_data_01_offset)
            self.unk_data_01 = stream.read(self.unk_data_01_size)

        if self.unk_data_02_size > 0:
            stream.seek(offset_start + self.unk_data_02_offset)
            self.unk_data_02 = stream.read(self.unk_data_02_size)

        if self.unk_data_03_size > 0:
            stream.seek(offset_start + self.unk_data_03_offset)
            item = Unk03Item()
            item.load(stream)
            self.unk_data_03 = item

        if self.ragdoll_count > 0:
            stream.seek(offset_start + self.ragdoll_offset)
            self.ragdolls = []
            for _ in range(self.ragdoll_count):
                ragdoll = RagdollItem()
                ragdoll.load(stream)
                self.ragdolls.append(ragdoll)

        for layer in self.layers:
            for state in layer.states:
                for animation_id in state.animation_ids:
                    self.animation_ids.add(animation_id)

    def save(self, stream):
        offset_start = stream.tell()
        self.unk = stream.uint32(self.unk)
        self.layer_count = stream.uint32(self.layer_count)
        self.layer_data_offset = stream.uint32(self.layer_data_offset)
        self.animation_events_count = stream.uint32(self.animation_events_count)
        self.animation_events_offset = stream.uint32(self.animation_events_offset)
        self.animation_vars_count = stream.uint32(self.animation_vars_count)
        self.animation_vars_offset = stream.uint32(self.animation_vars_offset)
        self.blend_mask_count = stream.uint32(self.blend_mask_count)
        self.blend_mask_offset = stream.uint32(self.blend_mask_offset)
        self.unk_data_00_size = stream.uint32(self.unk_data_00_size)
        self.unk_data_00_offset = stream.uint32(self.unk_data_00_offset)
        self.unk_data_01_size = stream.uint32(self.unk_data_01_size)
        self.unk_data_01_offset = stream.uint32(self.unk_data_01_offset)
        self.unk_data_02_size = stream.uint32(self.unk_data_02_size)
        self.unk_data_02_offset = stream.uint32(self.unk_data_02_offset)
        self.unk_data_03_size = stream.uint32(self.unk_data_03_size)
        self.unk_data_03_offset = stream.uint32(self.unk_data_03_offset)
        self.ragdoll_count = stream.uint32(self.ragdoll_count)
        self.ragdoll_offset = stream.uint32(self.ragdoll_offset)
        stream.write(self.pre_blend_mask_data)

        self.blend_mask_count = stream.uint32(self.blend_mask_count)
        for offset in self.blend_mask_offsets:
            stream.uint32(offset)
        for index, blend_mask in enumerate(self.blend_masks):
            self.blend_mask_offsets[index] = stream.tell() - offset_start - self.blend_mask_offset
            blend_mask.save(stream)

        if self.unk_data_00_size > 0:
            self.unk_data_00_offset = stream.tell() - offset_start
            self.unk_data_00.save(stream)
        if self.unk_data_01_size > 0:
            self.unk_data_01_offset = stream.tell() - offset_start
            stream.write(self.unk_data_01)
        if self.unk_data_02_size > 0:
            if stream.tell() % 8 != 0:
                stream.seek(stream.tell() + 4)
            self.unk_data_02_offset = stream.tell() - offset_start
            stream.write(self.unk_data_02)
        if self.unk_data_03_size > 0:
            self.unk_data_03_offset = stream.tell() - offset_start
            self.unk_data_03.save(stream)
        if self.ragdoll_count > 0:
            self.ragdoll_offset = stream.tell() - offset_start
            for ragdoll in self.ragdolls:
                ragdoll.save(stream)

    def serialize(self, stream):
        if stream.is_reading():
            self.load(stream)
        else:
            start = stream.tell()
            self.save(stream)
            stream.seek(start)
            self.save(stream)


class Layer:
    def __init__(self):
        self.magic = 0
        self.default_state = 0
        self.num_states = 0
        self.state_offsets = []
        self.states = []

    def load(self, stream):
        offset_start = stream.tell()
        self.magic = stream.uint32(self.magic)
        self.default_state = stream.uint32(self.default_state)
        self.num_states = stream.uint32(self.num_states)
        self.state_offsets = [stream.uint32(0) for _ in range(self.num_states)]
        self.states = []
        for state_offset in self.state_offsets:
            stream.seek(offset_start + state_offset)
            state = State()
            state.load(stream)
            self.states.append(state)


class State:
    def __init__(self):
        self.name = 0
        self.state_type = 0
        self.animation_count = 0
        self.animation_offset = 0
        self.blend_mask_index = 0
        self.animation_ids = []

    def load(self, stream):
        offset_start = stream.tell()
        self.name = stream.uint64(self.name)
        self.state_type = stream.uint32(self.state_type)
        self.animation_count = stream.uint32(self.animation_count)
        self.animation_offset = stream.uint32(self.animation_offset)
        stream.seek(stream.tell() + 88)
        self.blend_mask_index = stream.uint32(self.blend_mask_index)
        stream.seek(offset_start + self.animation_offset)
        self.animation_ids = [stream.uint64(0) for _ in range(self.animation_count)]


class BlendMask:
    def __init__(self):
        self.bone_count = 0
        self.bone_weights = []

    def load(self, stream):
        self.bone_count = stream.uint32(self.bone_count)
        self.bone_weights = [stream.float32(0) for _ in range(self.bone_count)]

    def save(self, stream):
        self.bone_count = stream.uint32(self.bone_count)
        self.bone_weights = [stream.float32(weight) for weight in self.bone_weights]


class RagdollItem:
    def __init__(self):
        self.bone_index = 0
        self.params = [0] * 9
        self.unk_hash = 0
        self.unk_enum = 2
        self.unk = 0

    def load(self, stream):
        self.bone_index = stream.uint32(self.bone_index)
        self.params = [stream.float32(value) for value in self.params]
        self.unk_hash = stream.uint64(self.unk_hash)
        self.unk_enum = stream.uint32(self.unk_enum)
        self.unk = stream.uint32(self.unk)

    def save(self, stream):
        self.bone_index = stream.uint32(self.bone_index)
        self.params = [stream.float32(value) for value in self.params]
        self.unk_hash = stream.uint64(self.unk_hash)
        self.unk_enum = stream.uint32(self.unk_enum)
        self.unk = stream.uint32(self.unk)


class Unk00Item:
    def __init__(self):
        self.count = 0
        self.data = bytearray()

    def load(self, stream):
        self.count = stream.uint32(self.count)
        self.data = stream.read(16 * self.count)

    def save(self, stream):
        self.count = stream.uint32(self.count)
        stream.write(self.data)


class Unk03ItemSection:
    def __init__(self):
        self.unk = 0
        self.data = [bytearray(), bytearray()]
        self.offsets = [0, 0]
        self.counts = [0, 0]

    def load(self, stream):
        start_offset = stream.tell()
        self.unk = stream.uint64(self.unk)
        self.counts[0] = stream.uint16(0)
        self.offsets[0] = stream.uint16(0)
        self.counts[1] = stream.uint16(0)
        self.offsets[1] = stream.uint16(0)
        if self.counts[0] > 0:
            stream.seek(start_offset + self.offsets[0])
            self.data[0] = stream.read(4 * self.counts[0])
        if self.counts[1] > 0:
            stream.seek(start_offset + self.offsets[1])
            self.data[1] = stream.read(4 * self.counts[1])

    def save(self, stream):
        stream.uint64(self.unk)
        stream.uint16(self.counts[0])
        stream.uint16(self.offsets[0])
        stream.uint16(self.counts[1])
        stream.uint16(self.offsets[1])
        if self.counts[0] > 0:
            stream.write(self.data[0])
        if self.counts[1] > 0:
            stream.write(self.data[1])


class Unk03Item:
    def __init__(self):
        self.count = 0
        self.section_offsets = []
        self.sections = []

    def load(self, stream):
        start_offset = stream.tell()
        self.count = stream.uint32(self.count)
        self.sections = [None] * self.count
        self.section_offsets = [0] * self.count
        for index in range(self.count):
            self.section_offsets[index] = stream.uint32(0)
        for index in range(self.count):
            stream.seek(start_offset + self.section_offsets[index])
            section = Unk03ItemSection()
            section.load(stream)
            self.sections[index] = section

    def save(self, stream):
        start_offset = stream.tell()
        self.count = stream.uint32(self.count)
        for offset in self.section_offsets:
            stream.uint32(offset)
        for index, section in enumerate(self.sections):
            stream.seek(start_offset + self.section_offsets[index])
            section.save(stream)
