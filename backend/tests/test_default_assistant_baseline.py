"""Every default entry point ships one baseline (#12343).

Whatever door a person comes through (the CTEM Assistant, the XTM One
Assistant, the OpenCTI / OpenAEV / OpenCRQ assistants of each module's chat,
the FIMI Analyst, the General Assistant), the assistant that answers carries
the fleet skills, builds agents, assignments, skills and fleets in place, and
reads the XTM One documentation, which the image ships and the seed loads.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.agents import seed
from app.agents.builtin_agents import (
    BUILTIN_AGENTS,
    BUILTIN_KNOWLEDGE_BASES,
    BUILTIN_SKILLS,
    DEFAULT_ASSISTANT_BASELINE_KB_SLUGS,
    DEFAULT_ASSISTANT_BASELINE_SKILL_SLUGS,
    DEFAULT_ASSISTANT_MODULE_SKILL_SLUGS,
    DEFAULT_ASSISTANT_PLATFORM_TOOL_IDS,
    DEFAULT_ASSISTANT_SLUGS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _agent(slug: str) -> dict[str, Any]:
    return next(agent for agent in BUILTIN_AGENTS if agent["slug"] == slug)


def _skill(slug: str) -> dict[str, Any]:
    return next(skill for skill in BUILTIN_SKILLS if skill["slug"] == slug)


def _kb(slug: str) -> dict[str, Any]:
    return next(kb for kb in BUILTIN_KNOWLEDGE_BASES if kb["slug"] == slug)


@pytest.mark.parametrize("slug", DEFAULT_ASSISTANT_SLUGS)
def test_every_default_assistant_builds_the_platform_and_its_fleets(slug: str) -> None:
    tools = _agent(slug)["builtin_tool_ids"]
    missing = [tool for tool in DEFAULT_ASSISTANT_PLATFORM_TOOL_IDS if tool not in tools]
    assert not missing, f"{slug} lacks {missing!r}"
    assert len(tools) == len(set(tools)), f"{slug} lists a tool twice"


@pytest.mark.parametrize("skill_slug", DEFAULT_ASSISTANT_BASELINE_SKILL_SLUGS)
def test_every_default_assistant_and_the_general_assistant_carry_the_baseline_skills(skill_slug: str) -> None:
    from app.services.general_assistant_skills import GENERAL_ASSISTANT_SKILL_SLUGS

    skill = _skill(skill_slug)
    missing = [slug for slug in DEFAULT_ASSISTANT_SLUGS if slug not in skill["bind_to_agent_slugs"]]
    assert not missing, f"{skill_slug} is not bound to {missing!r}"
    assert skill_slug in GENERAL_ASSISTANT_SKILL_SLUGS
    for mode in ("copilot", "xtm_one", "dev"):
        assert seed.seed_matches_platform(skill, mode), mode


def test_the_fleet_skills_teach_the_fleet_health_read_the_assistants_now_carry() -> None:
    content = seed._resolve_seed_content("skill_content", _skill("fleet-health-advisory")["content"])
    assert content is not None
    assert "get_fleet_health" in content


def test_get_fleet_health_comes_with_the_platform_tools() -> None:
    from app.services.builtin_tool_aliases import BUILTIN_GROUP_ALIASES

    group, functions = BUILTIN_GROUP_ALIASES["fleets"]
    assert group == "platform"
    assert "get_fleet_health" in functions
    builder_group, builder = BUILTIN_GROUP_ALIASES["agent_builder"]
    assert builder_group == "platform"
    for name in ("create_agent", "update_agent", "create_assignment", "create_skill"):
        assert name in builder, name


@pytest.mark.parametrize(
    "name", ["create_agent", "update_agent", "create_assignment", "create_skill", "get_fleet_health"]
)
def test_an_administrator_and_a_member_are_both_offered_the_builder_writes(name: str) -> None:
    from app.services.builtin_tool_aliases import caller_may_run

    assert caller_may_run(name, is_admin=True)
    assert caller_may_run(name, is_admin=False)


@pytest.mark.parametrize(("agent_slug", "skill_slugs"), sorted(DEFAULT_ASSISTANT_MODULE_SKILL_SLUGS.items()))
def test_each_assistant_carries_its_module_skills(agent_slug: str, skill_slugs: tuple[str, ...]) -> None:
    assert agent_slug in DEFAULT_ASSISTANT_SLUGS
    for skill_slug in skill_slugs:
        assert agent_slug in _skill(skill_slug)["bind_to_agent_slugs"], (agent_slug, skill_slug)


@pytest.mark.parametrize("kb_slug", DEFAULT_ASSISTANT_BASELINE_KB_SLUGS)
def test_every_default_assistant_reads_the_documentation(kb_slug: str) -> None:
    assert set(DEFAULT_ASSISTANT_SLUGS) <= set(_kb(kb_slug)["bind_to_agent_slugs"])


def test_the_documentation_is_seeded_from_the_bundled_docs_not_the_private_repository() -> None:
    kb = _kb("xtm-one-documentation")
    assert kb.get("bundled_docs") is True
    sources = kb.get("sync_sources") or ([kb["sync_source"]] if "sync_source" in kb else [])
    assert not [s for s in sources if s["config"].get("repo") == "XTM-One-Platform/xtm-one"]


def test_the_image_ships_the_documentation_where_the_seed_reads_it() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"COPY docs/ {seed._IMAGE_DOCS_DIR}/" in dockerfile
    assert "\ndocs\n" not in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")


def test_a_source_checkout_finds_the_repository_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XTM_ONE_DOCS_DIR", raising=False)
    monkeypatch.setattr(seed, "_IMAGE_DOCS_DIR", Path("/nonexistent-xtm-one-docs"))
    assert seed.bundled_docs_dir() == REPO_ROOT / "docs"


def test_the_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XTM_ONE_DOCS_DIR", str(tmp_path))
    assert seed.bundled_docs_dir() == tmp_path


def test_no_docs_seeds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XTM_ONE_DOCS_DIR", raising=False)
    monkeypatch.setattr(seed, "_IMAGE_DOCS_DIR", Path("/nonexistent-a"))
    monkeypatch.setattr(seed, "_CHECKOUT_DOCS_DIR", Path("/nonexistent-b"))
    assert seed.bundled_docs_dir() is None
    added = asyncio.run(
        seed._seed_kb_documents(cast(Any, None), cast(Any, SimpleNamespace()), {"slug": "x", "bundled_docs": True})
    )
    assert added == 0


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _Db:
    """A session double: no document exists yet, every add is kept."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    async def execute(self, _stmt: object) -> _Result:
        return _Result(None)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def test_the_bundled_docs_seed_every_markdown_file_by_its_relative_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "guides").mkdir()
    (tmp_path / "index.md").write_text("# Home\n", encoding="utf-8")
    (tmp_path / "guides" / "fleets.md").write_text("# Fleets\n", encoding="utf-8")
    (tmp_path / "guides" / "logo.png").write_bytes(b"\x89PNG")
    monkeypatch.setenv("XTM_ONE_DOCS_DIR", str(tmp_path))

    async def _bump(_db: object, _kb_id: object) -> None:
        return None

    monkeypatch.setattr(seed, "bump_kb_revision", _bump)
    db = _Db()
    kb = SimpleNamespace(id="kb", name="XTM-One Documentation", document_count=0)
    added = asyncio.run(
        seed._seed_kb_documents(cast(Any, db), cast(Any, kb), {"slug": "xtm-one-documentation", "bundled_docs": True})
    )
    assert added == 2
    assert sorted(doc.filename for doc in db.added) == ["guides/fleets.md", "index.md"]
    assert kb.document_count == 2


