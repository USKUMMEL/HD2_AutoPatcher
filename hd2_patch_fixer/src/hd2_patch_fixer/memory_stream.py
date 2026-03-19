import struct


class MemoryStream:
    def __init__(self, data=b"", io_mode="read"):
        self.location = 0
        self.data = bytearray(data)
        self.reading = io_mode == "read"

    def open(self, data, io_mode="read"):
        self.data = bytearray(data)
        self.location = 0
        self.reading = io_mode == "read"

    def set_read_mode(self):
        self.reading = True

    def set_write_mode(self):
        self.reading = False

    def is_reading(self):
        return self.reading

    def is_writing(self):
        return not self.reading

    def seek(self, location):
        self.location = location
        if self.location > len(self.data):
            self.data.extend(bytearray(self.location - len(self.data)))

    def tell(self):
        return self.location

    def read(self, length=-1):
        if length == -1:
            length = len(self.data) - self.location
        if self.location + length > len(self.data):
            raise ValueError("reading past end of stream")
        new_data = self.data[self.location:self.location + length]
        self.location += length
        return bytes(new_data)

    def write(self, raw_bytes):
        length = len(raw_bytes)
        self.data[self.location:self.location + length] = raw_bytes
        self.location += length

    def serialize(self, value, fmt, size):
        if self.reading:
            return struct.unpack(fmt, self.read(size))[0]
        self.write(struct.pack(fmt, value))
        return value

    def uint32(self, value):
        return self.serialize(value, "<I", 4)

    def uint64(self, value):
        return self.serialize(value, "<Q", 8)

    def int8(self, value):
        return self.serialize(value, "<b", 1)

    def uint8(self, value):
        return self.serialize(value, "<B", 1)

    def int16(self, value):
        return self.serialize(value, "<h", 2)

    def uint16(self, value):
        return self.serialize(value, "<H", 2)

    def int32(self, value):
        return self.serialize(value, "<i", 4)

    def float16(self, value):
        return self.serialize(value, "<e", 2)

    def float32(self, value):
        return self.serialize(value, "<f", 4)

    def float64(self, value):
        return self.serialize(value, "<d", 8)

    def _resize_vec(self, value, length):
        value = list(value)
        if len(value) < length:
            value.extend([0] * (length - len(value)))
        if len(value) > length:
            value = value[:length]
        return value

    def vec3_float(self, value):
        value = self._resize_vec(value, 3)
        return [self.float32(value[0]), self.float32(value[1]), self.float32(value[2])]

    def bytes(self, value, size=-1):
        if size == -1:
            size = len(value)
        if len(value) != size:
            value = bytearray(size)
        if self.reading:
            return bytearray(self.read(size))
        self.write(value)
        return bytearray(value)
