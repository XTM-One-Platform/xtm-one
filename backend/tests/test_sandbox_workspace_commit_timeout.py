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

"""A workspace commit raises its own statement timeout before it takes the row lock.

A checkout of thousands of files, or a commit waiting on another commit's row
lock, ran past the pool's 30 s ``statement_timeout`` and the sandbox tool failed
with ``QueryCanceledError`` on ``SELECT ... FOR UPDATE`` or the final
``UPDATE sandbox_workspaces``.
"""

from __future__ import annotations

import uuid

import pytest

from app.core import config as config_module
from app.services.sandbox_workspace import store as store_module
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.store import MemoryBlobs, WorkspaceAccessError, WorkspaceStore


class _Result:
    def scalar_one_or_none(self):
        return None


class _Session:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        self.statements.append((str(stmt), params))
        return _Result()

    async def commit(self):
        return None


async def _commit_on(session: _Session) -> None:
    store = WorkspaceStore(session_factory=lambda: session, blobs=MemoryBlobs())
    # The row is gone (the fake answers no row): the commit stops after the lock.
    with pytest.raises(WorkspaceAccessError):
        await store.commit(uuid.uuid4(), [], [], Limits(quota_bytes=1024, max_files=10))


async def test_the_commit_sets_its_timeout_before_the_row_lock(monkeypatch):
    monkeypatch.setattr(config_module.settings, "sandbox_workspace_commit_statement_timeout_secs", 300)
    session = _Session()
    await _commit_on(session)
    first, params = session.statements[0]
    assert "set_config('statement_timeout'" in first
    assert params == {"ms": "300000"}
    assert "FOR UPDATE" in session.statements[1][0]


async def test_zero_keeps_the_pool_default(monkeypatch):
    monkeypatch.setattr(config_module.settings, "sandbox_workspace_commit_statement_timeout_secs", 0)
    session = _Session()
    await _commit_on(session)
    assert not any("statement_timeout" in sql for sql, _ in session.statements)
    assert "FOR UPDATE" in session.statements[0][0]


def test_the_timeout_is_clamped_to_what_postgresql_accepts(monkeypatch):
    monkeypatch.setattr(config_module.settings, "sandbox_workspace_commit_statement_timeout_secs", 10**9)
    assert store_module.commit_statement_timeout_ms() == 2_147_483_647
    monkeypatch.setattr(config_module.settings, "sandbox_workspace_commit_statement_timeout_secs", -5)
    assert store_module.commit_statement_timeout_ms() == 0
