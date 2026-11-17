"""Tests for the AttackIQ integration provider.

Covers tool introspection, the DRF-paginated list flow (params + envelope +
pagination hint), the run-assessment write path, the consolidated
manage_attackiq_assessment / manage_attackiq_tag action enums (valid + invalid
actions, required-arg guards), and the compound-token base URL / Token auth
header. Every API call routes through the shared ``safe_request`` helper, which
is monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import attackiq as aiq
from app.integrations.providers.attackiq import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://acme.attackiq.com||api-token-123"


class _StubResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x" if payload not in (None, "") else b""

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        payload = self._router(method, url)
        return _StubResponse(payload), None


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(aiq, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "list_attackiq_assessments" in names
    assert "run_attackiq_assessment" in names
    assert "manage_attackiq_assessment" in names
    assert "list_attackiq_tests" in names
    assert "list_attackiq_scenarios" in names
    assert "list_attackiq_results" in names
    assert "list_attackiq_assets" in names
    assert "manage_attackiq_tag" in names
    assert "list_attackiq_detection_rules" in names
    assert "list_attackiq_connectors" in names
    # Exact count so an accidentally dropped tool fails loudly.
    assert len(tools) == 22


def test_all_tools_use_attackiq_source_and_sorted_priority():
    tools = build_tools(
        token="https://dummy.example.com||__dummy__", executor=CallableToolExecutor()
    )
    assert all(t.source == "integration:attackiq" for t in tools)
    # Reads (0) exist, writes (1/2) exist; run is a write, delete-capable
    # manage tools are destructive (2).
    priorities = {t.name: t.priority for t in tools}
    assert priorities["list_attackiq_assessments"] == 0
    assert priorities["run_attackiq_assessment"] == 1
    assert priorities["manage_attackiq_assessment"] == 2


# ---------------------------------------------------------------------------
# List flow: auth header, params, DRF envelope, pagination hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_assessments_params_envelope_and_pagination(monkeypatch):
    def router(method, url):
        return {
            "count": 42,
            "next": "https://acme.attackiq.com/v1/assessments?page=2",
            "results": [
                {
                    "id": "a1",
                    "name": "Ransomware Emulation",
                    "project_state": "Active",
                    "master_job_count": 3,
                    "modified": "2024-05-01T12:00:00Z",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_attackiq_assessments",
            arguments={"search": "ransomware", "page": 1},
        )
    )
    assert not result.is_error
    assert "Ransomware Emulation" in result.content
    assert "Found 42 assessments" in result.content
    # Pagination hint points to the next page.
    assert 'page="2"' in result.content
    call = recorder.calls[0]
    assert call["method"] == "get"
    assert "/v1/assessments?" in call["url"]
    assert "search=ransomware" in call["url"]
    # Token (not Bearer) auth header.
    assert call["headers"]["Authorization"] == "Token api-token-123"


@pytest.mark.asyncio
async def test_list_assets_scopes_to_assessment_via_tests(monkeypatch):
    """Assessment scoping walks /v1/tests?project= and aggregates the inline
    per-test assets (``/v2/assets`` has no documented assessment filter),
    deduplicating assets shared by several tests."""

    def router(method, url):
        assert "/v1/tests?" in url
        shared = {"id": "as1", "hostname": "WIN-01", "status": "Active"}
        return {
            "count": 2,
            "results": [
                {"id": "t1", "assets": [shared]},
                {"id": "t2", "assets": [shared, {"id": "as2", "hostname": "LNX-02"}]},
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="list_attackiq_assets",
            arguments={"assessment_id": "a1"},
        )
    )
    assert not result.is_error
    assert "WIN-01" in result.content
    assert "LNX-02" in result.content
    assert "Found 2 assets" in result.content
    call = recorder.calls[0]
    assert "/v1/tests?" in call["url"]
    assert "project=a1" in call["url"]


@pytest.mark.asyncio
async def test_list_assets_unscoped_uses_v2_assets(monkeypatch):
    def router(method, url):
        return {"count": 1, "results": [{"id": "as1", "hostname": "WIN-01", "status": "Active"}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c2b", name="list_attackiq_assets", arguments={})
    )
    assert not result.is_error
    assert "WIN-01" in result.content
    call = recorder.calls[0]
    assert "/v2/assets?" in call["url"]
    assert "project" not in call["url"]


# ---------------------------------------------------------------------------
# Assessment execution status: boolean message parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"message": True}, "currently running"),
        ({"message": False}, "currently not running"),
        # A non-boolean string must be surfaced verbatim, never coerced via
        # truthiness into "running".
        ({"message": "not running"}, "status: not running"),
        ({}, "unknown"),
    ],
)
async def test_assessment_status_parses_message_safely(
    monkeypatch, payload, expected
):
    executor, recorder = _build(monkeypatch, lambda m, u: payload)
    result = await executor.execute_tool(
        ToolCall(
            id="cs1",
            name="get_attackiq_assessment_status",
            arguments={"assessment_id": "a1"},
        )
    )
    assert not result.is_error
    assert expected in result.content
    assert "/v1/assessments/a1/is_on_demand_running" in recorder.calls[0]["url"]


# ---------------------------------------------------------------------------
# Results: assessment/run scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_results_scopes_to_assessment_and_run(monkeypatch):
    def router(method, url):
        return {"count": 1, "results": [{"id": "r1", "scenario_name": "S1"}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="cr1",
            name="list_attackiq_results",
            arguments={"assessment_id": "a1", "run_id": "run-9"},
        )
    )
    assert not result.is_error
    url = recorder.calls[0]["url"]
    assert "project_id=a1" in url
    assert "run_id=run-9" in url
    assert "assessment_results=true" in url
    # Unscoped listing sends none of the scoping params.
    await executor.execute_tool(
        ToolCall(id="cr2", name="list_attackiq_results", arguments={})
    )
    unscoped_url = recorder.calls[-1]["url"]
    assert "project_id" not in unscoped_url
    assert "assessment_results" not in unscoped_url


# ---------------------------------------------------------------------------
# Run assessment (write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_assessment_returns_run_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"run_id": "run-9"})
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="run_attackiq_assessment",
            arguments={"assessment_id": "a1"},
        )
    )
    assert not result.is_error
    assert "run-9" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert "/v1/assessments/a1/run_all" in call["url"]
    assert call["json"] == {}


# ---------------------------------------------------------------------------
# manage_attackiq_assessment action enum
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_assessment_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="manage_attackiq_assessment",
            arguments={"action": "nuke"},
        )
    )
    assert "invalid action" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_assessment_create_from_template_requires_fields(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"project_id": "new-1"})
    # Missing template_id -> guarded, no HTTP call.
    guarded = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="manage_attackiq_assessment",
            arguments={"action": "create_from_template", "name": "Test"},
        )
    )
    assert "required" in guarded.content.lower()
    assert recorder.calls == []
    # With both fields it posts to project_from_template.
    ok = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="manage_attackiq_assessment",
            arguments={
                "action": "create_from_template",
                "name": "Test",
                "template_id": "tmpl-1",
            },
        )
    )
    assert not ok.is_error
    assert "new-1" in ok.content
    call = recorder.calls[-1]
    assert "/v1/assessments/project_from_template" in call["url"]
    assert call["json"] == {"project_name": "Test", "template": "tmpl-1"}


@pytest.mark.asyncio
async def test_manage_assessment_delete(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="manage_attackiq_assessment",
            arguments={"action": "delete", "assessment_id": "a1"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "delete"
    assert "/v1/assessments/a1" in call["url"]


@pytest.mark.asyncio
async def test_manage_assessment_update_defaults_splits_csv(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"message": "ok"})
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="manage_attackiq_assessment",
            arguments={
                "action": "update_defaults",
                "assessment_id": "a1",
                "assets": "x1, x2",
                "tags": "t1",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "/v1/assessments/a1/update_defaults" in call["url"]
    assert call["json"]["assets"] == ["x1", "x2"]
    assert call["json"]["tags"] == ["t1"]


# ---------------------------------------------------------------------------
# manage_attackiq_tag action enum
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_tag_apply_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": "ti-1"})
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="manage_attackiq_tag",
            arguments={
                "action": "apply",
                "tag_id": "tag-1",
                "content_type": "asset",
                "object_id": "as1",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert "/v1/tagged_items" in call["url"]
    assert call["json"] == {
        "tag": "tag-1",
        "content_type": "asset",
        "object_id": "as1",
    }


@pytest.mark.asyncio
async def test_manage_tag_remove_uses_tagged_item_id(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c9b",
            name="manage_attackiq_tag",
            arguments={"action": "remove", "tagged_item_id": "ti-1"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "delete"
    assert "/v1/tagged_items/ti-1" in call["url"]
    # Legacy calls that passed the tagged-item ID via tag_id still work.
    await executor.execute_tool(
        ToolCall(
            id="c9c",
            name="manage_attackiq_tag",
            arguments={"action": "remove", "tag_id": "ti-2"},
        )
    )
    assert "/v1/tagged_items/ti-2" in recorder.calls[-1]["url"]
    # Neither provided -> guarded, no HTTP call.
    guarded = await executor.execute_tool(
        ToolCall(id="c9d", name="manage_attackiq_tag", arguments={"action": "remove"})
    )
    assert "tagged_item_id" in guarded.content
    assert len(recorder.calls) == 2


@pytest.mark.asyncio
async def test_manage_tag_create_requires_tag_set(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="manage_attackiq_tag",
            arguments={"action": "create", "name": "prod"},
        )
    )
    assert "required" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Missing base URL guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_base_url_is_friendly_error(monkeypatch):
    recorder = _Recorder(lambda m, u: {})
    monkeypatch.setattr(aiq, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token="||only-token", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="c11", name="list_attackiq_assessments", arguments={})
    )
    assert "base url is not configured" in result.content.lower()
    assert recorder.calls == []
