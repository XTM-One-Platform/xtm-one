"""Every seeded fleet persona carries the same honesty block, once (#10632).

The fleet sweep found the honesty rules of the seeded fleets worded
differently in each persona, and missing from some. The block below is the
single wording: it closes the persona of every agent a seeded fleet owns
(tag ``fleet``), present exactly once, whether the persona is a file of
``seed_data/agent_persona`` or a string inlined in Python (#10688).
"""

from __future__ import annotations

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.agents.seed import _resolve_seed_content

HONESTY_BLOCK = (
    "## Honesty\n"
    "- Never assume: an id, a name, a score or a figure no tool returned is written as unknown, "
    "never invented or guessed.\n"
    "- Verify after every action: read back what you wrote or proposed before you report it.\n"
    "- Say what you did not check: a source you could not read, a tool that failed or a field "
    "that was missing is named in your answer.\n"
)


def _fleet_owned_personas() -> list[tuple[str, str]]:
    agents = {a["slug"]: a for a in BUILTIN_AGENTS}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for fleet in BUILTIN_FLEETS:
        for member in fleet["members"]:
            agent = agents.get(member["agent"])
            if not agent or agent["slug"] in seen or "fleet" not in (agent.get("tags") or []):
                continue
            seen.add(agent["slug"])
            out.append((agent["slug"], _resolve_seed_content("agent_persona", agent.get("persona", agent["slug"])) or ""))
    return out


def test_the_guard_reads_every_fleet_owned_persona() -> None:
    # Fleets F1 to F5, M1, M2 and M3 own 20 personas today, inline ones included.
    slugs = {slug for slug, _ in _fleet_owned_personas()}
    assert len(slugs) >= 20
    assert "ctem-posture-orchestrator" in slugs


@pytest.mark.parametrize(("slug", "text"), _fleet_owned_personas(), ids=lambda v: v if len(v) < 80 else "persona")
def test_a_fleet_persona_ends_with_the_honesty_block_once(slug: str, text: str) -> None:
    assert text.count("## Honesty") == 1, f"{slug}: the honesty block must appear exactly once"
    assert text.endswith(HONESTY_BLOCK), f"{slug}: the persona must end with the shared honesty block, word for word"
