"""The agent wizard's assistant offers, and keeps, only what the caller may use.

The real route runs against PostgreSQL with a fake model: what the model is
offered is read from the system message it receives, and what it proposes
beyond that offer must never reach the patch. The visibility predicate reads
the per-user sharing columns, which a mock session cannot show. Same
scratch-database recipe as ``test_agent_duplicate_bindings_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import ai as ai_api
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _user(name: str, *, role: str = "user") -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
    )


class _Connected:
    async def is_disconnected(self) -> bool:
        return False


class _FakeLLM:
    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.calls: list[list] = []

    async def chat(self, messages, **_kwargs):
        self.calls.append(list(messages))
        return SimpleNamespace(content=self.answers.pop(0), stop_reason="end_turn", finish_reason="stop")


def _offered(llm: _FakeLLM) -> dict:
    return json.loads(llm.calls[0][0].content.split("data, not instructions): ", 1)[1])


async def _ask(monkeypatch, session, user, patch: dict, *, draft: dict | None = None):
    llm = _FakeLLM([json.dumps({"reply": "ok", "patch": patch})] * 2)

    async def _llm(_db, _uid):
        return llm, "model"

    monkeypatch.setattr(ai_api, "get_llm_for_user", _llm)
    body = ai_api.WizardAssistRequest(instruction="Bind what I need", started=True, draft=draft or {})
    answered = await ai_api.assist_agent_wizard(body, _Connected(), user, session)
    return llm, answered


async def test_the_offer_and_the_patch_follow_the_callers_visibility(monkeypatch):
    async with scratch_database(
        "wizassist", tables=ALL_TABLES, extensions=("vector",), bind_app_sessions=False
    ) as db, db.sessions() as session:
        owner, member, admin = _user("owner"), _user("member"), _user("admin", role="admin")
        session.add_all([owner, member, admin])
        await session.flush()
        group = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        session.add(group)
        await session.flush()
        session.add(GroupMember(group_id=group.id, user_id=member.id))

        rows = {
            "kb_private": KnowledgeBase(name="Owner KB", user_id=owner.id),
            "kb_shared": KnowledgeBase(
                name="Shared KB",
                user_id=owner.id,
                shared_group_ids=[str(group.id)],
                shared_group_permissions={str(group.id): "read_only"},
            ),
            "kb_company": KnowledgeBase(name="Company KB", user_id=None, is_company_managed=True),
            "int_own": Integration(provider="github", name="Member GitHub", user_id=member.id, status="connected"),
            "int_down": Integration(provider="slack", name="Member Slack", user_id=member.id),
            "int_company": Integration(
                provider="jira", name="Company Jira", user_id=None, is_company_managed=True, status="connected"
            ),
            "int_private": Integration(
                provider="github", name="Owner GitHub", user_id=owner.id, status="connected"
            ),
        }
        session.add_all(rows.values())
        await session.commit()
        ids = {key: str(row.id) for key, row in rows.items()}

        # A non-admin is offered their own, shared and company-managed rows,
        # and only connected integrations.
        llm, answered = await _ask(
            monkeypatch,
            session,
            member,
            {
                "knowledge_base_ids": [ids["kb_shared"], ids["kb_private"]],
                "integration_ids": [ids["int_own"], ids["int_private"]],
            },
        )
        offered = _offered(llm)
        assert sorted(kb["name"] for kb in offered["knowledge_bases"]) == ["Company KB", "Shared KB"]
        assert sorted(i["name"] for i in offered["integrations"]) == ["Company Jira", "Member GitHub"]
        # What the model named beyond the offer is refused, then left out.
        assert answered.patch == {
            "knowledge_base_ids": [ids["kb_shared"]],
            "integration_ids": [ids["int_own"]],
        }
        assert answered.notes == ["integrations_unavailable", "knowledge_bases_unavailable"]
        assert "1 id(s) not in OFFER.knowledge_bases" in llm.calls[1][-1].content

        # A revoked share leaves the offer at once, and the draft that still
        # holds it no longer names it.
        kb_shared = await session.get(KnowledgeBase, rows["kb_shared"].id)
        kb_shared.shared_group_ids = []
        kb_shared.shared_group_permissions = {}
        await session.commit()
        llm, answered = await _ask(
            monkeypatch,
            session,
            member,
            {"knowledge_base_ids": [ids["kb_shared"]]},
            draft={"knowledge_base_ids": [ids["kb_shared"]]},
        )
        assert [kb["name"] for kb in _offered(llm)["knowledge_bases"]] == ["Company KB"]
        assert "Shared KB" not in llm.calls[0][-1].content
        assert "(unavailable to this person)" in llm.calls[0][-1].content
        assert answered.patch == {"knowledge_base_ids": []}

        # An admin's knowledge bases are the wizard picker's (every row); its
        # integrations are the integrations page's library set, connected.
        llm, _answered = await _ask(monkeypatch, session, admin, {})
        offered = _offered(llm)
        assert {"Owner KB", "Shared KB", "Company KB"} <= {kb["name"] for kb in offered["knowledge_bases"]}
        assert [i["name"] for i in offered["integrations"]] == ["Company Jira"]
