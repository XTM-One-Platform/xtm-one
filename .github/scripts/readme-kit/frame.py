"""Screenshot frame of the README kit: one rounded window frame, same size, both themes.

Usage:
    python .github/scripts/readme-kit/frame.py CAPTURE.png THEME OUT.webp
THEME is dark or light. The capture is any lab screenshot (2880x1800 expected,
1440x900 at scale 2); it is fitted into the same 2880x1800 canvas for every
product: page margin, rounded window, a title bar with three dots, a 1 px border
in the palette's line colour and a soft shadow. Output is WebP, quality lowered
step by step until the file is under 400 KB (doc 33 s.3).
"""
import io
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(__file__))
from theme import DARK, LIGHT  # noqa: E402

W, H = 2880, 1800          # canvas, identical for every image of every README
MARGIN = 56                # page margin around the window
RADIUS = 28                # window corner radius
BAR = 72                   # title bar height
DOT_R, DOT_GAP = 11, 36
MAX_BYTES = 400 * 1024


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def frame(capture: Image.Image, theme: dict) -> Image.Image:
    page = Image.new("RGB", (W, H), _hex(theme["page"]))
    win = (MARGIN, MARGIN, W - MARGIN, H - MARGIN)
    # shadow
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle((win[0], win[1] + 12, win[2], win[3] + 12), RADIUS, fill=110)
    sh = sh.filter(ImageFilter.GaussianBlur(24))
    page.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), sh)
    d = ImageDraw.Draw(page)
    d.rounded_rectangle(win, RADIUS, fill=_hex(theme["card"]), outline=_hex(theme["line"]), width=2)
    # title bar dots
    cy = win[1] + BAR // 2
    for i, col in enumerate((theme["accent"], theme["products"]["xtm-one"], theme["accent2"])):
        cx = win[0] + 40 + i * DOT_GAP
        d.ellipse((cx - DOT_R, cy - DOT_R, cx + DOT_R, cy + DOT_R), fill=_hex(col))
    d.line((win[0], win[1] + BAR, win[2], win[1] + BAR), fill=_hex(theme["line"]), width=2)
    # content area, capture fitted by width then cropped at the bottom
    cw, ch = win[2] - win[0] - 4, win[3] - win[1] - BAR - 4
    cap = capture.convert("RGB")
    scale = cw / cap.width
    cap = cap.resize((cw, max(1, round(cap.height * scale))), Image.LANCZOS).crop((0, 0, cw, ch))
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, -RADIUS, cw, ch), RADIUS - 2, fill=255)
    page.paste(cap, (win[0] + 2, win[1] + BAR + 2), mask)
    return page


def to_webp(img: Image.Image) -> bytes:
    for q in (88, 82, 76, 70, 64, 58, 52):
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=q, method=6)
        if buf.tell() <= MAX_BYTES:
            return buf.getvalue()
    raise SystemExit(f"cannot fit under {MAX_BYTES} bytes")


if __name__ == "__main__":
    src, name, out = sys.argv[1:4]
    th = DARK if name == "dark" else LIGHT
    with open(out, "wb") as f:
        f.write(to_webp(frame(Image.open(src), th)))
