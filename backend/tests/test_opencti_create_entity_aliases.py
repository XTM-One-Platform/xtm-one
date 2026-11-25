"""create_opencti_entity sends the alias field each OpenCTI type takes.

Identities (Individual, Organization, Sector) and Course-Of-Action keep their
aliases in ``x_opencti_aliases``: their AddInput has no ``aliases`` and the
creation failed when one was passed. Threat types keep ``aliases``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.integrations.providers.opencti.knowledge as knowledge_mod
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"


class _Recorder:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    async def __call__(
        self,
        _client: Any,
        _base_url: str,
        _headers: Any,
        query: str,
        variables: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> tuple[dict[str, Any], None]:
        self.inputs.append((variables or {}).get("input") or {})
        return {}, None


def _build(monkeypatch: pytest.MonkeyPatch) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder()

    @contextlib.asynccontextmanager
    async def _client(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(knowledge_mod, "_client", _client)
    monkeypatch.setattr(knowledge_mod, "_graphql", recorder)
    executor = CallableToolExecutor()
    # direct_mutations skips the draft gate: the mutation document is what is pinned.
    build_tools(TOKEN, executor, direct_mutations=True)
    return executor, recorder


@pytest.mark.parametrize(
    ("entity_type", "field"),
    [
        ("Organization", "x_opencti_aliases"),
        ("Individual", "x_opencti_aliases"),
        ("Sector", "x_opencti_aliases"),
        ("Course-Of-Action", "x_opencti_aliases"),
        ("Intrusion-Set", "aliases"),
        ("Malware", "aliases"),
    ],
)
async def test_aliases_go_to_the_field_of_the_type(
    monkeypatch: pytest.MonkeyPatch, entity_type: str, field: str
) -> None:
    executor, rec = _build(monkeypatch)
    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="create_opencti_entity",
            arguments={"type": entity_type, "name": "Acme", "aliases": ["ACME Corp"]},
        )
    )
    assert rec.inputs, result.content
    sent = rec.inputs[-1]
    assert sent[field] == ["ACME Corp"]
    other = "aliases" if field == "x_opencti_aliases" else "x_opencti_aliases"
    assert other not in sent
