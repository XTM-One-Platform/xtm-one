import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import prompt_versioning as svc


class _FakeResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None


class _FakeDb:
    def __init__(self):
        self.added = []
        self.executed = []
        self.scalar_value = None
        self.next_execute_values = []

    async def execute(self, statement):
        self.executed.append(statement)
        values = self.next_execute_values.pop(0) if self.next_execute_values else []
        return _FakeResult(values)

    async def flush(self):
        return None

    def add(self, value):
        self.added.append(value)

    async def scalar(self, _statement):
        return self.scalar_value


def _version_row(version_number: int, *, author_id=None, content="x"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        version_number=version_number,
        author_id=author_id,
        commit_message=None,
        created_at=datetime.now(UTC),
        content=content,
    )


def test_normalize_commit_message():
    assert svc._normalize_commit_message(None) is None
    assert svc._normalize_commit_message("   ") is None
    assert svc._normalize_commit_message("  hello  ") == "hello"


@pytest.mark.asyncio
async def test_next_version_number_raises_without_parent():
    with pytest.raises(ValueError):
        await svc._next_version_number(SimpleNamespace(), prompt_id=None, agent_id=None)


@pytest.mark.asyncio
async def test_next_version_number_uses_current_max():
    db = _FakeDb()
    db.scalar_value = 4
    result = await svc._next_version_number(db, prompt_id=uuid.uuid4())
    assert result == 5


@pytest.mark.asyncio
async def test_create_prompt_version_creates_snapshot(monkeypatch):
    db = _FakeDb()
    prompt_id = uuid.uuid4()
    author_id = uuid.uuid4()
    retention_calls = []

    async def _fake_next_version_number(_db, *, prompt_id=None, agent_id=None):
        assert prompt_id is not None
        assert agent_id is None
        return 7

    async def _fake_retention(_db, *, prompt_id=None, agent_id=None):
        retention_calls.append((prompt_id, agent_id))

    monkeypatch.setattr(svc, "_next_version_number", _fake_next_version_number)
    monkeypatch.setattr(svc, "_enforce_retention", _fake_retention)

    version = await svc.create_prompt_version(
        db,
        prompt_id=prompt_id,
        content="new content",
        author_id=author_id,
        commit_message="  test message  ",
    )

    assert version.prompt_id == prompt_id
    assert version.agent_id is None
    assert version.version_number == 7
    assert version.content == "new content"
    assert version.author_id == author_id
    assert version.commit_message == "test message"
    assert db.added and db.added[0] is version
    assert retention_calls == [(prompt_id, None)]


@pytest.mark.asyncio
async def test_create_agent_persona_version_creates_snapshot(monkeypatch):
    db = _FakeDb()
    agent_id = uuid.uuid4()
    author_id = uuid.uuid4()
    retention_calls = []

    async def _fake_next_version_number(_db, *, prompt_id=None, agent_id=None):
        assert prompt_id is None
        assert agent_id is not None
        return 2

    async def _fake_retention(_db, *, prompt_id=None, agent_id=None):
        retention_calls.append((prompt_id, agent_id))

    locked = []

    async def _fake_lock(_db: object, locked_id: uuid.UUID) -> None:
        locked.append(locked_id)

    monkeypatch.setattr(svc, "_next_version_number", _fake_next_version_number)
    monkeypatch.setattr(svc, "_enforce_retention", _fake_retention)
    monkeypatch.setattr(svc, "lock_agent_for_versioning", _fake_lock)

    version = await svc.create_agent_persona_version(
        db,
        agent_id=agent_id,
        content="persona body",
        author_id=author_id,
        commit_message="   ",
    )

    assert version.prompt_id is None
    assert version.agent_id == agent_id
    assert version.version_number == 2
    assert version.content == "persona body"
    assert version.author_id == author_id
    assert version.commit_message is None
    assert db.added and db.added[0] is version
    assert retention_calls == [(None, agent_id)]
    assert locked == [agent_id]


