"""A note-only set_shared_state keeps the entry's status (gap lease-note-reactivates-resolved).

A closing note on a resolved lease (``dev:branch-lock:<branch>``) used to mark
it active again, so the lease had to be resolved twice. A note alone, or a hold
release with a note, is no live write: the tool passes no status. A value, a
list change, an unset or a take still marks the entry active.
"""

import uuid
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from tests.test_shared_state_list_ops import _call


def _executor(monkeypatch):
    calls: list[dict] = []

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "status": kwargs.get("status") or "resolved", "value": {}, "notes": []}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    return executor, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"note": "merged #12, lease done"},
        {"note": "released", "hold": "release"},
    ],
)
async def test_a_note_alone_passes_no_status(monkeypatch, arguments):
    executor, calls = _executor(monkeypatch)
    result = await _call(executor, "set_shared_state", key="dev:branch-lock:fix/x", **arguments)
    assert not result.is_error, result.content
    assert calls[0]["status"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"note": "n", "value_json": '{"pr": 12}'},
        {"note": "n", "append_json": '{"prs": [{"number": 1}]}'},
        {"note": "n", "update_json": '{"gaps": {"match": {"id": "a"}, "increment": {"count": 1}}}'},
        {"note": "n", "unset_keys": ["a"]},
        {"note": "n", "hold": "take"},
        {"note": "n", "terminal_condition": "the PR merges"},
        {"note": "n", "label": "Branch lock"},
    ],
)
async def test_a_live_write_marks_the_entry_active(monkeypatch, arguments):
    executor, calls = _executor(monkeypatch)
    result = await _call(executor, "set_shared_state", key="dev:branch-lock:fix/x", **arguments)
    assert not result.is_error, result.content
    assert calls[-1]["status"] == "active"
