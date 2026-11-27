"""OpenCTI request-context failures: an HTTP 500 whose body says why.

OpenCTI checks a request's draft before the GraphQL pipeline runs, and answers
a failed check with HTTP 500 plus a GraphQL ``errors`` body naming the cause
("User cannot be found" for an unauthenticated draft-scoped request, DRAFT_LOCKED
/ "Could not find draft workspace" / "not in an open state" for a dead draft).
XTM One used to discard that body, report a generic "server error (500) ...
temporarily unavailable", renew nothing, and keep sending the dead draft id on
every later call for the rest of the run.

These drive the real ``_client`` / ``safe_request`` / ``_graphql`` path with
``httpx.AsyncClient.request`` patched, like ``test_opencti_auth_refresh.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.http import RefreshableHeaders
from app.integrations.providers.opencti import build_tools
from app.integrations.providers.opencti._common import (
    _DRAFT_REQUIRED_MSG,
    _UI_DRAFT_UNUSABLE_MSG,
    _client,
    _drop_dead_draft,
    _graphql,
    _graphql_upload,
)
from app.tools.base import CallableToolExecutor, ToolCall, is_error_shaped_result

BASE_URL = "https://octi.test"
QUERY = "query ReadThing($id: String!) { stixCoreObject(id: $id) { id } }"
MUTATION = "mutation Change($id: ID!) { stixDomainObjectEdit(id: $id) { delete } }"
UPLOAD = "mutation Upload($file: Upload!) { stixCoreObjectEdit { importPush(file: $file) { id } } }"


def _jwt(seconds_from_now: float) -> str:
    def _seg(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{_seg({'alg': 'none'})}.{_seg({'exp': int(time.time() + seconds_from_now)})}.sig"


def _headers(token: str, *, draft: str | None = "draft-1", refresh_fn: Any = None) -> RefreshableHeaders:
    headers = RefreshableHeaders(
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        refresh_fn=refresh_fn,
    )
    if draft:
        headers["opencti-draft-id"] = draft
    return headers


def _error_body(message: str, code: str) -> dict:
    return {"errors": [{"message": message, "extensions": {"code": code}}]}


DRAFT_LOCKED = _error_body(
    "Cannot execute request in a draft that is not in an open state", "DRAFT_LOCKED"
)
DRAFT_MISSING = _error_body("Could not find draft workspace", "DRAFT_LOCKED")
DRAFT_FORBIDDEN = _error_body(
    "Draft draft-1 cannot be found, consider switching the user associated to your "
    "connector to a service account (instead of a user)",
    "FUNCTIONAL_ERROR",
)
USER_NOT_FOUND = _error_body("User cannot be found", "FUNCTIONAL_ERROR")


def _resp(url: str, body: Any, status: int) -> httpx.Response:
    request = httpx.Request("POST", url)
    if isinstance(body, str):
        return httpx.Response(status, text=body, request=request)
    return httpx.Response(status, json=body, request=request)


async def _run_graphql(headers: RefreshableHeaders, answer) -> tuple[dict, str | None, list]:
    seen: list[dict] = []

    async def _fake(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        sent = dict(kwargs.get("headers") or {})
        seen.append(sent)
        body, status = answer(len(seen), sent)
        return _resp(url, body, status)

    with patch.object(httpx.AsyncClient, "request", _fake):
        async with _client(headers, timeout=30) as client:
            data, err = await _graphql(client, BASE_URL, headers, QUERY, {"id": "x"})
    return data, err, seen


# ---------------------------------------------------------------------------
# _graphql
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [DRAFT_LOCKED, DRAFT_MISSING, DRAFT_FORBIDDEN], ids=["not-open", "missing", "no-access"])
async def test_a_dead_draft_is_named_and_dropped(body):
    headers = _headers(_jwt(600))
    data, err, seen = await _run_graphql(headers, lambda n, _h: (body, 500))

    assert data == {}
    assert err is not None and err.startswith("Error: the OpenCTI draft draft-1 is no longer usable")
    assert body["errors"][0]["message"] in err, "OpenCTI's own reason must reach the agent"
    assert "temporarily unavailable" not in err
    assert is_error_shaped_result(err)
    assert "opencti-draft-id" not in headers, "the dead draft must not be sent again"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_user_not_found_under_a_draft_renews_the_login_once():
    """An expired token + a draft header is a 500, not the 200 AUTH_REQUIRED
    the existing re-mint handled; it must be renewed and replayed once."""
    stale, fresh = _jwt(600), _jwt(1800)
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        return fresh

    headers = _headers(stale, refresh_fn=_remint)

    def answer(_n, sent):
        if sent.get("Authorization") == f"Bearer {stale}":
            return USER_NOT_FOUND, 500
        return {"data": {"stixCoreObject": {"id": "x"}}}, 200

    data, err, seen = await _run_graphql(headers, answer)
    assert err is None
    assert data == {"stixCoreObject": {"id": "x"}}
    assert remints["n"] == 1
    assert [s.get("Authorization") for s in seen] == [f"Bearer {stale}", f"Bearer {fresh}"]
    assert headers["opencti-draft-id"] == "draft-1", "a renewed login keeps the draft"


@pytest.mark.asyncio
async def test_user_not_found_without_a_draft_is_not_treated_as_an_auth_failure():
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        return _jwt(1800)

    headers = _headers(_jwt(600), draft=None, refresh_fn=_remint)
    _data, err, _seen = await _run_graphql(headers, lambda n, _h: (USER_NOT_FOUND, 500))
    assert remints["n"] == 0
    assert err.startswith("GraphQL error (HTTP 500) in 'ReadThing': User cannot be found")
    assert "token was rejected" not in err


@pytest.mark.asyncio
async def test_another_500_error_surfaces_opencti_message_and_keeps_the_draft():
    headers = _headers(_jwt(600))
    body = _error_body("Engine execution fail", "DATABASE_ERROR")
    _data, err, _seen = await _run_graphql(headers, lambda n, _h: (body, 500))
    assert err.startswith("GraphQL error (HTTP 500) in 'ReadThing': Engine execution fail [DATABASE_ERROR]")
    assert is_error_shaped_result(err)
    assert headers["opencti-draft-id"] == "draft-1"


@pytest.mark.asyncio
async def test_a_500_without_a_graphql_body_keeps_the_generic_message():
    headers = _headers(_jwt(600))
    _data, err, _seen = await _run_graphql(headers, lambda n, _h: ("<html>proxy error</html>", 500))
    assert "OpenCTI server error (500)" in err
    assert "ReadThing" in err
    assert headers["opencti-draft-id"] == "draft-1"


@pytest.mark.asyncio
async def test_every_500_leaves_one_diagnostic_line(caplog):
    headers = _headers(_jwt(600))
    with caplog.at_level(logging.WARNING, logger="app.integrations.providers.opencti._common"):
        await _run_graphql(headers, lambda n, _h: (DRAFT_LOCKED, 500))
    lines = [r.getMessage() for r in caplog.records if "OpenCTI HTTP 500" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "on 'ReadThing' (root stixCoreObject)" in line
    assert "draft=draft-1" in line
    assert "code=DRAFT_LOCKED" in line
    assert "not in an open state" in line
    assert "bearer_expires_in=" in line and "bearer_expires_in=n/a" not in line
    assert "Bearer" not in line, "the token itself is never logged"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_only_a_500_is_read_as_a_context_failure(status, caplog):
    """``expected_statuses`` returns any failed response; only 500 is read."""
    headers = _headers(_jwt(600))
    with caplog.at_level(logging.WARNING, logger="app.integrations.providers.opencti._common"):
        _data, err, _seen = await _run_graphql(headers, lambda n, _h: (DRAFT_LOCKED, status))
    assert "no longer usable" not in err
    assert "(HTTP 500)" not in err
    assert headers["opencti-draft-id"] == "draft-1"
    assert not [r for r in caplog.records if "OpenCTI HTTP" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_plain_dict_keeps_the_draft_rather_than_writing_live():
    """Only RefreshableHeaders can carry the revocation mark; without it the
    draft is kept (calls keep failing) instead of opening the live graph."""
    headers = {"Authorization": f"Bearer {_jwt(600)}", "opencti-draft-id": "draft-1"}

    async def _fake(self, method, url, **kwargs):
        return _resp(url, DRAFT_LOCKED, 500)

    with patch.object(httpx.AsyncClient, "request", _fake):
        async with _client(headers, timeout=30) as client:
            _data, err = await _graphql(client, BASE_URL, headers, QUERY, {"id": "x"})
    assert "no longer usable" in err
    assert headers["opencti-draft-id"] == "draft-1"


@pytest.mark.asyncio
async def test_after_a_revoked_draft_mutations_wait_for_a_new_one():
    headers = _headers(_jwt(600))
    sent: list[str] = []

    async def _fake(self, method, url, **kwargs):
        query = (kwargs.get("json") or {}).get("query", "")
        sent.append(query)
        if kwargs["headers"].get("opencti-draft-id") == "draft-1":
            return _resp(url, DRAFT_LOCKED, 500)
        return _resp(url, {"data": {"draftWorkspaceAdd": {"id": "draft-2"}}}, 200)

    create = "mutation draftWorkspaceAdd($input: DraftWorkspaceAddInput!) { draftWorkspaceAdd(input: $input) { id } }"
    with patch.object(httpx.AsyncClient, "request", _fake):
        async with _client(headers, timeout=30) as client:
            await _graphql(client, BASE_URL, headers, QUERY, {"id": "x"})
            _d, refused = await _graphql(client, BASE_URL, headers, MUTATION, {"id": "x"})
            _d, read_err = await _graphql(client, BASE_URL, headers, QUERY, {"id": "x"})
            _d, create_err = await _graphql(client, BASE_URL, headers, create, {"input": {}})
            headers["opencti-draft-id"] = "draft-2"  # what create_opencti_draft does
            _d, after = await _graphql(client, BASE_URL, headers, MUTATION, {"id": "x"})

    assert refused.startswith("Error: not sent - the OpenCTI draft draft-1 was rejected")
    assert read_err is None, "reads go to the live platform"
    assert create_err is None, "creating the replacement draft is allowed"
    assert after is None, "a new draft re-enables changes"
    assert sum("stixDomainObjectEdit" in q for q in sent) == 1  # only the one with draft-2


@pytest.mark.asyncio
async def test_upload_handles_a_dead_draft_and_an_expired_login():
    stale, fresh = _jwt(600), _jwt(1800)

    async def _remint() -> str:
        return fresh

    async def _call(headers, answer):
        async def _fake(self, method, url, **kwargs):
            return _resp(url, *answer(kwargs.get("headers") or {}))

        with patch.object(httpx.AsyncClient, "request", _fake):
            async with _client(headers, timeout=30) as client:
                return await _graphql_upload(
                    client, BASE_URL, headers, UPLOAD, {}, "file", "r.pdf", b"%PDF", "application/pdf"
                )

    headers = _headers(stale, refresh_fn=_remint)
    data, err = await _call(
        headers,
        lambda h: (USER_NOT_FOUND, 500)
        if h.get("Authorization") == f"Bearer {stale}"
        else ({"data": {"stixCoreObjectEdit": {"importPush": {"id": "f"}}}}, 200),
    )
    assert err is None and data == {"stixCoreObjectEdit": {"importPush": {"id": "f"}}}

    headers = _headers(stale)
    _data, err = await _call(headers, lambda h: (DRAFT_MISSING, 500))
    assert "no longer usable" in err and "Could not find draft workspace" in err
    assert "opencti-draft-id" not in headers


@pytest.mark.asyncio
@pytest.mark.parametrize(("draft", "expected"), [(None, ""), ("draft-1", "draft-1")], ids=["live", "draft"])
async def test_an_upload_names_its_draft_context(draft, expected):
    """Uploads build their headers apart from _graphql: without a draft they run
    in the live graph, not in the account's own UI draft."""
    headers = _headers(_jwt(600), draft=draft)
    seen: list[str | None] = []

    async def _fake(self, method, url, **kwargs):
        seen.append((kwargs.get("headers") or {}).get("opencti-draft-id"))
        return _resp(url, {"data": {"stixCoreObjectEdit": {"importPush": {"id": "f"}}}}, 200)

    with patch.object(httpx.AsyncClient, "request", _fake):
        async with _client(headers, timeout=30) as client:
            _data, err = await _graphql_upload(
                client, BASE_URL, headers, UPLOAD, {}, "file", "r.pdf", b"%PDF", "application/pdf"
            )
    assert err is None
    assert seen == [expected]


