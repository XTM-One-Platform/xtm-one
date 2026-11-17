"""Tests for the Drata control-notes tools.

Two defects observed against a live workspace on 2026-09-22 are pinned here:

1. Drata v2 control notes are identified by UUID, but ``update_drata_control_note``
   / ``delete_drata_control_note`` declared ``note_id`` as an integer, so the
   agent loop's schema pre-validation rejected every real note id and no note
   could ever be updated or deleted.
2. ``list_drata_control_notes`` sent ``expand[]=author``; Drata rejects it with
   HTTP 400 (``ControlNotesExpandEnum`` only allows ``owner``) and the generic
   error formatting hid the body behind a bare "bad request".

The fake ``safe_request`` below mirrors the real contract: a 2xx returns
``(response, None)``; a non-2xx returns ``(response, error)`` when the caller
opted in with ``expected_statuses`` and ``(None, error)`` otherwise.
"""

from __future__ import annotations

import json

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.agent_loop import validate_tool_arguments
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"
_BASE = "https://public-api.drata.com/public/v2"

_UUID = "81584418-17a7-44b1-b0f9-47d42b903812"
_UUID_2 = "9251de3f-1111-4222-8333-444444444444"

# The exact body Drata answered for ``expand[]=author`` on 2026-09-22.
_EXPAND_400_BODY = {
    "property": "expand",
    "constraints": {"EnumValidator": "expand must be one of the following values: owner"},
}

_OWNER = {
    "id": 345,
    "email": "agentic-security@filigran.io",
    "firstName": "Filigran Agentic Security",
    "lastName": "",
}


class _Resp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.content = b"" if payload is None else b"{}"
        self.text = "" if payload is None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


def _patch(monkeypatch, responses) -> list[dict]:
    """Install a fake ``safe_request`` answering *responses* in order.

    A single ``_Resp`` is repeated for every call; a list is consumed in order
    (the last entry is repeated once the list is exhausted).
    """
    calls: list[dict] = []
    queue = list(responses) if isinstance(responses, list) else [responses]

    async def _fake_safe_request(client, method, url, provider, action, *, expected_statuses=None, **kwargs):
        # Snapshot ``params``: the paginator reuses one dict across pages and
        # sets the cursor on it in place, so a reference would read the LAST
        # page's cursor when the first call is inspected.
        snapshot = dict(kwargs)
        if isinstance(snapshot.get("params"), dict):
            snapshot["params"] = dict(snapshot["params"])
        calls.append({"method": method, "url": url, "action": action, "kwargs": snapshot})
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        if resp.is_success:
            return resp, None
        err = f"Error: Drata generic failure ({resp.status_code})"
        if expected_statuses is not None:
            return resp, err
        return None, err

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    return calls


def _build() -> tuple[CallableToolExecutor, dict]:
    executor = CallableToolExecutor()
    defs = {d.name: d for d in drata.build_tools(_TOKEN, executor)}
    return executor, defs


async def _call(executor, name, args):
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))


# ---------------------------------------------------------------------------
# Schema: note_id is a string and a UUID passes the agent loop's validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["update_drata_control_note", "delete_drata_control_note"])
def test_note_id_schema_is_a_string(tool):
    _, defs = _build()
    prop = defs[tool].input_schema["properties"]["note_id"]
    assert prop["type"] == "string"
    assert "UUID" in prop["description"]
    assert "note_id" in defs[tool].input_schema["required"]


def test_agent_loop_validation_accepts_a_uuid_note_id():
    _, defs = _build()
    delete = defs["delete_drata_control_note"]
    update = defs["update_drata_control_note"]
    assert (
        validate_tool_arguments(
            delete.name, {"workspace_id": 1, "control_id": 89, "note_id": _UUID}, delete.input_schema
        )
        is None
    )
    assert (
        validate_tool_arguments(
            update.name,
            {"workspace_id": 1, "control_id": 89, "note_id": _UUID, "comment": "x"},
            update.input_schema,
        )
        is None
    )


