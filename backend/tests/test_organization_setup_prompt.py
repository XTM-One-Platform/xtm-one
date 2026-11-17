"""The first-login organization setup proposal (#9173, S3): once per instance, administrators, XTM One only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import organization_setup_prompt as prompt


def _row(**values: object) -> SimpleNamespace:
    base = {
        "organization_setup_prompted_at": None,
        "organization_setup_prompted_by_user_id": None,
        "organization_setup_prompt_outcome": None,
    }
    base.update(values)
    return SimpleNamespace(**base)


def test_an_administrator_of_a_fresh_xtm_one_instance_is_proposed_the_setup():
    assert prompt.should_propose(is_admin=True, is_xtm_one=True, has_profile=False, row=_row())


@pytest.mark.parametrize(
    ("is_admin", "is_xtm_one", "has_profile"),
    [(False, True, False), (True, False, False), (True, True, True)],
    ids=["not-admin", "copilot-mode", "profile-saved"],
)
def test_the_setup_is_never_proposed_outside_its_audience(is_admin, is_xtm_one, has_profile):
    assert not prompt.should_propose(is_admin=is_admin, is_xtm_one=is_xtm_one, has_profile=has_profile, row=_row())


@pytest.mark.parametrize("outcome", prompt.OUTCOMES)
def test_any_answer_closes_the_proposal_for_the_whole_instance(outcome):
    row = _row()
    assert prompt.record_answer(row, outcome=outcome, user_id=uuid.uuid4())
    assert row.organization_setup_prompt_outcome == outcome
    assert row.organization_setup_prompted_at is not None
    assert not prompt.should_propose(is_admin=True, is_xtm_one=True, has_profile=False, row=row)


def test_the_first_answer_wins():
    first, second = uuid.uuid4(), uuid.uuid4()
    at = datetime(2026, 11, 8, tzinfo=UTC)
    row = _row()
    assert prompt.record_answer(row, outcome="later", user_id=first, now=at)
    assert not prompt.record_answer(row, outcome="dismissed", user_id=second)
    assert (row.organization_setup_prompt_outcome, row.organization_setup_prompted_by_user_id) == ("later", first)
    assert row.organization_setup_prompted_at == at


def test_an_unknown_answer_is_refused():
    with pytest.raises(ValueError):
        prompt.record_answer(_row(), outcome="maybe", user_id=uuid.uuid4())  # type: ignore[arg-type]


def test_the_migration_adds_the_three_columns_the_model_declares():
    from app.models.platform_settings import PlatformSettings

    source = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "s9173orgsetup1_add_organization_setup_prompt.py"
    ).read_text()
    for column in (
        "organization_setup_prompted_at",
        "organization_setup_prompted_by_user_id",
        "organization_setup_prompt_outcome",
    ):
        assert column in PlatformSettings.__table__.columns
        assert f'"{column}"' in source