def test_build_version_diff_returns_structured_counts_and_unified_text():
    diff = svc.build_version_diff(
        from_content="line1\nline2",
        to_content="line1\nline3",
        from_version=1,
        to_version=2,
    )

    assert diff["from_version"] == 1
    assert diff["to_version"] == 2
    assert diff["added"] == 1
    assert diff["removed"] == 1
    assert any(line["type"] == "remove" and line["content"] == "line2" for line in diff["lines"])
    assert any(line["type"] == "add" and line["content"] == "line3" for line in diff["lines"])
    assert "--- v1" in diff["unified_diff"]
    assert "+++ v2" in diff["unified_diff"]


@pytest.mark.asyncio
async def test_rollback_prompt_to_version_creates_new_head(monkeypatch):
    prompt = SimpleNamespace(id=uuid.uuid4(), content="current")
    target = SimpleNamespace(version_number=3, content="target content")
    created = _version_row(4, content="target content")

    async def _fake_create_prompt_version(
        _db,
        *,
        prompt_id,
        content,
        author_id,
        commit_message,
    ):
        assert prompt_id == prompt.id
        assert content == target.content
        assert commit_message == "rollback msg"
        assert author_id is not None
        return created

    monkeypatch.setattr(svc, "create_prompt_version", _fake_create_prompt_version)

    result = await svc.rollback_prompt_to_version(
        SimpleNamespace(),
        prompt=prompt,
        target_version=target,
        author_id=uuid.uuid4(),
        commit_message="rollback msg",
    )

    assert prompt.content == "target content"
    assert result is created


@pytest.mark.asyncio
async def test_rollback_prompt_to_version_sets_default_commit_message(monkeypatch):
    prompt = SimpleNamespace(id=uuid.uuid4(), content="current")
    target = SimpleNamespace(version_number=11, content="target content")
    created = _version_row(12, content="target content")

    async def _fake_create_prompt_version(
        _db,
        *,
        prompt_id,
        content,
        author_id,
        commit_message,
    ):
        assert prompt_id == prompt.id
        assert content == target.content
        assert commit_message == "Rollback from version V11"
        assert author_id is not None
        return created

    monkeypatch.setattr(svc, "create_prompt_version", _fake_create_prompt_version)

    result = await svc.rollback_prompt_to_version(
        SimpleNamespace(),
        prompt=prompt,
        target_version=target,
        author_id=uuid.uuid4(),
        commit_message=None,
    )

    assert prompt.content == "target content"
    assert result is created


@pytest.mark.asyncio
async def test_rollback_agent_persona_to_version_creates_new_head(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), persona="current persona")
    target = SimpleNamespace(version_number=5, content="restored persona")
    created = _version_row(6, content="restored persona")

    async def _fake_create_agent_persona_version(
        _db,
        *,
        agent_id,
        content,
        author_id,
        commit_message,
    ):
        assert agent_id == agent.id
        assert content == target.content
        assert commit_message == "manual rollback"
        assert author_id is not None
        return created

    monkeypatch.setattr(
        svc, "create_agent_persona_version", _fake_create_agent_persona_version
    )

    result = await svc.rollback_agent_persona_to_version(
        SimpleNamespace(),
        agent=agent,
        target_version=target,
        author_id=uuid.uuid4(),
        commit_message="manual rollback",
    )

    assert agent.persona == "restored persona"
    assert result is created


@pytest.mark.asyncio
async def test_rollback_agent_persona_to_version_sets_default_commit_message(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), persona="current persona")
    target = SimpleNamespace(version_number=8, content="restored persona")
    created = _version_row(9, content="restored persona")

    async def _fake_create_agent_persona_version(
        _db,
        *,
        agent_id,
        content,
        author_id,
        commit_message,
    ):
        assert agent_id == agent.id
        assert content == target.content
        assert commit_message == "Rollback from version V8"
        assert author_id is not None
        return created

    monkeypatch.setattr(
        svc, "create_agent_persona_version", _fake_create_agent_persona_version
    )

    result = await svc.rollback_agent_persona_to_version(
        SimpleNamespace(),
        agent=agent,
        target_version=target,
        author_id=uuid.uuid4(),
        commit_message=None,
    )

    assert agent.persona == "restored persona"
    assert result is created