# ---------------------------------------------------------------------------
# note_id normalisation: UUID (any case) and legacy numeric ids are forwarded
# unchanged; anything else is refused before any HTTP call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_note_uuid_is_forwarded_unchanged(monkeypatch):
    calls = _patch(monkeypatch, _Resp({}))
    executor, _ = _build()
    result = await _call(
        executor,
        "delete_drata_control_note",
        {"workspace_id": 1, "control_id": 89, "note_id": _UUID},
    )

    assert result.is_error is False, result.content
    assert len(calls) == 1
    assert calls[0]["method"] == "delete"
    assert calls[0]["url"] == f"{_BASE}/workspaces/1/controls/89/notes/{_UUID}"
    assert calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer test-token"
    assert _UUID in result.content and "deleted" in result.content


@pytest.mark.asyncio
async def test_delete_note_uppercase_uuid_is_accepted_and_kept(monkeypatch):
    calls = _patch(monkeypatch, _Resp({}))
    executor, _ = _build()
    upper = _UUID.upper()
    result = await _call(
        executor,
        "delete_drata_control_note",
        {"workspace_id": 1, "control_id": 89, "note_id": upper},
    )
    assert result.is_error is False, result.content
    assert calls[0]["url"].endswith(f"/notes/{upper}")


@pytest.mark.asyncio
async def test_delete_note_numeric_string_is_forwarded_unchanged(monkeypatch):
    calls = _patch(monkeypatch, _Resp({}))
    executor, _ = _build()
    result = await _call(
        executor,
        "delete_drata_control_note",
        {"workspace_id": 1, "control_id": 89, "note_id": "12345"},
    )
    assert result.is_error is False, result.content
    assert calls[0]["url"].endswith("/workspaces/1/controls/89/notes/12345")


@pytest.mark.asyncio
async def test_delete_note_integer_from_bypassed_validation_path(monkeypatch):
    # The integration tool-test endpoint dispatches straight to the handler
    # without jsonschema, so a JSON integer can still arrive here.
    calls = _patch(monkeypatch, _Resp({}))
    executor, _ = _build()
    result = await _call(
        executor,
        "delete_drata_control_note",
        {"workspace_id": "1", "control_id": "89", "note_id": 12345},
    )
    assert result.is_error is False, result.content
    assert calls[0]["url"].endswith("/workspaces/1/controls/89/notes/12345")


@pytest.mark.asyncio
async def test_update_note_uuid_puts_the_comment(monkeypatch):
    calls = _patch(monkeypatch, _Resp({"id": _UUID, "comment": "new"}))
    executor, _ = _build()
    result = await _call(
        executor,
        "update_drata_control_note",
        {"workspace_id": 1, "control_id": 127, "note_id": _UUID, "comment": "new"},
    )

    assert result.is_error is False, result.content
    assert calls[0]["method"] == "put"
    assert calls[0]["url"] == f"{_BASE}/workspaces/1/controls/127/notes/{_UUID}"
    assert calls[0]["kwargs"]["json"] == {"comment": "new"}
    assert "updated" in result.content


_MALFORMED_NOTE_IDS = [
    "not-a-uuid",
    "81584418-17a7-44b1-b0f9-47d42b90381",  # one hex digit short
    "81584418-17a7-44b1-b0f9-47d42b903812x",  # trailing garbage
    "8158441817a744b1b0f947d42b903812",  # no dashes
    f"{_UUID}/../../notes",  # path injection
    f"{_UUID}?expand[]=owner",  # query injection
    f"{_UUID}#frag",  # fragment injection
    "12 34",
    "-1",
    "",
    True,
    12.5,
    {"id": _UUID},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["update_drata_control_note", "delete_drata_control_note"])
@pytest.mark.parametrize("bad", _MALFORMED_NOTE_IDS)
async def test_malformed_note_id_is_rejected_before_any_http_call(monkeypatch, tool, bad):
    calls = _patch(monkeypatch, _Resp({}))
    executor, _ = _build()
    args = {"workspace_id": 1, "control_id": 89, "note_id": bad}
    if tool == "update_drata_control_note":
        args["comment"] = "x"
    result = await _call(executor, tool, args)

    assert result.is_error is True  # a guarded refusal, not a raise
    assert "note_id must be the note's UUID" in result.content
    assert "No request was sent" in result.content
    assert calls == []


