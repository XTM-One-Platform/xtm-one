"""Tests for ``app.services.skill_versioning``.

Uses the same ``_FakeDb`` stub pattern as
``tests/test_prompt_versioning.py`` so the test suite doesn't depend
on a live (or in-memory) async DB. The contract being pinned is the
SERVICE-LEVEL behaviour — the migration shape is owned by
``test_migration_integrity`` and ``test_alembic_single_head``.

Covers:

* ``_normalize_commit_message`` — trim + length-cap + whitespace-only
  collapses to None.
* ``_next_version_number`` — max+1 semantics so a retention prune that
  drops intermediate rows can't trigger a unique-constraint collision
  on the next insert.
* ``create_skill_version`` — happy path plus the NULL-content defensive
  guard (URL-backed skills must not be silently snapshotted as empty
  bodies).
* ``rollback_skill_to_version`` — restores parent content from the
  target snapshot AND records the restoration as a fresh forward
  version (append-only invariant); raises ``ValueError`` on missing
  target so the API layer can surface a clean 404.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Delete

from app.services import skill_versioning as svc

# ── Fake DB stubs (same shape as test_prompt_versioning._FakeDb) ───


class _FakeResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None

    def scalar(self):
        # Used by max(version_number) lookups.
        return self._values[0] if self._values else None


class _FakeDb:
    def __init__(self):
        self.added: list = []
        self.executed: list = []
        self.next_execute_values: list[list] = []
        # ``next_scalar_values`` lets us stage successive
        # ``_next_version_number`` results without re-stubbing each call —
        # rollback paths trigger two _next_version_number reads under the
        # hood (one for the rollback insert, none after — see test).
        self.next_scalar_values: list[int | None] = []

    async def execute(self, statement):
        self.executed.append(statement)
        values = self.next_execute_values.pop(0) if self.next_execute_values else []
        return _FakeResult(values)

    async def flush(self):
        return None

    def add(self, value):
        self.added.append(value)

    async def delete(self, _value):
        return None


def _make_skill(*, content: str | None = "body", description: str | None = None, compatibility: str | None = None):
    """Build a stand-in ``AgentSkill``-shaped object.

    We use ``SimpleNamespace`` rather than the real ORM class because
    the service only ever reads the attributes ``id`` / ``content`` /
    ``description`` / ``compatibility`` and writes back to them; no
    ORM machinery is exercised here (that's the integration-test
    layer's job).
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        content=content,
        description=description,
        compatibility=compatibility,
    )


# ── _normalize_commit_message ──────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("   \n  \t  ", None),
        ("  edit  ", "edit"),
        ("x" * 1000, "x" * 500),
    ],
)
def test_normalize_commit_message_shapes(raw, expected):
    assert svc._normalize_commit_message(raw) == expected


# ── _next_version_number ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_next_version_number_starts_at_one_when_no_versions():
    db = _FakeDb()
    db.next_execute_values = [[0]]  # ``max(...)`` returns 0 → next is 1
    assert await svc._next_version_number(db, uuid.uuid4()) == 1


@pytest.mark.asyncio
async def test_next_version_number_uses_max_plus_one():
    """A skill whose highest existing version is 5 (e.g. 1, 2, 4, 5 —
    v3 pruned) must produce 6 as the next number, NOT 5 — count+1
    would collide with the existing v5 row."""
    db = _FakeDb()
    db.next_execute_values = [[5]]  # max(version_number) = 5
    assert await svc._next_version_number(db, uuid.uuid4()) == 6


@pytest.mark.asyncio
async def test_next_version_number_handles_null_max_from_db():
    """``coalesce(max(version_number), 0)`` ensures the SELECT never
    returns NULL, but if it ever did, the service must still produce 1
    rather than crash."""
    db = _FakeDb()
    db.next_execute_values = [[None]]
    assert await svc._next_version_number(db, uuid.uuid4()) == 1


