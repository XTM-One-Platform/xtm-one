#!/usr/bin/env python3
"""Regenerate the Lucide icon allow-list used by the presentation spec.

``app/services/presentation_icons.py`` is a GENERATED module: the set of icon
names an agent may put in a deck (``callout.icon``, ``feature_list.items[].icon``
...) is exactly the set of icons the frontend bundle can render, read from the
installed ``lucide-react`` package. Validating against a hand-written subset
would either reject icons the renderer supports or, worse, accept a name the
renderer cannot draw — which is a blank box on a slide with no error anywhere.

Usage::

    cd backend && uv run python scripts/refresh_lucide_icons.py          # regenerate
    cd backend && uv run python scripts/refresh_lucide_icons.py --check  # exit 1 if stale

Run it after bumping ``lucide-react`` in ``frontend/package.json``; the test
``tests/test_presentation_spec_v2.py::test_icon_allow_list_matches_installed_lucide``
fails when the two drift (it skips when ``frontend/node_modules`` is absent, so
a backend-only CI job is unaffected).

It needs PyYAML, through ``license_headers`` (the generated module keeps its EE
header): run it with ``uv run`` from ``backend/``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:  # run as a module (``python -m scripts.refresh_lucide_icons``, the generator check)
    from scripts.license_headers import tagged
except ModuleNotFoundError:  # run as a file: ``backend/scripts`` is on the path
    from license_headers import tagged

ROOT = Path(__file__).resolve().parents[2]
LUCIDE_DIR = ROOT / "frontend" / "node_modules" / "lucide-react"
IMPORT_MAP = LUCIDE_DIR / "dist" / "esm" / "dynamicIconImports.js"
PACKAGE_JSON = LUCIDE_DIR / "package.json"
TARGET = ROOT / "backend" / "app" / "services" / "presentation_icons.py"

#: One ``"icon-name": () => import('./icons/file-name.js'),`` line per name. The
#: key is the name an agent may write; the file is the icon it resolves to. The
#: two differ for Lucide's aliases (``"arrow-down-01"`` imports
#: ``arrow-down-0-1.js``), and the FILE name is the one the catalog module
#: (``dist/esm/icons/index.js``) exports in PascalCase — which is what the
#: frontend renderer looks up. So the file name is canonical, the key an alias.
_ENTRY_RE = re.compile(
    r'^\s*"([a-z0-9-]+)":\s*\(\)\s*=>\s*import\(\'\./icons/([a-z0-9-]+)\.js\'\)',
    re.MULTILINE,
)
_VERSION_RE = re.compile(r'"version":\s*"([^"]+)"')

_HEADER = '''"""Lucide icon allow-list for presentation specs. GENERATED FILE.

Do not edit by hand: run ``cd backend && uv run python scripts/refresh_lucide_icons.py``
after bumping ``lucide-react`` in ``frontend/package.json``.

``LUCIDE_ICON_NAMES`` are the canonical kebab-case ids — one per icon file in
``lucide-react/dist/esm/icons/`` — which is exactly what the frontend deck
renderer can draw (``deck-icons.ts`` looks the PascalCase form up in the icon
catalog module). ``LUCIDE_ICON_ALIASES`` are the extra names Lucide's
``dynamicIconImports`` map accepts for the same files (``arrow-down-01`` for
``arrow-down-0-1``); they are accepted on input and stored as their canonical
id, because the catalog module exports canonical names only. An icon name in
neither set would render as nothing, so the spec validator
(``presentation_spec.check_icon``) refuses it with a field-addressed error and
close-match suggestions instead of storing it.