@pytest.mark.asyncio
async def test_a_200_graphql_error_is_unchanged():
    headers = _headers(_jwt(600))
    body = _error_body("Boom", "FUNCTIONAL_ERROR")
    _data, err, _seen = await _run_graphql(headers, lambda n, _h: (body, 200))
    assert err.startswith("GraphQL error in 'ReadThing': Boom")


async def _change(kind: str, client: Any, headers: RefreshableHeaders) -> tuple[dict, str | None]:
    if kind == "graphql":
        return await _graphql(client, BASE_URL, headers, MUTATION, {"id": "x"})
    return await _graphql_upload(
        client, BASE_URL, headers, UPLOAD, {}, "file", "r.pdf", b"%PDF", "application/pdf"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["graphql", "upload"])
async def test_a_draft_revoked_while_a_change_awaits_its_token_refresh_is_still_sent(kind):
    """The tools of one batch share ``headers`` (``asyncio.gather``): a change
    that passed the revocation check keeps its draft while it awaits a token
    refresh, whatever a concurrent call dropped meanwhile."""
    refreshing, release = asyncio.Event(), asyncio.Event()
    fresh = _jwt(1800)

    async def _slow_remint() -> str:
        refreshing.set()
        await release.wait()
        return fresh

    headers = _headers(_jwt(60), refresh_fn=_slow_remint)  # inside the refresh skew
    sent: list[dict] = []

    async def _fake(self, method, url, **kwargs):
        sent.append(dict(kwargs.get("headers") or {}))
        return _resp(url, {"data": {"ok": True}}, 200)

    with patch.object(httpx.AsyncClient, "request", _fake):
        async with _client(headers, timeout=30) as client:
            change = asyncio.create_task(_change(kind, client, headers))
            await refreshing.wait()
            # What a concurrent call's dead-draft 500 does to the shared headers.
            _drop_dead_draft(headers, "draft-1", DRAFT_LOCKED["errors"])
            release.set()
            _data, err = await change

    assert err is None
    assert [s.get("opencti-draft-id") for s in sent] == ["draft-1"], "never sent without its draft"
    assert sent[0]["Authorization"] == f"Bearer {fresh}", "the refreshed token is still sent"