def test_note_ref_helper_contract():
    assert drata_common._note_ref(_UUID) == _UUID
    assert drata_common._note_ref(_UUID.upper()) == _UUID.upper()
    assert drata_common._note_ref(f"  {_UUID}  ") == _UUID
    assert drata_common._note_ref("42") == "42"
    assert drata_common._note_ref(42) == "42"
    assert drata_common._note_ref(0) == "0"
    assert drata_common._note_ref(-1) == ""
    assert drata_common._note_ref(True) == ""
    assert drata_common._note_ref(None) == ""
    assert drata_common._note_ref("\u00b2") == ""  # a Unicode digit is not an id


# ---------------------------------------------------------------------------
# list: expand=owner (never author), every field rendered, cursor pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_notes_expands_owner_not_author(monkeypatch):
    calls = _patch(monkeypatch, _Resp({"data": [], "pagination": {}}))
    executor, _ = _build()
    result = await _call(executor, "list_drata_control_notes", {"workspace_id": 1, "control_id": 89})

    assert result.is_error is False, result.content
    assert calls[0]["method"] == "get"
    assert calls[0]["url"] == f"{_BASE}/workspaces/1/controls/89/notes"
    params = calls[0]["kwargs"]["params"]
    assert params["expand[]"] == ["owner"]
    assert "author" not in json.dumps(params)
    assert "No control notes found." in result.content


@pytest.mark.asyncio
async def test_list_notes_renders_id_comment_owner_and_timestamps(monkeypatch):
    _patch(
        monkeypatch,
        _Resp(
            {
                "data": [
                    {
                        "id": _UUID,
                        "ownerId": 345,
                        "comment": "Evidence bypass\nneeds review",
                        "createdAt": "2026-09-22T07:30:12.000Z",
                        "updatedAt": "2026-09-22T07:31:00.000Z",
                        "owner": _OWNER,
                    },
                    {
                        # No expansion on this row: fall back to ownerId.
                        "id": _UUID_2,
                        "ownerId": 12,
                        "comment": "Clean note",
                        "createdAt": "2026-09-21T10:00:00.000Z",
                        "updatedAt": "2026-09-21T10:00:00.000Z",
                    },
                ],
                "pagination": {},
            }
        ),
    )
    executor, _ = _build()
    result = await _call(executor, "list_drata_control_notes", {"workspace_id": 1, "control_id": 127})

    assert result.is_error is False, result.content
    assert result.content.startswith("Found 2 control notes:")
    lines = result.content.splitlines()[1:]
    assert lines[0] == (
        f"[{_UUID}] Evidence bypass needs review | "
        "Owner: Filigran Agentic Security (id=345, agentic-security@filigran.io) | "
        "Created: 2026-09-22T07:30:12.000Z | Updated: 2026-09-22T07:31:00.000Z"
    )
    assert lines[1] == (
        f"[{_UUID_2}] Clean note | Owner: id=12 | Created: 2026-09-21T10:00:00.000Z | Updated: 2026-09-21T10:00:00.000Z"
    )


@pytest.mark.asyncio
async def test_list_notes_follows_cursor_pagination_to_the_last_page(monkeypatch):
    calls = _patch(
        monkeypatch,
        [
            _Resp({"data": [{"id": _UUID, "comment": "first"}], "pagination": {"cursor": "next-1"}}),
            _Resp({"data": [{"id": _UUID_2, "comment": "second"}], "pagination": {"cursor": None}}),
        ],
    )
    executor, _ = _build()
    result = await _call(executor, "list_drata_control_notes", {"workspace_id": 1, "control_id": 89})

    assert result.is_error is False, result.content
    assert len(calls) == 2
    assert "cursor" not in calls[0]["kwargs"]["params"]
    assert calls[1]["kwargs"]["params"]["cursor"] == "next-1"
    assert result.content.startswith("Found 2 control notes:")
    assert _UUID in result.content and _UUID_2 in result.content


