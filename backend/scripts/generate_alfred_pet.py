"""Draw Alfred, the Concierge companion's built-in pet (#8261), and write its package.

Alfred is an original character: an elderly gentleman butler in a tailcoat,
bow tie and white gloves, with a silver tray and a pocket watch. Every pixel
is drawn by this script, nothing is traced or copied from existing art, and
the output is regenerated from it alone:

    cd backend && uv run python scripts/generate_alfred_pet.py

It writes ``app/agents/seed_data/health_pets/alfred/pet.json`` and
``spritesheet.webp``: a version 1 Codex pet atlas, 8 columns of 192x208 cells
and 9 rows, one state a row with the format's frame counts
(``app/services/health/pets.py``, ``STATES``). Each cell is drawn on a
48x52 grid and scaled 4x, nearest neighbour, so the frames stay consistent.
``--check`` regenerates in memory and fails when the committed atlas differs.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.health.pets import CELL_HEIGHT, CELL_WIDTH, COLUMNS, ROWS_BY_VERSION, STATES

OUT_DIR = ROOT / "app" / "agents" / "seed_data" / "health_pets" / "alfred"
GRID_W, GRID_H = 48, 52
SCALE = CELL_WIDTH // GRID_W
if GRID_W * SCALE != CELL_WIDTH or GRID_H * SCALE != CELL_HEIGHT:
    raise SystemExit("the drawing grid must scale to the format's cell exactly")

Color = tuple[int, int, int, int]


def _hex(value: str) -> Color:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), 255


OUTLINE = _hex("0b0c10")
SKIN, SKIN_SHADE = _hex("f1c9a5"), _hex("d9a47c")
HAIR, HAIR_SHADE = _hex("f4f4f2"), _hex("c4c7cc")
EYE = _hex("2a2420")
COAT, COAT_SHADE, COAT_LIGHT = _hex("252a33"), _hex("171a20"), _hex("3b4250")
SHIRT, SHIRT_SHADE = _hex("fbfbfb"), _hex("d5dbe2")
TIE = _hex("111216")
GLOVE, GLOVE_SHADE = _hex("ffffff"), _hex("cdd3da")
TROUSERS = _hex("30353f")
SHOE, SHOE_SHINE = _hex("0d0d10"), _hex("505665")
SILVER, SILVER_DARK, SILVER_LIGHT = _hex("cfd7df"), _hex("8995a3"), _hex("ffffff")
GOLD, GOLD_DARK = _hex("e4b43e"), _hex("9c731a")
PAPER, SEAL = _hex("fff7e3"), _hex("c0392b")
SWEAT = _hex("86ccff")


@dataclass(frozen=True)
class Pose:
    dy: int = 0  # body bob, in grid pixels (down is positive)
    left_hand: tuple[int, int] = (14, 34)
    left_elbow: tuple[int, int] | None = None
    right_hand: tuple[int, int] = (34, 34)
    right_elbow: tuple[int, int] | None = None
    left_foot: tuple[int, int] = (0, 0)  # (dx, lift)
    right_foot: tuple[int, int] = (0, 0)
    eyes: str = "open"  # open | closed | worried
    tray: str | None = None  # held | tilted | falling | floor | present
    tray_at: tuple[int, int] = (37, 25)
    watch: bool = False
    letter: bool = False
    seal_lit: bool = True
    sweat: bool = False
    squash: int = 0  # crouch before and after a jump


class Canvas:
    def __init__(self) -> None:
        self.image = Image.new("RGBA", (GRID_W, GRID_H), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)

    def rect(self, x0: int, y0: int, x1: int, y1: int, color: Color) -> None:
        self.draw.rectangle((x0, y0, x1, y1), fill=color)

    def px(self, x: int, y: int, color: Color) -> None:
        if 0 <= x < GRID_W and 0 <= y < GRID_H:
            self.image.putpixel((x, y), color)

    def ellipse(self, x0: int, y0: int, x1: int, y1: int, color: Color) -> None:
        self.draw.ellipse((x0, y0, x1, y1), fill=color)

    def line(self, a: tuple[int, int], b: tuple[int, int], color: Color, width: int = 1) -> None:
        self.draw.line((a, b), fill=color, width=width)

    def outline(self) -> Image.Image:
        """The sprite with a one-pixel dark outline around every opaque pixel."""
        src = self.image.copy()
        out = self.image.copy()
        for y in range(GRID_H):
            for x in range(GRID_W):
                if src.getpixel((x, y))[3]:
                    continue
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if 0 <= nx < GRID_W and 0 <= ny < GRID_H and src.getpixel((nx, ny))[3]:
                        out.putpixel((x, y), OUTLINE)
                        break
        return out


def _legs(c: Canvas, pose: Pose, top: int) -> None:
    floor = 47
    for base_x, (dx, lift) in ((19, pose.left_foot), (26, pose.right_foot)):
        x = base_x + dx
        c.rect(x, top, x + 3, floor - 2 - lift, TROUSERS)
        c.rect(x - 1, floor - 1 - lift, x + 4, floor - lift, SHOE)
        c.px(x + 3, floor - 1 - lift, SHOE_SHINE)


def _arm(c: Canvas, shoulder: tuple[int, int], elbow: tuple[int, int] | None, hand: tuple[int, int]) -> None:
    joint = elbow or ((shoulder[0] + hand[0]) // 2, (shoulder[1] + hand[1]) // 2)
    c.line(shoulder, joint, COAT, width=3)
    c.line(joint, hand, COAT, width=3)
    c.px(joint[0], joint[1], COAT_LIGHT)
    hx, hy = hand
    c.rect(hx - 1, hy - 1, hx + 1, hy + 1, GLOVE)
    c.px(hx + 1, hy + 1, GLOVE_SHADE)


def _tray(c: Canvas, pose: Pose, dy: int) -> None:
    x, y = pose.tray_at
    y += dy
    if pose.tray in ("held", "present"):
        c.ellipse(x - 6, y - 1, x + 6, y + 2, SILVER_DARK)
        c.ellipse(x - 6, y - 2, x + 6, y + 1, SILVER)
        c.line((x - 3, y - 1), (x + 1, y - 1), SILVER_LIGHT)
        if pose.letter:
            c.rect(x - 3, y - 4, x + 3, y - 2, PAPER)
            c.line((x - 3, y - 4), (x, y - 3), SHIRT_SHADE)
            c.line((x, y - 3), (x + 3, y - 4), SHIRT_SHADE)
            if pose.seal_lit:
                c.rect(x - 1, y - 3, x, y - 2, SEAL)
    elif pose.tray == "tilted":
        c.line((x - 6, y - 4), (x + 6, y + 2), SILVER_DARK, width=2)
        c.line((x - 6, y - 5), (x + 5, y + 1), SILVER)
    elif pose.tray == "falling":
        c.line((x - 5, y + 2), (x + 5, y - 2), SILVER_DARK, width=2)
        c.line((x - 5, y + 1), (x + 4, y - 3), SILVER)
    elif pose.tray == "floor":
        c.ellipse(x - 6, 45, x + 6, 47, SILVER_DARK)
        c.line((x - 4, 45), (x + 3, 45), SILVER)


def _watch(c: Canvas, hand: tuple[int, int]) -> None:
    hx, hy = hand
    c.line((24, 27), (hx, hy - 2), GOLD_DARK)
    c.ellipse(hx - 2, hy - 5, hx + 2, hy - 1, GOLD)
    c.px(hx, hy - 3, GOLD_DARK)
    c.px(hx, hy - 4, OUTLINE)
    c.px(hx + 1, hy - 3, OUTLINE)


def _head(c: Canvas, pose: Pose, dy: int) -> None:
    top = 5 + dy + pose.squash
    # Face and the bald crown, a shine on top.
    c.ellipse(18, top, 30, top + 13, SKIN)
    c.rect(18, top + 8, 30, top + 10, SKIN)
    c.px(21, top + 2, SILVER_LIGHT)
    c.px(22, top + 1, SILVER_LIGHT)
    # White hair on the sides and the back, ears peeking out.
    c.rect(17, top + 5, 18, top + 10, HAIR)
    c.rect(30, top + 5, 31, top + 10, HAIR)
    c.px(17, top + 10, HAIR_SHADE)
    c.px(31, top + 10, HAIR_SHADE)
    c.px(16, top + 7, SKIN_SHADE)
    c.px(32, top + 7, SKIN_SHADE)
    # Brows, eyes, nose and the trim white moustache.
    brow_y = top + 5 + (1 if pose.eyes == "worried" else 0)
    c.line((20, brow_y), (22, brow_y - (1 if pose.eyes == "worried" else 0)), HAIR_SHADE)
    c.line((26, brow_y - (1 if pose.eyes == "worried" else 0)), (28, brow_y), HAIR_SHADE)
    eye_y = top + 7
    if pose.eyes == "closed":
        c.line((20, eye_y), (22, eye_y), EYE)
        c.line((26, eye_y), (28, eye_y), EYE)
    else:
        c.rect(21, eye_y - 1, 21, eye_y, EYE)
        c.rect(27, eye_y - 1, 27, eye_y, EYE)
    c.px(24, eye_y + 1, SKIN_SHADE)
    c.px(24, eye_y + 2, SKIN_SHADE)
    c.line((21, eye_y + 3), (27, eye_y + 3), HAIR)
    c.px(20, eye_y + 4, HAIR)
    c.px(28, eye_y + 4, HAIR)
    mouth = SKIN_SHADE if pose.eyes != "worried" else EYE
    c.line((23, eye_y + 5), (25, eye_y + 5), mouth)
    if pose.sweat:
        c.px(33, top + 3, SWEAT)
        c.rect(33, top + 4, 34, top + 5, SWEAT)


def draw_frame(pose: Pose) -> Image.Image:
    c = Canvas()
    dy = pose.dy
    body_top = 19 + dy + pose.squash
    waist = 36 + dy
    # Coat tails behind, then the legs.
    c.rect(16, waist - 2, 19, waist + 5, COAT_SHADE)
    c.rect(29, waist - 2, 32, waist + 5, COAT_SHADE)
    _legs(c, pose, waist)
    # Tailcoat body with lapels, the shirt front and the bow tie.
    c.rect(16, body_top + 1, 32, waist, COAT)
    c.rect(17, body_top, 31, body_top + 1, COAT)
    # The shirt front, a V narrowing to the waistcoat, with its studs; the lapels edge it.
    depth = waist - 3 - body_top
    for row in range(depth + 1):
        half = 3 - (3 * row) // max(depth, 1)
        c.rect(24 - half, body_top + row, 24 + half, body_top + row, SHIRT)
        c.px(24 - half - 1, body_top + row, COAT_LIGHT)
        c.px(24 + half + 1, body_top + row, COAT_LIGHT)
    c.px(23, body_top + 3, SHIRT_SHADE)
    c.px(24, body_top + 4, EYE)
    c.px(24, body_top + 7, EYE)
    c.rect(17, waist - 1, 31, waist, COAT_SHADE)
    c.rect(21, body_top, 27, body_top + 1, TIE)
    c.rect(23, body_top, 25, body_top + 1, COAT_LIGHT)
    c.rect(23, body_top - 1, 25, body_top - 1, SKIN_SHADE)
    _head(c, pose, dy)
    offset = (0, dy + pose.squash)

    def moved(point: tuple[int, int] | None) -> tuple[int, int] | None:
        return None if point is None else (point[0] + offset[0], point[1] + offset[1])

    _arm(c, (17, body_top + 2), moved(pose.left_elbow), moved(pose.left_hand))  # type: ignore[arg-type]
    if pose.tray in ("held", "present"):
        _tray(c, pose, dy + pose.squash)
    _arm(c, (31, body_top + 2), moved(pose.right_elbow), moved(pose.right_hand))  # type: ignore[arg-type]
    if pose.tray in ("tilted", "falling", "floor"):
        _tray(c, pose, dy)
    if pose.watch:
        _watch(c, moved(pose.left_hand))  # type: ignore[arg-type]
    return c.outline()


# ── The states, one pose list each ─────────────────────────────────────────

HOLD = Pose(right_elbow=(35, 30), right_hand=(37, 27), tray="held")


def idle(i: int) -> Pose:
    return replace(HOLD, dy=(0, 0, 1, 1, 0, 0)[i], eyes="closed" if i == 5 else "open")


def walk(i: int) -> Pose:
    stride = (-2, -1, 0, 1, 2, 1, 0, -1)[i]
    return replace(
        HOLD,
        dy=(0, 1, 0, 1, 0, 1, 0, 1)[i],
        left_foot=(stride, 1 if stride > 0 else 0),
        right_foot=(-stride, 1 if stride < 0 else 0),
        left_hand=(14 - stride, 34),
    )


def walk_left(i: int) -> Pose:
    return walk(i)


def waving(i: int) -> Pose:
    hands = ((38, 11), (41, 13), (38, 11), (35, 13))
    return Pose(right_elbow=(37, 18), right_hand=hands[i], dy=(0, 0, 0, 1)[i])


def jumping(i: int) -> Pose:
    return replace(
        HOLD,
        dy=(0, -4, -7, -4, 0)[i],
        squash=(1, 0, 0, 0, 1)[i],
        left_hand=(13, 30) if i in (1, 2, 3) else (14, 34),
        left_foot=(0, 0),
        right_foot=(0, 0),
        eyes="closed" if i == 2 else "open",
    )


def failed(i: int) -> Pose:
    trays = ("held", "tilted", "tilted", "falling", "falling", "floor", "floor", "floor")
    base = Pose(
        right_elbow=(35, 30) if i < 3 else (36, 31),
        right_hand=(37, 27) if i < 3 else (37, 33),
        tray=trays[i],
        tray_at=(37, 25 + (0, 0, 1, 7, 13, 0, 0, 0)[i]),
        eyes="worried" if i >= 2 else "open",
        sweat=i >= 4,
        dy=1 if i >= 5 else 0,
    )
    if i >= 5:
        # A gloved hand to the forehead.
        base = replace(base, left_elbow=(15, 22), left_hand=(20, 13), eyes="closed")
    return base


def waiting(i: int) -> Pose:
    return Pose(
        left_elbow=(15, 31),
        left_hand=(22, 30) if i % 2 == 0 else (22, 29),
        watch=True,
        right_hand=(34, 34),
        right_foot=(1, (0, 1, 0, 1, 0, 0)[i]),
        eyes="closed" if i == 5 else "open",
    )


def busy(i: int) -> Pose:
    swing = (-2, -1, 1, 2, 1, -1)[i]
    return Pose(
        dy=(0, 1, 0, 1, 0, 1)[i],
        left_hand=(14 + swing, 33),
        right_hand=(34 + swing, 33),
        left_foot=(0, 1 if i % 3 == 1 else 0),
        right_foot=(0, 1 if i % 3 == 2 else 0),
    )


def review(i: int) -> Pose:
    return Pose(
        left_elbow=(17, 31),
        left_hand=(19, 29),
        right_elbow=(31, 31),
        right_hand=(29, 29),
        tray="present",
        tray_at=(24, 28),
        letter=True,
        seal_lit=i % 2 == 0,
        dy=(0, 0, 1, 1, 0, 0)[i],
        eyes="closed" if i == 3 else "open",
    )


POSES: dict[str, Callable[[int], Pose]] = {
    "idle": idle,
    "running-right": walk,
    "running-left": walk_left,
    "waving": waving,
    "jumping": jumping,
    "failed": failed,
    "waiting": waiting,
    "running": busy,
    "review": review,
}


def build_atlas() -> Image.Image:
    atlas = Image.new("RGBA", (COLUMNS * CELL_WIDTH, ROWS_BY_VERSION[1] * CELL_HEIGHT), (0, 0, 0, 0))
    for state, row, durations in STATES:
        for column in range(len(durations)):
            frame = draw_frame(POSES[state](column))
            if state == "running-left":
                frame = frame.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            cell = frame.resize((CELL_WIDTH, CELL_HEIGHT), Image.Resampling.NEAREST)
            atlas.paste(cell, (column * CELL_WIDTH, row * CELL_HEIGHT))
    return atlas


MANIFEST = {
    "id": "alfred",
    "displayName": "Alfred",
    "description": "A gentleman butler with a silver tray: he shows what the Concierge is doing.",
    "spritesheetPath": "spritesheet.webp",
}


def encode(atlas: Image.Image) -> bytes:
    out = io.BytesIO()
    atlas.save(out, format="WEBP", lossless=True, quality=100, method=6)
    return out.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed atlas is not this script's")
    args = parser.parse_args()
    atlas = build_atlas()
    if args.check:
        committed = Image.open(OUT_DIR / "spritesheet.webp").convert("RGBA")
        same = committed.size == atlas.size and committed.tobytes() == atlas.tobytes()
        print("up to date" if same else "differs: rerun scripts/generate_alfred_pet.py")
        return 0 if same else 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "spritesheet.webp").write_bytes(encode(atlas))
    (OUT_DIR / "pet.json").write_text(json.dumps(MANIFEST, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT_DIR / 'spritesheet.webp'} {atlas.size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