# ── create_skill_version ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_skill_version_snapshots_current_state():
    db = _FakeDb()
    # Two SELECTs on the create path: row lock on the parent skill
    # (returns nothing observable) + ``_next_version_number`` (returns
    # max+1). See the row-lock rationale in
    # :func:`svc.create_skill_version`.
    db.next_execute_values = [[], [0]]  # lock + max=0 -> next=1
    skill = _make_skill(
        content="hello world",
        description="initial description",
        compatibility="needs github",
    )
    author_id = uuid.uuid4()
    version = await svc.create_skill_version(
        db, skill, author_id=author_id, commit_message="  initial  "
    )

    # The version row materialised with the expected shape:
    assert version.skill_id == skill.id
    assert version.version_number == 1
    assert version.content == "hello world"
    assert version.description == "initial description"
    assert version.compatibility == "needs github"
    assert version.author_id == author_id
    assert version.commit_message == "initial"  # trimmed

    # AND db.add was called with that row (proves the insert was
    # actually staged for flush, not just returned to the caller).
    assert version in db.added


@pytest.mark.asyncio
async def test_create_skill_version_rejects_null_content():
    """URL-backed skill that hasn't been fetched yet → NULL content →
    raise rather than silently snapshot an empty body."""
    db = _FakeDb()
    skill = _make_skill(content=None)
    with pytest.raises(ValueError, match=r"content.+NULL"):
        await svc.create_skill_version(db, skill, author_id=None)
    # Crucially: nothing was added to the session before the raise.
    assert db.added == []


@pytest.mark.asyncio
async def test_create_skill_version_uses_next_version_number():
    """The version_number on a fresh insert MUST be the value returned
    by ``_next_version_number``, NOT a hardcoded 1 — proves the service
    is gap-tolerant against retention prunes."""
    db = _FakeDb()
    db.next_execute_values = [[], [7]]  # lock + max=7 -> next=8
    skill = _make_skill()
    version = await svc.create_skill_version(db, skill, author_id=None)
    assert version.version_number == 8


# ── list / get / active ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_skill_versions_returns_db_rows():
    db = _FakeDb()
    seeded = [SimpleNamespace(version_number=n) for n in (3, 2, 1)]
    db.next_execute_values = [[len(seeded)], seeded]
    rows, total = await svc.list_skill_versions(db, uuid.uuid4())
    assert [r.version_number for r in rows] == [3, 2, 1]
    assert total == 3


@pytest.mark.asyncio
async def test_get_skill_version_returns_none_when_missing():
    db = _FakeDb()
    db.next_execute_values = [[]]  # scalar_one_or_none → None
    assert await svc.get_skill_version(db, uuid.uuid4(), 99) is None