@pytest.mark.asyncio
async def test_a_draft_revoked_during_the_remint_replay_is_still_sent():
    stale, fresh = _jwt(600), _jwt(1800)
    reminting, release = asyncio.Event(), asyncio.Event()

    async def _slow_remint() -> str:
        reminting.set()
        await release.wait()
        return fresh

    headers = _headers(stale, refresh_fn=_slow_remint)
    sent: list[dict] = []

    async def _fake(self, method, url, **kwargs):
        sent.append(dict(kwargs.get("headers") or {}))
        if kwargs["headers"].get("Authorization") == f"Bearer {stale}":
            return _resp(url, USER_NOT_FOUND, 500)
        return _resp(url, {"data": {"ok": True}}, 200)

    with patch.object(httpx.AsyncClient, "request", _fake):
        async with _client(headers, timeout=30) as client:
            change = asyncio.create_task(_change("graphql", client, headers))
            await reminting.wait()
            _drop_dead_draft(headers, "draft-1", DRAFT_LOCKED["errors"])
            release.set()
            _data, err = await change

    assert err is None
    assert [(s.get("opencti-draft-id"), s.get("Authorization")) for s in sent] == [
        ("draft-1", f"Bearer {stale}"),
        ("draft-1", f"Bearer {fresh}"),
    ]


