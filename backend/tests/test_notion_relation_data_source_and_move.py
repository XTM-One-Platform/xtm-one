"""Regression tests for Notion relation data_source_id resolution and move-page parent validation.

Covers two production failure modes (API 2025-09-03):

1. ``create_notion_database`` / ``update_notion_database`` built relation
   columns with the legacy ``{"relation": {"database_id": ...}}`` shape;
   Notion rejects it with ``relation.data_source_id should be defined,
   instead was 'undefined'`` — the target database must be resolved to
   its primary data source.
2. ``move_notion_page`` forwarded the literal parent ``"workspace"`` (and
   arbitrary non-UUID strings) straight to ``POST /v1/pages/{id}/move``,
   which Notion rejects — the tool must fail fast with actionable
   guidance and zero HTTP calls.
"""

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor

PARENT_UUID = "3768fce1-7f2a-8105-bc9f-ee6e193027c9"
PARENT_UUID_DASHLESS = "3768fce17f2a8105bc9fee6e193027c9"


def _response(status_code: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _build_handler(tool_name: str):
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    return executor._handlers[tool_name]


# ── Relation property schema resolution ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_database_resolves_relation_database_id():
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url, **kwargs})
        if method == "get" and url.endswith("/databases/db-target"):
            return _response(200, {"data_sources": [{"id": "ds-target"}]}), None
        if method == "post" and url.endswith("/databases"):
            return _response(200, {"id": "db-new", "url": "https://notion.so/db-new"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("create_notion_database")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            parent_page_id="page-1",
            title="Briefs",
            properties_json=(
                '{"Name": {"title": {}}, '
                '"Related Brief": {"relation": {"database_id": "db-target"}}}'
            ),
        )

    assert "Database created:" in result
    post_call = next(call for call in calls if call["method"] == "post")
    relation = post_call["json"]["initial_data_source"]["properties"]["Related Brief"][
        "relation"
    ]
    assert relation == {"data_source_id": "ds-target"}
    assert "database_id" not in relation


@pytest.mark.asyncio
async def test_create_database_keeps_existing_relation_data_source_id():
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url, **kwargs})
        if method == "post" and url.endswith("/databases"):
            return _response(200, {"id": "db-new", "url": "https://notion.so/db-new"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("create_notion_database")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            parent_page_id="page-1",
            title="Briefs",
            properties_json=(
                '{"Related": {"relation": {"data_source_id": "ds-already"}}}'
            ),
        )

    assert "Database created:" in result
    # No resolution round-trip: only the create POST fired.
    assert [c["method"] for c in calls] == ["post"]
    post_call = calls[0]
    relation = post_call["json"]["initial_data_source"]["properties"]["Related"][
        "relation"
    ]
    assert relation == {"data_source_id": "ds-already"}


@pytest.mark.asyncio
async def test_create_database_rejects_relation_without_target():
    async def fake_safe_request(*_args: Any, **_kwargs: Any):
        raise AssertionError("No HTTP call expected for an invalid relation schema")

    handler = _build_handler("create_notion_database")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            parent_page_id="page-1",
            title="Briefs",
            properties_json='{"Related": {"relation": {}}}',
        )

    assert result.startswith("Error:")
    assert "relation property 'Related'" in result
    assert "database_id" in result


