"""The owner a new CTEM case starts with (#1736 slice 3).

Rules of ``services/ctem_case_owner.py``: an asset's owner first, then its
group's owner, then the instance default; only an active account's email
counts; a failed read opens the case without an owner.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.services import ctem_case_owner as owner_of

ALICE = uuid.uuid4()
TEAM = uuid.uuid4()
DEFAULT = uuid.uuid4()
USERS = {"alice@example.com": ALICE, "team@example.com": TEAM, "default@example.com": DEFAULT}


def _signal(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        organization_id=uuid.uuid4(),
        subject_kind="asset",
        subject_ref="Web-01",
        decision={
            "scope": {
                "sources": [
                    {"from": "asset", "name": "db-02 ", "criticality": "high"},
                    {"from": "asset_group", "name": "web-01", "criticality": "high"},
                    {"from": "crown_jewel", "name": "Customer portal", "criticality": "critical"},
                ]
            }
        },
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_asset_names_reads_the_subject_then_the_asset_sources() -> None:
    assert owner_of.asset_names(_signal()) == ["web-01", "db-02"]
    assert owner_of.asset_names(_signal(subject_kind="software", decision=None)) == []
    assert owner_of.asset_names(_signal(subject_kind="software", decision={"scope": "bad"})) == []


def test_an_asset_owner_wins_then_a_group_owner_then_the_default() -> None:
    assert owner_of.pick_owner(["Alice@Example.com"], ["team@example.com"], USERS, "default@example.com") == (
        ALICE,
        owner_of.FROM_ASSET,
    )
    assert owner_of.pick_owner(["ops team"], ["team@example.com"], USERS, "default@example.com") == (
        TEAM,
        owner_of.FROM_GROUP,
    )
    assert owner_of.pick_owner([], [], USERS, "default@example.com") == (DEFAULT, owner_of.FROM_DEFAULT)


def test_a_name_that_is_no_account_is_never_guessed() -> None:
    assert owner_of.pick_owner(["Alice"], ["unknown@example.com"], USERS, "") == (None, None)


def test_the_default_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTEM_CASE_DEFAULT_OWNER", raising=False)
    assert owner_of.default_owner_email() == ""
    monkeypatch.setenv("CTEM_CASE_DEFAULT_OWNER", " SecOps@Example.com ")
    assert owner_of.default_owner_email() == "secops@example.com"


class _Savepoint:
    async def __aenter__(self) -> _Savepoint:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalars(self) -> _Result:
        return self


class OwnerSession:
    """Answers the reads in order: asset records, group owners, users."""

    def __init__(self, *reads: list[Any], fail: bool = False) -> None:
        self.reads = list(reads)
        self.fail = fail

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    async def execute(self, _statement: object) -> _Result:
        if self.fail:
            raise RuntimeError("inventory unavailable")
        return _Result(self.reads.pop(0) if self.reads else [])


@pytest.mark.asyncio
async def test_a_group_owner_is_read_when_the_assets_have_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTEM_CASE_DEFAULT_OWNER", raising=False)
    db = OwnerSession([("", "web servers")], ["team@example.com"], [(TEAM, "team@example.com")])
    assert await owner_of.owner_for_signal(cast("Any", db), _signal()) == (TEAM, owner_of.FROM_GROUP)


@pytest.mark.asyncio
async def test_the_default_owner_when_the_inventory_names_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTEM_CASE_DEFAULT_OWNER", "default@example.com")
    db = OwnerSession([], [(DEFAULT, "default@example.com")])
    assert await owner_of.owner_for_signal(cast("Any", db), _signal()) == (DEFAULT, owner_of.FROM_DEFAULT)


@pytest.mark.asyncio
async def test_a_failed_read_opens_the_case_without_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTEM_CASE_DEFAULT_OWNER", "default@example.com")
    db = OwnerSession(fail=True)
    assert await owner_of.owner_for_signal(cast("Any", db), _signal()) == (None, None)


@pytest.mark.asyncio
async def test_nothing_is_read_without_asset_or_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CTEM_CASE_DEFAULT_OWNER", raising=False)
    db = OwnerSession(fail=True)
    signal = _signal(subject_kind="software", decision={})
    assert await owner_of.owner_for_signal(cast("Any", db), signal) == (None, None)