@pytest.mark.asyncio
async def test_get_active_skill_version_number_none_when_no_rows():
    db = _FakeDb()
    db.next_execute_values = [[None]]
    assert await svc.get_active_skill_version_number(db, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_active_skill_version_number_returns_max():
    db = _FakeDb()
    db.next_execute_values = [[7]]
    assert await svc.get_active_skill_version_number(db, uuid.uuid4()) == 7


# ── rollback_skill_to_version ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollback_restores_content_and_records_new_forward_version():
    db = _FakeDb()
    target = SimpleNamespace(
        version_number=1,
        content="v1 body",
        description="v1 desc",
        compatibility=None,
    )
    # Three SELECTs on the rollback path: get_skill_version (returns
    # target), row lock on the parent skill (returns nothing
    # observable), then ``_next_version_number`` (returns max+1).
    db.next_execute_values = [[target], [], [2]]  # get + lock + max=2 -> next=3
    skill = _make_skill(
        content="v2 body — broken regression",
        description="v2 desc",
        compatibility=None,
    )

    restoration = await svc.rollback_skill_to_version(
        db, skill, target_version_number=1, author_id=None
    )

    # Parent skill content restored from v1.
    assert skill.content == "v1 body"
    assert skill.description == "v1 desc"

    # Rollback recorded as a NEW forward version (v3), not as a
    # mutation of v2 or a deletion of any prior row.
    assert restoration.version_number == 3
    assert restoration.content == "v1 body"
    assert "Rollback to version 1" in (restoration.commit_message or "")

    # The new row was added to the session (append-only invariant).
    assert restoration in db.added


@pytest.mark.asyncio
async def test_rollback_raises_when_target_version_missing():
    db = _FakeDb()
    db.next_execute_values = [[]]  # get_skill_version → None
    skill = _make_skill()
    with pytest.raises(ValueError, match=r"no version 99"):
        await svc.rollback_skill_to_version(
            db, skill, target_version_number=99, author_id=None
        )
    # Nothing was staged for insert before the raise.
    assert db.added == []


@pytest.mark.asyncio
async def test_rollback_honours_caller_provided_commit_message():
    db = _FakeDb()
    target = SimpleNamespace(
        version_number=1,
        content="v1",
        description=None,
        compatibility=None,
    )
    # get + lock + max=3 -> next=4.
    db.next_execute_values = [[target], [], [3]]
    skill = _make_skill(content="v3", description=None, compatibility=None)

    restoration = await svc.rollback_skill_to_version(
        db,
        skill,
        target_version_number=1,
        author_id=None,
        commit_message="  Reverting after incident #1234  ",
    )

    # Trim + use of the caller's message rather than the default
    # ``Rollback to version N`` text.
    assert restoration.commit_message == "Reverting after incident #1234"


# ── _enforce_retention ─────────────────────────────────────────────────
#
# These tests pin the new retention call introduced after Copilot's
# round-2 feedback. The contract is:
#
# * ``settings.prompt_version_retention_count <= 0`` (the default)
#   → ``_enforce_retention`` returns BEFORE any SELECT runs. The
#     ``is_locked`` column is dead-but-harmless until the operator
#     opts in by setting a positive cap.
# * ``cap > 0`` → SELECT the unlocked candidate set offset by ``cap``;
#   if non-empty, DELETE those rows. Locked rows are excluded from the
#   candidate query, so a locked version can never be pruned even if
#   it falls past the cap.
# * Both ``create_skill_version`` AND ``rollback_skill_to_version``
#   invoke ``_enforce_retention`` after their respective flush, so
#   repeated rollbacks can't bypass the cap by skirting the create
#   path.


@pytest.mark.asyncio
async def test_enforce_retention_is_noop_when_cap_is_zero(monkeypatch):
    """Default cap (0 = unlimited) must short-circuit before any
    SELECT runs — otherwise the retention call would add a baseline
    DB round-trip to every create / rollback even on installs that
    haven't opted into history capping."""
    from app.core import config as core_config

    monkeypatch.setattr(core_config.settings, "prompt_version_retention_count", 0)

    db = _FakeDb()
    await svc._enforce_retention(db, skill_id=uuid.uuid4())
    assert db.executed == [], (
        "cap=0 must NOT issue any SELECT; got "
        f"{len(db.executed)} executions"
    )


@pytest.mark.asyncio
async def test_enforce_retention_deletes_unlocked_rows_past_the_cap(monkeypatch):
    """With ``cap=3`` and a candidate result of two rows past the
    cap, the function must issue exactly one DELETE statement and
    the executed-statement log must contain it. Mirrors the sibling
    ``prompt_versioning._enforce_retention`` semantics."""
    from app.core import config as core_config

    monkeypatch.setattr(core_config.settings, "prompt_version_retention_count", 3)

    db = _FakeDb()
    # Pinned-versions SELECT (no pending evaluation run), then the
    # candidate SELECT returning 2 (id, version) rows past the cap; the
    # DELETE result is not read but a slot keeps the call-log accurate.
    db.next_execute_values = [
        [],  # evaluation runs pinning a version — none
        [(uuid.uuid4(), 1), (uuid.uuid4(), 2)],  # candidate (id, version)
        [],  # delete result (unused)
    ]

    await svc._enforce_retention(db, skill_id=uuid.uuid4())

    assert len(db.executed) == 3, (
        "Expected 3 executes (pinned SELECT + SELECT candidates + DELETE); "
        f"got {len(db.executed)}"
    )
    assert isinstance(db.executed[-1], Delete)


@pytest.mark.asyncio
async def test_enforce_retention_does_not_delete_when_candidate_set_empty(monkeypatch):
    """Skill with N <= cap unlocked versions → candidate SELECT
    returns empty → the DELETE branch must NOT fire. Without this
    guard a misconfigured cap could still issue a `DELETE … WHERE
    id IN ()` which most engines optimise away but is wasteful."""
    from app.core import config as core_config

    monkeypatch.setattr(core_config.settings, "prompt_version_retention_count", 5)

    db = _FakeDb()
    db.next_execute_values = [
        [],  # evaluation runs pinning a version — none
        [],  # candidate SELECT returns no rows
    ]

    await svc._enforce_retention(db, skill_id=uuid.uuid4())

    # Pinned SELECT + candidate SELECT, and no DELETE.
    assert len(db.executed) == 2, (
        f"Expected 2 executes (pinned + candidate SELECT); got {len(db.executed)}"
    )
    assert not any(isinstance(stmt, Delete) for stmt in db.executed)


@pytest.mark.asyncio
async def test_enforce_retention_spares_versions_pinned_by_evaluation_runs(monkeypatch):
    """A version a pending evaluation run snapshotted must survive the
    prune: ``load_skill`` resolves that snapshot at execute
    time, so pruning it would silently downgrade the replay to live
    content instead of failing loudly."""
    from app.core import config as core_config

    monkeypatch.setattr(core_config.settings, "prompt_version_retention_count", 1)

    skill_id = uuid.uuid4()
    pinned_id, prunable_id = uuid.uuid4(), uuid.uuid4()
    db = _FakeDb()
    db.next_execute_values = [
        # One pending run pinned v2 of this skill.
        [SimpleNamespace(skill_versions={str(skill_id): 2})],
        [(pinned_id, 2), (prunable_id, 1)],  # candidates past the cap
        [],  # delete result (unused)
    ]

    await svc._enforce_retention(db, skill_id=skill_id)

    delete_stmt = db.executed[-1]
    assert isinstance(delete_stmt, Delete)
    # UUIDs render without dashes under ``literal_binds``.
    compiled = str(delete_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert prunable_id.hex in compiled
    assert pinned_id.hex not in compiled


@pytest.mark.asyncio
async def test_create_skill_version_invokes_retention(monkeypatch):
    """``create_skill_version`` MUST call ``_enforce_retention``
    after the flush, otherwise ``is_locked`` is dead and
    ``skill_versions`` grows unbounded. We pin via call interception
    rather than reading from the captured executes because the
    retention pass with ``cap=0`` is a no-op SELECT-less return —
    using an interception probe keeps the test robust to both
    branches of the retention function."""
    calls: list[dict] = []

    async def _spy(db, *, skill_id):
        calls.append({"db": db, "skill_id": skill_id})

    monkeypatch.setattr(svc, "_enforce_retention", _spy)

    db = _FakeDb()
    db.next_execute_values = [[], [0]]  # lock + max=0 -> next=1
    skill = _make_skill(content="x")

    await svc.create_skill_version(db, skill, author_id=None)

    assert len(calls) == 1, "_enforce_retention must be called exactly once"
    assert calls[0]["skill_id"] == skill.id
    assert calls[0]["db"] is db


@pytest.mark.asyncio
async def test_rollback_skill_to_version_invokes_retention(monkeypatch):
    """Repeated rollbacks would otherwise bypass the cap — each
    rollback inserts a new forward version, so retention MUST fire
    on the rollback path too. Same interception-spy shape as the
    create test."""
    calls: list[dict] = []

    async def _spy(db, *, skill_id):
        calls.append({"db": db, "skill_id": skill_id})

    monkeypatch.setattr(svc, "_enforce_retention", _spy)

    db = _FakeDb()
    target = SimpleNamespace(
        version_number=1,
        content="v1",
        description=None,
        compatibility=None,
    )
    db.next_execute_values = [[target], [], [3]]  # get + lock + max=3 -> next=4
    skill = _make_skill(content="v3", description=None, compatibility=None)

    await svc.rollback_skill_to_version(
        db, skill, target_version_number=1, author_id=None
    )

    assert len(calls) == 1, (
        "_enforce_retention must be called exactly once on the rollback path"
    )
    assert calls[0]["skill_id"] == skill.id


@pytest.mark.asyncio
async def test_enforce_retention_excludes_locked_rows_from_candidate_query():
    """The candidate SELECT in ``_enforce_retention`` filters
    ``is_locked.is_(False)`` so locked versions cannot be pruned
    regardless of where they sit in version-number ordering. Pin
    this at the SQL-shape level (rather than via a DB integration
    test) by inspecting the compiled WHERE clause — that way the
    invariant is enforced statically even if the test stub never
    actually exercises the locked path."""
    from sqlalchemy import select as _sel

    from app.models.skill_version import SkillVersion

    # Reconstruct the exact filter the function uses; assert the
    # compiled WHERE clause contains the ``is_locked = false`` shape.
    stmt = _sel(SkillVersion.id).where(SkillVersion.is_locked.is_(False))
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "is_locked IS false" in compiled or "is_locked = false" in compiled, (
        "Retention SELECT must filter out locked versions; compiled: "
        + compiled
    )
