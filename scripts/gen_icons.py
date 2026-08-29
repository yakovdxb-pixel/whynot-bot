"""
Generate the PWA app icons — WHY NOT? Creative Bureau brand mark:
deep-plum square, white "WHY / NOT" stacked, mint exclamation dot.

    python scripts/gen_icons.py

Uses Pillow when available; otherwise a small pure-Python PNG writer so it
runs anywhere (no external deps).
"""
import os
import struct
import zlib

BG = (0x1A, 0x08, 0x20)      # deep plum  #330A3A
FG = (255, 255, 255)          # white
ACCENT = (0x1E, 0xC8, 0x9A)   # mint       #1EC89A
SIZES = (192, 512)
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp")


# ── nice path: Pillow ───────────────────────────────────────────
def with_pillow():
    from PIL import Image, ImageDraw, ImageFont

    def load_font(px):
        for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "Arialbd.ttf"):
            try:
                return ImageFont.truetype(name, px)
            except OSError:
                continue
        return ImageFont.load_default()

    for size in SIZES:
        img = Image.new("RGB", (size, size), BG)
        d = ImageDraw.Draw(img)
        f = load_font(int(size * 0.30))
        d.text((size * 0.50, size * 0.38), "WHY", fill=FG, anchor="mm", font=f)
        d.text((size * 0.50, size * 0.66), "NOT", fill=FG, anchor="mm", font=f)
        r = size * 0.05
        cx, cy = size * 0.78, size * 0.50
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)
        img.save(os.path.join(OUT_DIR, f"icon-{size}.png"))
    return True


# ── fallback: hand-rasterised PNG ───────────────────────────────
def _png(w, h, rgb):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # RGB, 8-bit
    raw = bytearray()
    row = w * 3
    for y in range(h):
        raw.append(0)
        raw.extend(rgb[y * row:(y + 1) * row])
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


# glyph strokes on a 0..1 box (origin top-left)
GLYPHS = {
    "W": [[(0, 0), (0.22, 1), (0.5, 0.4), (0.78, 1), (1, 0)]],
    "H": [[(0, 0), (0, 1)], [(1, 0), (1, 1)], [(0, 0.5), (1, 0.5)]],
    "Y": [[(0, 0), (0.5, 0.5)], [(1, 0), (0.5, 0.5)], [(0.5, 0.5), (0.5, 1)]],
    "N": [[(0, 1), (0, 0), (1, 1), (1, 0)]],
    "O": [[(0.5, 0), (0.95, 0.2), (1, 0.5), (0.95, 0.8), (0.5, 1),
           (0.05, 0.8), (0, 0.5), (0.05, 0.2), (0.5, 0)]],
    "T": [[(0, 0), (1, 0)], [(0.5, 0), (0.5, 1)]],
}


def _plot(buf, w, h, x, y, rad, col):
    for yy in range(int(y - rad), int(y + rad) + 1):
        for xx in range(int(x - rad), int(x + rad) + 1):
            if 0 <= xx < w and 0 <= yy < h and (xx - x) ** 2 + (yy - y) ** 2 <= rad * rad:
                o = (yy * w + xx) * 3
                buf[o:o + 3] = bytes(col)


def _seg(buf, w, h, a, b, rad, col):
    (x0, y0), (x1, y1) = a, b
    n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    for i in range(n + 1):
        t = i / n
        _plot(buf, w, h, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, rad, col)


def _word(buf, size, text, x0, x1, y0, y1, rad):
    n = len(text)
    gap = (x1 - x0) * 0.14 / max(1, n - 1)
    gw = ((x1 - x0) - gap * (n - 1)) / n
    for i, ch in enumerate(text):
        gx = x0 + i * (gw + gap)
        for stroke in GLYPHS.get(ch, []):
            pts = [((gx + px * gw) * size, (y0 + py * (y1 - y0)) * size) for px, py in stroke]
            for a, b in zip(pts, pts[1:]):
                _seg(buf, size, size, a, b, rad, FG)


def with_raster():
    for size in SIZES:
        buf = bytearray(bytes(BG) * (size * size))
        rad = max(3, int(size * 0.032))
        _word(buf, size, "WHY", 0.14, 0.86, 0.20, 0.42, rad)
        _word(buf, size, "NOT", 0.14, 0.86, 0.56, 0.78, rad)
        _plot(buf, size, size, size * 0.79, size * 0.30, size * 0.055, ACCENT)  # ! dot
        with open(os.path.join(OUT_DIR, f"icon-{size}.png"), "wb") as fh:
            fh.write(_png(size, size, bytes(buf)))
    return True


if __name__ == "__main__":
    try:
        with_pillow()
        print("icons written (Pillow)")
    except Exception as e:
        with_raster()
        print(f"icons written (pure-python fallback; Pillow unavailable: {e})")
