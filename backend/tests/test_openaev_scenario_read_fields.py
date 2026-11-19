"""Scenario read and search answers name recurrence, origin, inject count and dates (#252, #487)."""

import pytest

from app.integrations.providers.openaev import _common, build_tools
from app.integrations.providers.openaev._common import _fmt_scenario, count_scenario_injects
from app.tools.base import CallableToolExecutor, ToolCall

BASE = "https://demo.openaev.io"


def test_scheduled_builtin_scenario_names_recurrence_origin_and_counts() -> None:
    text = _fmt_scenario(
        {
            "scenario_id": "s-1",
            "scenario_name": "Ransomware drill",
            "scenario_recurrence": "0 0 9 * * MON",
            "scenario_recurrence_start": "2026-11-01T00:00:00Z",
            "scenario_recurrence_end": "2026-12-31T00:00:00Z",
            "scenario_dependencies": ["STARTERPACK"],
            "scenario_injects": ["i-1", "i-2", "i-3"],
            "scenario_tags": ["t-1"],
            "scenario_created_at": "2026-10-01T10:00:00Z",
            "scenario_updated_at": "2026-11-02T10:00:00Z",
        }
    )
    assert "Recurrence: 0 0 9 * * MON (cron, UTC), from 2026-11-01T00:00:00Z, until 2026-12-31T00:00:00Z" in text
    assert "Origin: built-in (starter pack)" in text
    assert "Injects: 3" in text
    assert "Tags: 1" in text
    assert "Created: 2026-10-01T10:00:00Z" in text
    assert "Updated: 2026-11-02T10:00:00Z" in text


def test_unplanned_user_scenario_says_not_planned_and_user_origin() -> None:
    text = _fmt_scenario(
        {
            "scenario_id": "s-2",
            "scenario_name": "Custom",
            "scenario_recurrence": None,
            "scenario_dependencies": [],
            "scenario_injects": [],
        }
    )
    assert "Recurrence: not planned" in text
    assert "Origin: created or imported by a user" in text
    assert "Injects: 0" in text
    assert "Tags:" not in text


def test_empty_tags_returned_say_none() -> None:
    text = _fmt_scenario({"scenario_id": "s-4", "scenario_name": "Untagged", "scenario_tags": []})
    assert "Tags: none" in text


def test_inject_count_key_renders_count_or_not_returned() -> None:
    counted = _fmt_scenario({"scenario_id": "s-5", "scenario_name": "A", "_inject_count": 4})
    assert "Injects: 4" in counted
    missing = _fmt_scenario({"scenario_id": "s-6", "scenario_name": "B", "_inject_count": None})
    assert "Injects: not returned" in missing


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([{"inject_id": "a"}, {"inject_id": "b"}], 2),
        ({"content": [{}], "totalElements": 7}, 7),
        ({"content": [{}, {}]}, 2),
        ({"unexpected": True}, None),
        (None, None),
    ],
)
def test_count_scenario_injects(payload, expected) -> None:
    assert count_scenario_injects(payload) == expected


class _Resp:
    status_code = 200
    content = b"{}"
    text = "{}"
    headers: dict = {}

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _install(monkeypatch, injects_answer):
    """Patch the OpenAEV transport: the scenario read and its inject list."""
    requested: list[str] = []

    async def transport(client, method, url, provider, action, **kwargs):
        if url == f"{BASE}/api/settings":
            return _Resp({}), None
        if url == f"{BASE}/api/me/tenants":
            return None, "OpenAEV error (HTTP 404): Not Found"
        requested.append(url)
        if url.endswith("/injects"):
            return injects_answer
        return _Resp({"scenario_id": "s9", "scenario_name": "Drill", "scenario_tags": []}), None

    monkeypatch.setattr(_common, "safe_request", transport)
    executor = CallableToolExecutor()
    build_tools(f"{BASE}||test-token", executor)
    return requested, executor


async def _get(executor):
    return await executor.execute_tool(
        ToolCall(id="call-get", name="get_openaev_scenario", arguments={"scenario_id": "s9"})
    )


async def test_get_scenario_counts_injects_from_the_inject_list(monkeypatch) -> None:
    requested, executor = _install(monkeypatch, (_Resp([{"inject_id": "i1"}, {"inject_id": "i2"}]), None))
    result = await _get(executor)
    assert f"{BASE}/api/scenarios/s9/injects" in requested
    assert "Injects: 2" in result.content
    assert "Tags: none" in result.content


async def test_get_scenario_says_count_not_returned_when_the_list_fails(monkeypatch) -> None:
    _, executor = _install(monkeypatch, (None, "OpenAEV error (HTTP 500): boom"))
    result = await _get(executor)
    assert "Injects: not returned" in result.content
    assert "[Scenario] Drill" in result.content


def test_minimal_payload_adds_no_state_line() -> None:
    text = _fmt_scenario({"scenario_id": "s-3", "scenario_name": "Bare"})
    for label in ("Recurrence:", "Origin:", "Injects:", "Tags:", "Created:", "Updated:"):
        assert label not in text
