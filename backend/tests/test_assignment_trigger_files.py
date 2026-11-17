"""Trigger files an assignment run may read, and an honest HITL resume.

Two halves of the same contract - what the run puts in front of the model
must be what it may read, and what the prompt SAYS about a file must be
true for the transcript being sent:

* ``trigger_payload["file_ids"]`` is caller-supplied (a generic webhook body
  is copied into the payload verbatim). Loaded by bare primary key it let
  anyone who knew or guessed a ``FileAttachment`` UUID inline its bytes into
  a prompt. :func:`load_authorized_trigger_files` admits a row only through
  ``authorize_file_access`` as the run's own identity, or when the row is the
  run lineage's own output; everything else is dropped without failing the
  run.
* On a HITL resume the persisted user message still carries the ``Available
  Files`` section written before the pause but none of the refs it
  described (``serialize_messages`` persists no files). The engine
  strips that section, re-attaches the authorized trigger refs, registers
  the run's own generated files by reference and rewrites the section from
  the transcript it is about to send.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm.base import ChatMessage, FileRef
from app.models.assignment import AssignmentRun
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.services.assignment_engine import (
    load_authorized_trigger_files,
    load_run_generated_files,
)
from app.tools.base import (
    FILE_SECTION_HEADER,
    FileContext,
    inlined_user_message_file_ids,
    strip_file_section,
)


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        if self._value is None:
            return []
        return list(self._value) if isinstance(self._value, list) else [self._value]


class _EngineStubDB:
    """Fake session that answers by the entity a statement selects.

    ``User`` -> the run's user row; ``FileAttachment`` by id -> the matching
    stub row; ``FileAttachment`` by ``assignment_run_id`` -> every stub row
    bound to that run; anything else (the run lookup, the group lookup and
    the readable-deck scan inside ``authorize_file_access``) -> no rows, so
    a file nothing admits falls through to the 403 exactly as in production.
    """

    def __init__(self, user=None, files=(), runs=()):
        self.user = user
        self.files = {str(f.id): f for f in files}
        self.runs = {str(r.id): r for r in runs}

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        params = statement.compile().params
        if entity is User:
            return _Result(self.user)
        if entity is FileAttachment:
            if any(key.startswith("assignment_run_id") for key in params):
                wanted = str(next(iter(params.values())))
                return _Result([f for f in self.files.values() if str(f.assignment_run_id) == wanted])
            wanted = str(next(iter(params.values())))
            return _Result(self.files.get(wanted))
        if entity is AssignmentRun:
            wanted = str(next(iter(params.values())))
            return _Result(self.runs.get(wanted))
        return _Result(None)


def _file(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        user_id=None,
        conversation_id=None,
        assignment_run_id=None,
        template_container_id=None,
        source="chat",
        filename="input.csv",
        content_type="text/csv",
        size=12,
        s3_object_name="chat/x/input.csv",
        created_at=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _run(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        parent_run_id=None,
        is_company_managed=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── load_authorized_trigger_files ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_foreign_file_id_is_dropped_and_an_owned_one_kept():
    """The webhook case: two UUIDs in the payload, one the run's user owns,
    one belonging to a stranger. Only the owned row reaches the prompt; the
    foreign one is dropped silently (the run goes on)."""
    run = _run()
    owned = _file(user_id=run.user_id, filename="mine.csv")
    foreign = _file(user_id=uuid.uuid4(), filename="theirs.csv")
    db = _EngineStubDB(user=SimpleNamespace(id=run.user_id, role="user"), files=[owned, foreign])

    admitted = await load_authorized_trigger_files(db, run, [str(foreign.id), str(owned.id)])
    assert [f.filename for f in admitted] == ["mine.csv"]


@pytest.mark.asyncio
async def test_an_admin_run_user_reads_any_file_through_the_contract():
    """The predicate IS ``authorize_file_access``: its admin shortcut applies."""
    run = _run()
    foreign = _file(user_id=uuid.uuid4())
    db = _EngineStubDB(user=SimpleNamespace(id=run.user_id, role="admin"), files=[foreign])
    admitted = await load_authorized_trigger_files(db, run, [str(foreign.id)])
    assert admitted == [foreign]


@pytest.mark.asyncio
async def test_a_run_with_no_user_only_reads_its_own_lineage_s_files():
    """A company-managed automation carries no user: nothing can vouch for a
    foreign row, so only a file bound to this run or its parent run - the
    lineage's own output - is admitted."""
    parent = _run(user_id=None, is_company_managed=True)
    run = _run(user_id=None, is_company_managed=True, parent_run_id=parent.id)
    own = _file(assignment_run_id=run.id, source="agent_download", filename="own.csv")
    parents = _file(assignment_run_id=parent.id, source="agent_download", filename="parent.csv")
    other_run = _file(assignment_run_id=uuid.uuid4(), source="agent_download", filename="other.csv")
    upload = _file(user_id=uuid.uuid4(), filename="upload.csv")
    db = _EngineStubDB(user=None, files=[own, parents, other_run, upload])

    admitted = await load_authorized_trigger_files(
        db, run, [str(own.id), str(parents.id), str(other_run.id), str(upload.id)]
    )
    assert [f.filename for f in admitted] == ["own.csv", "parent.csv"]


