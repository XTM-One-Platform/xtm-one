"""Which ``{{var_id.<uuid>}}`` a run resolves, and what it says about the rest.

``load_variables`` takes its visibility from ``user_visible_filter`` (the
semantics are proven against PostgreSQL in
``test_variable_resolution_postgres.py``); an assignment run resolves as the
identity it acts as; and a placeholder the run leaves unresolved is reported
as a missing or a not-visible variable, never with advice to use the format
it is already written in.

Fake-session convention of the backend suite: statements are captured and
compiled with the PostgreSQL dialect.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1.helpers import user_visible_filter
from app.core.security import encrypt_value
from app.models.variable import Variable
from app.services import variable_resolver
from app.services.assignment_helpers import _warn_unresolved_placeholders
from app.services.variable_resolver import load_variables


def _render(clause) -> tuple[str, dict]:
    compiled = clause.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


class _CapturingDB:
    def __init__(self, rows: list | None = None) -> None:
        self.rows = rows or []
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


@pytest.mark.parametrize("as_text", [False, True], ids=["uuid", "str"])
async def test_a_signed_in_run_resolves_through_the_shared_visibility_helper(as_text):
    user_id, groups = uuid.uuid4(), [str(uuid.uuid4())]
    db = _CapturingDB()

    await load_variables(db, str(user_id) if as_text else user_id, groups)

    assert _render(db.statements[0].whereclause) == _render(
        user_visible_filter(Variable, user_id, groups)
    )


@pytest.mark.parametrize("no_account", [None, "", "anonymous"])
async def test_a_run_with_no_account_binds_no_user_and_no_group(no_account):
    db = _CapturingDB()

    await load_variables(db, no_account, [str(uuid.uuid4())])

    sql, params = _render(db.statements[0].whereclause)
    assert sql == "variables.is_company_managed IS true"
    assert params == {}


async def test_values_are_keyed_by_id_and_secrets_decrypted():
    secret = Variable(id=uuid.uuid4(), name="PASSWORD", is_secret=True, value_enc=encrypt_value("s3cret"))
    plain = Variable(id=uuid.uuid4(), name="URL", is_secret=False, value="https://qa")
    empty = Variable(id=uuid.uuid4(), name="EMPTY", is_secret=False, value=None)

    resolved = await load_variables(_CapturingDB([secret, plain, empty]), uuid.uuid4(), [])

    assert resolved == {str(secret.id): "s3cret", str(plain.id): "https://qa", str(empty.id): ""}


# -- The warning -------------------------------------------------------------


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


async def test_unresolved_variables_are_named_missing_or_not_visible_apart_from_typos(
    monkeypatch, caplog
):
    deleted, hidden, resolver = str(uuid.uuid4()), str(uuid.uuid4()), uuid.uuid4()
    lookup = AsyncMock(return_value={deleted})
    monkeypatch.setattr(variable_resolver, "missing_variable_ids", lookup)
    text = (
        f"Log in as {{{{var_id.{hidden.upper()}}}}} on {{{{var_id.{deleted}}}}}, "
        f"board {{{{BRAINSTORM_DB_ID}}}} and {{{{var_id.not-a-uuid}}}} and "
        f"again {{{{var_id.{hidden}}}}}"
    )

    with caplog.at_level(logging.WARNING, logger="app.services.assignment_helpers"):
        await _warn_unresolved_placeholders(
            object(),
            text,
            where="system prompt",
            agent_name="QA Browser",
            assignment_name="Launcher TNR",
            assignment_id=uuid.uuid4(),
            resolved_as=resolver,
        )

    lookup.assert_awaited_once()
    assert sorted(lookup.await_args.args[1]) == sorted([deleted, hidden])
    variable_warning, typo_warning = _warnings(caplog)
    assert "Unresolved platform variable(s) in system prompt" in variable_warning
    assert f"resolved as user {resolver}" in variable_warning
    assert f"no such variable (deleted, or copied from another install): {deleted}" in variable_warning
    assert f"not visible to that identity (share them with it or one of its groups, or make them company-managed): {hidden}" in variable_warning
    assert "Use {{var_id.UUID}}" not in variable_warning
    assert "{{BRAINSTORM_DB_ID}}, {{var_id.not-a-uuid}}" in typo_warning
    assert "Use {{var_id.UUID}}" in typo_warning
    assert deleted not in typo_warning and hidden not in typo_warning


async def test_a_run_with_no_user_is_said_to_resolve_company_managed_variables_only(
    monkeypatch, caplog
):
    hidden = str(uuid.uuid4())
    monkeypatch.setattr(variable_resolver, "missing_variable_ids", AsyncMock(return_value=set()))

    with caplog.at_level(logging.WARNING, logger="app.services.assignment_helpers"):
        await _warn_unresolved_placeholders(
            object(),
            f"Use {{{{var_id.{hidden}}}}}",
            where="assignment prompt",
            agent_name="QA Browser",
            assignment_name="Watcher TNR",
            assignment_id=uuid.uuid4(),
            resolved_as=None,
        )

    (warning,) = _warnings(caplog)
    assert "resolved as no user (company-managed variables only)" in warning
    assert "no such variable" not in warning


async def test_a_fully_rendered_text_logs_and_looks_up_nothing(monkeypatch, caplog):
    lookup = AsyncMock(return_value=set())
    monkeypatch.setattr(variable_resolver, "missing_variable_ids", lookup)

    with caplog.at_level(logging.WARNING, logger="app.services.assignment_helpers"):
        await _warn_unresolved_placeholders(
            object(),
            "Everything resolved.",
            where="system prompt",
            agent_name="QA Browser",
            assignment_name="Launcher TNR",
            assignment_id=uuid.uuid4(),
            resolved_as=uuid.uuid4(),
        )

    assert _warnings(caplog) == []
    lookup.assert_not_awaited()


# -- The identity an assignment run resolves as ------------------------------


@pytest.mark.parametrize("run_has_user", [True, False], ids=["run-user", "no-run-user"])
async def test_an_assignment_run_resolves_as_the_identity_it_acts_as(
    monkeypatch, caplog, run_has_user
):
    """``run.user_id`` (triggering user, run-as user, assignment owner), else
    the agent's owner - the identity quota, integrations and skills act as."""
    from app.api.v1 import helpers
    from tests.agentic_gate_stub import stub_agentic_gate
    from tests.test_assignment_stale_reference import _drive_run, _engine_objects

    stub_agentic_gate(monkeypatch)
    run, assignment, agent = _engine_objects()
    run_as = uuid.uuid4()
    run.user_id = run_as if run_has_user else None
    expected = run_as if run_has_user else agent.user_id
    hidden = str(uuid.uuid4())
    assignment.prompt = f"Log in with {{{{var_id.{hidden}}}}}."
    monkeypatch.setattr(variable_resolver, "missing_variable_ids", AsyncMock(return_value=set()))

    with caplog.at_level(logging.WARNING, logger="app.services.assignment_helpers"):
        await _drive_run(
            monkeypatch, report_integration_id=None, engine_objects=(run, assignment, agent)
        )

    loaded = variable_resolver.load_variables
    loaded.assert_awaited_once()
    assert loaded.await_args.args[1] == expected
    assert loaded.await_args.args[2] is helpers.get_user_group_ids.return_value
    assert any(call.args[1] == expected for call in helpers.get_user_group_ids.await_args_list)
    (warning,) = [w for w in _warnings(caplog) if "platform variable" in w]
    assert "in assignment prompt" in warning
    assert f"resolved as user {expected}" in warning and hidden in warning
