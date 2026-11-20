"""Rule H3 (Sam, 19 Nov 2026): WCAG AA contrast gate for every text / background
pair the README kit draws, in both themes.

Every text the kit paints (banner, diagrams, cards, stage pills, stars chart,
frames) takes its colour from theme.py or banner.py; this script lists each pair
the generators use, computes the WCAG 2.x contrast ratio and fails (exit 1) on
any pair under 4.5:1 (3:1 for large text, >= 24 px regular or >= 18.7 px bold
at 1x). It also refuses white text on a light fill (luminance > 0.4) anywhere.

    python contrast.py            # table (Markdown) and exit status
"""

import sys

import banner
import theme

PAIRS = (
    # (where, fg token, bg token, size px, bold)
    ("diagram and card titles", "fg", "card", 21, True),
    ("diagram and card subtitles", "muted", "card", 18, False),
    ("panel subtitles on the raised card", "muted", "card2", 18, False),
    ("panel titles on the raised card", "fg", "card2", 21, True),
    ("figure title on the page", "fg", "page", 40, True),
    ("figure subtitle and lane labels on the page", "muted", "page", 16, True),
    ("stars chart axis labels", "muted", "page", 17, False),
    ("stage number on the accent dot", "page", "accent", 22, True),
    ("pill label on the accent fill", "page", "accent", 18, True),
)


def _lum(hex_: str) -> float:
    h = hex_.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _large(size: int, bold: bool) -> bool:
    return size >= 24 or (bold and size >= 18.7)


def rows() -> list[tuple]:
    out = []
    for t in theme.THEMES:
        for where, f, b, size, bold in PAIRS:
            out.append((t["name"], where, t[f], t[b], size, bold))
    for name in ("dark", "light"):  # the banner is Filigran blue on both themes
        out.append((name, "banner lockup and By Filigran", banner.WHITE, banner.BLUE, 60, True))
    out.append(("both", "badge value (shields.io)", "#FFFFFF", "#001BDA", 11, False))
    out.append(("both", "badge label (shields.io)", "#FFFFFF", "#0A0C1C", 11, False))
    return out


def main() -> int:
    bad = 0
    print("| theme | where | text | background | ratio | needs | result |")
    print("|---|---|---|---|---|---|---|")
    for th, where, f, b, size, bold in rows():
        r = ratio(f, b)
        need = 3.0 if _large(size, bold) else 4.5
        white_on_light = f.upper() in ("#FFFFFF", "#FBFBFB") and _lum(b) > 0.4
        ok = r >= need and not white_on_light
        bad += not ok
        print(f"| {th} | {where} | {f} | {b} | {r:.2f}:1 | {need}:1 | {'PASS' if ok else 'FAIL'} |")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
