"""query_opencti_pir_relationships answers a total and a next-page cursor (xtm-one #1801).

``pirRelationships`` takes ``after`` and answers ``pageInfo``; the tool read
neither, so an agent saw the first page alone with no way to know more existed.
"""

from typing import Any

import app.integrations.providers.opencti.pir as pir_mod
from app.tools.base import CallableToolExecutor, ToolCall


class _Fake:
    def __init__(self, page: dict[str, Any]) -> None:
        self.page = page
        self.variables: list[dict[str, Any]] = []

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.variables.append(variables or {})
        node = {
            "id": "r1",
            "pir_score": 80,
            "fromId": "e1",
            "fromType": "Malware",
            "toId": "p",
            "created_at": "2026-11-01",
        }
        return {"pirRelationships": {"edges": [{"node": node}], "pageInfo": self.page}}, None


async def _query(monkeypatch, fake, **arguments) -> str:
    monkeypatch.setattr(pir_mod, "_graphql", fake)
    executor = CallableToolExecutor()
    pir_mod.register("https://demo.opencti.io", {}, executor)
    result = await executor.execute_tool(
        ToolCall(id="q", name="query_opencti_pir_relationships", arguments={"pir_id": "pir-1", **arguments})
    )
    return result.content


async def test_total_and_next_cursor_are_named(monkeypatch):
    fake = _Fake({"globalCount": 140, "hasNextPage": True, "endCursor": "CUR2"})
    out = await _query(monkeypatch, fake, limit=1)
    assert "(1 shown of 140)" in out, out
    assert "after='CUR2'" in out


async def test_after_is_sent(monkeypatch):
    fake = _Fake({"globalCount": 140, "hasNextPage": False, "endCursor": "CUR3"})
    out = await _query(monkeypatch, fake, after="CUR2")
    assert fake.variables[-1]["after"] == "CUR2"
    assert "Next page" not in out


async def test_last_page_has_no_cursor_line(monkeypatch):
    fake = _Fake({"globalCount": 1, "hasNextPage": False, "endCursor": None})
    out = await _query(monkeypatch, fake)
    assert "(1 shown of 1)" in out and "Next page" not in out
