"""The first-connection setup proposal (#9173 S3, versioned by #1087): once per instance and version,
administrators, XTM One only; each step of the guided flow saved on its own."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import organization_setup_prompt as prompt


def _row(**values: object) -> Any:
    base = {
        "organization_setup_prompted_at": None,
        "organization_setup_prompted_by_user_id": None,
        "organization_setup_prompt_outcome": None,
        "setup_prompt_version": None,
        "setup_prompt_progress": None,
    }
    base.update(values)
    return SimpleNamespace(**base)


def test_an_administrator_of_a_fresh_xtm_one_instance_is_proposed_the_setup() -> None:
    assert prompt.should_propose(is_admin=True, is_xtm_one=True, row=_row())


@pytest.mark.parametrize(
    ("is_admin", "is_xtm_one"),
    [(False, True), (True, False), (False, False)],
    ids=["not-admin", "copilot-mode", "neither"],
)
def test_the_setup_is_never_proposed_outside_its_audience(is_admin: bool, is_xtm_one: bool) -> None:
    assert not prompt.should_propose(is_admin=is_admin, is_xtm_one=is_xtm_one, row=_row())


def test_an_upgraded_instance_that_answered_the_unversioned_proposal_is_proposed_the_new_version_once() -> None:
    legacy = _row(
        organization_setup_prompted_at=datetime(2026, 11, 8, tzinfo=UTC),
        organization_setup_prompt_outcome="dismissed",
    )
    assert prompt.should_propose(is_admin=True, is_xtm_one=True, row=legacy)
    assert prompt.state_of(propose=True, row=legacy).answered_version == "legacy"
    assert prompt.state_of(propose=True, row=legacy).outcome is None
    assert prompt.record_answer(legacy, outcome="later", user_id=uuid.uuid4())
    assert not prompt.should_propose(is_admin=True, is_xtm_one=True, row=legacy)


def test_a_newer_setup_version_is_proposed_again_and_an_answered_one_is_not() -> None:
    row = _row()
    prompt.record_answer(row, outcome="started", user_id=uuid.uuid4())
    assert not prompt.should_propose(is_admin=True, is_xtm_one=True, row=row)
    assert prompt.should_propose(is_admin=True, is_xtm_one=True, row=row, version="ctem-loop-2")


@pytest.mark.parametrize("outcome", prompt.OUTCOMES)
def test_any_answer_closes_the_proposal_of_that_version_for_the_whole_instance(outcome: prompt.Outcome) -> None:
    row = _row()
    assert prompt.record_answer(row, outcome=outcome, user_id=uuid.uuid4())
    assert row.organization_setup_prompt_outcome == outcome
    assert row.setup_prompt_version == prompt.SETUP_VERSION
    assert row.organization_setup_prompted_at is not None
    assert not prompt.should_propose(is_admin=True, is_xtm_one=True, row=row)


def test_the_first_answer_wins_and_a_second_administrator_never_sees_it_again() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    at = datetime(2026, 11, 22, tzinfo=UTC)
    row = _row()
    assert prompt.record_answer(row, outcome="later", user_id=first, now=at)
    assert not prompt.record_answer(row, outcome="dismissed", user_id=second)
    assert (row.organization_setup_prompt_outcome, row.organization_setup_prompted_by_user_id) == ("later", first)
    assert row.organization_setup_prompted_at == at
    assert not prompt.should_propose(is_admin=True, is_xtm_one=True, row=row)


def test_an_unknown_answer_is_refused() -> None:
    with pytest.raises(ValueError):
        prompt.record_answer(_row(), outcome="maybe", user_id=uuid.uuid4())  # type: ignore[arg-type]


def test_each_step_saves_on_its_own_and_resumes() -> None:
    row = _row()
    assert prompt.record_step(row, step="organization", state="done") == {"organization": "done"}
    assert prompt.record_step(row, step="modules", state="skipped") == {"organization": "done", "modules": "skipped"}
    assert prompt.state_of(propose=True, row=row).steps == {"organization": "done", "modules": "skipped"}
    # Recording progress answers nothing and enables nothing: the proposal is still open.
    assert prompt.should_propose(is_admin=True, is_xtm_one=True, row=row)


def test_progress_of_an_older_version_reads_empty() -> None:
    row = _row(setup_prompt_progress={"version": "ctem-loop-0", "steps": {"organization": "done"}})
    assert prompt.steps_of(row) == {}
    assert prompt.record_step(row, step="gates", state="done") == {"gates": "done"}
    assert row.setup_prompt_progress == {"version": prompt.SETUP_VERSION, "steps": {"gates": "done"}}


def test_garbage_in_the_progress_column_is_ignored() -> None:
    row = _row(
        setup_prompt_progress={
            "version": prompt.SETUP_VERSION,
            "steps": {"organization": "done", "x": "done", "gates": "maybe"},
        }
    )
    assert prompt.steps_of(row) == {"organization": "done"}
    assert prompt.steps_of(_row(setup_prompt_progress=["not", "a", "dict"])) == {}


@pytest.mark.parametrize(("step", "state"), [("billing", "done"), ("gates", "maybe")])
def test_an_unknown_step_or_state_is_refused(step: str, state: str) -> None:
    with pytest.raises(ValueError):
        prompt.record_step(_row(), step=step, state=state)  # type: ignore[arg-type]


def test_the_steps_are_the_six_of_the_ctem_loop_setup_in_order() -> None:
    assert prompt.STEPS == ("organization", "modules", "ecosystem", "gates", "preview", "enable")
    assert prompt.SETUP_VERSION == "ctem-loop-1"


def _answered_row() -> Any:
    row = _row()
    prompt.record_step(row, step="organization", state="done")
    prompt.record_step(row, step="modules", state="skipped")
    assert prompt.record_answer(row, outcome="started", user_id=uuid.uuid4())
    return row


def test_reset_onboarding_proposes_the_setup_again_from_its_first_step() -> None:
    row = _answered_row()
    assert not prompt.should_propose(is_admin=True, is_xtm_one=True, row=row)
    assert prompt.reset_answer(row)
    assert prompt.should_propose(is_admin=True, is_xtm_one=True, row=row)
    state = prompt.state_of(propose=True, row=row)
    assert (state.outcome, state.answered_at, state.answered_version, state.steps) == (None, None, None, {})
    assert row.organization_setup_prompted_by_user_id is None
    # The version the build carries is unchanged: a reset never bumps it.
    assert state.version == prompt.SETUP_VERSION == "ctem-loop-1"


@pytest.mark.parametrize(
    ("is_admin", "is_xtm_one"),
    [(False, True), (True, False)],
    ids=["not-admin", "copilot-mode"],
)
def test_reset_onboarding_keeps_the_audience_rule(is_admin: bool, is_xtm_one: bool) -> None:
    row = _answered_row()
    prompt.reset_answer(row)
    assert not prompt.should_propose(is_admin=is_admin, is_xtm_one=is_xtm_one, row=row)


def test_reset_onboarding_on_a_never_answered_instance_is_a_no_op() -> None:
    row = _row()
    assert not prompt.reset_answer(row)
    assert vars(row) == vars(_row())
    assert prompt.should_propose(is_admin=True, is_xtm_one=True, row=row)


class _Db:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _patch_route(monkeypatch: pytest.MonkeyPatch, row: Any, *, xtm_one: bool) -> list[dict[str, Any]]:
    from unittest.mock import PropertyMock

    from app.api.v1 import platform_settings_helpers
    from app.core import config
    from app.services import audit

    audits: list[dict[str, Any]] = []

    async def _settings_row(_db: object) -> Any:
        return row

    async def _log_audit(_db: object, **kwargs: Any) -> None:
        audits.append(kwargs)

    async def _has_profile(_db: object) -> bool:
        return True

    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", _settings_row)
    monkeypatch.setattr(audit, "log_audit", _log_audit)
    monkeypatch.setattr(prompt, "has_saved_profile", _has_profile)
    # Patch the class of the instance the route reads, never config.Settings by name:
    # after a module reload the two differ and the patch would miss (#1239).
    settings_class = type(config.settings)
    monkeypatch.setattr(settings_class, "is_xtm_one", PropertyMock(return_value=xtm_one))
    monkeypatch.setattr(settings_class, "is_copilot_like", PropertyMock(return_value=not xtm_one))
    return audits


async def test_the_reset_route_clears_the_answer_audits_and_answers_the_open_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.v1.organization_context import reset_setup_prompt

    row = _answered_row()
    audits = _patch_route(monkeypatch, row, xtm_one=True)
    db = _Db()
    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    response = await reset_setup_prompt(db=db, admin=admin)  # type: ignore[arg-type]
    assert response.propose is True
    assert (response.outcome, response.answered_at, response.steps) == (None, None, {})
    assert response.version == "ctem-loop-1"
    assert db.commits == 1
    assert len(audits) == 1
    assert audits[0]["action"] == "configure"
    assert (audits[0]["entity_type"], audits[0]["entity_name"]) == ("organization_context", "setup_prompt")
    assert audits[0]["summary"] == "Reset the setup proposal ctem-loop-1"
    # A second reset clears nothing and writes no second audit entry.
    await reset_setup_prompt(db=db, admin=admin)  # type: ignore[arg-type]
    assert len(audits) == 1


async def test_the_reset_route_is_refused_in_copilot_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from app.api.v1.organization_context import reset_setup_prompt

    row = _answered_row()
    audits = _patch_route(monkeypatch, row, xtm_one=False)
    with pytest.raises(HTTPException) as exc:
        await reset_setup_prompt(db=_Db(), admin=SimpleNamespace(id=uuid.uuid4(), is_admin=True))  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    assert row.setup_prompt_version == prompt.SETUP_VERSION
    assert audits == []


async def test_the_reset_route_is_for_administrators_only() -> None:
    from fastapi import HTTPException
    from fastapi.routing import APIRoute

    from app.api.deps import require_admin
    from app.api.v1.organization_context import reset_setup_prompt, router

    route = next(
        r for r in router.routes if isinstance(r, APIRoute) and r.path == "/setup-prompt" and "DELETE" in r.methods
    )
    assert route.endpoint is reset_setup_prompt
    assert any(dep.call is require_admin for dep in route.dependant.dependencies)
    with pytest.raises(HTTPException) as exc:
        await require_admin(user=SimpleNamespace(is_admin=False))  # type: ignore[arg-type]
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    ("migration", "columns"),
    [
        (
            "s9173orgsetup1_add_organization_setup_prompt.py",
            (
                "organization_setup_prompted_at",
                "organization_setup_prompted_by_user_id",
                "organization_setup_prompt_outcome",
            ),
        ),
        ("s1087setupver1_add_setup_prompt_version.py", ("setup_prompt_version", "setup_prompt_progress")),
    ],
)
def test_the_migrations_add_the_columns_the_model_declares(migration: str, columns: tuple[str, ...]) -> None:
    from app.models.platform_settings import PlatformSettings

    source = (Path(__file__).resolve().parents[1] / "alembic" / "versions" / migration).read_text()
    for column in columns:
        assert column in PlatformSettings.__table__.columns
        assert f'"{column}"' in source
