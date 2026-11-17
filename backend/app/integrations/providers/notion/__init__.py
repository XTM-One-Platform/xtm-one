"""Notion integration provider package.

Comprehensive tooling for Pages, Databases, Blocks, Comments, Users, and
Search across the Notion API (2025-09-03). Split by domain (mirrors the
``jira`` package):

* ``_common``    - constants, shared logger, the ``NotionContext`` HTTP
                   machinery (throttled requests, page/block traversal,
                   data-source resolution), and the ``_parse_json_arg``
                   coercion + small extractors.
* ``formatting`` - Markdown <-> Notion block / rich-text conversion, property
                   summarisation, and database-schema rendering.
* ``search``     - search_notion, list_notion_children.
* ``pages``      - page read / properties / create / update / move / trash.
* ``databases``  - query, list, schema, templates, create, update.
* ``blocks``     - get / list children / append / update / delete blocks.
* ``files``      - the Direct Upload API tool (upload_notion_file).
* ``comments``   - list / add comments.
* ``users``      - list / get users.

Notion API uses either an internal integration token (api_key) or public
OAuth; both use Bearer token auth with the Notion-Version header.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from . import blocks, comments, databases, files, pages, search, users
from ._common import (
    _NOTION_RICH_TEXT_LIMIT,  # noqa: F401 - re-exported for tests
    _PAGE_NOT_DATABASE_SENTINEL,  # noqa: F401 - re-exported for tests
    _RESOLUTION_FAILED_SENTINEL,  # noqa: F401 - re-exported for tests
    NOTION_API,
    NOTION_VERSION,
    AutoRefreshClient,  # noqa: F401 - re-exported for external importers + tests
    NotionContext,
    _parse_json_arg,  # noqa: F401 - re-exported for tests
    _rich_text_to_plain,  # noqa: F401 - re-exported for kb_sync
    clear_stale_ref_counters,  # re-exported for the integration test endpoint
    logger,  # noqa: F401 - shared logger re-exported for caplog assertions
    safe_request,  # noqa: F401 - re-exported for tests
    with_unnamed_people_note,
)
from .databases import (  # noqa: F401 - re-exported for tests
    _build_filter_mismatch_hint,
    _extract_filter_property_name,
    _extract_filter_type_key,
    _is_filter_properties_invalid_400,
    _is_filter_type_mismatch_400,
    _is_filter_undefined_type_400,
    _validate_filter_missing_type,
)
from .files import _notion_file_block_type  # noqa: F401 - re-exported for tests
from .formatting import (
    _build_nested_list,  # noqa: F401 - re-exported for tests
    _normalize_notion_language,  # noqa: F401 - re-exported for tests
    _normalize_table_row_cells,  # noqa: F401 - re-exported for tests
    _parse_inline_markdown,  # noqa: F401 - re-exported for tests
    _sanitize_rich_text,  # noqa: F401 - re-exported for tests
    _text_to_blocks,  # noqa: F401 - re-exported for tests
    _truncate_properties_rich_text,  # noqa: F401 - re-exported for tests
)

__all__ = ["NOTION_API", "NOTION_VERSION", "build_tools", "clear_stale_ref_counters"]


class _NotingExecutor:
    """Registers every Notion tool so its result says when Notion withheld names."""

    def __init__(self, inner: CallableToolExecutor) -> None:
        self._inner = inner

    def register(self, name: str, handler: Callable[..., Awaitable[Any]]) -> None:
        self._inner.register(name, with_unnamed_people_note(handler))


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    refresh_fn: Callable[[], Awaitable[str | None]] | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    ctx = NotionContext(token, refresh_fn, integration_id=_kwargs.get("integration_id"))
    persist_file_fn = _kwargs.get("persist_file_fn")
    noting = _NotingExecutor(executor)
    tools: list[ToolDefinition] = []
    tools += search.register(ctx, noting)
    tools += pages.register(ctx, noting)
    tools += databases.register(ctx, noting)
    tools += blocks.register(ctx, noting)
    tools += files.register(ctx, noting, persist_file_fn=persist_file_fn)
    tools += comments.register(ctx, noting)
    tools += users.register(ctx, noting)
    return tools