@pytest.mark.asyncio
async def test_a_stale_call_never_drops_the_draft_that_replaced_its_own():
    headers = _headers(_jwt(600))

    def answer(_n, _sent):
        # While this read was in flight, the run moved on to draft-2.
        headers["opencti-draft-id"] = "draft-2"
        return DRAFT_LOCKED, 500

    _data, err, seen = await _run_graphql(headers, answer)
    assert seen[0]["opencti-draft-id"] == "draft-1"
    assert err.startswith("Error: the OpenCTI draft draft-1 is no longer usable")
    assert "now uses draft draft-2" in err
    assert headers["opencti-draft-id"] == "draft-2"
    assert not getattr(headers, "revoked_opencti_draft", None)


@pytest.mark.asyncio
async def test_a_dead_draft_the_request_did_not_carry_revokes_nothing():
    """A request that named no draft ran in the live graph: a dead-draft answer
    to it drops and refuses nothing of this run."""
    headers = _headers(_jwt(600), draft=None)
    answers = iter([(DRAFT_LOCKED, 500), ({"data": {"ok": True}}, 200)])

    async def _fake(self, method, url, **kwargs):
        return _resp(url, *next(answers))

    with patch.object(httpx.AsyncClient, "request", _fake):
        async with _client(headers, timeout=30) as client:
            _d, read_err = await _graphql(client, BASE_URL, headers, QUERY, {"id": "x"})
            _d, change_err = await _graphql(client, BASE_URL, headers, MUTATION, {"id": "x"})

    assert read_err.startswith(
        "GraphQL error (HTTP 500) in 'ReadThing': Cannot execute request in a draft "
        "that is not in an open state [DRAFT_LOCKED]"
    )
    assert not getattr(headers, "revoked_opencti_draft", None)
    assert change_err is None, "a change is not refused over a draft the run never used"