Agents tend to write icon names the way ``lucide-react`` exports them
(``ShieldCheck``); :func:`normalize_icon_name` accepts that spelling — and
``shield_check`` / ``Shield Check`` — and canonicalises to ``shield-check``.
"""

from __future__ import annotations

import difflib
import re

LUCIDE_VERSION = "%(version)s"

LUCIDE_ICON_NAMES: frozenset[str] = frozenset(
    (
%(names)s
    )
)

#: alias -> canonical id (the icon file the alias imports).
LUCIDE_ICON_ALIASES: dict[str, str] = {
%(aliases)s
}

_FOLD_RE = re.compile(r"[^a-z0-9]")
#: ``shieldcheck -> shield-check``: every spelling folds to letters + digits, so
#: PascalCase, snake_case and spaced input all resolve to the canonical id.
#: Aliases fold to their canonical target; canonical names are folded last so
#: they win any overlap.
_FOLDED: dict[str, str] = {}
for _alias, _target in LUCIDE_ICON_ALIASES.items():
    _FOLDED[_FOLD_RE.sub("", _alias)] = _target
for _name in LUCIDE_ICON_NAMES:
    _FOLDED[_FOLD_RE.sub("", _name)] = _name
del _alias, _target, _name


def normalize_icon_name(raw: object) -> str | None:
    """Return the canonical kebab-case Lucide id for *raw*, or ``None``."""
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if candidate in LUCIDE_ICON_NAMES:
        return candidate
    alias_target = LUCIDE_ICON_ALIASES.get(candidate)
    if alias_target is not None:
        return alias_target
    folded = _FOLD_RE.sub("", candidate.lower())
    if not folded:
        return None
    return _FOLDED.get(folded)


def suggest_icon_names(raw: str, *, limit: int = 4) -> list[str]:
    """Close matches for an unknown icon name, for the validation message."""
    folded = _FOLD_RE.sub("", raw.strip().lower())
    if not folded:
        return []
    matches = difflib.get_close_matches(folded, _FOLDED.keys(), n=limit, cutoff=0.6)
    return [_FOLDED[match] for match in matches]
'''


def _render(version: str, names: list[str], aliases: dict[str, str]) -> str:
    body = "\n".join(f'        "{name}",' for name in names)
    alias_body = "\n".join(f'    "{alias}": "{target}",' for alias, target in sorted(aliases.items()))
    return _HEADER % {"version": version, "names": body, "aliases": alias_body}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report whether the generated module is stale without writing (exit 1 if so).",
    )
    args = parser.parse_args()

    if not IMPORT_MAP.is_file():
        print(f"lucide-react is not installed: {IMPORT_MAP} missing (run yarn install)")
        return 2
    version_match = _VERSION_RE.search(PACKAGE_JSON.read_text(encoding="utf-8"))
    if not version_match:
        print(f"could not read the lucide-react version from {PACKAGE_JSON}")
        return 2
    version = version_match.group(1)

    entries = _ENTRY_RE.findall(IMPORT_MAP.read_text(encoding="utf-8"))
    names = sorted({target for _key, target in entries})
    aliases = {key: target for key, target in entries if key != target}
    if len(names) < 1000:
        print(f"only {len(names)} icons parsed from {IMPORT_MAP} - format changed?")
        return 2
    # Every canonical id must be its own key too, or the parse is off.
    keys = {key for key, _target in entries}
    missing = sorted(set(names) - keys)
    if missing:
        print(f"{len(missing)} icon files are not keys of the import map (e.g. {missing[:3]})")
        return 2

    # Two CANONICAL names folding to the same key would make a spelling resolve
    # to the wrong icon; surface it for review (aliases folding onto their own
    # target are expected and silent).
    folded: dict[str, str] = {}
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name)
        if key in folded:
            print(f"note: {folded[key]!r} and {name!r} fold to the same key ({key!r})")
        folded[key] = name

    rendered = tagged(TARGET.relative_to(ROOT).as_posix(), _render(version, names, aliases))
    current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
    summary = f"{len(names)} icons + {len(aliases)} aliases, lucide-react {version}"
    if rendered == current:
        print(f"{TARGET.relative_to(ROOT)} is up to date ({summary})")
        return 0
    if args.check:
        print(f"{TARGET.relative_to(ROOT)} is STALE ({summary})")
        return 1
    TARGET.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(ROOT)}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
