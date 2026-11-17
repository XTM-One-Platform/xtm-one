"""Managing an existing workspace, and what it holds.

Two gaps the end-to-end run exposed, both about workspaces that
already exist:

* Nothing said what a workspace CONTAINS, so a management view could list
  workspaces and never answer the question a person opens it with.
* A conversation could be filed into a workspace only at creation. A thread
  that already existed could never be moved into one — so the report an
  investigation files there was unreachable for every existing thread.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

# ── What a workspace holds ──────────────────────────────────────────────────


def test_contents_are_absent_rather_than_zero_when_nobody_counted() -> None:
    """Zero says "this workspace is empty". None says "nobody counted".

    The difference decides whether deleting it is safe, so the response field
    is optional rather than defaulted.
    """
    from app.api.v1.schemas.workspace import WorkspaceResponse

    row = WorkspaceResponse(
        id=str(uuid.uuid4()), user_id=None, name="W", slug="w", description="",
        icon="Layers", color="cyan", is_default=False, is_archived=False,
        is_company_managed=False, shared_group_ids=None,
        shared_group_permissions=None, can_manage=False, created_at="",
        updated_at="",
    )
    assert row.contents is None


def test_every_scoped_thing_is_counted() -> None:
    """A workspace scopes five things plus its knowledge-base bindings.

    A management view that counted four of them would report a workspace as
    empty while something still held it — and the work-item foreign key is
    RESTRICT, so "empty" and "deletable" have to agree.
    """
    from app.api.v1.schemas.workspace import WorkspaceContents

    assert set(WorkspaceContents.model_fields) == {
        "conversations",
        "work_items",
        # The filed ones, the ones that block a delete.
        "held_work_items",
        "open_work_items",
        "shared_states",
        "worksets",
        "knowledge_bases",
    }


@pytest.mark.asyncio
async def test_the_counts_cost_the_same_for_one_workspace_or_fifty() -> None:
    """The N+1 that turns a tidy page into a slow one.

    A management view lists every workspace a person can see, so a count
    fetched per row multiplies with the list. Asserted by COUNTING QUERIES
    rather than by reading the source: a future rewrite that reintroduces the
    per-row fetch would still contain the right words.
    """
    from app.services.workspaces import load_workspace_contents

    class _CountingDB:
        def __init__(self):
            self.queries = 0

        async def execute(self, *_a, **_k):
            self.queries += 1
            return SimpleNamespace(all=lambda: [])

    one = _CountingDB()
    await load_workspace_contents(one, [SimpleNamespace(id=uuid.uuid4())])

    fifty = _CountingDB()
    await load_workspace_contents(
        fifty, [SimpleNamespace(id=uuid.uuid4()) for _ in range(50)]
    )

    assert one.queries == fifty.queries
    # One per scoped thing, and nothing else.
    assert one.queries == 6


@pytest.mark.asyncio
async def test_an_empty_workspace_still_gets_every_key() -> None:
    """So a caller renders a row without checking for missing keys."""
    from app.services.workspaces import load_workspace_contents

    class _DB:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(all=lambda: [])

    ws = SimpleNamespace(id=uuid.uuid4())
    counts = await load_workspace_contents(_DB(), [ws])
    assert counts[str(ws.id)] == {
        "conversations": 0, "work_items": 0, "held_work_items": 0, "open_work_items": 0,
        "shared_states": 0, "worksets": 0, "knowledge_bases": 0,
    }


@pytest.mark.asyncio
async def test_no_workspaces_asks_the_database_nothing() -> None:
    from app.services.workspaces import load_workspace_contents

    class _DB:
        async def execute(self, *_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("queried with no workspaces to count")

    assert await load_workspace_contents(_DB(), []) == {}


# ── Filing an existing conversation ─────────────────────────────────────────


def test_a_conversation_can_be_filed_after_it_was_created() -> None:
    """The gap the E2E hit: `ConversationUpdate` carried only `title`."""
    from app.api.v1.chat import ConversationUpdate

    assert "workspace_id" in ConversationUpdate.model_fields
    # Title becomes optional so filing alone is a valid request.
    assert ConversationUpdate(workspace_id=str(uuid.uuid4())).title is None


def test_leaving_the_workspace_alone_differs_from_unfiling_it() -> None:
    """`None` alone cannot express both, which is why the endpoint branches on
    `model_fields_set` rather than on the value."""
    from app.api.v1.chat import ConversationUpdate

    untouched = ConversationUpdate(title="Renamed")
    unfiled = ConversationUpdate(workspace_id=None)
    assert "workspace_id" not in untouched.model_fields_set
    assert "workspace_id" in unfiled.model_fields_set
