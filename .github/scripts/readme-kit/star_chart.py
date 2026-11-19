"""Branded GitHub stars chart, light and dark, for the product READMEs.

Usage (from the repository root, a token with read access in GITHUB_TOKEN):
    python .github/scripts/readme-kit/star_chart.py OWNER/NAME .github/img/readme

Reads every stargazer with its star date from the GitHub REST API
(Accept: application/vnd.github.star+json, 100 per page), counts the
cumulative stars at each month end, and writes stars-dark.svg and
stars-light.svg in the Filigran palette. Refresh it when we want: the data is
fetched at build time, nothing calls a third-party chart service.
"""
import datetime as dt
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import svgkit as k  # noqa: E402
import textpath as tp  # noqa: E402
from theme import THEMES  # noqa: E402

W, H = 1440, 560
L, R, T, B = 120, 56, 150, 84


def star_dates(repo: str) -> list[dt.date]:
    token = os.environ.get("GITHUB_TOKEN", "")
    dates, page = [], 1
    while True:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/stargazers?per_page=100&page={page}",
            headers={"Accept": "application/vnd.github.star+json",
                     **({"Authorization": f"Bearer {token}"} if token else {})})
        with urllib.request.urlopen(req) as r:
            rows = json.load(r)
        if not rows:
            break
        dates += [dt.date.fromisoformat(x["starred_at"][:10]) for x in rows]
        page += 1
    return sorted(dates)


def daily(dates: list[dt.date]) -> list[tuple[dt.date, int]]:
    out, d, i = [], dates[0], 0
    today = dt.date.today()
    while d <= today:
        while i < len(dates) and dates[i] <= d:
            i += 1
        out.append((d, i))
        d += dt.timedelta(days=1)
    return out


def monthly(dates: list[dt.date]) -> list[tuple[dt.date, int]]:
    """Cumulative stars per month; per day when the history is under 90 days."""
    if not dates:
        return []
    if (dt.date.today() - dates[0]).days < 90:
        return daily(dates)
    y, m = dates[0].year, dates[0].month
    today = dt.date.today()
    out, i = [], 0
    while (y, m) <= (today.year, today.month):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = dt.date(ny, nm, 1)
        while i < len(dates) and dates[i] < end:
            i += 1
        out.append((dt.date(y, m, 1), i))
        y, m = ny, nm
    return out


def _nice(v: int) -> int:
    """Smallest 1, 2 or 5 times a power of ten giving at most six grid steps."""
    for p in range(0, 7):
        for step in (1, 2, 5):
            s = step * 10 ** p
            if v / s <= 6:
                return s
    return 10 ** 6


def chart(theme: dict, repo: str, series: list[tuple[dt.date, int]]) -> str:
    tp.reset_defs()
    total = series[-1][1] if series else 0
    name = repo.split("/")[1]
    unit = "day" if len(series) > 1 and (series[-1][0] - series[0][0]).days < 90 else "month"
    b = [k.header(theme, "GitHub stars", f"github.com/{repo}, cumulative stars per {unit}")]
    pw, ph = W - L - R, H - T - B
    top = max(total, 1)
    step = _nice(top)
    ymax = ((top + step - 1) // step) * step
    for v in range(0, ymax + 1, step):
        y = T + ph - ph * v / ymax
        b.append(f'<path d="M{L} {y:.1f}H{W - R}" stroke="{theme["grid"]}" stroke-width="1"/>')
        b.append(k.text(f"{v:,}", L - 16, y + 6, 17, theme["muted"], "body", 400, "end"))
    n = max(len(series) - 1, 1)
    pts = [(L + pw * i / n, T + ph - ph * c / ymax) for i, (_, c) in enumerate(series)]
    short = len(series) > 1 and (series[-1][0] - series[0][0]).days < 90
    if short:
        ticks = [(i, d.strftime("%d %b")) for i, (d, _) in enumerate(series)]
        ticks = ticks[:: max(1, len(ticks) // 8)]
    else:
        ticks = []
        for yr in sorted({d.year for d, _ in series}):
            ticks.append((next(j for j, (d, _) in enumerate(series) if d.year == yr), str(yr)))
    for i, lab in ticks:
        b.append(k.text(lab, L + pw * i / n, H - B + 36, 17, theme["muted"], "body", 400, "middle"))
    if pts:
        line = "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
        area = line + f"L{pts[-1][0]:.1f} {T + ph}L{pts[0][0]:.1f} {T + ph}Z"
        gid = f"fill-{theme['name']}"
        b.insert(0, f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
                    f'<stop offset="0" stop-color="{theme["accent"]}" stop-opacity="0.35"/>'
                    f'<stop offset="1" stop-color="{theme["accent"]}" stop-opacity="0"/></linearGradient></defs>')
        b.append(f'<path d="{area}" fill="url(#{gid})"/>')
        b.append(f'<path d="{line}" stroke="{theme["accent"]}" stroke-width="3.5" fill="none" stroke-linejoin="round"/>')
        x, y = pts[-1]
        b.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{theme["accent"]}" stroke="{theme["page"]}" stroke-width="3"/>')
    return k.canvas(theme, f"{name} GitHub stars", b, W, H)


def main() -> None:
    repo, out = sys.argv[1], sys.argv[2]
    series = monthly(star_dates(repo))
    os.makedirs(out, exist_ok=True)
    for t in THEMES:
        with open(os.path.join(out, f"stars-{t['name']}.svg"), "w") as f:
            f.write(chart(t, repo, series))


if __name__ == "__main__":
    main()
