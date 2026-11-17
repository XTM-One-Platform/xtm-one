"""The built-in agent seed keeps the version history of what it writes.

The seed rewrites every built-in agent on each boot. A built-in it creates
starts with the "Initial version" of its persona and of each set tracked
config field; a value a release changes is versioned (no author, a commit
message naming the release), its previous state first when nothing recorded
it; a value it leaves unchanged writes nothing, so a boot without a content
change records no version.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper
from app.agents import seed
from app.core.config import settings
from app.models.agent import Agent
from app.models.agent_config_version import AgentConfigVersion
from app.models.prompt_version import PromptVersion
from app.services.agent_config_versioning import (
    BASELINE_COMMIT_MESSAGE,
    INITIAL_COMMIT_MESSAGE,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_SLUG = "versioned-builtin"
_ENTRY = {
    "slug": _SLUG,
    "persona": "You are a versioned built-in.",
    "name": "Versioned Built-in",
    "description": "A built-in whose history is kept.",
    "icon": "Bot",
    "output_format": "markdown",
    "disable_chat": False,
    "max_iterations": 3,
    "tags": ["builtin"],
    "handover_agent_slugs": [],
    "handover_type_slugs": {},
    "builtin_tool_ids": [],
    "integration_template_bindings": [],
}


def _release_message() -> str:
    return f"Updated by the built-in agent seed (release {settings.app_version})"


async def _seed(monkeypatch, sessions, **changes) -> None:
    entry = {**_ENTRY, **changes}
    monkeypatch.setattr(seed, "_seeded_builtin_agents", lambda: [dict(entry)])
    async with sessions() as session:
        await seed.seed_builtin_agents(session, platform_mode="xtm_one")
        await session.commit()


async def _history(sessions) -> tuple[list[tuple], dict[str, list[tuple]]]:
    """The built-in's persona versions and config versions, oldest first."""
    async with sessions() as session:
        agent_id = (
            await session.execute(select(Agent.id).where(Agent.slug == _SLUG))
        ).scalar_one()
        personas = (
            await session.execute(
                select(
                    PromptVersion.version_number,
                    PromptVersion.content,
                    PromptVersion.author_id,
                    PromptVersion.commit_message,
                )
                .where(PromptVersion.agent_id == agent_id)
                .order_by(PromptVersion.version_number)
            )
        ).all()
        configs: dict[str, list[tuple]] = {}
        rows = (
            await session.execute(
                select(
                    AgentConfigVersion.field_name,
                    AgentConfigVersion.version_number,
                    AgentConfigVersion.value_json,
                    AgentConfigVersion.author_id,
                    AgentConfigVersion.commit_message,
                )
                .where(AgentConfigVersion.agent_id == agent_id)
                .order_by(AgentConfigVersion.field_name, AgentConfigVersion.version_number)
            )
        ).all()
        for field_name, *rest in rows:
            configs.setdefault(field_name, []).append(tuple(rest))
    return [tuple(p) for p in personas], configs


@pytest.mark.asyncio
async def test_a_created_builtin_starts_with_its_initial_versions(monkeypatch):
    async with scratch_database("seedversions", tables=ALL_TABLES, extensions=("vector",)) as db:
        await _seed(monkeypatch, db.sessions)
        personas, configs = await _history(db.sessions)

    assert personas == [
        (1, "You are a versioned built-in.", None, INITIAL_COMMIT_MESSAGE),
    ]
    # Only the set tracked fields: temperature, reasoning effort and
    # guardrails are unset on this built-in.
    assert configs == {"max_iterations": [(1, 3, None, INITIAL_COMMIT_MESSAGE)]}


@pytest.mark.asyncio
async def test_a_boot_without_a_content_change_records_nothing(monkeypatch):
    async with scratch_database("seedversions", tables=ALL_TABLES, extensions=("vector",)) as db:
        await _seed(monkeypatch, db.sessions)
        first = await _history(db.sessions)
        await _seed(monkeypatch, db.sessions)
        await _seed(monkeypatch, db.sessions)
        again = await _history(db.sessions)

    assert again == first


@pytest.mark.asyncio
async def test_a_release_change_is_versioned_once(monkeypatch):
    async with scratch_database("seedversions", tables=ALL_TABLES, extensions=("vector",)) as db:
        await _seed(monkeypatch, db.sessions)
        changed = {"persona": "You are the next release's built-in.", "temperature": 0.3}
        await _seed(monkeypatch, db.sessions, **changed)
        # Another replica of the new release booting after it.
        await _seed(monkeypatch, db.sessions, **changed)
        personas, configs = await _history(db.sessions)

    assert personas == [
        (1, "You are a versioned built-in.", None, INITIAL_COMMIT_MESSAGE),
        (2, "You are the next release's built-in.", None, _release_message()),
    ]
    assert configs == {
        "max_iterations": [(1, 3, None, INITIAL_COMMIT_MESSAGE)],
        # Unset before the release: the platform default is recorded first,
        # so a rollback can restore it.
        "temperature": [
            (1, None, None, BASELINE_COMMIT_MESSAGE),
            (2, 0.3, None, _release_message()),
        ],
    }


@pytest.mark.asyncio
async def test_a_builtin_seeded_without_history_records_what_the_release_replaced(
    monkeypatch,
):
    """A built-in an earlier release seeded with no version gets none on an
    unchanged boot, and its replaced value first on a changed one."""
    async with scratch_database("seedversions", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            session.add(
                Agent(
                    id=uuid.uuid4(),
                    user_id=None,
                    is_company_managed=True,
                    is_builtin=True,
                    slug=_SLUG,
                    name="Versioned Built-in",
                    persona="You are a versioned built-in.",
                    max_iterations=3,
                )
            )
            await session.commit()

        await _seed(monkeypatch, db.sessions)
        unchanged = await _history(db.sessions)
        await _seed(monkeypatch, db.sessions, persona="Rewritten.", max_iterations=5)
        personas, configs = await _history(db.sessions)

    assert unchanged == ([], {})
    assert personas == [
        (1, "You are a versioned built-in.", None, BASELINE_COMMIT_MESSAGE),
        (2, "Rewritten.", None, _release_message()),
    ]
    assert configs == {
        "max_iterations": [
            (1, 3, None, BASELINE_COMMIT_MESSAGE),
            (2, 5, None, _release_message()),
        ],
    }
