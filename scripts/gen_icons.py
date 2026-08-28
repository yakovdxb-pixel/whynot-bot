"""
Generate the PWA app icons — a purple square with white "WN".

    python scripts/gen_icons.py

Uses Pillow when available; otherwise falls back to a tiny pure-Python PNG
writer so it runs anywhere.
"""
import os
import struct
import zlib

BG = (0x6c, 0x5c, 0xe7)   # #6c5ce7
FG = (255, 255, 255)
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
        draw = ImageDraw.Draw(img)
        draw.text((size / 2, size / 2), "WN", fill=FG, anchor="mm", font=load_font(size // 3))
        img.save(os.path.join(OUT_DIR, f"icon-{size}.png"))
    return True


# ── fallback: hand-rasterised PNG ───────────────────────────────
def _png(width, height, rgb):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # RGB, 8-bit
    raw = bytearray()
    row = width * 3
    for y in range(height):
        raw.append(0)
        raw.extend(rgb[y * row:(y + 1) * row])
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _stroke(buf, w, h, x0, y0, x1, y1, rad):
    steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    for i in range(steps + 1):
        t = i / steps
        cx, cy = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        for yy in range(int(cy - rad), int(cy + rad) + 1):
            for xx in range(int(cx - rad), int(cx + rad) + 1):
                if 0 <= xx < w and 0 <= yy < h and (xx - cx) ** 2 + (yy - cy) ** 2 <= rad * rad:
                    o = (yy * w + xx) * 3
                    buf[o:o + 3] = bytes(FG)


def with_raster():
    # glyph strokes on a 0..1 grid: W on the left, N on the right
    W = [(0.10, 0.30), (0.19, 0.70), (0.275, 0.44), (0.36, 0.70), (0.45, 0.30)]
    N = [(0.55, 0.70), (0.55, 0.30), (0.90, 0.70), (0.90, 0.30)]
    for size in SIZES:
        buf = bytearray(bytes(BG) * (size * size))
        rad = max(2, size // 26)
        for pts in (W, N):
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                _stroke(buf, size, size, ax * size, ay * size, bx * size, by * size, rad)
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
