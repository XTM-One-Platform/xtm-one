"""Platform Health S1 (#101): the seeded skills ``concierge-*`` are renamed ``health-*`` in place.

The seed adopts a row still tagged ``slug:<former>`` under its current slug, so the row (and
every agent's ``skill_ids`` pointing at it) survives the rename instead of being deleted as
stale and inserted again under a new id.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.agents import seed
from app.agents.builtin_agents import BUILTIN_SKILLS, RENAMED_BUILTIN_SKILL_SLUGS
from app.services.health.fleet_seed import FLEET_AGENTS

_SKILL_CONTENT = Path(seed.__file__).parent / "seed_data" / "skill_content"


def _row(slug: str, name: str = "skill") -> SimpleNamespace:
    return SimpleNamespace(name=name, tags=["builtin", f"slug:{slug}"])


def test_renamed_skills_are_seeded_under_their_current_slug_only() -> None:
    slugs = {skill["slug"] for skill in BUILTIN_SKILLS}
    for former, current in RENAMED_BUILTIN_SKILL_SLUGS.items():
        assert current in slugs
        assert former not in slugs
        assert (_SKILL_CONTENT / f"{current}.md").is_file()
        assert not (_SKILL_CONTENT / f"{former}.md").exists()


def test_no_seeded_skill_keeps_a_concierge_slug_or_tag() -> None:
    for skill in BUILTIN_SKILLS:
        assert not skill["slug"].startswith("concierge-")
        assert "concierge" not in skill.get("tags", [])


def test_fleet_agents_carry_seeded_skill_slugs() -> None:
    slugs = {skill["slug"] for skill in BUILTIN_SKILLS}
    for spec in FLEET_AGENTS:
        for slug in spec.skill_slugs:
            assert slug in slugs, (spec.slug, slug)
            assert slug not in RENAMED_BUILTIN_SKILL_SLUGS


def test_a_row_on_a_former_slug_is_adopted_under_the_current_one() -> None:
    former = _row("concierge-flow-lab")
    other = _row("cti-ach")
    slug_to_skill = {"cti-ach": other}
    adopted = seed._adopt_renamed_skills([former, other], slug_to_skill, {"health-flow-lab", "cti-ach"})
    assert slug_to_skill["health-flow-lab"] is former
    assert adopted == {id(former)}


def test_a_row_already_on_the_current_slug_wins_over_the_former_one() -> None:
    former = _row("concierge-experimenter")
    current = _row("health-experimenter")
    slug_to_skill = {"health-experimenter": current}
    adopted = seed._adopt_renamed_skills([former, current], slug_to_skill, {"health-experimenter"})
    assert slug_to_skill["health-experimenter"] is current
    assert adopted == set()


def test_a_former_slug_whose_current_skill_is_not_seeded_is_not_adopted() -> None:
    former = _row("concierge-quality-analyst")
    slug_to_skill: dict = {}
    adopted = seed._adopt_renamed_skills([former], slug_to_skill, set())
    assert slug_to_skill == {}
    assert adopted == set()
