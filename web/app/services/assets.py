"""Generate the iPXE menu background PNG (pure stdlib, no Pillow needed).

Produces a vertical gradient with the brand accent glow so the boot menu has a
designed backdrop instead of plain black/white.  The palette mirrors the web
UI's light/dark theme (see static/style.css) so the menu and the management UI
match.  iPXE renders it behind the coloured menu when the build supports PNG
backgrounds (CONSOLE_FRAMEBUFFER + IMAGE_PNG — see dnsmasq/Dockerfile).
"""
import struct
import zlib
from pathlib import Path

from .. import config

# Gradient endpoints per theme, taken from the web UI's --bg family.  Top is the
# lighter/elevated tone, bottom the base background, so the accent glow reads
# against it.  Values mirror static/style.css [data-theme=...] blocks.
_THEMES = {
    "dark": {"top": (29, 36, 48), "bottom": (10, 17, 23)},     # #1d2430 -> #0e1117
    "light": {"top": (255, 255, 255), "bottom": (238, 241, 247)},  # #ffffff -> #eef1f7
}
_ACCENT = (0, 180, 216)  # #00b4d8


def _png(width: int, height: int, rows: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    idat = zlib.compress(rows, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _gradient_rows(width: int, height: int, theme: str) -> bytes:
    # Top colour -> bottom colour, with a soft accent glow near the top-left.
    palette = _THEMES.get(theme, _THEMES["dark"])
    top = palette["top"]
    bottom = palette["bottom"]
    # The glow reads as a subtle highlight on dark, but would wash out a light
    # background, so pull it back there.
    glow_strength = 0.25 if theme == "dark" else 0.12
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
            r = min(255, int(r + (_ACCENT[0] - r) * glow * glow_strength))
            g = min(255, int(g + (_ACCENT[1] - g) * glow * glow_strength))
            b = min(255, int(b + (_ACCENT[2] - b) * glow * glow_strength))
            out += bytes((r, g, b))
    return bytes(out)


def ensure_background(theme: str = "dark", width: int = 1024,
                      height: int = 768) -> Path:
    """Write background.png into the boot root for the given theme.

    Always rewrites (cheap, pure stdlib) so a theme change is reflected on the
    next render rather than being stuck on the first-generated palette.
    """
    dest = config.BOOTROOT_DIR / "background.png"
    dest.write_bytes(_png(width, height, _gradient_rows(width, height, theme)))
    return dest
