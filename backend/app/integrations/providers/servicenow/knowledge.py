"""ServiceNow Knowledge Management (the ``kb_knowledge`` table).

Search and read knowledge base articles - useful for an agent answering support
questions from the organisation's documented knowledge.
"""

from __future__ import annotations

import re
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _MAX_BODY_CHARS,
    _SOURCE,
    build_encoded_query,
    disp,
    field_lines,
    get_record,
    list_table,
    record_url,
    truncate,
)

_TABLE = "kb_knowledge"
_LIST_FIELDS = (
    "number,short_description,kb_knowledge_base,kb_category,workflow_state,"
    "sys_view_count,sys_updated_on,sys_id"
)
# Article bodies are HTML; strip tags for a readable plain-text rendering.
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: Any) -> str:
    text = _TAG_RE.sub(" ", disp(html))
    return re.sub(r"\s+", " ", text).strip()


def _format_line(rec: dict[str, Any]) -> str:
    return (
        f"  {disp(rec.get('number'))}: {truncate(disp(rec.get('short_description')), 100)}\n"
        f"    State: {disp(rec.get('workflow_state')) or '?'} | "
        f"Category: {disp(rec.get('kb_category')) or 'n/a'} | "
        f"Views: {disp(rec.get('sys_view_count')) or '0'} | "
        f"sys_id: {disp(rec.get('sys_id'))}"
    )


def _detail_formatter(base: str) -> Any:
    def _format(rec: dict[str, Any]) -> str:
        lines = [f"{disp(rec.get('number'))}: {disp(rec.get('short_description'))}"]
        lines += field_lines(
            rec,
            [
                ("Knowledge base", "kb_knowledge_base"),
                ("Category", "kb_category"),
                ("State", "workflow_state"),
                ("Author", "author"),
                ("Views", "sys_view_count"),
                ("Updated", "sys_updated_on"),
            ],
        )
        body = _strip_html(rec.get("text"))
        if body:
            lines.append(f"\n  Article:\n  {truncate(body, _MAX_BODY_CHARS)}")
        sys_id = disp(rec.get("sys_id"))
        link = record_url(base, _TABLE, sys_id)
        if link:
            lines.append(f"\n  Link: {link}")
        lines.append(f"  sys_id: {sys_id}")
        return "\n".join(lines)

    return _format


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    detail = _detail_formatter(base)

    async def search_servicenow_knowledge(
        text: str = "",
        published_only: bool = True,
        query: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """Search knowledge base articles by text (title + body).

        ``published_only`` (default true) restricts to published articles.
        ``query`` is a raw encoded query for advanced filters (e.g. by
        kb_knowledge_base or kb_category sys_id).
        """
        clauses = []
        if text and text.strip():
            t = text.strip()
            clauses.append(f"short_descriptionLIKE{t}^ORtextLIKE{t}")
        if published_only:
            clauses.append("workflow_state=published")
        base_query = "^".join(clauses)
        encoded = build_encoded_query(
            {}, raw_query=query, order_by="sys_view_count"
        )
        full = "^".join(c for c in (base_query, encoded) if c)
        return await list_table(
            base,
            headers,
            _TABLE,
            "search knowledge",
            _format_line,
            noun="article",
            query=full,
            limit=limit,
            offset=offset,
            fields=_LIST_FIELDS,
        )

    async def get_servicenow_knowledge_article(article: str) -> str:
        """Get a knowledge article's full text by number (KB...) or sys_id."""
        return await get_record(
            base, headers, _TABLE, article, f"get article {article}", detail
        )

    executor.register("search_servicenow_knowledge", search_servicenow_knowledge)
    executor.register(
        "get_servicenow_knowledge_article", get_servicenow_knowledge_article
    )

    return [
        ToolDefinition(
            name="search_servicenow_knowledge",
            description=(
                "Search ServiceNow knowledge base articles by text (title + body), "
                "ranked by view count. published_only (default true) restricts to "
                "published articles. Use to answer questions from documented "
                "knowledge."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Search text"},
                    "published_only": {"type": "boolean", "default": True},
                    "query": {"type": "string", "description": "Raw encoded query"},
                    "limit": {"type": "integer", "default": 20},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_servicenow_knowledge_article",
            description=(
                "Get a knowledge article's full text (HTML stripped to plain text) "
                "by number (KB...) or sys_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "article": {"type": "string", "description": "KB number or sys_id"}
                },
                "required": ["article"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
