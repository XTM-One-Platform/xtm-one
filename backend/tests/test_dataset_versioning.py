"""Dataset versions, the dataset's answer shape and the output schema guard.

Snapshot versioning runs against an isolated PostgreSQL database (skipped when
none is reachable); schema resolution and validation run on in-memory fakes.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.models.dataset import Dataset, DatasetEntry
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
from app.models.user import User
from app.services.evaluation import dataset_versioning
from app.services.evaluation.dataset_conformance import (
    check_expected_answers,
    first_conformance_failure,
    needs_expected_answers,
)
from app.services.evaluation.metric_package.quality_adapter import (
    build_quality_config,
    run_quality,
)
from app.services.evaluation.scorers import structured_output
from app.services.evaluation.scorers.structured_output import validate_output_schema
from tests.scratch_database import scratch_database

# ── Dataset versioning and reproducibility ────────────────────────────────────────────
# A version and frozen entry snapshot is created on mutation and stays immutable;
# deleting a live entry SET-NULLs the snapshot link but keeps the row; a pinned
# version reproduces the original content; a failure mid-snapshot rolls back fully.


_TABLES = [
    User.__table__,
    Dataset.__table__,
    DatasetEntry.__table__,
    DatasetVersion.__table__,
    DatasetVersionEntry.__table__,
]


async def _make_db():
    """An isolated database with the tables this module needs (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("dsver", tables=_TABLES))
    return db.sessions, stack.aclose


async def _seed_dataset(session, *, n=2) -> Dataset:
    ds = Dataset(name="ds", current_version=0)
    session.add(ds)
    await session.flush()
    for i in range(n):
        session.add(
            DatasetEntry(
                dataset_id=ds.id,
                position=i,
                input_prompt=f"q{i}",
                expected_answer=f"a{i}",
                evaluation_prompt=None,
            )
        )
    await session.commit()
    return ds


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bump_version_creates_snapshot_on_mutation():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            ds = await _seed_dataset(s, n=3)
            version = await dataset_versioning.bump_version(s, ds)
            await s.commit()

            assert ds.current_version == 1
            assert version.version_number == 1
            assert version.entry_count == 3

            rows = (
                await s.execute(
                    select(DatasetVersionEntry).where(
                        DatasetVersionEntry.version_id == version.id
                    )
                )
            ).scalars().all()
            assert len(rows) == 3
            assert {r.expected_answer for r in rows} == {"a0", "a1", "a2"}
            assert all(r.entry_id is not None for r in rows)
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_is_immutable_after_edit():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            ds = await _seed_dataset(s, n=2)
            v1 = await dataset_versioning.bump_version(s, ds)
            await s.commit()
            v1_id = v1.id

            # Edit a live entry then snapshot again.
            entry = (
                await s.execute(
                    select(DatasetEntry)
                    .where(DatasetEntry.dataset_id == ds.id)
                    .order_by(DatasetEntry.position)
                )
            ).scalars().first()
            entry.expected_answer = "EDITED"
            v2 = await dataset_versioning.bump_version(s, ds)
            await s.commit()

            assert ds.current_version == 2
            v1_answers = {
                r.expected_answer
                for r in await dataset_versioning.load_version_entries(s, v1_id)
            }
            v2_answers = {
                r.expected_answer
                for r in await dataset_versioning.load_version_entries(s, v2.id)
            }
            assert "EDITED" not in v1_answers
            assert "EDITED" in v2_answers
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_entry_sets_snapshot_link_null_but_keeps_row():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            ds = await _seed_dataset(s, n=2)
            v1 = await dataset_versioning.bump_version(s, ds)
            await s.commit()

            entry = (
                await s.execute(
                    select(DatasetEntry).where(DatasetEntry.dataset_id == ds.id)
                )
            ).scalars().first()
            deleted_answer = entry.expected_answer
            await s.delete(entry)
            await s.commit()

            snap = await dataset_versioning.load_version_entries(s, v1.id)
            assert len(snap) == 2  # snapshot rows survive the delete
            orphan = [r for r in snap if r.entry_id is None]
            assert len(orphan) == 1
            assert orphan[0].expected_answer == deleted_answer  # content preserved
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rerun_against_pinned_version_is_reproducible():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            ds = await _seed_dataset(s, n=2)
            await dataset_versioning.bump_version(s, ds)
            await s.commit()

            for e in (
                await s.execute(
                    select(DatasetEntry).where(DatasetEntry.dataset_id == ds.id)
                )
            ).scalars().all():
                e.expected_answer = e.expected_answer + "-v2"
            await dataset_versioning.bump_version(s, ds)
            await s.commit()

            fetched = await dataset_versioning.get_version(s, ds.id, 1)
            assert fetched is not None
            v1_answers = {
                r.expected_answer
                for r in await dataset_versioning.load_version_entries(s, fetched.id)
            }
            assert v1_answers == {"a0", "a1"}  # original content, unaffected by v2
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_failure_rolls_back_fully():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            ds = await _seed_dataset(s, n=2)
            await s.commit()
            ds_id = ds.id

        # New session; force the second flush (entry snapshot) to fail.
        async with sm() as s:
            ds = await s.get(Dataset, ds_id)
            original_flush = s.flush
            calls = {"n": 0}

            async def failing_flush(*a, **k):
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise RuntimeError("boom mid-snapshot")
                return await original_flush(*a, **k)

            s.flush = failing_flush  # type: ignore[method-assign]
            with pytest.raises(RuntimeError):
                await dataset_versioning.bump_version(s, ds)
            await s.rollback()

        # Fresh session: nothing partial persisted, counter did not advance.
        async with sm() as s:
            ds = await s.get(Dataset, ds_id)
            assert ds.current_version == 0
            versions = (
                await s.execute(
                    select(DatasetVersion).where(DatasetVersion.dataset_id == ds_id)
                )
            ).scalars().all()
            assert versions == []
    finally:
        await cleanup()