@pytest.mark.asyncio
async def test_user_not_found_under_a_draft_names_the_token_when_it_cannot_be_renewed():
    """A static API key cannot be re-minted: the message says what the re-mint
    path acts on - the token was rejected - and keeps the draft."""
    headers = _headers(_jwt(600))
    _data, err, seen = await _run_graphql(headers, lambda n, _h: (USER_NOT_FOUND, 500))
    assert len(seen) == 1
    assert err.startswith("GraphQL error (HTTP 500) in 'ReadThing': User cannot be found")
    assert "the OpenCTI API token was rejected" in err
    assert headers["opencti-draft-id"] == "draft-1"


# ---------------------------------------------------------------------------
# Through the registered tools: the run stops reusing the draft, and stays
# draft-staged for changes.
# ---------------------------------------------------------------------------


def _executor(*, autonomous: bool, ui_draft: bool = True) -> CallableToolExecutor:
    executor = CallableToolExecutor()
    build_tools(
        "https://octi.test||static-token",
        executor,
        scope_headers={"opencti-draft-id": "draft-1"} if ui_draft else None,
        autonomous=autonomous,
    )
    return executor


async def _drive(executor, calls, answer) -> list[tuple[str | None, str]]:
    """Run the tool calls; return (draft header, operation) per request sent."""
    sent: list[tuple[str | None, str]] = []

    async def _fake(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        query = (kwargs.get("json") or {}).get("query", "")
        sent.append((headers.get("opencti-draft-id"), query))
        body, status = answer(headers, query)
        return _resp(url, body, status)

    results = []
    with patch.object(httpx.AsyncClient, "request", _fake):
        for name, arguments in calls:
            result = await executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))
            results.append(result.content)
    return sent, results


def _dead_draft_1(headers, query):
    if headers.get("opencti-draft-id") == "draft-1":
        return DRAFT_LOCKED, 500
    if "draftWorkspaceAdd" in query:
        return {"data": {"draftWorkspaceAdd": {"id": "draft-2"}}}, 200
    return {"data": {}}, 200