@pytest.mark.asyncio
async def test_create_database_memoizes_relation_resolution():
    """Several relation columns pointing at the same target database must
    cost a single resolution lookup (per-call memoisation)."""
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url, **kwargs})
        if method == "get" and url.endswith("/databases/db-target"):
            return _response(200, {"data_sources": [{"id": "ds-target"}]}), None
        if method == "post" and url.endswith("/databases"):
            return _response(200, {"id": "db-new", "url": "https://notion.so/db-new"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("create_notion_database")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            parent_page_id="page-1",
            title="Briefs",
            properties_json=(
                '{"Name": {"title": {}}, '
                '"Related A": {"relation": {"database_id": "db-target"}}, '
                '"Related B": {"relation": {"database_id": "db-target"}}}'
            ),
        )

    assert "Database created:" in result
    resolution_gets = [
        c
        for c in calls
        if c["method"] == "get" and c["url"].endswith("/databases/db-target")
    ]
    assert len(resolution_gets) == 1
    post_call = next(call for call in calls if call["method"] == "post")
    props = post_call["json"]["initial_data_source"]["properties"]
    assert props["Related A"]["relation"] == {"data_source_id": "ds-target"}
    assert props["Related B"]["relation"] == {"data_source_id": "ds-target"}


@pytest.mark.asyncio
async def test_create_database_strips_whitespace_in_relation_database_id():
    """Whitespace around the incoming database_id must be stripped once and
    the cleaned value used for both the memoization cache key and the
    resolution call (no whitespace-polluted lookups or data_source_ids)."""
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url, **kwargs})
        if method == "get" and url.endswith("/databases/db-target"):
            return _response(200, {"data_sources": [{"id": "ds-target"}]}), None
        if method == "post" and url.endswith("/databases"):
            return _response(200, {"id": "db-new", "url": "https://notion.so/db-new"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("create_notion_database")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            parent_page_id="page-1",
            title="Briefs",
            properties_json='{"Related": {"relation": {"database_id": "  db-target  "}}}',
        )

    assert "Database created:" in result
    # The resolution GET used the stripped ID (the fake would have raised
    # on a whitespace-polluted URL).
    post_call = next(call for call in calls if call["method"] == "post")
    relation = post_call["json"]["initial_data_source"]["properties"]["Related"][
        "relation"
    ]
    assert relation == {"data_source_id": "ds-target"}


@pytest.mark.asyncio
async def test_update_database_resolves_relation_database_id():
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url, **kwargs})
        if method == "get" and url.endswith("/databases/db-rel"):
            return _response(200, {"data_sources": [{"id": "ds-rel"}]}), None
        if method == "get" and url.endswith("/databases/db-main"):
            return _response(200, {"data_sources": [{"id": "ds-main"}]}), None
        if method == "patch" and url.endswith("/data_sources/ds-main"):
            return _response(200, {"id": "ds-main"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("update_notion_database")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-main",
            properties_json='{"Related": {"relation": {"database_id": "db-rel"}}}',
        )

    assert "updated" in result
    patch_call = next(call for call in calls if call["method"] == "patch")
    relation = patch_call["json"]["properties"]["Related"]["relation"]
    assert relation == {"data_source_id": "ds-rel"}


# ── move_notion_page parent validation ───────────────────────────────────


@pytest.mark.asyncio
async def test_move_page_rejects_workspace_parent_without_http_call():
    async def fake_safe_request(*_args: Any, **_kwargs: Any):
        raise AssertionError("No HTTP call expected for a 'workspace' parent")

    handler = _build_handler("move_notion_page")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id="page-1", new_parent_id="workspace")

    assert result.startswith("Error:")
    assert "workspace root" in result
    assert "UUID" in result


@pytest.mark.asyncio
async def test_move_page_rejects_non_uuid_parent_without_http_call():
    async def fake_safe_request(*_args: Any, **_kwargs: Any):
        raise AssertionError("No HTTP call expected for a non-UUID parent")

    handler = _build_handler("move_notion_page")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id="page-1", new_parent_id="My Cool Page")

    assert result.startswith("Error:")
    assert "not a valid Notion" in result


