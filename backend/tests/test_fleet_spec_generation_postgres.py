"""Create a fleet from a prompt on PostgreSQL, through the tools.

A fleet of one orchestrator and three members is created
from one prompt (``generate_fleet_spec`` with a stub model, then
``apply_fleet_spec``), exported, deleted with its agents, re-applied, and the
new export is the same spec. Generating alone writes nothing.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.fleet import Fleet
from app.models.user import User
from app.services.builtin_tools.fleets import build_fleet_tools
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_ANSWER = {
    "metadata": {"name": "Alert Desk", "slug": "alert-desk", "description": "Triage incoming alerts."},
    "spec": {
        "orchestrator": "desk-lead",
        "members": [
            {
                "key": "desk-lead",
                "role_label": "Lead",
                "handovers": [{"to": "desk-analyst", "type": "delegate"}, {"to": "desk-reporter", "type": "consult"}],
                "agent": {"name": "Desk Lead", "description": "Routes alerts.", "persona": "You route alerts."},
            },
            {
                "key": "desk-analyst",
                "parent": "desk-lead",
                "agent": {"name": "Desk Analyst", "persona": "You analyse alerts."},
            },
            {
                "key": "desk-hunter",
                "parent": "desk-analyst",
                "agent": {"name": "Desk Hunter", "persona": "You hunt.", "llm_model": "invented"},
            },
            {
                "key": "desk-reporter",
                "parent": "desk-lead",
                "exposed": True,
                "agent": {"name": "Desk Reporter", "persona": "You write reports."},
            },
        ],
    },
}


@asynccontextmanager
async def _database():
    async with scratch_database(
        "fleetgen", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db, db.sessions() as session:
        yield session


@pytest.fixture
def stub_model(monkeypatch):
    class _Model:
        async def chat(self, messages, *, model, temperature, max_tokens):
            return SimpleNamespace(content="```json\n" + json.dumps(_ANSWER) + "\n```", stop_reason="end_turn")

    async def _resolve(_db, _user_id, *_a, **_k):
        return _Model(), "stub"

    monkeypatch.setattr("app.llm.router.get_llm_for_user", _resolve)


async def _call(user: User, tool: str, **args) -> str:
    _defs, executor = build_fleet_tools(str(user.id))
    return await executor.handler(tool)(**args)


def _spec_of(answer: str) -> dict:
    match = re.search(r"```json\n(.*)\n```", answer, re.S)
    assert match, answer
    return json.loads(match.group(1))


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_a_fleet_from_one_prompt_is_applied_exported_deleted_and_reapplied_identical(stub_model) -> None:
    async with _database() as session:
        alice = User(
            id=uuid.uuid4(),
            email=f"alice-{uuid.uuid4().hex[:6]}@example.com",
            full_name="alice",
            hashed_password="x",
            role="user",
        )
        session.add(alice)
        await session.commit()

        before_counts = (await _count(session, Agent), await _count(session, Fleet))
        prompt = "An alert desk: a lead, an analyst, a hunter, a reporter."
        preview = await _call(alice, "generate_fleet_spec", prompt=prompt)
        assert preview.startswith("Generated fleet spec, previewed as a dry run"), preview
        assert "create fleet" in preview and preview.count("create agent") == 4, preview
        assert "llm_model left out" in preview, preview
        assert (await _count(session, Agent), await _count(session, Fleet)) == before_counts, "generating writes nothing"

        spec = _spec_of(preview)
        applied = await _call(alice, "apply_fleet_spec", spec=spec, dry_run=False)
        assert applied.startswith("Applied.") and "Unresolved" not in applied, applied
        fleet_id = re.search(r"\(`([0-9a-f-]{36})`\)", applied).group(1)  # type: ignore[union-attr]

        exported = _spec_of(await _call(alice, "export_fleet_spec", fleet_id=fleet_id))
        assert exported["spec"]["orchestrator"] == "desk-lead"
        assert sorted(m["key"] for m in exported["spec"]["members"]) == [
            "desk-analyst",
            "desk-hunter",
            "desk-lead",
            "desk-reporter",
        ]

        assert "deleted" in (await _call(alice, "delete_fleet", fleet_id=fleet_id)).lower()
        for agent in (await session.execute(select(Agent).where(Agent.user_id == alice.id))).scalars().all():
            await session.delete(agent)
        await session.commit()

        again = await _call(alice, "apply_fleet_spec", spec=exported, dry_run=False)
        assert again.startswith("Applied.") and "Unresolved" not in again, again
        new_id = re.search(r"\(`([0-9a-f-]{36})`\)", again).group(1)  # type: ignore[union-attr]
        assert _spec_of(await _call(alice, "export_fleet_spec", fleet_id=new_id)) == exported
