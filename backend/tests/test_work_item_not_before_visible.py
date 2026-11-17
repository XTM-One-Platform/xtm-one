"""``not_before`` is visible where an item is read, not only where it is written.

``list_work_items`` (full and compact) and ``get_work_item`` name an item's
``not_before``; an open item whose ``not_before`` is still ahead reads
"deferred until <t>", so a watchdog tells a deferred item from a stuck one.
An item without ``not_before`` shows nothing of it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.services.builtin_tools import build_work_item_tools
from app.services.builtin_tools.work_items import _full_item_text, _not_before_text
from app.tools.base import ToolCall

FUTURE = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
PAST = (datetime.now(UTC) - timedelta(hours=2)).isoformat()


def _item(not_before: str | None = None, status: str = "open") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "title": "Watchdog reads an item",
        "status": status,
        "priority": "medium",
        "owner_agent_id": str(uuid.uuid4()),
        "correlation_key": "dev:watchdog",
        "is_stale": False,
        "description": "brief",
        "not_before": not_before,
    }


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


@pytest.fixture
def _scope(monkeypatch):
    from app.services import coordination_scope

    scope = SimpleNamespace(work_item_clause=lambda: object(), label=lambda: "Scope: fleet.")

    async def _resolve(*_args, **_kwargs):
        return scope

    monkeypatch.setattr(coordination_scope, "resolve_coordination_scope", _resolve)


async def _listed(monkeypatch, rows: list[dict], **arguments) -> str:
    async def _list(**_kwargs):
        return rows

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "list_work_items", **arguments)
    assert not result.is_error
    return result.content


# ── the text ────────────────────────────────────────────────────────────────


def test_an_open_item_with_a_future_not_before_is_deferred():
    assert _not_before_text(_item(FUTURE)) == f"deferred until {FUTURE}"


def test_a_past_not_before_or_a_claimed_item_is_named_not_deferred():
    assert _not_before_text(_item(PAST)) == f"not_before {PAST}"
    assert _not_before_text(_item(FUTURE, status="claimed")) == f"not_before {FUTURE}"


def test_no_not_before_says_nothing():
    assert _not_before_text(_item()) == ""


# ── list_work_items ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("_scope")
async def test_list_full_renders_not_before_when_set(monkeypatch):
    deferred, plain = _item(FUTURE), _item()
    content = await _listed(monkeypatch, [deferred, plain])
    deferred_line = next(line for line in content.splitlines() if deferred["id"] in line)
    plain_line = next(line for line in content.splitlines() if plain["id"] in line)
    assert f"deferred until {FUTURE}" in deferred_line
    assert "not_before" not in plain_line and "deferred" not in plain_line


@pytest.mark.asyncio
@pytest.mark.usefixtures("_scope")
async def test_list_compact_renders_not_before_when_set(monkeypatch):
    deferred, plain = _item(FUTURE), _item()
    content = await _listed(monkeypatch, [deferred, plain], compact=True)
    assert f"- `{deferred['id']}` dev:watchdog [open] (deferred until {FUTURE}) {deferred['title']}" in content
    assert f"- `{plain['id']}` dev:watchdog [open] {plain['title']}" in content


# ── get_work_item ───────────────────────────────────────────────────────────


def _status_line(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("  - status:"))


def test_get_status_block_renders_not_before_when_set():
    text = _full_item_text(_item(FUTURE), [], 0, False)
    assert _status_line(text) == f"  - status: open, priority: medium, deferred until {FUTURE}"


def test_get_status_block_without_not_before_is_unchanged():
    text = _full_item_text(_item(), [], 0, False)
    assert _status_line(text) == "  - status: open, priority: medium"
    assert "not_before" not in text and "deferred" not in text


def test_get_window_keeps_not_before_with_the_status():
    text = _full_item_text(_item(FUTURE), [], 0, False, offset=0, max_chars=100)
    assert f"deferred until {FUTURE}" in _status_line(text)
