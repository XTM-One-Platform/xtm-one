"""A workflow_run reference on a workflow_call-only workflow is a read failure, told on the item (#2211).

Item fd444037 waited from 21 Nov 22:00Z to 22 Nov 20:12Z on
OpenCRQ-Platform/opencrq ci-docker-build.yml, a reusable workflow
(``on: workflow_call``) that never has a run of its own. The reader now reads
the file of a workflow with no run at all and raises a LookupError naming the
cause, which the write-time check notes on the item at once (#386).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.integrations.providers.github import reference_state as rs

CALL_ONLY = "name: Docker build\non:\n  workflow_call:\n    inputs:\n      tag: {type: string}\njobs: {}\n"
CALLED_AND_PUSHED = "on: [push, workflow_call]\njobs: {}\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (CALL_ONLY, True),
        ("on: workflow_call\njobs: {}\n", True),
        ("on: [workflow_call]\n", True),
        (CALLED_AND_PUSHED, False),
        ("on:\n  push:\n    branches: [main]\n  workflow_call: {}\n", False),
        ("on: push\n", False),
        ("not: [valid", False),
        ("- a list\n", False),
    ],
)
def test_workflow_call_only_reads_every_form_of_on(text: str, expected: bool) -> None:
    assert rs.workflow_call_only(text) is expected


class _Resp:
    status_code = 200

    def __init__(self, data: Any = None, text: str = "") -> None:
        self._data = data
        self.text = text

    def json(self) -> Any:
        return self._data


def _fake(monkeypatch: pytest.MonkeyPatch, *, runs: list, file_text: str | None) -> list[str]:
    asked: list[str] = []

    async def fake_request(_client: object, _method: str, url: str, *_a: object, **_k: object):
        asked.append(url)
        if url.endswith("/runs"):
            return _Resp({"total_count": len(runs), "workflow_runs": runs}), None
        if "/contents/.github/workflows/" in url:
            return (None, "unreadable") if file_text is None else (_Resp(text=file_text), None)
        return None, "unexpected"

    @contextlib.asynccontextmanager
    async def fake_client(**_k: object) -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr("app.integrations.providers.github._common.safe_request", fake_request)
    monkeypatch.setattr(rs, "egress_client", fake_client)
    return asked


@pytest.mark.asyncio
async def test_a_call_only_workflow_with_no_run_is_a_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, runs=[], file_text=CALL_ONLY)
    with pytest.raises(LookupError, match="workflow_call"):
        await rs.read_successful_workflow_run(
            "opencrq-platform/opencrq", "ci-docker-build.yml", "2026-11-21T22:00:00Z", None, None, "t"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("file_text", [CALLED_AND_PUSHED, None])
async def test_a_workflow_that_runs_on_its_own_or_unreadable_waits(monkeypatch: pytest.MonkeyPatch, file_text) -> None:
    _fake(monkeypatch, runs=[], file_text=file_text)
    found = await rs.read_successful_workflow_run("o/n", "release.yml", "2026-11-21T22:00:00Z", "main", None, "t")
    assert found is None


@pytest.mark.asyncio
async def test_a_workflow_with_runs_is_never_read_for_its_triggers(monkeypatch: pytest.MonkeyPatch) -> None:
    run = {"conclusion": "failure", "created_at": "2026-11-22T00:00:00Z"}
    asked = _fake(monkeypatch, runs=[run], file_text=CALL_ONLY)
    assert await rs.read_successful_workflow_run("o/n", "ci.yml", "2026-11-21T22:00:00Z", None, None, "t") is None
    assert not any("/contents/" in u for u in asked)