@pytest.mark.asyncio
async def test_interactive_run_drops_the_dead_draft_and_never_writes_live():
    executor = _executor(autonomous=False)
    sent, results = await _drive(
        executor,
        [
            ("get_opencti_entity", {"entity_id": "x"}),
            ("get_opencti_entity", {"entity_id": "y"}),
            ("add_opencti_label", {"entity_id": "x", "label_name": "apt"}),
        ],
        _dead_draft_1,
    )
    assert "no longer usable" in results[0]
    # The second read goes to the live platform instead of failing again - not
    # to the account's own draft, which a request without the header reads.
    assert sent[1][0] == ""
    # The change is NOT written to the live graph: the gate asks for a draft -
    # from the OpenCTI UI, as a run started in a UI draft has no use_opencti_draft.
    assert results[2] == _UI_DRAFT_UNUSABLE_MSG
    assert "use_opencti_draft" not in executor.tool_names
    assert not any("use_opencti_draft" in r for r in results)
    assert not any("labelAdd" in q or "relationAdd" in q for _d, q in sent)


@pytest.mark.asyncio
async def test_a_run_without_a_ui_draft_is_still_asked_to_pick_or_create_one():
    executor = _executor(autonomous=False, ui_draft=False)

    def every_draft_dead(headers, query):
        if headers.get("opencti-draft-id"):
            return DRAFT_LOCKED, 500
        if "draftWorkspaceAdd" in query:
            return {"data": {"draftWorkspaceAdd": {"id": "draft-7"}}}, 200
        return {"data": {}}, 200

    _sent, results = await _drive(
        executor,
        [
            ("create_opencti_draft", {"name": "triage"}),
            ("get_opencti_entity", {"entity_id": "x"}),
            ("add_opencti_label", {"entity_id": "x", "label_name": "apt"}),
        ],
        every_draft_dead,
    )
    assert "draft-7 is no longer usable" in results[1]
    assert results[2] == _DRAFT_REQUIRED_MSG
    assert "use_opencti_draft" in executor.tool_names


@pytest.mark.asyncio
async def test_a_successful_change_that_says_locked_keeps_the_draft():
    """The gate's fallback reads error-shaped results only: 'blocked-ips' is a
    label name, not a dead draft."""
    executor = _executor(autonomous=False)

    def answer(_headers, query):
        if "FindLabel" in query:
            return {"data": {"labels": {"edges": [{"node": {"id": "l1", "value": "blocked-ips"}}]}}}, 200
        if "RefTarget" in query:
            target = {"id": "x", "entity_type": "Report", "parent_types": ["Stix-Core-Object"]}
            return {"data": {"stixObjectOrStixRelationship": target}}, 200
        if "AddLabel" in query:
            return {"data": {"stixCoreObjectEdit": {"relationAdd": {"id": "r1"}}}}, 200
        return {"data": {"stixCoreObject": None}}, 200

    sent, results = await _drive(
        executor,
        [
            ("add_opencti_label", {"entity_id": "x", "label_name": "blocked-ips"}),
            ("add_opencti_label", {"entity_id": "y", "label_name": "blocked-ips"}),
        ],
        answer,
    )
    assert results == [
        "Label 'blocked-ips' added to entity x.",
        "Label 'blocked-ips' added to entity y.",
    ]
    assert {d for d, _q in sent} == {"draft-1"}, "every request stays in the user's draft"


@pytest.mark.asyncio
async def test_autonomous_run_moves_to_a_new_draft_for_the_next_change():
    executor = _executor(autonomous=True)
    sent, results = await _drive(
        executor,
        [
            ("get_opencti_entity", {"entity_id": "x"}),
            ("add_opencti_label", {"entity_id": "x", "label_name": "apt"}),
        ],
        _dead_draft_1,
    )
    assert "no longer usable" in results[0]
    assert any("draftWorkspaceAdd" in q for _d, q in sent)
    label_calls = [d for d, q in sent if "labels(" in q or "labelAdd" in q]
    assert label_calls and all(d == "draft-2" for d in label_calls)


@pytest.mark.asyncio
async def test_a_multi_step_mutation_stops_writing_once_its_draft_is_rejected():
    """``add_opencti_to_container`` loops over ids: once the first call drops
    the draft, the next iterations must not reach the live graph."""
    executor = _executor(autonomous=False)
    sent, results = await _drive(
        executor,
        [("add_opencti_to_container", {"container_id": "report--1", "entity_ids": ["a", "b", "c"]})],
        _dead_draft_1,
    )
    mutations = [(d, q) for d, q in sent if "AddToContainer" in q]
    assert mutations == [("draft-1", mutations[0][1])], "only the first (rejected) call is sent"
    assert "no longer usable" in results[0]
    assert "not sent" in results[0]