# ── The dataset owns the answer shape ─────────────────────────────────────────────────
# A schema written on the dataset is read by every evaluation that uses it; a schema
# frozen with a version is read by every run pinned to that version, whatever the
# live dataset later says.


_SCHEMA = {
    "type": "json_schema",
    "definition": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["verdict"],
    },
}


# --------------------------------------------------------------------------- conformance


def test_a_conforming_schema_is_accepted() -> None:
    """The ordinary case: a well-formed schema whose entries satisfy it."""
    assert validate_output_schema(_SCHEMA) is None
    assert (
        first_conformance_failure(
            _SCHEMA,
            [("e1", '{"verdict": "malicious", "confidence": 0.9}')],
        )
        is None
    )


def test_a_contradicting_entry_is_named_with_its_pointer() -> None:
    """A rejection says *which* entry and *where* inside it (entry id + JSON Pointer).

    "Schema does not match" alone sends an author hunting through every entry.
    """
    failure = first_conformance_failure(
        _SCHEMA,
        [
            ("e1", '{"verdict": "clean"}'),
            ("e2", '{"confidence": 0.4}'),
        ],
    )
    assert failure is not None
    assert failure.entry_id == "e2"
    detail = failure.as_detail()
    assert "e2" in detail
    assert failure.pointer in detail


def test_every_contradicting_entry_is_reported_not_just_the_first() -> None:
    """``check_expected_answers`` reports the full set so a UI can list them.

    ``first_conformance_failure`` is the API's fail-fast wrapper; the seed-from-agent
    preview needs the whole list or an author fixes one entry per round trip.
    """
    failures = check_expected_answers(
        _SCHEMA,
        [
            ("ok", '{"verdict": "clean"}'),
            ("bad1", "{}"),
            ("bad2", "not json at all"),
        ],
    )
    assert [f.entry_id for f in failures] == ["bad1", "bad2"]


def test_a_hostile_pattern_is_rejected_wherever_it_is_nested() -> None:
    """ReDoS screening is recursive, not top-level only.

    A `pattern` appears on a leaf several levels down, never at the root of an
    object schema.
    """
    hostile = {
        "type": "json_schema",
        "definition": {
            "type": "object",
            "properties": {
                "nested": {
                    "type": "object",
                    "properties": {
                        "leaf": {"type": "string", "pattern": "(a+)+$"},
                    },
                }
            },
        },
    }
    error = validate_output_schema(hostile)
    assert error is not None
    assert "pattern" in error.lower()


