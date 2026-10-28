"""Minimal PNG encoder (stdlib zlib): camera frames leave the adapter as PNG files.

``a6g_skill_mcp`` turns them into event JPEGs (<= 300 KB).
"""

import struct
import zlib

_CHANNELS = {"rgb8": 3, "bgr8": 3, "rgba8": 4, "bgra8": 4, "mono8": 1}


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def encode_png(width: int, height: int, encoding: str, step: int, data: bytes) -> bytes:
    """``sensor_msgs/Image`` fields -> PNG bytes (rgb8, bgr8, rgba8, bgra8 or mono8)."""
    if encoding not in _CHANNELS:
        raise ValueError(f"unsupported image encoding {encoding!r}")
    ch = _CHANNELS[encoding]
    rows = []
    for r in range(height):
        row = bytearray(data[r * step : r * step + width * ch])
        if encoding.startswith("bgr"):
            row[0::ch], row[2::ch] = row[2::ch], row[0::ch]
        rows.append(b"\x00" + bytes(row))
    color_type = {1: 0, 3: 2, 4: 6}[ch]
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + _chunk(b"IEND", b"")
    )