def test_the_draft_gate_keeps_argument_filtering_when_a_draft_is_active():
    executor = _executor(autonomous=False)
    accepted = executor._accepted_params["add_opencti_label"]
    assert accepted == {"entity_id", "label_name"}


# ---------------------------------------------------------------------------
# Draft workspace operations run outside any draft
# ---------------------------------------------------------------------------

UNSUPPORTED_IN_DRAFT = _error_body(
    "Cannot index unsupported element in draft context", "UNSUPPORTED_ERROR"
)


def _opencti_with_a_ui_draft(*new_ids: str):
    """OpenCTI for an account that entered a draft in the OpenCTI UI.

    A request runs in the draft its header names, or in the account's own draft
    when the header is absent; only an empty header selects the live graph. A
    draft workspace cannot be created inside a draft.
    """
    ids = iter(new_ids)

    def answer(headers, query):
        context = headers.get("opencti-draft-id", "ui-draft")
        if "draftWorkspaceAdd" in query:
            if context:
                return UNSUPPORTED_IN_DRAFT, 500
            return {"data": {"draftWorkspaceAdd": {"id": next(ids)}}}, 200
        if "draftWorkspaces" in query:
            node = {"id": "d-9", "name": "Open one", "draft_status": "open"}
            return {"data": {"draftWorkspaces": {"edges": [{"node": node}]}}}, 200
        if "draftWorkspace(" in query:
            return {"data": {"draftWorkspace": {"id": "d-9", "name": "Open one", "draft_status": "open"}}}, 200
        return {"data": {"stixCoreObject": None}}, 200

    return answer


@pytest.mark.asyncio
@pytest.mark.parametrize("ui_draft", [False, True], ids=["no-draft-in-run", "draft-in-run"])
async def test_a_draft_is_created_in_the_live_graph_whatever_draft_is_in_effect(ui_draft):
    executor = _executor(autonomous=False, ui_draft=ui_draft)
    sent, results = await _drive(
        executor,
        [
            ("create_opencti_draft", {"name": "Repro"}),
            ("get_opencti_entity", {"entity_id": "x"}),
        ],
        _opencti_with_a_ui_draft("draft-new"),
    )
    assert results[0].startswith("✅ Draft created"), results[0]
    create = [d for d, q in sent if "draftWorkspaceAdd" in q]
    assert create == [""], "draftWorkspaceAdd must carry an empty draft header"
    assert sent[-1][0] == "draft-new", "the new draft becomes the run's active draft"


@pytest.mark.asyncio
async def test_a_second_draft_can_be_created_in_the_same_run():
    executor = _executor(autonomous=False, ui_draft=False)
    sent, results = await _drive(
        executor,
        [
            ("create_opencti_draft", {"name": "A"}),
            ("create_opencti_draft", {"name": "B"}),
            ("get_opencti_entity", {"entity_id": "x"}),
        ],
        _opencti_with_a_ui_draft("draft-a", "draft-b"),
    )
    assert all(r.startswith("✅ Draft created") for r in results[:2]), results
    assert [d for d, q in sent if "draftWorkspaceAdd" in q] == ["", ""]
    assert sent[-1][0] == "draft-b"


@pytest.mark.asyncio
async def test_draft_lookups_read_the_live_graph():
    executor = _executor(autonomous=False, ui_draft=False)
    sent, results = await _drive(
        executor,
        [
            ("list_opencti_drafts", {}),
            ("get_opencti_draft_info", {"draft_id": "d-9"}),
            ("use_opencti_draft", {"draft_id": "d-9"}),
            ("get_opencti_entity", {"entity_id": "x"}),
        ],
        _opencti_with_a_ui_draft(),
    )
    assert "Open one" in results[0] and "Open one" in results[1]
    assert results[2].startswith("✅ Draft activated"), results[2]
    assert [d for d, _q in sent[:3]] == ["", "", ""]
    assert sent[3][0] == "d-9", "knowledge calls still run in the run's draft"