def test_a_non_json_schema_wrapper_is_left_alone() -> None:
    """Conformance only speaks for ``json_schema``.

    Other wrapper kinds describe a shape this checker cannot evaluate; declaring
    their entries non-conforming would block a save for a contradiction that was
    never detected.
    """
    other = {"type": "regex", "definition": "^ok$"}
    assert first_conformance_failure(other, [("e1", "anything")]) is None


def test_an_empty_expected_answer_is_not_a_contradiction() -> None:
    """An entry with no expected answer has nothing to contradict.

    Datasets are commonly drafted with the answers filled in later; failing the
    schema save on a blank row would force the author to write the schema last.
    """
    assert first_conformance_failure(_SCHEMA, [("e1", ""), ("e2", None)]) is None


# --------------------------------------------------------------------------- schema resolution


@pytest.mark.asyncio
async def test_a_pinned_run_keeps_the_schema_it_was_scored_under() -> None:
    """Editing a dataset's schema does not re-describe a run pinned to a version.

    Without the version-level snapshot nothing raises; the numbers silently mean
    something else than when they were computed.
    """
    from app.services.evaluation_engine import _resolve_dataset_output_schema

    version_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    frozen = {"type": "json_schema", "definition": {"type": "object"}}
    live = {"type": "json_schema", "definition": {"type": "object", "title": "edited"}}

    class _Db:
        async def get(self, model, key):
            if key == version_id:
                return SimpleNamespace(output_schema=frozen)
            return SimpleNamespace(output_schema=live)

    pinned_entry = SimpleNamespace(version_id=version_id, dataset_id=dataset_id)
    live_entry = SimpleNamespace(version_id=None, dataset_id=dataset_id)

    memo: dict = {}
    assert await _resolve_dataset_output_schema(_Db(), pinned_entry, memo) is frozen
    assert await _resolve_dataset_output_schema(_Db(), live_entry, memo) is live


@pytest.mark.asyncio
async def test_the_resolver_reads_each_dataset_tier_once() -> None:
    """Memoisation is per-run, and it is what keeps rescore from N+1-ing.

    A rescore walks every result; resolving the schema per result would issue one
    query per entry against a row that cannot change mid-run.
    """
    from app.services.evaluation_engine import _resolve_dataset_output_schema

    dataset_id = uuid.uuid4()
    calls: list[uuid.UUID] = []

    class _Db:
        async def get(self, model, key):
            calls.append(key)
            return SimpleNamespace(output_schema=_SCHEMA)

    memo: dict = {}
    entry = SimpleNamespace(version_id=None, dataset_id=dataset_id)
    for _ in range(5):
        await _resolve_dataset_output_schema(_Db(), entry, memo)
    assert calls == [dataset_id]


@pytest.mark.asyncio
async def test_an_entry_belonging_to_nothing_resolves_to_no_schema() -> None:
    """A detached entry degrades to "no schema", never to a stray one."""
    from app.services.evaluation_engine import _resolve_dataset_output_schema

    detached = SimpleNamespace(version_id=None, dataset_id=None)
    assert await _resolve_dataset_output_schema(object(), detached, {}) is None


# --------------------------------------------------------------------------- missing schema


def test_structured_json_without_a_schema_is_unavailable_with_a_reason() -> None:
    """No schema is a *stated absence*, not a score.

    `invalid` would blame the agent for the dataset's omission, `0` would report a
    failure never measured, and a substitute measure a different quantity under the
    same name.
    """
    config = build_quality_config(
        "STRUCTURED_JSON", SimpleNamespace(default_output_schema=None), []
    )
    outcomes = run_quality(
        family="STRUCTURED_JSON",
        reference='{"verdict": "clean"}',
        prediction='{"verdict": "clean"}',
        output_schema=None,
        config=config,
    )
    field = [o for o in outcomes if o.key == "json.field_accuracy"]
    assert field, "the metric must still be reported, not omitted"
    outcome = field[0]
    assert outcome.status == "unavailable"
    assert outcome.value is None
    assert outcome.detail, "the absence must state why and where to fix it"


