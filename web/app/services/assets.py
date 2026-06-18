"""Generate the iPXE menu background PNG (pure stdlib, no Pillow needed).

Produces a dark vertical gradient with the brand accent glow so the boot menu
has a designed backdrop instead of plain black. iPXE renders it behind the
coloured menu when the build supports PNG backgrounds.
"""
import struct
import zlib
from pathlib import Path

from .. import config


def _png(width: int, height: int, rows: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    idat = zlib.compress(rows, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _gradient_rows(width: int, height: int) -> bytes:
    # Top colour -> bottom colour, with a soft accent glow near the top-left.
    top = (26, 29, 41)      # #1a1d29
    bottom = (10, 12, 18)   # #0a0c12
    accent = (0, 180, 216)  # #00b4d8
    out = bytearray()
    for y in range(height):
        out.append(0)  # filter type 0 per scanline
        ty = y / (height - 1)
        for x in range(width):
            r = int(top[0] + (bottom[0] - top[0]) * ty)
            g = int(top[1] + (bottom[1] - top[1]) * ty)
            b = int(top[2] + (bottom[2] - top[2]) * ty)
            # Accent glow radiating from upper-left quadrant.
            dx = x / width - 0.18
            dy = y / height - 0.12
            d = (dx * dx + dy * dy) ** 0.5
            glow = max(0.0, 0.35 - d) / 0.35
            r = min(255, int(r + (accent[0] - r) * glow * 0.25))
            g = min(255, int(g + (accent[1] - g) * glow * 0.25))
            b = min(255, int(b + (accent[2] - b) * glow * 0.25))
            out += bytes((r, g, b))
    return bytes(out)


def ensure_background(width: int = 1024, height: int = 768) -> Path:
    """Write background.png into the boot root if it isn't there already."""
    dest = config.BOOTROOT_DIR / "background.png"
    if not dest.exists():
        dest.write_bytes(_png(width, height, _gradient_rows(width, height)))
    return dest
