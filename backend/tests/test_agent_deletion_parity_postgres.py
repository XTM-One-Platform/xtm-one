"""The REST route and the agent-builder tool leave the same database behind.

``DELETE /api/v1/agents/{agent_id}`` and the agent builder's ``delete_agent``
tool drifted apart once: the tool left the agent's system-managed knowledge
bases, the live executions of its runs and its audit entry behind. They share
``services.agent_deletion`` now; this pins it where it matters, in the data.
One world is seeded and copied (``CREATE DATABASE ... TEMPLATE``, so even the
defaults the seed did not set are the same rows), the route deletes the agent
in one copy and the tool in the other, and every table of both is compared. A
step added to one path and not the other changes a table in one copy only and
fails here - which is also why the world holds one row for each step: a system
knowledge base with documents and vectors, a plain one that must survive, an
assignment, a run with its live execution and a background task spawned under
it, and references with no foreign key on another agent and on the owner.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import DateTime, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import agents as agents_api
from app.core.database import Base
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.audit_log import AuditLog
from app.models.embedding import Embedding
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.orchestration import OrchestrationExecution
from app.models.sub_agent_task import SubAgentTask
from app.models.user import User
from app.services import agent_memory, kb_helpers
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, ScratchDatabase, scratch_copy, scratch_database

pytestmark = pytest.mark.integration


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=0x4843_0000_0000_0000_0000_0000_0000 + n)


OWNER, DOOMED, PEER = _id(1), _id(2), _id(3)
SYSTEM_KB, PLAIN_KB = _id(10), _id(11)
SYSTEM_DOCS, PLAIN_DOC = (_id(20), _id(21)), _id(22)
ASSIGNMENT, RUN, EXECUTION, TASK = _id(30), _id(31), _id(32), _id(33)


@dataclass
class Outcome:
    state: dict[str, list[str]]
    cancelled: list[set[str]]
    memory_cleared: list[tuple]
    audit_details: list[dict]


async def _seed(sessions, *, shared: bool) -> None:
    """*shared*: the peer binds the doomed agent's system-managed knowledge base too."""
    async with sessions() as db:
        owner = User(id=OWNER, email="parity-owner@example.com", hashed_password="x", full_name="Owner", role="user")
        db.add(owner)
        await db.flush()
        db.add_all(
            [
                KnowledgeBase(id=SYSTEM_KB, name="Doomed's memory", user_id=OWNER, is_system_managed=True),
                KnowledgeBase(id=PLAIN_KB, name="Shared notes", user_id=OWNER),
            ]
        )
        await db.flush()
        db.add_all(
            [
                *(
                    KnowledgeDocument(id=doc_id, knowledge_base_id=SYSTEM_KB, filename=f"{doc_id}.md", source_type="text")
                    for doc_id in SYSTEM_DOCS
                ),
                KnowledgeDocument(id=PLAIN_DOC, knowledge_base_id=PLAIN_KB, filename="notes.md", source_type="text"),
            ]
        )
        await db.flush()
        db.add_all(
            Embedding(
                id=_id(40 + i),
                user_id=OWNER,
                knowledge_base_id=kb_id,
                source_type="document",
                source_id=str(doc_id),
                content="chunk",
                embedding=[0.1, 0.2, 0.3],
            )
            for i, (kb_id, doc_id) in enumerate(
                [(SYSTEM_KB, SYSTEM_DOCS[0]), (SYSTEM_KB, SYSTEM_DOCS[1]), (PLAIN_KB, PLAIN_DOC)]
            )
        )
        db.add_all(
            [
                Agent(
                    id=DOOMED,
                    user_id=OWNER,
                    name="Doomed",
                    slug="doomed",
                    persona="p",
                    knowledge_base_ids=[str(SYSTEM_KB), str(PLAIN_KB)],
                    handover_agent_ids=[str(PEER)],
                ),
                Agent(
                    id=PEER,
                    user_id=OWNER,
                    name="Peer",
                    slug="peer",
                    persona="p",
                    handover_agent_ids=[str(DOOMED)],
                    handover_types={str(DOOMED): "consult"},
                    knowledge_base_ids=[str(SYSTEM_KB)] if shared else None,
                ),
            ]
        )
        await db.flush()
        owner.default_agent_id = DOOMED
        owner.favorite_agent_ids = [str(DOOMED), str(PEER)]
        db.add(Assignment(id=ASSIGNMENT, agent_id=DOOMED, user_id=OWNER, name="Nightly", trigger_type="manual", prompt="go"))
        await db.flush()
        db.add(AssignmentRun(id=RUN, agent_id=DOOMED, assignment_id=ASSIGNMENT, user_id=OWNER, status="running"))
        await db.flush()
        db.add_all(
            [
                OrchestrationExecution(
                    id=EXECUTION,
                    adapter="assignment_run",
                    state="running",
                    payload={"run_id": str(RUN)},
                    dedup_key=f"assignment_run:{RUN}",
                ),
                SubAgentTask(
                    id=TASK,
                    user_id=OWNER,
                    parent_agent_id=PEER,
                    target_agent_id=PEER,
                    task_description="look into it",
                    status="pending",
                    metadata_json={"parent_run_id": str(RUN)},
                ),
            ]
        )
        await db.commit()