@pytest.mark.asyncio
async def test_a_draft_is_created_while_a_rejected_one_awaits_its_replacement():
    executor = _executor(autonomous=False, ui_draft=True)

    def answer(headers, query):
        if headers.get("opencti-draft-id") == "draft-1":
            return DRAFT_LOCKED, 500
        return _opencti_with_a_ui_draft("draft-2")(headers, query)

    sent, results = await _drive(
        executor,
        [
            ("get_opencti_entity", {"entity_id": "x"}),
            ("create_opencti_draft", {"name": "replacement"}),
        ],
        answer,
    )
    assert "no longer usable" in results[0]
    assert results[1].startswith("✅ Draft created"), results[1]
    assert [d for d, q in sent if "draftWorkspaceAdd" in q] == [""]


# ---------------------------------------------------------------------------
# A run without a draft works in the live graph, never in the account's own
# draft: the header is always sent, empty when the run has no draft.
# ---------------------------------------------------------------------------


def _label_answer(_headers, query):
    if "FindLabel" in query:
        return {"data": {"labels": {"edges": [{"node": {"id": "l1", "value": "apt"}}]}}}, 200
    if "RefTarget" in query:
        target = {"id": "x", "entity_type": "Report", "parent_types": ["Stix-Core-Object"]}
        return {"data": {"stixObjectOrStixRelationship": target}}, 200
    if "AddLabel" in query:
        return {"data": {"stixCoreObjectEdit": {"relationAdd": {"id": "r1"}}}}, 200
    return {"data": {}}, 200


@pytest.mark.asyncio
async def test_a_run_without_a_draft_reads_and_manages_the_live_graph():
    executor = _executor(autonomous=False, ui_draft=False)
    sent, _results = await _drive(
        executor,
        [
            ("get_opencti_entity", {"entity_id": "x"}),
            ("manage_opencti_workspace", {"action": "list", "type": "dashboard"}),
        ],
        lambda _h, _q: ({"data": {}}, 200),
    )
    assert sent and all(d == "" for d, _q in sent), sent


@pytest.mark.asyncio
async def test_direct_mutations_write_to_the_live_graph():
    """The platform MCP servers write without a draft: an absent header put
    those writes in the account's own draft instead."""
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor, direct_mutations=True)
    sent, results = await _drive(
        executor, [("add_opencti_label", {"entity_id": "x", "label_name": "apt"})], _label_answer
    )
    assert results == ["Label 'apt' added to entity x."]
    assert any("AddLabel" in q for _d, q in sent)
    assert all(d == "" for d, _q in sent), sent


@pytest.mark.asyncio
@pytest.mark.parametrize(("ui_draft", "expected"), [(False, ""), (True, "draft-1")], ids=["live", "draft"])
async def test_a_file_download_runs_where_the_run_works(ui_draft, expected):
    executor = _executor(autonomous=False, ui_draft=ui_draft)
    sent: list[str | None] = []

    async def _fake(self, method, url, **kwargs):
        sent.append((kwargs.get("headers") or {}).get("opencti-draft-id"))
        return httpx.Response(200, text="hello", headers={"content-type": "text/plain"}, request=httpx.Request(method, url))

    with patch.object(httpx.AsyncClient, "request", _fake):
        await executor.execute_tool(
            ToolCall(id="d", name="download_opencti_file", arguments={"file_id": "import/global/a.txt"})
        )
    assert sent == [expected]


@pytest.mark.asyncio
async def test_a_freshly_minted_token_refused_names_the_identity_not_the_token():
    """A re-minted token refused again is no expired token: OpenCTI knows no user with the person's email, and
    re-authorizing would change nothing (a registered platform's integration matches people by email)."""
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        return _jwt(1800)

    headers = _headers(_jwt(600), draft=None, refresh_fn=_remint)
    body = _error_body("You must be logged in to do this.", "AUTH_REQUIRED")
    _data, err, seen = await _run_graphql(headers, lambda n, _h: (body, 200))
    assert remints["n"] == 1 and len(seen) == 2
    assert "OpenCTI refused the identity XTM One vouched for" in err
    assert "same email" in err
    assert "re-authorize" not in err
    assert is_error_shaped_result(err)
