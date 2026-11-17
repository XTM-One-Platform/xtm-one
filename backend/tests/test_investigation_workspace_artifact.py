"""The report as a workspace artifact.

The report is available to the requesting agent and to any flows linked to
that agent. That reach is not an entity tree but a NAMED SCOPE — so the
artifact is a workspace-scoped shared-state entry, which every agent in that
workspace already knows how to read.

The interesting cases are the two boundaries: an unfiled conversation must
write NOTHING, and a coordination failure must not cost the reader the report.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.investigation_report import (
    ReportSource,
    artifact_key,
    artifact_value,
    validate,
)


def _investigation(**kw):
    return SimpleNamespace(
        id=kw.get("id", uuid.uuid4()),
        user_id=kw.get("user_id", uuid.uuid4()),
        conversation_id=kw.get("conversation_id"),
        subject_value=kw.get("subject", "APT29"),
        subject_type=kw.get("subject_type", "unknown"),
        completed_at=kw.get("completed_at"),
    )


def _draft():
    return validate(
        "APT29 is attributed to the SVR [1].",
        [ReportSource(n=1, source_name="Web research", label="MITRE", quote="q", href="https://m/")],
    )


# ── Where it is filed ───────────────────────────────────────────────────────


def test_the_key_is_namespaced_by_workspace_and_by_run() -> None:
    """Per INVESTIGATION, not per subject.

    Two runs on the same subject a week apart are two findings, and the later
    must not silently replace the earlier in a workspace people work in.
    """
    from app.services.workspaces import workspace_namespace

    ws, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert artifact_key(ws, first).startswith(workspace_namespace(ws))
    assert artifact_key(ws, first) != artifact_key(ws, second)
    # And the same run in two workspaces is two entries, never one shared.
    assert artifact_key(uuid.uuid4(), first) != artifact_key(ws, first)


def test_the_artifact_carries_the_sources_not_only_the_prose() -> None:
    """A report without its sources is prose whose markers point nowhere —
    the same failure the citation work exists to prevent, one hop later."""
    value = artifact_value(_investigation(), _draft())
    assert value["kind"] == "investigation_report"
    assert value["report"].startswith("APT29 is attributed")
    assert [s["label"] for s in value["sources"]] == ["MITRE"]
    assert value["claims"] == 1
    # An agent deciding how much weight to give this needs the count too.
    assert value["uncited_claims"] == 0


def test_the_artifact_names_the_run_it_came_from() -> None:
    investigation = _investigation()
    value = artifact_value(investigation, _draft())
    assert value["investigation_id"] == str(investigation.id)
    assert value["subject"] == "APT29"
    assert value["completed_at"] is None


# ── When it is filed ────────────────────────────────────────────────────────


class _DB:
    def __init__(self, conversation=None):
        self._conversation = conversation

    async def get(self, _model, _pk):
        return self._conversation


@pytest.mark.asyncio
async def test_an_unfiled_conversation_writes_nothing(monkeypatch) -> None:
    """There is no default workspace binding anywhere.

    Inventing one here to have somewhere to put the artifact would create a
    workspace nobody asked for, on the first investigation anyone runs.
    """
    from app.services import deep_investigation_chat as dic

    called = {"n": 0}

    async def _upsert(**_kw):  # pragma: no cover - must not run
        called["n"] += 1

    monkeypatch.setattr("app.services.shared_state.upsert_state", _upsert, raising=True)

    conversation = SimpleNamespace(workspace_id=None)
    filed = await dic._file_report_in_workspace(
        _DB(conversation), _investigation(conversation_id=uuid.uuid4()), _draft()
    )
    assert filed is False
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_a_run_with_no_conversation_writes_nothing(monkeypatch) -> None:
    """An assignment run or an A2A task has no conversation, so no workspace.

    The report still lives on the investigation, which is what a flow reads.
    """
    from app.services import deep_investigation_chat as dic

    async def _upsert(**_kw):  # pragma: no cover - must not run
        raise AssertionError("filed without a conversation")

    monkeypatch.setattr("app.services.shared_state.upsert_state", _upsert, raising=True)
    assert await dic._file_report_in_workspace(_DB(), _investigation(), _draft()) is False


@pytest.mark.asyncio
async def test_a_filed_conversation_writes_the_artifact(monkeypatch) -> None:
    from app.services import deep_investigation_chat as dic

    seen: dict = {}

    async def _upsert(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr("app.services.shared_state.upsert_state", _upsert, raising=True)

    workspace_id = uuid.uuid4()
    investigation = _investigation(conversation_id=uuid.uuid4())
    filed = await dic._file_report_in_workspace(
        _DB(SimpleNamespace(workspace_id=workspace_id)), investigation, _draft()
    )

    assert filed is True
    assert seen["workspace_id"] == workspace_id
    assert seen["key"] == artifact_key(workspace_id, investigation.id)
    assert seen["value"]["kind"] == "investigation_report"
    assert "APT29" in seen["label"]
    # Replaced, not merged: half an old report inside a new one is a document
    # nobody wrote.
    assert seen["replace_value"] is True


@pytest.mark.asyncio
async def test_a_coordination_failure_does_not_cost_the_reader_the_report(
    monkeypatch,
) -> None:
    """The report is already on the run and already in the reply. Losing
    either because a shared-state write failed would be a bad trade."""
    from app.services import deep_investigation_chat as dic

    async def _boom(**_kw):
        raise RuntimeError("shared state unavailable")

    monkeypatch.setattr("app.services.shared_state.upsert_state", _boom, raising=True)

    filed = await dic._file_report_in_workspace(
        _DB(SimpleNamespace(workspace_id=uuid.uuid4())),
        _investigation(conversation_id=uuid.uuid4()),
        _draft(),
    )
    assert filed is False


@pytest.mark.asyncio
async def test_filing_happens_after_the_report_is_on_the_run(monkeypatch) -> None:
    """Order matters: filing is additive, and a coordination write must never
    decide whether the reader gets the report.

    Asserted by OBSERVING the order, not by reading the source. The previous
    version matched the literal `investigation.report = draft.text`, so it
    broke the moment that line became `set_committed_value(...)` — while the
    property it names was still perfectly true. A test that fails on a
    refactor it does not care about is a test nobody trusts.
    """
    from app.models.investigation import Investigation
    from app.services import deep_investigation_chat as dic
    from app.services import investigation_report as report_mod

    run = Investigation(
        subject_type="ip",
        subject_value="8.8.8.8",
        source_pack_slugs=[],
        status="completed",
        revision=1,
        iterations_used=1,
        max_iterations=10,
    )
    draft = report_mod.ReportDraft(text="The report.", sources=(), claim_sentences=1)

    seen: dict = {}

    async def _generate(*_a, **_k):
        return draft

    async def _persisted(_id, _draft):
        return True

    async def _file(_db, investigation, _draft):
        # What the run looked like at the moment filing was asked to happen.
        seen["report_at_filing"] = investigation.report
        return True

    monkeypatch.setattr(report_mod, "generate_report", _generate, raising=True)
    monkeypatch.setattr(dic, "_persist_report", _persisted, raising=True)
    monkeypatch.setattr(dic, "_file_report_in_workspace", _file, raising=True)

    await dic._write_report(
        _DB(None),
        SimpleNamespace(investigation=run, steps=[]),
        subject="8.8.8.8",
        subject_type="ip",
        user_id=None,
    )

    assert seen["report_at_filing"] == "The report.", (
        "filing ran before the report was on the run"
    )
