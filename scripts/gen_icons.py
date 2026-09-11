"""Generates the PWA app icons (PNG) from the coffee cup design in app/static/icon.svg.

Pillow does not parse SVG, so the shapes below are a hand drawn approximation of
icon.svg (rounded square, cup body, handle, steam) rendered at each required size.
Run with: python scripts/gen_icons.py
Requires Pillow (see requirements-dev.txt).
"""
from pathlib import Path

from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "app" / "static" / "icons"

BG = (0x5B, 0x3A, 0x29, 255)  # --cafe
FG = (0xF7, 0xF1, 0xEA, 255)  # --creme

SUPERSAMPLE = 4  # render bigger, then downsample for anti aliasing


def _cubic_bezier(p0, p1, p2, p3, steps=24):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = (mt ** 3) * p0[0] + 3 * (mt ** 2) * t * p1[0] + 3 * mt * (t ** 2) * p2[0] + (t ** 3) * p3[0]
        y = (mt ** 3) * p0[1] + 3 * (mt ** 2) * t * p1[1] + 3 * mt * (t ** 2) * p2[1] + (t ** 3) * p3[1]
        pts.append((x, y))
    return pts


def _stroke_path(draw, pts, width, color):
    draw.line(pts, fill=color, width=width, joint="curve")
    r = width / 2
    for (x, y) in (pts[0], pts[-1]):
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)


def _draw_icon(size, maskable=False):
    """Draws the icon at `size` pixels. `maskable` fills the whole canvas with
    the background colour and shrinks the artwork into the safe zone, per the
    maskable icon spec (no transparent bleed, content inside the centre ~62%)."""
    big = size * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if maskable:
        draw.rectangle([0, 0, big, big], fill=BG)
        content = big * 0.62
        off = (big - content) / 2
    else:
        radius = big * 14 / 64
        draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=radius, fill=BG)
        content = big
        off = 0

    def X(v):
        return off + v / 64 * content

    def Y(v):
        return off + v / 64 * content

    def S(v):
        return v / 64 * content

    # cup body: flat top, rounded bottom corners (mirrors the SVG path)
    draw.rounded_rectangle(
        [X(16), Y(24), X(44), Y(48)],
        radius=S(10),
        corners=(False, False, True, True),
        fill=FG,
    )

    # handle: a "D" shape attached to the right side of the cup
    handle_w = max(1, round(S(3)))
    cx, cy, r = X(48), Y(32), S(5)
    draw.line([(X(44), Y(27)), (cx, Y(27))], fill=FG, width=handle_w)
    draw.line([(X(44), Y(37)), (cx, Y(37))], fill=FG, width=handle_w)
    draw.arc([cx - r, cy - r, cx + r, cy + r], -90, 90, fill=FG, width=handle_w)

    # steam: two soft wisps above the cup
    steam_w = max(1, round(S(2.5)))
    for x0 in (24, 32):
        pts = _cubic_bezier((X(x0), Y(12)), (X(x0), Y(16)), (X(x0 + 4), Y(16)), (X(x0 + 4), Y(20)))
        _stroke_path(draw, pts, steam_w, FG)

    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    _draw_icon(180).save(OUT / "icon-180.png")
    _draw_icon(192).save(OUT / "icon-192.png")
    _draw_icon(512).save(OUT / "icon-512.png")
    _draw_icon(512, maskable=True).save(OUT / "icon-512-maskable.png")
    print(f"Icons written to {OUT}")


if __name__ == "__main__":
    main()
