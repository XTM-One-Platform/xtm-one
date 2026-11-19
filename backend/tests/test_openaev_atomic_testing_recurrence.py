# SPDX-License-Identifier: Apache-2.0
"""Atomic testing recurrence: a cron OpenAEV never fires is refused (xtm-one #242).

OpenAEV parses the recurrence as a 6-field Spring cron (seconds first) or an
ISO 8601 period, in UTC, and stores any string without validation. The tool
refuses the shapes that would never fire, and its answer names what the call
stored and what it cleared (the three fields are replaced together).
"""

from __future__ import annotations

import pytest

from app.integrations.providers.openaev import build_tools, testing
from app.integrations.providers.openaev.testing import recurrence_answer, recurrence_refusal
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"


class _Api:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, base_url, headers, method, path, action, **kw):
        self.calls.append((method, path, kw.get("json")))
        return None, None


async def _run(arguments: dict) -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(
        ToolCall(id="call-recurrence", name="manage_openaev_atomic_testing", arguments=arguments)
    )
    assert not result.is_error, result.content
    return result.content


@pytest.mark.parametrize("value", ["0 0 2 * * *", "0 */15 * * * MON-FRI", "P1D", "PT6H", "@daily"])
def test_usable_recurrences_pass(value):
    assert recurrence_refusal(value) is None


@pytest.mark.parametrize(
    ("value", "shape"),
    [("0 2 * * *", "5-field Unix"), ("0 0 2 * * ? 2027", "7-field Quartz"), ("daily", "1 field")],
)
def test_never_firing_recurrences_are_refused(value, shape):
    refusal = recurrence_refusal(value)
    assert refusal and shape in refusal and "UTC" in refusal and "Nothing was written" in refusal


async def test_unix_cron_is_refused_before_any_write(monkeypatch):
    api = _Api()
    monkeypatch.setattr(testing, "_api", api)
    content = await _run({"action": "recurrence", "inject_id": "a1", "recurrence": "0 2 * * *"})
    assert content.startswith("Refused")
    assert api.calls == []


async def test_spring_cron_is_stored_with_its_fields(monkeypatch):
    api = _Api()
    monkeypatch.setattr(testing, "_api", api)
    content = await _run(
        {
            "action": "recurrence",
            "inject_id": "a1",
            "recurrence": " 0 0 2 * * * ",
            "recurrence_start": "2026-11-19T00:00:00Z",
            "recurrence_end": "2026-12-31T00:00:00Z",
        }
    )
    assert api.calls == [
        (
            "put",
            "/api/atomic-testings/a1/recurrence",
            {
                "inject_recurrence": "0 0 2 * * *",
                "inject_recurrence_start": "2026-11-19T00:00:00Z",
                "inject_recurrence_end": "2026-12-31T00:00:00Z",
            },
        )
    ]
    assert "'0 0 2 * * *' (UTC)" in content
    assert "cleared" not in content
    assert "NEW inject id" in content


def test_answer_names_the_cleared_fields():
    answer = recurrence_answer("a1", "0 0 2 * * *", "", "")
    assert "cleared: recurrence_start, recurrence_end" in answer


def test_answer_without_recurrence_says_the_schedule_was_cleared():
    answer = recurrence_answer("a1", "", "", "2026-12-31T00:00:00Z")
    assert answer.startswith("Cleared the schedule of atomic test a1")
    assert "cleared: recurrence, recurrence_start" in answer
