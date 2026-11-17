"""Every retry or fix-and-save-again loop a seeded skill or persona prescribes has a cap.

A loop with no stated cap lets one turn spend its whole budget on one object (a
video saved again and again on a finding no edit clears, a scan polled forever).
The rule ported from the fleet sweep: a line that tells the agent to call, save,
resubmit or retry again states its cap ("at most N") and what it does at the cap,
or sits in a section that does. A line that forbids the retry ("do not retry",
"never retry", "not a retry") is not a loop.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SEED_DATA = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data"
SEEDED_TEXTS = sorted([*(SEED_DATA / "skill_content").glob("*.md"), *(SEED_DATA / "agent_persona").glob("*.md")])

LOOP = re.compile(
    r"(?i)\b(save again|call again|capture again|resubmit|retry|regenerate the same"
    r"|until it (reads|is))\b"
)
CAP = re.compile(r"(?i)\bat most\b|\bone time only\b")
# A forbidden retry, and a single call made again with the user's answer, are not loops.
NOT_A_LOOP = re.compile(r"(?i)\b(not|never|no)( a)? retry\b|\bcall again with the answer\b")
QUOTED = re.compile(r'"[^"\n]*"')


def _sections(text: str) -> list[str]:
    sections: list[list[str]] = [[]]
    for line in text.split("\n"):
        if line.startswith("#") and sections[-1]:
            sections.append([])
        sections[-1].append(line)
    return ["\n".join(lines) for lines in sections]


def _uncapped_loops(text: str) -> list[str]:
    found = []
    for section in _sections(QUOTED.sub('""', text)):
        section_capped = bool(CAP.search(section))
        for line in section.split("\n"):
            if section_capped or CAP.search(line) or not LOOP.search(NOT_A_LOOP.sub("", line)):
                continue
            found.append(line.strip()[:120])
    return found


def test_the_seeded_texts_are_found() -> None:
    assert len(SEEDED_TEXTS) > 50


@pytest.mark.parametrize("path", SEEDED_TEXTS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_seeded_loop_states_its_cap(path: Path) -> None:
    assert _uncapped_loops(path.read_text(encoding="utf-8")) == []


def test_the_guard_catches_an_uncapped_loop() -> None:
    assert _uncapped_loops("## Fix\nFix it with `edit_video` and save again until it reads clean.\n")
    assert not _uncapped_loops("## Fix\nSave again until it reads clean, at most 6 rounds; then list what is open.\n")
    assert not _uncapped_loops("## Scope\nA creation rejected as out-of-scope: do NOT retry against the perimeter.\n")
    assert not _uncapped_loops("## Ask\nIt returns the list: ask which one, then call again with the answer.\n")
