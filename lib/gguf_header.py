"""Bounded GGUF header inspection; does not initialize CPU/GPU backends."""
import struct

def read_vocabulary(path):
    """Read GGUF metadata only; never read tensor payloads or initialize a backend."""
    fixed = {0: ('B', 1), 1: ('b', 1), 2: ('H', 2), 3: ('h', 2), 4: ('I', 4),
             5: ('i', 4), 6: ('f', 4), 7: ('?', 1), 10: ('Q', 8), 11: ('q', 8), 12: ('d', 8)}
    with path.open('rb') as f:
        def take(n):
            if n < 0 or f.tell() + n > 512 * 1024 * 1024:
                raise ValueError('GGUF metadata exceeds bounded reader')
            value = f.read(n)
            if len(value) != n:
                raise ValueError('Truncated GGUF metadata')
            return value

        def number(fmt):
            return struct.unpack('<' + fmt, take(struct.calcsize('<' + fmt)))[0]

        def string(keep):
            n = number('Q')
            if n > 32 * 1024 * 1024:
                raise ValueError('Oversized GGUF metadata string')
            if keep:
                return take(n).decode('utf-8')
            if f.tell() + n > 512 * 1024 * 1024:
                raise ValueError('Oversized GGUF metadata')
            f.seek(n, 1)

        def value(kind, keep):
            if kind in fixed:
                fmt, size = fixed[kind]
                data = take(size)
                return struct.unpack('<' + fmt, data)[0] if keep else None
            if kind == 8:
                return string(keep)
            if kind == 9:
                subtype, count = number('I'), number('Q')
                if count > 1000000:
                    raise ValueError('Oversized metadata array')
                if keep:
                    return [value(subtype, True) for _ in range(count)]
                if subtype in fixed:
                    n = count * fixed[subtype][1]
                    if f.tell() + n > 512 * 1024 * 1024:
                        raise ValueError('Oversized metadata array')
                    f.seek(n, 1)
                else:
                    for _ in range(count):
                        value(subtype, False)
                return None
            raise ValueError('Unknown GGUF type: ' + str(kind))

        if take(4) != b'GGUF' or number('I') not in (2, 3):
            raise ValueError('Unsupported GGUF header')
        number('Q')
        count = number('Q')
        if count > 100000:
            raise ValueError('Oversized metadata map')
        result = {}
        for _ in range(count):
            key, kind = string(True), number('I')
            keep = key.startswith('tokenizer.') or key.startswith('qwen35.') or key == 'general.architecture'
            item = value(kind, keep)
            if keep:
                result[key] = item
        result['metadata_bytes_read_or_skipped'] = f.tell()
        return result

def read_metadata(path):
    metadata = read_vocabulary(path)
    with path.open('rb') as stream:
        stream.seek(8)
        count = struct.unpack('<Q', stream.read(8))[0]
        if count > 10000:
            raise ValueError('Too many tensor descriptors')
        stream.seek(metadata['metadata_bytes_read_or_skipped'])
        tensors = {}
        for _ in range(count):
            length = struct.unpack('<Q', stream.read(8))[0]
            if length > 4096:
                raise ValueError('Oversized tensor name')
            name = stream.read(length).decode('utf-8')
            dims = struct.unpack('<I', stream.read(4))[0]
            if not 1 <= dims <= 4:
                raise ValueError('Invalid tensor rank')
            shape = list(struct.unpack('<' + 'Q' * dims, stream.read(8 * dims)))
            kind, offset = struct.unpack('<IQ', stream.read(12))
            if name in tensors:
                raise ValueError('Duplicate tensor name')
            tensors[name] = {'shape': shape, 'ggml_type': kind}
        descriptor_end = stream.tell()
    return metadata, tensors, descriptor_end
