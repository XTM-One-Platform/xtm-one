# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Knowledge-base writes across offices (#1763).

- A built-in KB whose seed entry leaves its documents to the customer
  (Company Context) is not content-locked; every other built-in KB stays locked.
- get_agent labels a write grant on a content-locked KB with its lock.
- ``update_kb_document`` edits a document of any KB the caller may write.
- A document write runs under the KB write timeout, not the 30 s pool default.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.kb_helpers as kb_helpers
from app.api.v1 import helpers as api_helpers
from app.core import database
from app.services.builtin_tools import agent_builder, build_agent_builder_tool
from tests.test_agent_builder_crud_tools import (
    _call,
    _session_factory,
    _stub_manageable_gate,
)


def _builtin_kb(slug: str, **extra):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=slug,
        is_builtin=True,
        is_read_only=False,
        is_company_managed=True,
        owner_workspace_id=None,
        tags=["builtin", f"slug:{slug}"],
        document_count=1,
        chunk_count=4,
        **extra,
    )


def test_company_context_is_not_content_locked():
    kb = _builtin_kb("company-context")
    assert api_helpers.builtin_knowledge_base_holds_user_content(kb)
    assert api_helpers.knowledge_base_content_lock(kb, package_installed=False) is None


def test_other_builtin_kbs_stay_locked():
    kb = _builtin_kb("stix-standard")
    assert not api_helpers.builtin_knowledge_base_holds_user_content(kb)
    assert api_helpers.knowledge_base_content_lock(kb, package_installed=False) == api_helpers.KB_WRITE_BUILTIN


def test_a_user_content_slug_tag_on_a_non_builtin_kb_changes_nothing():
    kb = _builtin_kb("company-context")
    kb.is_builtin = False
    assert not api_helpers.builtin_knowledge_base_holds_user_content(kb)
    assert api_helpers.knowledge_base_content_lock(kb, package_installed=False) is None


def test_a_read_only_company_context_stays_locked():
    kb = _builtin_kb("company-context")
    kb.is_read_only = True
    assert api_helpers.knowledge_base_content_lock(kb, package_installed=False) == api_helpers.KB_WRITE_READ_ONLY


class _RecordingDb:
    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, statement, *_args):
        self.statements.append(str(statement))


@pytest.mark.asyncio
async def test_document_write_timeout_uses_the_kb_write_bound(monkeypatch):
    monkeypatch.setattr(kb_helpers.app_settings, "kb_sync_timeout_secs", 1800)
    db = _RecordingDb()
    await kb_helpers.apply_document_write_timeout(db)
    assert db.statements == ["SET LOCAL statement_timeout = 1800000"]


@pytest.mark.asyncio
async def test_document_write_timeout_off_keeps_the_pool_default(monkeypatch):
    monkeypatch.setattr(kb_helpers.app_settings, "kb_sync_timeout_secs", 0)
    db = _RecordingDb()
    await kb_helpers.apply_document_write_timeout(db)
    assert db.statements == []


@pytest.mark.asyncio
async def test_granted_kb_locks_names_the_lock_of_a_builtin_kb(monkeypatch):
    locked = _builtin_kb("stix-standard")
    open_kb = _builtin_kb("company-context")

    async def _origin(_db, _ids):
        return set()

    monkeypatch.setattr(api_helpers, "package_origin_knowledge_base_ids", _origin)

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [locked, open_kb]))

    locks = await agent_builder._granted_kb_locks(_Db(), [str(locked.id), str(open_kb.id)])
    assert locks == {str(locked.id): api_helpers.KB_WRITE_BUILTIN}
    assert await agent_builder._granted_kb_locks(_Db(), ["not-a-uuid"]) == {}


def _patch_document_write(monkeypatch):
    timeouts: list[object] = []

    async def _timeout(db):
        timeouts.append(db)

    async def _reset(_db, doc, *, kb=None, vectors_deleted=False):
        doc.status = "pending"

    monkeypatch.setattr(kb_helpers, "apply_document_write_timeout", _timeout)
    monkeypatch.setattr(kb_helpers, "reset_changed_document", _reset)
    monkeypatch.setattr(kb_helpers, "bump_kb_revision", AsyncMock(return_value=None))
    monkeypatch.setattr(kb_helpers, "log_kb_activity", AsyncMock(return_value=None))
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", AsyncMock(return_value=True))
    return timeouts


@pytest.mark.asyncio
async def test_update_kb_document_edits_a_document_of_a_manageable_kb(monkeypatch):
    timeouts = _patch_document_write(monkeypatch)
    kb = _builtin_kb("company-context")
    _stub_manageable_gate(monkeypatch, entity=kb)
    doc = SimpleNamespace(
        id=uuid.uuid4(),
        knowledge_base_id=kb.id,
        filename="pricing.md",
        source_type="text",
        content="Plans: Starter, Pro.",
        status="ready",
        chunk_count=2,
    )
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created, execute_results=[doc]))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(
        executor,
        "update_kb_document",
        knowledge_base_id=str(kb.id),
        document_id=str(doc.id),
        content="Enterprise on request.",
        append=True,
    )

    assert not result.is_error, result.content
    assert "updated" in result.content
    assert doc.content == "Plans: Starter, Pro.\n\nEnterprise on request."
    assert doc.status == "pending"
    assert created[0].commits == 1
    assert timeouts == [created[0]]


@pytest.mark.asyncio
async def test_update_kb_document_refuses_a_locked_builtin_kb(monkeypatch):
    _patch_document_write(monkeypatch)
    kb = _builtin_kb("stix-standard")
    _stub_manageable_gate(monkeypatch, entity=kb)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(
        executor,
        "update_kb_document",
        knowledge_base_id=str(kb.id),
        document_id=str(uuid.uuid4()),
        content="anything",
    )

    assert "Built-in knowledge bases cannot be modified" in result.content
    assert created[0].commits == 0


@pytest.mark.asyncio
async def test_update_kb_document_needs_content():
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_kb_document",
        knowledge_base_id=str(uuid.uuid4()),
        document_id=str(uuid.uuid4()),
    )
    assert "'content' is required" in result.content
