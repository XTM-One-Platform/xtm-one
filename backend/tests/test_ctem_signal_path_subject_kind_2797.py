"""#2797: a subject kind the trigger path does not take is refused, naming the path it belongs to.

A vulnerability recorded on ``relevant_threat`` opened a manual "Respond to" threat case where a
version-verified upgrade case on ``new_vulnerability`` was expected. The intake now refuses the
pairing (never a silent re-route: the caller keeps its dedupe key), one bulk row alone by index.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services import ctem_signals as signals
from app.services import ctem_webhook_sources as webhook_sources
from tests.test_ctem_signals import ALICE, ONE_PER_PATH, _Db, client, store, tools  # noqa: F401

CVE = "CVE-2026-27970"


def _input(path: str, kind: str, ref: str = CVE) -> signals.SignalInput:
    return signals.SignalInput(
        trigger_path=path, source_provider="manual", subject_kind=kind, subject_ref=ref, lens="inside"
    )


def _row(path: str, provider: str, kind: str, ref: str) -> dict[str, str]:
    return {"trigger_path": path, "source_provider": provider, "subject_kind": kind, "subject_ref": ref}


def test_a_vulnerability_on_relevant_threat_is_refused_and_named_new_vulnerability() -> None:
    with pytest.raises(signals.SignalError) as refused:
        signals.normalize(_input("relevant_threat", "vulnerability"))
    assert "new_vulnerability" in str(refused.value)
    assert "relevant_threat" in str(refused.value)


def test_relevant_threat_with_a_threat_is_unchanged() -> None:
    clean = signals.normalize(_input("relevant_threat", "threat", "APT-X"))
    assert (clean.trigger_path, clean.subject_kind, clean.dedupe_key) == (
        "relevant_threat",
        "threat",
        "relevant_threat:threat:apt-x",
    )


def test_the_inside_new_vulnerability_path_keeps_its_cve_and_dedupe_key() -> None:
    clean = signals.normalize(_input("new_vulnerability", "vulnerability"))
    assert (clean.trigger_path, clean.lens, clean.dedupe_key) == (
        "new_vulnerability",
        "inside",
        f"new_vulnerability:vulnerability:{CVE.lower()}",
    )


@pytest.mark.parametrize(("path", "provider", "kind", "ref"), ONE_PER_PATH)
def test_every_plausible_signal_per_path_still_fits(path: str, provider: str, kind: str, ref: str) -> None:
    assert signals.subject_kind_error(path, kind) is None


def test_a_webhook_source_with_the_pairing_is_refused_at_creation() -> None:
    with pytest.raises(webhook_sources.WebhookSourceError, match="new_vulnerability"):
        webhook_sources.check_new("relevant_threat", "VM feed", "vulnerability")
    assert webhook_sources.check_new("relevant_threat", "Threat feed", "threat") == "Threat feed"


async def test_the_tool_refuses_the_pairing_and_records_nothing(tools: Any) -> None:  # noqa: F811
    _, call = tools(ALICE)
    out = await call(
        "record_ctem_signal",
        trigger_path="relevant_threat",
        source_provider="manual",
        subject_kind="vulnerability",
        subject_ref=CVE,
        lens="inside",
    )
    assert out.startswith("Refused:") and "new_vulnerability" in out and "Nothing was recorded" in out
    assert json.loads(await call("list_ctem_signals"))["total"] == 0


async def test_the_bulk_tool_refuses_that_row_by_index_and_keeps_the_others(
    tools: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import ctem_scope_gate_store

    async def _no_gate(_db: Any, _row: Any, *, created: bool) -> None:
        return None

    monkeypatch.setattr(ctem_scope_gate_store, "evaluate_after_record", _no_gate)
    _, call = tools(ALICE)
    rows = [
        _row("relevant_threat", "opencti", "threat", "APT-X"),
        _row("relevant_threat", "manual", "vulnerability", CVE),
        _row("new_vulnerability", "manual", "vulnerability", CVE),
    ]
    before = _Db.commits
    out = json.loads(await call("record_ctem_signals", signals=rows))
    assert _Db.commits == before + 1
    assert (out["recorded"], out["created"], out["refused"]) == (2, 2, 1)
    assert out["results"][1]["index"] == 1 and "new_vulnerability" in out["results"][1]["error"]
    assert out["results"][0]["created"] is True and out["results"][2]["created"] is True
    listed = json.loads(await call("list_ctem_signals"))
    assert sorted(s["trigger_path"] for s in listed["signals"]) == ["new_vulnerability", "relevant_threat"]


def test_the_tool_schema_names_the_allowed_subject_kind_of_relevant_threat(tools: Any) -> None:  # noqa: F811
    defs, _ = tools(ALICE)
    record = next(d for d in defs if d.name == "record_ctem_signal")
    description = record.input_schema["properties"]["subject_kind"]["description"]
    assert "relevant_threat" in description and "new_vulnerability" in description