# ── What a database holds ────────────────────────────────────────────────────

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _plain(value: Any, known: set[str]) -> Any:
    """A value both copies can hold equally: ids minted by the deletion and clock
    readings differ between two runs of the same code, everything else may not."""
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, uuid.UUID):
        value = str(value)
    if isinstance(value, dt.datetime | dt.date):
        return "<time>"
    if isinstance(value, str):
        if _UUID.match(value):
            return value if value in known else "<minted>"
        return "<time>" if _TIMESTAMP.match(value) else value
    if isinstance(value, dict):
        return {k: _plain(v, known) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_plain(v, known) for v in value]
    return value


async def _state(scratch: ScratchDatabase, known: set[str] | None = None) -> dict[str, list[str]]:
    state: dict[str, list[str]] = {}
    async with scratch.engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            columns = [c for c in table.columns if not isinstance(c.type, DateTime)]
            rows = (await conn.execute(select(*columns))).mappings().all()
            if known is None:
                state[table.name] = [repr(dict(row)) for row in rows]
                continue
            plain = []
            for row in rows:
                values = {key: _plain(value, known) for key, value in row.items()}
                if table.name == "audit_logs":
                    # The one thing the paths word differently: the surface.
                    values["details"] = {k: v for k, v in (values["details"] or {}).items() if k != "via"}
                plain.append(repr(values))
            state[table.name] = sorted(plain)
    return state


def _known_ids(state: dict[str, list[str]]) -> set[str]:
    return set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "".join(map("".join, state.values()))))


# ── The two paths ────────────────────────────────────────────────────────────


async def _rest(scratch: ScratchDatabase) -> None:
    async with scratch.sessions() as db:
        owner = await db.get(User, OWNER)
        await agents_api.delete_agent(agent_id=str(DOOMED), request=None, user=owner, db=db, _ai_gate=None)


async def _builder(scratch: ScratchDatabase) -> None:
    _, executor = build_agent_builder_tool(None, str(OWNER))
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="delete_agent", arguments={"agent_id": str(DOOMED)})
    )
    assert result.content == "Agent **Doomed** and all its assignments have been deleted.", result.content


async def _delete_with(path, scratch: ScratchDatabase, known: set[str], monkeypatch) -> Outcome:
    cancelled: list[set[str]] = []

    async def _record(doc_ids) -> int:
        cancelled.append(set(doc_ids))
        return 0

    clear = AsyncMock()
    monkeypatch.setattr(kb_helpers, "cancel_document_processing_jobs", _record)
    monkeypatch.setattr(agent_memory, "clear_deleted_agent_memory", clear)
    await path(scratch)
    loop = asyncio.get_running_loop()
    await asyncio.gather(*(t for t in kb_helpers._cancellations_in_flight if t.get_loop() is loop))
    async with scratch.sessions() as db:
        audit = (await db.execute(select(AuditLog.details))).scalars().all()
    return Outcome(
        state=await _state(scratch, known),
        cancelled=cancelled,
        memory_cleared=[c.args for c in clear.await_args_list],
        audit_details=list(audit),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True], ids=["own-system-kb", "system-kb-another-agent-binds"])
async def test_the_rest_route_and_the_agent_builder_tool_leave_the_same_database_behind(monkeypatch, shared):
    async with scratch_database("agentdel", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as rest_db:
        await _seed(rest_db.sessions, shared=shared)
        known = _known_ids(await _state(rest_db))
        async with scratch_copy(rest_db) as builder_db:
            builder = await _delete_with(_builder, builder_db, known, monkeypatch)
        rest = await _delete_with(_rest, rest_db, known, monkeypatch)

    # The same database, table by table.
    assert rest.state.keys() == builder.state.keys()
    different = [name for name in rest.state if rest.state[name] != builder.state[name]]
    assert different == [], {name: (rest.state[name], builder.state[name]) for name in different}

    # And the deletion both of them are.
    kept_docs = {*SYSTEM_DOCS, PLAIN_DOC} if shared else {PLAIN_DOC}
    for outcome in (rest, builder):
        rows = outcome.state
        assert rows["agents"] and all("'name': 'Doomed'" not in row for row in rows["agents"])
        # A system-managed knowledge base another agent binds is theirs too: it stays.
        assert bool([row for row in rows["knowledge_bases"] if str(SYSTEM_KB) in row]) is shared
        assert [row for row in rows["knowledge_bases"] if str(PLAIN_KB) in row] != []
        assert {doc for doc in (*SYSTEM_DOCS, PLAIN_DOC) if any(str(doc) in row for row in rows["knowledge_documents"])} == kept_docs
        assert len(rows["embeddings"]) == len(kept_docs)
        assert rows["assignments"] == [] and rows["assignment_runs"] == []
        assert "'state': 'cancelled'" in rows["orchestration_executions"][0]
        assert "'status': 'cancelled'" in rows["sub_agent_tasks"][0]
        assert all(str(DOOMED) not in row for row in rows["agents"] + rows["users"])
        assert outcome.cancelled == ([] if shared else [{str(doc) for doc in SYSTEM_DOCS}])
        assert outcome.memory_cleared == [(str(DOOMED),)]
    # The run purge and the deletion, each naming the surface.
    assert [d.get("via") for d in rest.audit_details] == [None, None]
    assert [d.get("via") for d in builder.audit_details] == ["agent", "agent"]