@pytest.mark.asyncio
async def test_resolve_author_names_maps_user_ids():
    db = _FakeDb()
    uid = uuid.uuid4()
    db.next_execute_values = [[(uid, "Alice")]]

    versions = [SimpleNamespace(author_id=uid)]
    names = await svc.resolve_author_names(db, versions)

    assert names == {str(uid): "Alice"}


@pytest.mark.asyncio
async def test_resolve_author_names_returns_empty_without_authors():
    names = await svc.resolve_author_names(_FakeDb(), [SimpleNamespace(author_id=None)])
    assert names == {}


@pytest.mark.asyncio
async def test_list_prompt_versions_returns_rows_and_total():
    db = _FakeDb()
    db.scalar_value = 3
    db.next_execute_values = [[_version_row(3), _version_row(2)]]

    rows, total = await svc.list_prompt_versions(
        db,
        prompt_id=uuid.uuid4(),
        limit=2,
        offset=0,
    )

    assert total == 3
    assert [r.version_number for r in rows] == [3, 2]


@pytest.mark.asyncio
async def test_list_agent_persona_versions_returns_rows_and_total():
    db = _FakeDb()
    db.scalar_value = 2
    db.next_execute_values = [[_version_row(2), _version_row(1)]]

    rows, total = await svc.list_agent_persona_versions(
        db,
        agent_id=uuid.uuid4(),
        limit=2,
        offset=0,
    )

    assert total == 2
    assert [r.version_number for r in rows] == [2, 1]


@pytest.mark.asyncio
async def test_get_prompt_version_found_and_missing():
    db = _FakeDb()
    found = _version_row(1)
    db.next_execute_values = [[found], []]
    prompt_id = uuid.uuid4()

    assert await svc.get_prompt_version(db, prompt_id=prompt_id, version_number=1) is found
    assert await svc.get_prompt_version(db, prompt_id=prompt_id, version_number=9) is None


@pytest.mark.asyncio
async def test_get_agent_persona_version_found_and_missing():
    db = _FakeDb()
    found = _version_row(1)
    db.next_execute_values = [[found], []]
    agent_id = uuid.uuid4()

    assert (
        await svc.get_agent_persona_version(db, agent_id=agent_id, version_number=1)
        is found
    )
    assert await svc.get_agent_persona_version(db, agent_id=agent_id, version_number=7) is None