# --------------------------------------------------------------------------- portability


def test_one_dataset_scores_identically_across_two_evaluations() -> None:
    """The schema travels with the dataset, so no per-evaluation setup exists.

    Nothing the *evaluation* owns (judge model, prompt, instructions) reaches field
    scoring; a divergence means the schema leaked back onto the evaluation.
    """
    reference = '{"verdict": "malicious", "confidence": 0.9}'
    prediction = '{"verdict": "malicious", "confidence": 0.4}'

    def score(evaluation: SimpleNamespace) -> float | None:
        config = build_quality_config("STRUCTURED_JSON", evaluation, [])
        outcomes = run_quality(
            family="STRUCTURED_JSON",
            reference=reference,
            prediction=prediction,
            output_schema=_SCHEMA,
            config=config,
        )
        return next(o.value for o in outcomes if o.key == "json.field_accuracy")

    first = score(
        SimpleNamespace(
            default_output_schema=None,
            evaluator_model="judge-a",
            evaluation_instructions="be strict",
        )
    )
    second = score(
        SimpleNamespace(
            default_output_schema=None,
            evaluator_model="judge-b",
            evaluation_instructions="be lenient",
        )
    )
    assert first is not None
    assert first == second


# --------------------------------------------------------------------------- clearing


@pytest.mark.asyncio
async def test_an_empty_object_clears_the_dataset_schema() -> None:
    """`{}` is the clear sentinel, and the UI depends on it.

    On a partial update `None` means "leave this field alone", so it cannot also mean
    "unset it"; sending `None` would keep the schema while the UI reports it removed.

    The clear path returns before touching the database, so a bare sentinel stands in
    for the session.
    """
    from app.api.v1.datasets import _apply_dataset_output_schema

    dataset = SimpleNamespace(id=str(uuid.uuid4()), output_schema={"type": "json_schema"})
    await _apply_dataset_output_schema(object(), dataset, {})
    assert dataset.output_schema is None


# --------------------------------------------------------------------------- pointers


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param("a/b", "/a~1b", id="slash-in-key"),
        pytest.param("c~d", "/c~0d", id="tilde-in-key"),
        pytest.param("", "/", id="empty-key"),
        pytest.param("plain", "/plain", id="ordinary-key"),
    ],
)
def test_a_conformance_pointer_is_rfc_6901_escaped(key: str, expected: str) -> None:
    """A key's `/` and `~` are escaped, and an empty key is not the root pointer.

    Unescaped, `/` reads as two levels of nesting and `~` as an invalid escape; an empty
    key collapsed to `""` would blame the whole document for one field.
    """
    import json as _json

    schema = {
        "type": "json_schema",
        "definition": {"type": "object", "properties": {key: {"type": "string"}}},
    }
    failure = first_conformance_failure(schema, [("e1", _json.dumps({key: 123}))])
    assert failure is not None
    assert failure.pointer == expected


def test_a_whole_document_failure_still_points_at_the_document() -> None:
    """The root stays `""`, matching the sibling "not valid JSON" failure.

    `pointer_from_tokens` renders an empty token list as `/` for readability, which would
    make a root failure indistinguishable from one at a key named `""` in the stored
    `conflicting_entries`. The two are different facts, so the root keeps its own value.
    """
    schema = {"type": "json_schema", "definition": {"type": "object"}}
    failure = first_conformance_failure(schema, [("e1", "[]")])
    assert failure is not None
    assert failure.pointer == ""


@pytest.mark.parametrize(
    ("output_schema", "expected"),
    [
        pytest.param({"type": "json_schema", "definition": {}}, True, id="json-schema"),
        pytest.param({"type": "regex", "definition": "x"}, False, id="regex"),
        pytest.param({"type": "tool_sequence", "definition": []}, False, id="tool-seq"),
        pytest.param(None, False, id="none"),
    ],
)
def test_only_a_json_schema_needs_the_expected_answers(
    output_schema: dict | None, expected: bool
) -> None:
    """Saving a non-JSON schema must not read every entry's answer text.

    `check_expected_answers` returns `[]` for these types. The predicate uses the same
    private helper as the check, so the route and the check agree on which types have
    anything to verify.
    """
    assert needs_expected_answers(output_schema) is expected


