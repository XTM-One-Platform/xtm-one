"""Regression coverage for child-run shared-visibility propagation.

``is_company_managed`` — not ``user_id IS NULL`` — is
the shared-visibility signal for ``AssignmentRun`` rows. Delegation/handover
children (sub-agent background tasks, consult, transfer) previously derived
their visibility from ``user_id`` / anonymity, which now misclassifies children
of a company-managed parent run (which carries an owner/run-as ``user_id``) as
private and hides them from shared run/status views.

``resolve_child_run_is_company_managed`` centralises the fix: it reads the
parent run's flag and falls back to the caller's legacy ownerless signal only
when there is no parent id or the parent row can't be loaded.
"""

from __future__ import annotations

import uuid

import pytest

from app.api.v1.helpers import resolve_child_run_is_company_managed


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    """Minimal async-session stub recording how many queries were issued."""

    def __init__(self, value, *, raise_exc=False):
        self._value = value
        self._raise = raise_exc
        self.calls = 0

    async def execute(self, *_args, **_kwargs):
        self.calls += 1
        if self._raise:
            raise RuntimeError("boom")
        return _Result(self._value)


@pytest.mark.asyncio
async def test_company_managed_parent_propagates_true():
    """A company-managed parent forces the child shared even if the caller's
    fallback (legacy ownerless signal) would have said private."""
    session = _Session(True)
    result = await resolve_child_run_is_company_managed(
        session, str(uuid.uuid4()), fallback=False
    )
    assert result is True
    assert session.calls == 1


@pytest.mark.asyncio
async def test_private_parent_propagates_false():
    """A private parent forces the child private even if the fallback says
    shared — the parent flag is authoritative when it can be loaded."""
    session = _Session(False)
    result = await resolve_child_run_is_company_managed(
        session, str(uuid.uuid4()), fallback=True
    )
    assert result is False


@pytest.mark.asyncio
async def test_missing_parent_row_uses_fallback():
    """When the parent row can't be loaded, preserve the caller's legacy
    signal in both directions."""
    assert (
        await resolve_child_run_is_company_managed(
            _Session(None), str(uuid.uuid4()), fallback=True
        )
        is True
    )
    assert (
        await resolve_child_run_is_company_managed(
            _Session(None), str(uuid.uuid4()), fallback=False
        )
        is False
    )


@pytest.mark.asyncio
async def test_no_parent_run_id_skips_query():
    """No parent id → never issue a query, just return the fallback."""
    session = _Session(True)
    result = await resolve_child_run_is_company_managed(
        session, None, fallback=True
    )
    assert result is True
    assert session.calls == 0


@pytest.mark.asyncio
async def test_accepts_uuid_object():
    session = _Session(True)
    result = await resolve_child_run_is_company_managed(
        session, uuid.uuid4(), fallback=False
    )
    assert result is True


@pytest.mark.asyncio
async def test_malformed_parent_id_falls_back_without_query():
    session = _Session(True)
    result = await resolve_child_run_is_company_managed(
        session, "not-a-uuid", fallback=False
    )
    assert result is False
    assert session.calls == 0


@pytest.mark.asyncio
async def test_query_exception_falls_back():
    session = _Session(None, raise_exc=True)
    result = await resolve_child_run_is_company_managed(
        session, str(uuid.uuid4()), fallback=True
    )
    assert result is True


class TestTheChildRunCarriesThePromptItWasGiven:
    """A delegation waits on the worker's reconciler, so it can sit at
    ``pending`` for a while. The run-detail panel rebuilds the rendered prompt
    from the first user-role message in ``AssignmentRun.messages``, so without
    one at creation the run reads as having been sent nothing for exactly as
    long as someone might be looking at it.
    """

    def test_the_task_is_recorded_when_the_run_is_created(self):
        import inspect

        from app.services.sub_agent import create_sub_agent_task

        src = inspect.getsource(create_sub_agent_task)
        made = src.index("child_run = AssignmentRun(")
        added = src.index("own_db.add(child_run)", made)
        assert (
            'messages=[{"role": "user", "content": task_description}]'
            in src[made:added]
        )
