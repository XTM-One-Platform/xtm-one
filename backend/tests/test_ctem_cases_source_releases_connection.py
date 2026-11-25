# SPDX-License-Identifier: Apache-2.0
"""A CTEM source read hands its DB connection back before the product reads (#2232).

During a case-opening drain the case tools of agent runs held a pool connection
for 340-498 s each: the session that read the credentials kept its transaction
open through the OpenCTI and OpenAEV reads. The read now commits before the
network, so no connection stays checked out while a product answers.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import ctem_cases_source as source

STACK = [{"name": "Tomcat", "vendor": "Apache", "version": "9.0.1"}]


class FakeSession:
    """A session that records whether a transaction was open when a product was read."""

    def __init__(self) -> None:
        self.open = True
        self.commits = 0
        self.open_during_read: list[bool] = []

    def in_transaction(self) -> bool:
        return self.open

    async def commit(self) -> None:
        self.commits += 1
        self.open = False

    async def rollback(self) -> None:
        self.open = False


def _patch(monkeypatch: pytest.MonkeyPatch, db: FakeSession) -> None:
    async def stack(_db: Any) -> list[dict[str, str]]:
        db.open = True  # the stack read opens the transaction
        return STACK

    async def creds(_db: Any, user_id: str, provider: str) -> tuple[str, dict[str, str]]:
        db.open = True
        return (f"https://{provider}", {})

    async def read_opencti(client: Any, base_url: str, headers: Any, stack: Any) -> source.OpenCtiRead:
        db.open_during_read.append(db.open)
        return source.OpenCtiRead([])

    async def read_scoped(client: Any, base_url: str, headers: Any, stack: Any, ids: Any) -> source.OpenCtiRead:
        db.open_during_read.append(db.open)
        return source.OpenCtiRead([])

    async def read_openaev(client: Any, base_url: str, headers: Any, now: Any) -> list[Any]:
        db.open_during_read.append(db.open)
        return []

    async def closed(out: Any, creds: Any, members: Any) -> None:
        return None

    monkeypatch.setattr(source, "_organization_software", stack)
    monkeypatch.setattr(source, "_user_creds", creds)
    monkeypatch.setattr(source, "_read_opencti", read_opencti)
    monkeypatch.setattr(source, "_read_opencti_scoped", read_scoped)
    monkeypatch.setattr(source, "_read_openaev", read_openaev)
    monkeypatch.setattr(source, "_read_closed_members", closed)
    monkeypatch.setattr(source, "attach_probe", lambda headers, base_url: headers)


@pytest.mark.asyncio
async def test_the_whole_read_holds_no_connection_while_products_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeSession()
    _patch(monkeypatch, db)
    read = await source.read_open_issues(db, "u-1", "cyber")  # type: ignore[arg-type]
    assert read.missing == []
    assert db.open_during_read == [False, False]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_the_case_read_holds_no_connection_while_products_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeSession()
    _patch(monkeypatch, db)
    out = source.SourceRead()
    await source._read_case_members(db, "u-1", out, {"opencti": ["v1"]})  # type: ignore[arg-type]
    assert db.open_during_read == [False]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_a_stand_in_session_without_transactions_is_left_alone() -> None:
    await source.release_before_network(None)
    await source.release_before_network(object())