# ---------------------------------------------------------------------------
# Errors: verbatim Drata body + status code; 403 on a write = owner mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_notes_400_surfaces_drata_body_verbatim(monkeypatch):
    _patch(monkeypatch, _Resp(_EXPAND_400_BODY, status_code=400))
    executor, _ = _build()
    result = await _call(executor, "list_drata_control_notes", {"workspace_id": 1, "control_id": 89})

    assert result.is_error is True
    assert "HTTP 400" in result.content
    assert "list control notes" in result.content
    assert json.dumps(_EXPAND_400_BODY, ensure_ascii=False, separators=(",", ":")) in result.content
    assert "bad request" not in result.content.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "verb", "extra"),
    [
        ("delete_drata_control_note", "delete", {}),
        ("update_drata_control_note", "update", {"comment": "x"}),
    ],
)
async def test_403_on_a_note_write_is_an_owner_mismatch_error(monkeypatch, tool, verb, extra):
    body = {"statusCode": 403, "message": "Forbidden resource", "code": 1403}
    calls = _patch(monkeypatch, _Resp(body, status_code=403))
    executor, _ = _build()
    result = await _call(executor, tool, {"workspace_id": 1, "control_id": 89, "note_id": _UUID, **extra})

    assert result.is_error is True
    assert len(calls) == 1
    assert f"refused to {verb} note {_UUID} on control 89 (HTTP 403)" in result.content
    assert "only the note's owner can update or delete it" in result.content
    assert "API key" in result.content
    assert json.dumps(body, ensure_ascii=False, separators=(",", ":")) in result.content


@pytest.mark.asyncio
async def test_delete_note_404_surfaces_status_and_body(monkeypatch):
    body = {"statusCode": 404, "message": "Not Found"}
    _patch(monkeypatch, _Resp(body, status_code=404))
    executor, _ = _build()
    result = await _call(executor, "delete_drata_control_note", {"workspace_id": 1, "control_id": 89, "note_id": _UUID})
    assert result.is_error is True
    assert "HTTP 404" in result.content
    assert "delete control note" in result.content
    assert '"message":"Not Found"' in result.content
    assert "owner" not in result.content.lower()  # a 404 is not an owner mismatch


@pytest.mark.asyncio
async def test_network_error_keeps_the_safe_request_message(monkeypatch):
    calls: list[dict] = []

    async def _fake_safe_request(client, method, url, provider, action, *, expected_statuses=None, **kwargs):
        calls.append({"url": url})
        return None, "Error: Could not connect to Drata while trying to delete control note."

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    executor, _ = _build()
    result = await _call(executor, "delete_drata_control_note", {"workspace_id": 1, "control_id": 89, "note_id": _UUID})
    assert result.is_error is True
    assert result.content == "Error: Could not connect to Drata while trying to delete control note."
    assert len(calls) == 1


def test_verbatim_body_falls_back_to_text_and_is_bounded():
    assert drata_common._verbatim_body(_Resp(None, status_code=502)) == "<empty body>"

    class _HtmlResp:
        status_code = 502
        text = "<html>Bad gateway</html>"

        def json(self):
            raise ValueError("not JSON")

    assert drata_common._verbatim_body(_HtmlResp()) == "<html>Bad gateway</html>"

    huge = _Resp({"message": "x" * (drata_common._ERROR_BODY_MAX_CHARS + 100)}, status_code=500)
    rendered = drata_common._verbatim_body(huge)
    assert rendered.endswith("... [truncated]")
    assert len(rendered) == drata_common._ERROR_BODY_MAX_CHARS + len("... [truncated]")


# ---------------------------------------------------------------------------
# create + path guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_note_posts_comment_and_reports_uuid(monkeypatch):
    calls = _patch(monkeypatch, _Resp({"id": _UUID, "ownerId": 345, "comment": "hello"}, status_code=201))
    executor, _ = _build()
    result = await _call(
        executor, "create_drata_control_note", {"workspace_id": 1, "control_id": 89, "comment": "hello"}
    )

    assert result.is_error is False, result.content
    assert calls[0]["method"] == "post"
    assert calls[0]["url"] == f"{_BASE}/workspaces/1/controls/89/notes"
    assert calls[0]["kwargs"]["json"] == {"comment": "hello"}
    assert f"Note created (ID: {_UUID}) on control 89 (owner id=345)." == result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool",
    [
        "list_drata_control_notes",
        "create_drata_control_note",
        "update_drata_control_note",
        "delete_drata_control_note",
    ],
)
async def test_non_numeric_control_id_is_rejected_before_any_http_call(monkeypatch, tool):
    calls = _patch(monkeypatch, _Resp({"data": [], "pagination": {}}))
    executor, _ = _build()
    args = {"workspace_id": 1, "control_id": "89/../owners", "note_id": _UUID, "comment": "x"}
    result = await _call(executor, tool, args)

    assert result.is_error is True
    assert "control_id must be a numeric Drata control ID" in result.content
    assert calls == []
