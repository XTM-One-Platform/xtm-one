"""Every "Settings > X" a person reads names a section the Settings page has.

The Settings page groups its pages in sections (``frontend/src/pages/Settings.tsx``:
the top tabs, then each section's sub-navigation). Copy that sends a person to
"Settings > Security" after the section became "Access and security" leaves
them looking for a place that does not exist (#9173, UX review of #9359).

This reads the English catalog and the string literals of ``backend/app``
(docstrings and comments are not shown to anyone and are skipped), takes what
follows "Settings >" (or "Settings →"), and checks it starts with a label
Settings.tsx declares. Another product's own settings ("GitHub > Settings >
...", the setup steps of an integration) is not ours and is skipped.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_TSX = _ROOT / "frontend" / "src" / "pages" / "Settings.tsx"
_EN_JSON = _ROOT / "frontend" / "src" / "i18n" / "lang" / "en.json"
_BACKEND_APP = _ROOT / "backend" / "app"

#: "Settings >" or "Settings →" not preceded by another product's path ("GitHub > Settings").
_PATH = re.compile(r"(?<![>→] )(?<!Account )\bSettings ?(?:>|→) ?")

#: Files whose strings are third-party setup steps (the product's own settings pages).
_THIRD_PARTY_FILES = (
    "integrations/providers/",
    "api/v1/integration_catalog.py",
    "api/v1/integration_github_webhooks.py",
    "api/v1/openrouter.py",
    "api/v1/channel_bots.py",
    "api/v1/webhooks.py",
)


def _labels() -> set[str]:
    source = _SETTINGS_TSX.read_text(encoding="utf-8")
    labels = set(re.findall(r'label: "([^"]+)"', source))
    assert "Access and security" in labels, 'Settings.tsx no longer declares its sections as label: "..."'
    return labels


def _offenders(text: str, labels: set[str]) -> list[str]:
    found = []
    for match in _PATH.finditer(text):
        rest = text[match.end() :]
        if not rest[:1].isupper():
            continue
        if not any(rest.startswith(label) and not rest[len(label) : len(label) + 1].isalnum() for label in labels):
            found.append(f"{' '.join(rest.split()[:3])!r} in: {' '.join(text.split())[:160]}")
    return found


def _shown_strings(source: str) -> list[str]:
    """The string literals of a module, implicit concatenations joined, docstrings left out."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def test_the_english_catalog_names_only_real_sections():
    labels = _labels()
    catalog = json.loads(_EN_JSON.read_text(encoding="utf-8"))
    offenders = []
    for key, value in catalog.items():
        offenders += _offenders(key, labels)
        if value != key:
            offenders += _offenders(value, labels)
    assert not offenders, "Stale Settings paths in en.json:\n" + "\n".join(sorted(set(offenders)))


def test_backend_strings_name_only_real_sections():
    labels = _labels()
    offenders = []
    for path in sorted(_BACKEND_APP.rglob("*.py")):
        relative = path.relative_to(_BACKEND_APP).as_posix()
        if relative.startswith(_THIRD_PARTY_FILES):
            continue
        for text in _shown_strings(path.read_text(encoding="utf-8")):
            offenders += [f"backend/app/{relative}: {line}" for line in _offenders(text, labels)]
    assert not offenders, "Stale Settings paths in backend/app:\n" + "\n".join(offenders)


def test_the_gate_catches_a_stale_section():
    labels = _labels()
    assert _offenders("An administrator connects one in Settings > Channels.", labels)
    assert _offenders("Change it in Settings → Security → Tool approvals.", labels)
    assert not _offenders("Settings > Access and security > Tool approvals", labels)
    assert not _offenders("Settings → Integrations → Channels & Bots", labels)
    assert not _offenders("open WhatsApp > Settings > Linked devices", labels)
