"""Animated walkthrough of the README kit: the hero and the six gallery captures in sequence.

Usage:
    python .github/scripts/readme-kit/walkthrough.py IMG_DIR THEME OUT.webp
Reads IMG_DIR/hero-THEME.webp and IMG_DIR/g1..g6-THEME.webp (the framed 2880x1800
captures), scales them to WIDTH, holds each scene HOLD_MS and cross-fades into the
next over FADE_STEPS frames, then loops. Output is an animated WebP, quality lowered
step by step until it is under 5 MB (doc 33 s.3: 10 to 20 s, under 5 MB, both themes).
"""
import io
import os
import sys

from PIL import Image

SCENES = ("hero", "g1", "g2", "g3", "g4", "g5", "g6")
WIDTH = 1440                 # half the capture canvas: GitHub shows it at most 880 px wide
HOLD_MS = 1800               # time on each scene
FADE_STEPS, FADE_MS = 6, 60  # cross-fade
MAX_BYTES = 5 * 1024 * 1024


def frames(img_dir: str, theme: str) -> tuple[list[Image.Image], list[int]]:
    shots = []
    for s in SCENES:
        im = Image.open(os.path.join(img_dir, f"{s}-{theme}.webp")).convert("RGB")
        shots.append(im.resize((WIDTH, round(im.height * WIDTH / im.width)), Image.LANCZOS))
    out, dur = [], []
    for i, cur in enumerate(shots):
        nxt = shots[(i + 1) % len(shots)]
        out.append(cur)
        dur.append(HOLD_MS)
        for k in range(1, FADE_STEPS + 1):
            out.append(Image.blend(cur, nxt, k / (FADE_STEPS + 1)))
            dur.append(FADE_MS)
    return out, dur


def to_webp(out: list[Image.Image], dur: list[int]) -> bytes:
    for q in (80, 72, 64, 56, 48, 40):
        buf = io.BytesIO()
        out[0].save(buf, "WEBP", save_all=True, append_images=out[1:], duration=dur,
                    loop=0, quality=q, method=4)
        if buf.tell() <= MAX_BYTES:
            return buf.getvalue()
    raise SystemExit(f"cannot fit under {MAX_BYTES} bytes")


if __name__ == "__main__":
    img_dir, theme, dst = sys.argv[1:4]
    fr, du = frames(img_dir, theme)
    data = to_webp(fr, du)
    with open(dst, "wb") as f:
        f.write(data)
    print(f"{dst}: {len(fr)} frames, {sum(du) / 1000:.1f} s, {len(data) / 1024:.0f} KB")
