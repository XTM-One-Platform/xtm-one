# SPDX-License-Identifier: Apache-2.0

"""The sender's directory profile in the context of a turn (services/sender_directory.py)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations.directory import DirectoryProfile
from app.integrations.providers import hibob
from app.services import sender_directory

Lookup = Callable[[str, str], Awaitable[DirectoryProfile | None]]


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.ttls[key] = ex or 0


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()

    async def _client() -> _FakeRedis:
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    return fake


def _agent(**config: Any) -> SimpleNamespace:
    return SimpleNamespace(integration_bindings={"hibob": ["integ-1"]}, config=config)


def _stub(monkeypatch: pytest.MonkeyPatch, lookup: Lookup) -> list[str]:
    calls: list[str] = []

    async def _token(_db: Any, _provider: str, _integration_id: str, _user_id: str | None) -> str:
        return "svc||key"

    async def _lookup(token: str, email: str) -> DirectoryProfile | None:
        calls.append(email)
        return await lookup(token, email)

    monkeypatch.setattr(sender_directory, "_resolve_token", _token)
    monkeypatch.setattr(sender_directory, "directory_lookup", lambda _provider: _lookup)
    return calls


def _block(agent: Any, email: str | None) -> str:
    return asyncio.run(sender_directory.build_sender_directory_block(None, agent, email)) or ""


_PROFILE = DirectoryProfile(
    display_name="Jane Doe",
    title="Head of Sales",
    department="Sales",
    sub_department="EMEA",
    site="Paris",
    timezone="Europe/Paris",
    manager_name="John Roe",
    manager_title="Chief Revenue Officer",
    direct_report_count=2,
    direct_report_names=("Ann Lee", "Bob Kim"),
    start_date="2021-03-01",
)


async def _found(_token: str, _email: str) -> DirectoryProfile | None:
    return _PROFILE


async def _absent(_token: str, _email: str) -> DirectoryProfile | None:
    return None


def test_employee_block_lists_the_allowed_fields(monkeypatch: pytest.MonkeyPatch, redis: _FakeRedis) -> None:
    _stub(monkeypatch, _found)
    block = _block(_agent(), "Jane.Doe@example.com")
    assert sender_directory.HEADING in block
    assert "<<UNTRUSTED-" in block  # directory data, quarantined
    for text in (
        "Name: Jane Doe",
        "Title: Head of Sales",
        "Department: Sales / EMEA",
        "Site: Paris, Europe/Paris",
        "Manager: John Roe (Chief Revenue Officer)",
        "Direct reports: 2 (Ann Lee, Bob Kim)",
        "Start date: 2021-03-01",
    ):
        assert text in block


def test_external_sender_is_not_in_the_directory(monkeypatch: pytest.MonkeyPatch, redis: _FakeRedis) -> None:
    _stub(monkeypatch, _absent)
    assert sender_directory.NOT_IN_DIRECTORY in _block(_agent(), "someone@partner.test")


def test_second_message_reads_the_cache(monkeypatch: pytest.MonkeyPatch, redis: _FakeRedis) -> None:
    calls = _stub(monkeypatch, _found)
    first = _block(_agent(), "jane.doe@example.com")
    second = _block(_agent(), "JANE.DOE@example.com")
    assert calls == ["jane.doe@example.com"]
    assert first == second
    assert set(redis.ttls.values()) == {sender_directory.CACHE_TTL_SECONDS}


def test_failure_never_blocks_and_is_not_cached(monkeypatch: pytest.MonkeyPatch, redis: _FakeRedis) -> None:
    async def failing(_token: str, _email: str) -> DirectoryProfile | None:
        raise RuntimeError("directory API error 503")

    calls = _stub(monkeypatch, failing)
    assert sender_directory.UNAVAILABLE in _block(_agent(), "jane.doe@example.com")
    _block(_agent(), "jane.doe@example.com")
    assert len(calls) == 2
    assert redis.store == {}


def test_timeout_says_unavailable(monkeypatch: pytest.MonkeyPatch, redis: _FakeRedis) -> None:
    monkeypatch.setattr(sender_directory, "LOOKUP_TIMEOUT_SECONDS", 0.01)

    async def slow(_token: str, _email: str) -> DirectoryProfile | None:
        await asyncio.sleep(1)
        return _PROFILE

    _stub(monkeypatch, slow)
    assert sender_directory.UNAVAILABLE in _block(_agent(), "jane.doe@example.com")


def test_not_applied_without_directory_email_or_when_off(
    monkeypatch: pytest.MonkeyPatch, redis: _FakeRedis
) -> None:
    calls = _stub(monkeypatch, _found)
    run = sender_directory.build_sender_directory_block
    no_directory = SimpleNamespace(integration_bindings={"github": ["x"]}, config={})
    assert asyncio.run(run(None, no_directory, "a@b.c")) is None
    assert asyncio.run(run(None, _agent(), None)) is None
    assert asyncio.run(run(None, _agent(sender_directory_context=False), "a@b.c")) is None
    assert calls == []


def test_block_stays_under_the_cap() -> None:
    many = DirectoryProfile(display_name="X" * 400, title="Y" * 400, direct_report_count=3)
    assert len(sender_directory.render_profile(many)) <= sender_directory.MAX_BLOCK_CHARS


def test_reports_named_only_up_to_ten() -> None:
    names = tuple(f"Person {i}" for i in range(10))
    assert "Person 9" in sender_directory.render_profile(
        DirectoryProfile(direct_report_count=10, direct_report_names=names)
    )
    assert sender_directory.render_profile(DirectoryProfile(direct_report_count=12)).endswith("Direct reports: 12")


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _fake_api(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[dict[str, Any]], tuple[_Resp | None, str | None]]
) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_client(_timeout: int = 30) -> AsyncIterator[object]:
        yield object()

    async def fake_safe_request(*_args: Any, **kwargs: Any) -> tuple[_Resp | None, str | None]:
        bodies.append(kwargs["json"])
        return answer(kwargs["json"])

    monkeypatch.setattr(hibob, "_client", fake_client)
    monkeypatch.setattr(hibob, "safe_request", fake_safe_request)
    return bodies


def test_hibob_lookup_requests_and_keeps_allowed_fields_only(monkeypatch: pytest.MonkeyPatch) -> None:
    person = {
        "id": "e1",
        "/root/id": "e1",
        "/root/displayName": "Jane Doe",
        "/work/title": "Head of Sales",
        "/work/department": "Sales",
        "/work/division": "EMEA",
        "/work/site": "Paris",
        "/work/startDate": "2021-03-01",
        "/work/reportsTo": {"value": {"id": "m1"}},
        # Sensitive fields a privileged service user could still receive.
        "/payroll/salary": "999999",
        "/personal/communication/personalEmail": "private@mail.test",
    }
    roster = [
        person,
        {"/root/id": "m1", "/root/displayName": "John Roe", "/work/title": "Chief Revenue Officer"},
        {"/root/id": "r1", "/root/displayName": "Bob Kim", "/work/reportsTo": {"value": {"id": "e1"}}},
        {"/root/id": "r2", "/root/displayName": "Ann Lee", "work": {"reportsTo": {"id": "e1"}}},
    ]
    bodies = _fake_api(
        monkeypatch, lambda body: (_Resp({"employees": [person] if "filters" in body else roster}), None)
    )
    profile = asyncio.run(hibob.lookup_directory_profile("svc||key", "jane.doe@example.com"))

    assert profile == DirectoryProfile(
        display_name="Jane Doe",
        title="Head of Sales",
        department="Sales",
        sub_department="EMEA",
        site="Paris",
        manager_name="John Roe",
        manager_title="Chief Revenue Officer",
        direct_report_count=2,
        direct_report_names=("Ann Lee", "Bob Kim"),
        start_date="2021-03-01",
    )
    for body in bodies:
        assert set(body["fields"]) <= set(hibob._DIRECTORY_FIELDS)
        assert not any(f.split(".")[0] in {"payroll", "personal", "financial", "equity"} for f in body["fields"])
    rendered = sender_directory.render_profile(profile)
    assert "999999" not in rendered
    assert "private@mail.test" not in rendered


def test_hibob_lookup_answers_none_when_nobody_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_api(monkeypatch, lambda _body: (_Resp({"employees": []}), None))
    assert asyncio.run(hibob.lookup_directory_profile("svc||key", "x@partner.test")) is None


def test_hibob_lookup_raises_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_api(monkeypatch, lambda _body: (None, "Error: directory API returned 401"))
    with pytest.raises(RuntimeError):
        asyncio.run(hibob.lookup_directory_profile("svc||key", "x@example.com"))
