"""Lossless ESC/POS tokenization helpers.

The print server must never inspect bytes inside a raster/image payload as if
those bytes were text or commands.  This module keeps every token's original
bytes so a native job can be passed through byte-for-byte.
"""

from dataclasses import dataclass, field
from typing import Dict, Iterator, Optional


@dataclass(frozen=True)
class EscPosToken:
    kind: str
    raw: bytes
    start: int
    end: int
    metadata: Dict[str, int] = field(default_factory=dict)


def _u16(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def decode_gs_character_size(value: int):
    """Decode the producer's GS ! size convention into width and height."""
    value &= 0xFF
    width = max(1, value & 0x0F)
    height = max(1, (value >> 4) & 0x0F)
    return width, height


# Native mode preserves GS ! bytes; raster modes use the producer convention
# because existing receipts use GS ! 0x01 for normal-size text.
GS_CHARACTER_SIZE_CONVENTION = "producer"


def decode_gs_character_size_official(value: int):
    """Decode official Epson GS ! zero-based width/height fields."""
    value &= 0xFF
    return ((value >> 4) & 0x07) + 1, (value & 0x07) + 1


def _bounded_end(start: int, wanted: int, length: int) -> int:
    return min(length, start + max(0, wanted))


def _variable_command_end(data: bytes, start: int) -> Optional[int]:
    """Return the end of a known variable-length command, if valid."""
    n = len(data)
    b = data[start]

    # ESC * m nL nH d... (bit image)
    if b == 0x1B and start + 4 < n and data[start + 1] == 0x2A:
        m = data[start + 2]
        width = _u16(data, start + 3)
        rows_per_pass = 24 if m in (32, 33, 34) else 8
        return _bounded_end(start, 5 + width * rows_per_pass, n)

    # ESC GS S m xL xH yL yH d... (Star/Bixolon raster)
    if (b == 0x1B and start + 7 < n and data[start + 1] == 0x1D
            and data[start + 2] == 0x53):
        width_bytes = _u16(data, start + 4)
        height = _u16(data, start + 6)
        wanted = 8 + width_bytes * height
        if width_bytes > 0 and height > 0:
            return _bounded_end(start, wanted, n)
        return None

    # GS v 0 m xL xH yL yH d... (Epson raster)
    if b == 0x1D and start + 7 < n and data[start + 1:start + 3] == b"v0":
        width_bytes = _u16(data, start + 4)
        height = _u16(data, start + 6)
        wanted = 8 + width_bytes * height
        if width_bytes > 0 and height > 0:
            return _bounded_end(start, wanted, n)
        return None

    # GS ( k / GS ( L: pL pH bytes follow the four-byte prefix.
    if b == 0x1D and start + 4 < n and data[start + 1] == 0x28:
        payload_len = _u16(data, start + 3)
        return _bounded_end(start, 5 + payload_len, n)

    # GS k barcode.  Support the NUL-terminated form and the newer length form.
    if b == 0x1D and start + 2 < n and data[start + 1] == 0x6B:
        mode = data[start + 2]
        if mode in (0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A,
                    0x4B, 0x61, 0x62, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68,
                    0x69, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x6F, 0x70):
            # New form: GS k m n data (where n is length).
            if start + 3 < n:
                return _bounded_end(start, 4 + data[start + 3], n)
        try:
            nul = data.index(0, start + 3)
        except ValueError:
            return n
        return nul + 1

    return None


def _command_length(data: bytes, start: int) -> int:
    """Return a fixed command length, or zero for variable/unknown commands."""
    n = len(data)
    b = data[start]
    nxt = data[start + 1] if start + 1 < n else None

    if b in (0x0A, 0x0D, 0x0C):  # LF, CR, FF
        return 1

    if b == 0x1B:
        if nxt is None:
            return 0
        if nxt == 0x1D:  # ESC GS family; variable commands handled separately
            return 0
        # Commands with no parameter.
        if nxt in (0x40, 0x32, 0x52, 0x57):
            return 2
        # Commands with one parameter.
        if nxt in (0x21, 0x25, 0x2D, 0x33, 0x3D, 0x45, 0x61, 0x64, 0x74,
                   0x7B, 0x56):
            return 3
        return 0

    if b == 0x1D:
        if nxt is None:
            return 0
        if nxt in (0x76, 0x6B, 0x28):  # variable commands
            return 0
        if nxt == 0x56:  # GS V m [n]
            if start + 2 < n and data[start + 2] in (0x41, 0x42, 0x61, 0x62):
                return 4
            return 3
        if nxt in (0x21, 0x42, 0x62, 0x68, 0x77, 0x48, 0x66):
            return 3
        return 2

    if b in (0x1C, 0x10):
        return 2 if nxt is not None else 0

    return 0


def iter_tokens(data: bytes) -> Iterator[EscPosToken]:
    """Yield an ordered, lossless token stream for *data*."""
    i = 0
    text_start = 0
    n = len(data)

    def flush_text(end: int) -> Iterator[EscPosToken]:
        nonlocal text_start
        if end > text_start:
            yield EscPosToken("text", data[text_start:end], text_start, end)
        text_start = end

    while i < n:
        b = data[i]
        command_end = _variable_command_end(data, i)
        if command_end is not None:
            yield from flush_text(i)
            metadata = {}
            if data[i:i + 3] == b"\x1dv0":
                metadata = {
                    "width_bytes": _u16(data, i + 4),
                    "height": _u16(data, i + 6),
                }
                kind = "image"
            elif data[i:i + 3] == b"\x1b\x1dS":
                metadata = {
                    "width_bytes": _u16(data, i + 4),
                    "height": _u16(data, i + 6),
                }
                kind = "image"
            elif data[i:i + 2] == b"\x1d(":
                kind = "barcode_or_qr"
            else:
                kind = "bit_image"
            yield EscPosToken(kind, data[i:command_end], i, command_end, metadata)
            i = command_end
            text_start = i
            continue

        fixed_len = _command_length(data, i)
        if fixed_len:
            end = min(n, i + fixed_len)
            yield from flush_text(i)
            prefix = data[i:end]
            if prefix[:2] == b"\x1dV":
                kind = "cut"
            elif prefix[:1] in (b"\x1b", b"\x1d", b"\x1c", b"\x10"):
                kind = "control"
            else:
                kind = "feed"
            yield EscPosToken(kind, prefix, i, end)
            i = end
            text_start = i
            continue

        # Unknown ESC/GS/FS/DLE bytes are kept opaque as a short token.  This
        # prevents an incomplete command from swallowing the following text.
        if b in (0x1B, 0x1D, 0x1C, 0x10):
            yield from flush_text(i)
            end = min(n, i + 2)
            yield EscPosToken("unknown", data[i:end], i, end)
            i = end
            text_start = i
            continue

        i += 1

    yield from flush_text(n)


def parse_stream(data: bytes):
    """Return tokens and guarantee that concatenating their raw bytes is exact."""
    tokens = list(iter_tokens(data))
    if b"".join(token.raw for token in tokens) != data:
        raise ValueError("ESC/POS tokenizer lost bytes")
    return tokens


def has_valid_image(tokens) -> bool:
    return any(token.kind == "image" for token in tokens)


def text_is_non_ascii(token: EscPosToken) -> bool:
    if token.kind != "text":
        return False
    return any(byte >= 0x80 for byte in token.raw)


def decode_text(raw: bytes, encodings=("utf-8", "gbk", "gb2312")) -> str:
    """Decode POS text without interpreting image bytes."""
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def replace_cuts(data: bytes, cut_mode: str) -> bytes:
    """Rewrite only parsed cut tokens; never match cut bytes in image data."""
    output = bytearray()
    for token in parse_stream(data):
        if token.kind != "cut":
            output.extend(token.raw)
            continue
        if cut_mode == "none":
            continue
        if len(token.raw) == 4:
            mode = 0x41 if cut_mode == "partial" else 0x42
            output.extend((0x1D, 0x56, mode, token.raw[3]))
        else:
            mode = 0x01 if cut_mode == "partial" else 0x00
            output.extend((0x1D, 0x56, mode))
    return bytes(output)