def test_the_repository_ships_documentation_to_seed() -> None:
    files = [p for p in (REPO_ROOT / "docs").rglob("*") if p.suffix in (".md", ".mdx")]
    assert len(files) >= 10


#: Every module assistant reads its product's documentation; the CTEM
#: Assistant reads all of them.
_MODULE_DOCS = {
    "opencti-documentation": "opencti-assistant",
    "openaev-documentation": "openaev-assistant",
    "opencrq-documentation": "opencrq-assistant",
}


@pytest.mark.parametrize(("kb_slug", "assistant"), sorted(_MODULE_DOCS.items()))
def test_each_module_assistant_and_the_ctem_assistant_read_the_module_docs(kb_slug: str, assistant: str) -> None:
    bound = _kb(kb_slug)["bind_to_agent_slugs"]
    assert assistant in bound
    assert "ctem-assistant" in bound


@pytest.mark.parametrize("kb_slug", sorted(_MODULE_DOCS))
def test_each_module_docs_knowledge_base_has_a_source_that_can_fill_it(kb_slug: str) -> None:
    """A shipped seed folder with Markdown, or a public sync of a repository that
    is not the archived documentation space (OpenAEV-Platform/docs, archived)."""
    kb = _kb(kb_slug)
    folder = kb.get("knowledge_base_seed")
    if folder:
        files = list((seed.SEED_DATA_DIR / "knowledge_base" / folder).glob("*.md"))
        assert len(files) >= 5, folder
        return
    sources = kb.get("sync_sources") or []
    assert sources, kb_slug
    for source in sources:
        assert source["source_type"] == "github_public"
        assert source["config"]["repo"] not in {"OpenAEV-Platform/docs", "OpenCTI-Platform/docs"}
        assert source["config"].get("path") == "docs"


def test_the_opencrq_docs_seed_into_their_knowledge_base(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _bump(_db: object, _kb_id: object) -> None:
        return None

    monkeypatch.setattr(seed, "bump_kb_revision", _bump)
    db = _Db()
    kb = SimpleNamespace(id="kb", name="OpenCRQ Documentation", document_count=0)
    added = asyncio.run(seed._seed_kb_documents(cast(Any, db), cast(Any, kb), _kb("opencrq-documentation")))
    assert added >= 5
    assert "quick-start.md" in {doc.filename for doc in db.added}