# ── The write-time ``output_schema`` guard ────────────────────────────────────────────
# JSON quality is scored per field by the metric package; this only validates the
# schema an author saves. The ReDoS and timeout fences are the point: without them
# saving a dataset would be a CPU-exhaustion primitive.


# --------------------------------------------------------------------------- #
# Acceptance                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "schema",
    [
        None,
        {},
        {"type": "json_schema", "definition": {"type": "object"}},
    ],
)
def test_well_formed_schemas_are_accepted(schema) -> None:
    assert validate_output_schema(schema) is None


# --------------------------------------------------------------------------- #
# Shape rejection                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("schema", "fragment"),
    [
        ({"type": "nope", "definition": {}}, "output_schema.type must be 'json_schema'"),
        ({"type": "json_schema"}, "definition is required"),
        ({"type": "json_schema", "definition": "nope"}, "must be an object"),
        ({"type": "json_schema", "definition": {"type": "nonsense"}}, "invalid JSON Schema"),
        # regex/tool_sequence are retired: even a well-formed one is refused.
        ({"type": "regex", "definition": r"^\d{4}-\d{2}-\d{2}$"}, "must be 'json_schema'"),
        ({"type": "tool_sequence", "definition": ["search", "report"]}, "must be 'json_schema'"),
        (["not", "an", "object"], "output_schema must be an object"),
    ],
    ids=[
        "unknown_type",
        "missing_definition",
        "non_object_definition",
        "invalid_json_schema",
        "retired_regex",
        "retired_tool_sequence",
        "not_a_dict",
    ],
)
def test_malformed_schemas_are_refused_with_a_message(schema, fragment: str) -> None:
    """Never raises, even on a non-dict: the caller turns the string into a 422."""
    error = validate_output_schema(schema)
    assert error is not None
    assert fragment in error


# --------------------------------------------------------------------------- #
# ReDoS fence                                                                  #
# --------------------------------------------------------------------------- #


def test_a_hostile_pattern_nested_in_a_json_schema_is_rejected_by_pointer() -> None:
    """A nested pattern, the form authors write, is screened and named by its pointer."""
    error = validate_output_schema(
        {
            "type": "json_schema",
            "definition": {
                "type": "object",
                "properties": {"id": {"type": "string", "pattern": "(a+)+$"}},
            },
        }
    )
    assert error is not None
    assert "catastrophic-backtracking" in error
    assert "/properties/id/pattern" in error


def test_a_hostile_pattern_inside_an_array_branch_is_still_found() -> None:
    """`anyOf` / `items` are lists, so the walk must descend through sequences too."""
    error = validate_output_schema(
        {
            "type": "json_schema",
            "definition": {"anyOf": [{"type": "string"}, {"pattern": "(a|aa)+"}]},
        }
    )
    assert error is not None
    assert "/anyOf/1/pattern" in error


# --------------------------------------------------------------------------- #
# Timeout fence                                                                #
# --------------------------------------------------------------------------- #


def test_a_slow_inspection_is_refused_rather_than_allowed_to_run(monkeypatch) -> None:
    """Inspection is bounded in wall-clock time: the document's depth is author-controlled."""
    import time

    monkeypatch.setattr(structured_output, "VALIDATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        structured_output,
        "_inspect_json_schema",
        lambda _definition: time.sleep(5) or None,
    )
    error = validate_output_schema({"type": "json_schema", "definition": {"type": "object"}})
    assert error is not None
    assert "exceeded" in error


def test_an_unexpected_error_during_inspection_fails_closed(monkeypatch) -> None:
    def _boom(_definition):
        raise RuntimeError("boom")

    monkeypatch.setattr(structured_output, "_inspect_json_schema", _boom)
    error = validate_output_schema({"type": "json_schema", "definition": {"type": "object"}})
    assert error is not None
    assert "exceeded" in error or "invalid JSON Schema" in error