@pytest.mark.asyncio
async def test_a_non_admin_run_as_user_still_reads_the_parent_run_s_output():
    """Company-managed run with a non-admin run-as user: ``require_run_access``
    would refuse the parent run's file (admin-gated), but the file is the
    lineage's own output, so the structural grant admits it."""
    parent = _run(user_id=None, is_company_managed=True)
    run = _run(is_company_managed=True, parent_run_id=parent.id)
    handed_down = _file(assignment_run_id=parent.id, source="agent_download")
    db = _EngineStubDB(
        user=SimpleNamespace(id=run.user_id, role="user"),
        files=[handed_down],
        runs=[parent],
    )
    admitted = await load_authorized_trigger_files(db, run, [str(handed_down.id)])
    assert admitted == [handed_down]


@pytest.mark.asyncio
async def test_malformed_missing_and_duplicate_ids_never_fail_the_run():
    run = _run()
    owned = _file(user_id=run.user_id)
    db = _EngineStubDB(user=SimpleNamespace(id=run.user_id, role="user"), files=[owned])

    admitted = await load_authorized_trigger_files(
        db,
        run,
        ["not-a-uuid", {"nested": "object"}, None, str(uuid.uuid4()), str(owned.id), str(owned.id)],
    )
    assert admitted == [owned]
    # Not a list at all: nothing to admit, nothing raised.
    assert await load_authorized_trigger_files(db, run, "abc") == []
    assert await load_authorized_trigger_files(db, run, None) == []


@pytest.mark.asyncio
async def test_a_deleted_run_user_admits_nothing_foreign():
    """``run.user_id`` set but the row is gone: no identity can vouch, so
    only the lineage grant is left."""
    run = _run()
    own = _file(assignment_run_id=run.id, source="agent_working")
    foreign = _file(user_id=uuid.uuid4())
    db = _EngineStubDB(user=None, files=[own, foreign])
    admitted = await load_authorized_trigger_files(db, run, [str(foreign.id), str(own.id)])
    assert admitted == [own]


# ── load_run_generated_files ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_generated_files_are_this_run_s_rows_minus_the_trigger_files():
    run = _run()
    trigger = _file(assignment_run_id=run.id, filename="trigger.csv")
    produced = _file(assignment_run_id=run.id, source="agent_download", filename="out.md")
    elsewhere = _file(assignment_run_id=uuid.uuid4(), source="agent_download")
    db = _EngineStubDB(files=[trigger, produced, elsewhere])

    rows = await load_run_generated_files(db, run, exclude_ids={str(trigger.id)})
    assert rows == [produced]


@pytest.mark.asyncio
async def test_a_pre_pause_mcp_download_is_restored_on_resume():
    """An MCP download now carries the run id, so the HITL resume
    re-registers it like any other output of the run."""
    run = _run()
    mcp_file = _file(assignment_run_id=run.id, source="mcp", filename="mcp_render_chart_1.png")
    db = _EngineStubDB(files=[mcp_file])

    assert await load_run_generated_files(db, run, exclude_ids=set()) == [mcp_file]


