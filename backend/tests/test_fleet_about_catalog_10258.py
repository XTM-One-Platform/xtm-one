"""The page translates every reason code and platform label the checklist sends (#10258, UX round 1 B1).

The frontend's ``PREREQUISITE_REASONS`` and ``PREREQUISITE_LABELS`` tables
(``fleet-prerequisites.ts``) must name exactly the codes and labels
``fleet_about`` emits: a code the page does not know would show nothing, a
label it does not know would show untranslated.
"""

from __future__ import annotations

import pathlib
import re

from app.services import fleet_about

_TS = (pathlib.Path(__file__).resolve().parents[2] / "frontend/src/components/fleets/fleet-prerequisites.ts").read_text(
    encoding="utf-8"
)


def _block(name: str) -> str:
    match = re.search(rf"export const {name} = [\[{{](.*?)[\]}}] as const;", _TS, re.S)
    assert match, name
    return match.group(1)


def test_the_page_knows_every_reason_code():
    page = set(re.findall(r"^\s+(\w+): \"", _block("PREREQUISITE_REASONS"), re.M))
    assert page == set(fleet_about.REASON_CODES)


def test_the_page_knows_every_platform_label():
    page = set(re.findall(r"^\s+\"([^\"]+)\",", _block("PREREQUISITE_LABELS"), re.M))
    emitted = {
        value for name, value in vars(fleet_about).items() if name.startswith("LABEL_") and isinstance(value, str)
    }
    assert page == emitted | set(fleet_about.PROFILE_LABELS.values())
