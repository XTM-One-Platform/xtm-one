"""What an agent turn receives from its workspace.

``services.workspace_knowledge`` is the one place that decides it (spec 032
FR-025). These pin its fail-closed edges without a database; the visibility of
real rows is covered in ``test_workspace_knowledge_postgres.py``.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import workspace_knowledge as wk


async def test_a_stand_in_conversation_id_never_touches_the_database():
    """Smoke tests drive a turn with ``db=object()`` and ``"conv-1"``: the
    workspace read must skip before the database is touched."""
    assert await wk.load_conversation_workspace_id(object(), "conv-1") is None
    assert await wk.load_conversation_workspace_id(object(), None) is None


@pytest.mark.parametrize("user_id", [None, "", "anonymous", "anon-participant"])
async def test_an_anonymous_participant_gets_no_workspace_knowledge(user_id):
    assert (
        await wk.resolve_turn_workspace_knowledge(object(), user_id=user_id, workspace_id=uuid.uuid4())
        is None
    )


async def test_no_workspace_means_no_knowledge():
    assert await wk.resolve_turn_workspace_knowledge(object(), user_id=str(uuid.uuid4()), workspace_id=None) is None


@pytest.mark.parametrize(
    ("window", "percent", "expected"),
    [
        (200_000, 15, 30_000),
        (200_000, 1, 10_000),  # clamped up to 5 %
        (200_000, 90, 80_000),  # clamped down to 40 %
        (8_000, 15, 1_200),
    ],
)
def test_the_sources_budget_is_a_clamped_share_of_the_window(window, percent, expected):
    assert wk.sources_budget_tokens(window, percent) == expected
