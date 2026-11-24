"""Seeded prompts speak OpenCRQ's language: primary assets, never crown jewels (#1591).

The organization profile key ``crown_jewels`` stays as the stored key and alias
until the key rename slice; this check reads the prose a person or an agent
reads: seeded personas, skills, CTEM templates, output schemas and the fleet
docs and agent definitions written in Python.
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
SEED_DATA = APP / "agents" / "seed_data"
PYTHON_SEEDS = (
    APP / "agents" / "builtin_agents.py",
    APP / "agents" / "builtin_fleet_docs.py",
    APP / "agents" / "builtin_profile_readers.py",
    APP / "agents" / "openaev_detection_desk.py",
    APP / "agents" / "ctem_third_party_risk.py",
)
# The prose form only: "crown jewel(s)" with a space or a hyphen. The
# underscore key crown_jewels is the stored key, kept as the alias.
_PROSE = re.compile(r"crown[ -]jewel", re.IGNORECASE)


def _offenders(paths: list[Path]) -> list[str]:
    found: list[str] = []
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _PROSE.search(line):
                found.append(f"{path.relative_to(APP)}:{number}: {line.strip()[:120]}")
    return found


def test_no_seeded_persona_skill_or_schema_says_crown_jewel() -> None:
    files = [p for p in SEED_DATA.rglob("*") if p.is_file() and p.suffix in {".md", ".json", ".txt", ".yaml", ".yml"}]
    assert files, "seed_data holds no file: the check would pass on nothing"
    assert _offenders(files) == []


def test_no_seeded_agent_or_fleet_doc_says_crown_jewel() -> None:
    assert _offenders(list(PYTHON_SEEDS)) == []