@pytest.mark.asyncio
async def test_enforce_retention_noop_when_cap_disabled(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr(svc.settings, "prompt_version_retention_count", 0)
    await svc._enforce_retention(db, prompt_id=uuid.uuid4())
    assert db.executed == []


@pytest.mark.asyncio
async def test_enforce_retention_deletes_rows_beyond_cap(monkeypatch):
    db = _FakeDb()
    prune_candidates = [(uuid.uuid4(), 5), (uuid.uuid4(), 4)]
    # The pinning lookup runs FIRST and must return an empty run set so
    # no version_number is excluded; then the candidate select returns
    # ``(id, version_number)`` tuples; then the DELETE.
    db.next_execute_values = [[], prune_candidates, []]

    monkeypatch.setattr(svc.settings, "prompt_version_retention_count", 1)
    await svc._enforce_retention(db, prompt_id=uuid.uuid4())

    # Three queries: pinning lookup + candidate select + DELETE.
    assert len(db.executed) == 3
    pinning_query = str(db.executed[0]).lower()
    # Pinning lookup hits the evaluation_runs table.
    assert "evaluation_runs" in pinning_query
    candidate_query = str(db.executed[1]).lower()
    # Locked versions must be excluded from the prune candidate set.
    assert "is_locked" in candidate_query
    # The active head is the row with the highest version_number; we rely
    # on ``order_by(version_number desc) + offset(cap)`` to keep the top
    # ``cap`` rows (including the head) and prune the rest — explicit
    # head-exclusion via ``max()`` is no longer needed (and was the source
    # of the off-by-one where cap=1 actually kept 2 rows).
    assert "version_number desc" in candidate_query
    assert "offset" in candidate_query
    assert "max(" not in candidate_query


@pytest.mark.asyncio
async def test_enforce_retention_keeps_exactly_cap_versions_per_series(monkeypatch):
    """Regression for the off-by-one fix.

    Before the fix: ``cap=1`` retained the active head **plus** one more
    older unlocked version (the head was excluded from the prune set, so
    ``.offset(cap)`` returned everything beyond position ``cap`` in the
    head-exclusive ordering).

    After the fix: ``.offset(cap)`` operates on the full descending list
    (head at offset 0), so ``cap=1`` keeps exactly 1 unlocked row — the
    head — and prunes all older unlocked rows.
    """
    db = _FakeDb()
    # Caller would observe: 5 unlocked rows in DB ordered desc by version,
    # cap=1 → keep position 0 (head), prune positions 1..4.
    candidates = [
        (uuid.uuid4(), 5),
        (uuid.uuid4(), 4),
        (uuid.uuid4(), 3),
        (uuid.uuid4(), 2),
    ]
    db.next_execute_values = [[], candidates, []]

    monkeypatch.setattr(svc.settings, "prompt_version_retention_count", 1)
    await svc._enforce_retention(db, prompt_id=uuid.uuid4())

    # Three queries: pinning lookup + candidate select + DELETE.
    assert len(db.executed) == 3
    delete_query = str(db.executed[2]).lower()
    assert "delete from prompt_versions" in delete_query


@pytest.mark.asyncio
async def test_enforce_retention_excludes_versions_pinned_by_pending_evaluation_runs(
    monkeypatch,
):
    """Regression: retention must not prune snapshots
    that a pending/running ``EvaluationRun`` still depends on.

    Scenario: cap=1, three unlocked prompt versions (v5, v4, v3). A
    queued evaluation run references prompt version v4 via its
    ``prompt_versions`` JSON map.  Retention should keep v5 (the head,
    by `offset(cap)`) AND v4 (pinned by the run) and prune ONLY v3.
    Without the pinning guard, v4 would be deleted and the queued run
    would hard-fail at execute time with `missing snapshot: prompt
    <uuid> v4`.
    """
    db = _FakeDb()
    prompt_id = uuid.uuid4()

    # First execute: pinning lookup returns one fake non-terminal run
    # that references v4 for this prompt.
    pending_run = SimpleNamespace(
        persona_version_number=None,
        prompt_versions={str(prompt_id): 4},
        agent_config_versions=None,
    )
    # Second execute: candidate (id, version_number) tuples ordered desc.
    # The active head (v5) is at offset 0 and therefore NEVER appears in
    # the candidate list — ``.offset(cap)`` already excluded it.  v4 is
    # the first candidate but is pinned; v3 is the only one we expect to
    # actually delete.
    pinned_id, evictable_id = uuid.uuid4(), uuid.uuid4()
    candidates_beyond_cap = [(pinned_id, 4), (evictable_id, 3)]
    db.next_execute_values = [[pending_run], candidates_beyond_cap, []]

    monkeypatch.setattr(svc.settings, "prompt_version_retention_count", 1)
    await svc._enforce_retention(db, prompt_id=prompt_id)

    # Three queries: pinning lookup + candidate select + DELETE.
    assert len(db.executed) == 3
    delete_query = str(db.executed[2])
    # The DELETE must include ``evictable_id`` (v3) but NOT ``pinned_id``
    # (v4) — confirm by inspecting the bound IN-list values.
    delete_sql = delete_query.lower()
    assert "delete from prompt_versions" in delete_sql
    # SQLAlchemy renders IN-lists with bound parameters, so we inspect
    # the compiled bindparam values via the statement's compiled form.
    # SQLAlchemy renders UUID values as hex strings WITHOUT hyphens
    # (``a57b614d27434729bd5279ad213871b5``), so the assertions
    # normalise both sides by stripping hyphens.
    compiled = db.executed[2].compile(compile_kwargs={"literal_binds": True})
    rendered = str(compiled).lower().replace("-", "")
    assert str(evictable_id).replace("-", "") in rendered
    assert str(pinned_id).replace("-", "") not in rendered, (
        "pinned version (referenced by pending evaluation run) must NOT "
        f"appear in the DELETE statement: {rendered}"
    )