@pytest.mark.asyncio
async def test_move_page_to_page_parent_sends_page_id():
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url, **kwargs})
        # The parent probes pass expected_statuses={404}, which opts into
        # receiving the failed response object alongside the error string
        # (mirrors the real safe_request contract).
        if method == "get" and f"/databases/{PARENT_UUID}" in url:
            return (
                _response(404, {"object": "error", "status": 404}),
                "Error: Notion resource not found while trying to detect move parent type.",
            )
        if method == "get" and f"/data_sources/{PARENT_UUID}" in url:
            return (
                _response(404, {"object": "error", "status": 404}),
                "Error: Notion resource not found while trying to check data source.",
            )
        # Both probes missed: the page parent is read directly.
        if method == "get" and url.endswith(f"/pages/{PARENT_UUID}"):
            return _response(200, {"object": "page", "id": PARENT_UUID}), None
        if method == "post" and url.endswith("/pages/page-1/move"):
            return _response(200, {"id": "page-1", "url": "https://notion.so/page-1"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("move_notion_page")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id="page-1", new_parent_id=PARENT_UUID)

    assert "Page moved:" in result
    # Both probes must opt into expected_statuses so benign 404 misses do
    # not spam the warning log on every move.
    probes = [
        c
        for c in calls
        if c["method"] == "get" and ("/databases/" in c["url"] or "/data_sources/" in c["url"])
    ]
    assert len(probes) == 2
    for probe in probes:
        assert probe.get("expected_statuses") == {400, 404}
    move_call = next(call for call in calls if call["method"] == "post")
    assert move_call["json"]["parent"] == {"type": "page_id", "page_id": PARENT_UUID}


_PAGE_NOT_DATABASE = (
    f"Provided database_id {PARENT_UUID} is a page, not a database. Use the "
    "pages API instead, or pass the ID of the database itself."
)


@pytest.mark.asyncio
async def test_move_page_to_page_parent_when_database_probe_says_is_a_page() -> None:
    """Notion answers the database probe of a regular page with a 400 "is a
    page, not a database": a detection miss proving the page exists, so the
    move goes out with a page parent and no further probe or read."""
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url, **kwargs})
        if method == "get" and f"/databases/{PARENT_UUID}" in url:
            return (
                _response(
                    400,
                    {"object": "error", "status": 400, "message": _PAGE_NOT_DATABASE},
                ),
                "Error: Notion rejected the request to detect move parent type - bad request.",
            )
        if method == "post" and url.endswith("/pages/page-1/move"):
            return _response(200, {"id": "page-1", "url": "https://notion.so/page-1"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("move_notion_page")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id="page-1", new_parent_id=PARENT_UUID)

    assert "Page moved:" in result
    assert calls[0].get("expected_statuses") == {400, 404}
    move_call = next(call for call in calls if call["method"] == "post")
    assert move_call["json"]["parent"] == {"type": "page_id", "page_id": PARENT_UUID}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_move_page_to_page_parent_when_data_source_probe_says_is_a_page() -> None:
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url, **kwargs})
        if method == "get" and f"/databases/{PARENT_UUID}" in url:
            return (
                _response(404, {"object": "error", "status": 404}),
                "Error: Notion resource not found while trying to detect move parent type.",
            )
        if method == "get" and f"/data_sources/{PARENT_UUID}" in url:
            return (
                _response(
                    400,
                    {
                        "object": "error",
                        "status": 400,
                        "message": f"Provided data_source_id {PARENT_UUID} is a page.",
                    },
                ),
                "Error: Notion rejected the request to check data source - bad request.",
            )
        if method == "post" and url.endswith("/pages/page-1/move"):
            return _response(200, {"id": "page-1", "url": "https://notion.so/page-1"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("move_notion_page")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id="page-1", new_parent_id=PARENT_UUID)

    assert "Page moved:" in result
    move_call = next(call for call in calls if call["method"] == "post")
    assert move_call["json"]["parent"] == {"type": "page_id", "page_id": PARENT_UUID}


@pytest.mark.asyncio
async def test_move_page_fails_fast_on_other_400_probe_error() -> None:
    """Only the "is a page" 400 is a detection miss; any other 400 (a
    validation error about something else) still fails fast."""
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url})
        if method == "get" and f"/databases/{PARENT_UUID}" in url:
            return (
                _response(
                    400,
                    {"object": "error", "status": 400, "message": "body failed validation"},
                ),
                "Error: Notion rejected the request to detect move parent type - bad request.",
            )
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("move_notion_page")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id="page-1", new_parent_id=PARENT_UUID)

    assert "bad request" in result
    assert all(c["method"] == "get" for c in calls)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_move_page_to_database_parent_resolves_data_source():
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url, **kwargs})
        if method == "get" and f"/databases/{PARENT_UUID_DASHLESS}" in url:
            return _response(200, {"data_sources": [{"id": "ds-9"}]}), None
        if method == "post" and url.endswith("/pages/page-1/move"):
            return _response(200, {"id": "page-1", "url": "https://notion.so/page-1"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("move_notion_page")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id="page-1", new_parent_id=PARENT_UUID_DASHLESS)

    assert "Page moved:" in result
    move_call = next(call for call in calls if call["method"] == "post")
    assert move_call["json"]["parent"] == {
        "type": "data_source_id",
        "data_source_id": "ds-9",
    }


@pytest.mark.asyncio
async def test_move_page_fails_fast_on_non_404_probe_error():
    """A transient/non-404 probe failure (401/403/429/5xx or a network
    error) means the parent type CANNOT be detected — the tool must
    surface the underlying error instead of guessing "page" and sending
    a misleading move body to Notion."""
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url})
        if method == "get" and f"/databases/{PARENT_UUID}" in url:
            return (
                _response(503, {"object": "error", "status": 503}),
                "Error: Notion returned HTTP 503 while trying to detect move parent type.",
            )
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("move_notion_page")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id="page-1", new_parent_id=PARENT_UUID)

    assert "HTTP 503" in result
    # The move POST must never fire when parent detection failed.
    assert all(c["method"] == "get" for c in calls)


@pytest.mark.asyncio
async def test_move_page_enriches_block_id_error():
    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get":
            # Probes miss with a 404 (expected_statuses opt-in returns
            # the failed response object alongside the error string).
            return (
                _response(404, {"object": "error", "status": 404}),
                "Error: Notion resource not found.",
            )
        if method == "post" and url.endswith("/move"):
            return None, (
                "Error: Notion rejected the request to move page - bad request. "
                "Object 3988fce1-7f2a-81dc-b259-ee1ee06c2d6a is not a page or "
                "database and cannot be moved"
            )
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _build_handler("move_notion_page")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            page_id="3988fce1-7f2a-81dc-b259-ee1ee06c2d6a",
            new_parent_id=PARENT_UUID,
        )

    assert "is not a page or database" in result
    assert "Only pages can be moved" in result
    assert "containing page" in result