# ── The resume rewrite: the section says what the transcript carries ──────


def test_strip_file_section_removes_the_tail_and_leaves_prose_alone():
    prose = "Summarise the attached report.\nBe brief."
    ctx = FileContext.from_file_refs([FileRef.from_bytes(b"a,b", "report.csv", "text/csv", file_id="f1")])
    with_section = prose + "\n\n" + ctx.to_prompt_section(inlined_file_ids={"f1"})
    assert FILE_SECTION_HEADER in with_section
    assert strip_file_section(with_section) == prose
    assert strip_file_section(prose) == prose
    assert strip_file_section("") == ""


def test_the_resumed_section_is_rebuilt_from_the_transcript_not_from_the_claim():
    """What the engine does on resume, in miniature.

    The persisted user message says the trigger file is inlined; the
    restored message carries no ``files``. Rebuilding from
    ``inlined_user_message_file_ids`` over the transcript being SENT puts the
    file where it really is: by reference when the refs were not re-attached,
    inlined once they are - and the run's own pre-pause output is always by
    reference, since it hangs off an assistant message.
    """
    trigger = FileRef.from_bytes(b"a,b", "trigger.csv", "text/csv", file_id="t1")
    trigger.source = "chat"
    produced = FileRef.from_bytes(b"# out", "out.md", "text/markdown", file_id="p1")
    produced.source = "agent_download"

    original_ctx = FileContext.from_file_refs([trigger])
    persisted_content = "Do the work." + "\n\n" + original_ctx.to_prompt_section(inlined_file_ids={"t1"})
    assert "ALREADY INCLUDED" in persisted_content

    restored = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content=persisted_content),
        ChatMessage(role="assistant", content="Generated [[FILE:p1]]"),
    ]
    resumed_ctx = FileContext.from_file_refs([trigger, produced])
    first_user = restored[1]

    # Without re-attaching the refs, the honest section says "by reference".
    honest_unattached = (
        strip_file_section(first_user.content)
        + "\n\n"
        + (resumed_ctx.to_prompt_section(inlined_file_ids=inlined_user_message_file_ids(restored)))
    )
    assert "ALREADY INCLUDED" not in honest_unattached
    assert 'file_id="t1"' in honest_unattached
    assert 'file_id="p1"' in honest_unattached
    assert honest_unattached.count(FILE_SECTION_HEADER) == 1

    # With the trigger refs re-attached (what the engine does), the trigger
    # file is inlined again and the produced file stays by reference.
    first_user.files = [trigger]
    first_user.content = (
        strip_file_section(first_user.content)
        + "\n\n"
        + (resumed_ctx.to_prompt_section(inlined_file_ids=inlined_user_message_file_ids(restored)))
    )
    assert first_user.content.startswith("Do the work.")
    assert first_user.content.count(FILE_SECTION_HEADER) == 1
    inlined_part, by_ref_part = first_user.content.split("[By reference only", 1)
    assert 'file_id="t1"' in inlined_part
    assert 'file_id="p1"' in by_ref_part
    assert "ALREADY INCLUDED" in first_user.content


def test_the_engine_resume_path_rebuilds_files_and_rewrites_the_section():
    """Tripwire: the HITL resume block in the engine goes through the three
    helpers this contract rests on. A resume that restores the persisted
    section verbatim, or forgets the run's own files, is the bug again."""
    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "assignment_engine.py").read_text(
        encoding="utf-8"
    )
    start = source.index('original_status == "awaiting_human"')
    end = source.index("# Find the pending request_human_input tool call id", start)
    resume_block = source[start:end]
    assert "load_run_generated_files(" in resume_block
    assert "strip_file_section(" in resume_block
    assert "inlined_user_message_file_ids(restored)" in resume_block
    assert "_first_user.files = trigger_file_refs" in resume_block
    # And the trigger files are only ever loaded through the authorized path.
    assert "load_authorized_trigger_files(" in source
    assert 'get("file_ids", [])' not in source
