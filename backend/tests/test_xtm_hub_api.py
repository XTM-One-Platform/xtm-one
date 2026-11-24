"""Tests for the schema-driven XTM Hub tools (``xtm_hub_api``).

The contract: every query and mutation of the vendored Hub schema is a tool,
a curated tool of ``xtm_hub.py`` or not exposed with a reason; every tool
reads or writes as its name says. Calls run through ``xtm_hub.build_tools``
against an ``httpx.MockTransport`` Hub, so the token checks and the request
hardening of ``xtm_hub_graphql`` run for real.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.api.v1.integration_catalog import build_dummy_token
from app.core.egress import egress_client
from app.integrations.providers import xtm_hub as hub
from app.integrations.providers import xtm_hub_api as api
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall

_BASE = "https://hub.example.com"
_TOKEN = "xhp_" + "a" * 43


def _answer(payload: Any) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        stream=httpx.ByteStream(json.dumps(payload).encode()),
    )


class _Hub:
    """A fake Hub: the capability probe answers API_TOKENS, each operation its stub."""

    def __init__(self, answers: dict[str, Any], errors: list[dict[str, Any]] | None = None):
        self.answers = answers
        self.errors = errors
        self.calls: list[dict[str, Any]] = []
        self.multipart: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("content-type", "").startswith("multipart/"):
            self.multipart.append(request)
            body = request.content.decode("latin-1")
            operations = json.loads(body.split('name="operations"', 1)[1].split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0])
        else:
            operations = json.loads(request.content)
        query = operations["query"]
        if "XtmOneHubCapabilities" in query:
            return _answer({"data": {"settings": {"capabilities": ["API_TOKENS"]}}})
        op = query.split("XtmOneHub_", 1)[1].split("(", 1)[0].split(" ", 1)[0]
        self.calls.append({"op": op, "query": query, "variables": operations["variables"], "headers": request.headers})
        if op not in self.answers:
            return _answer({"errors": [{"message": f"no stub for {op}"}]})
        payload: dict[str, Any] = {"data": {op: self.answers[op]}}
        if self.errors:
            payload["errors"] = self.errors
        return _answer(payload)


_Install = Callable[..., _Hub]


@pytest.fixture
def fake_hub(monkeypatch: pytest.MonkeyPatch) -> _Install:
    def install(answers: dict[str, Any], errors: list[dict[str, Any]] | None = None) -> _Hub:
        fake = _Hub(answers, errors)

        def factory(**kwargs: Any) -> httpx.AsyncClient:
            return egress_client(transport=httpx.MockTransport(fake), **kwargs)

        monkeypatch.setattr("app.integrations.providers.xtm_hub.egress_client", factory)
        return fake

    return install


def _executor(token: str = _TOKEN) -> tuple[CallableToolExecutor, list[Any]]:
    executor = CallableToolExecutor()
    defs = hub.build_tools(f"{_BASE}||{token}", executor)
    return executor, defs


async def _call(executor: CallableToolExecutor, name: str, **arguments: Any) -> str:
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))
    return result.content


# ---------------------------------------------------------------------------
# Contract with the vendored schema
# ---------------------------------------------------------------------------


def test_every_hub_operation_is_a_tool_covered_or_not_exposed_with_a_reason() -> None:
    schema_ops = set(api.root_fields("Query")) | set(api.root_fields("Mutation"))
    placed = set(api.OPERATIONS) | set(api.COVERED) | set(api.NOT_EXPOSED)
    assert schema_ops - placed == set(), "a Hub operation is neither a tool, covered nor not exposed"
    assert placed - schema_ops == set(), "a table names an operation the schema no longer has"
    assert not set(api.OPERATIONS) & set(api.COVERED)
    assert not set(api.OPERATIONS) & set(api.NOT_EXPOSED)
    assert not set(api.COVERED) & set(api.NOT_EXPOSED)
    assert all(reason.strip() for reason in api.NOT_EXPOSED.values())


def test_the_schema_parses_with_every_type_an_operation_names() -> None:
    types = api.schema()
    assert len(api.root_fields("Query")) >= 60
    assert len(api.root_fields("Mutation")) >= 90
    for kind in ("Query", "Mutation"):
        for root in api.root_fields(kind).values():
            assert root.type.named in types or root.type.named in api._BUILTIN_SCALARS, root.name
            for arg in root.args:
                assert arg.type.named in types or arg.type.named in api._BUILTIN_SCALARS, (root.name, arg.name)


def test_tool_names_follow_the_convention_and_read_or_write_as_the_operation() -> None:
    defs = hub.build_tools(build_dummy_token("xtm_hub"), CallableToolExecutor())
    by_name = {d.name: d for d in defs}
    names = [d.name for d in defs]
    assert len(names) == len(set(names)), "a tool name is repeated"
    curated = set(api.COVERED.values()) | {"publish_xtm_hub_resource", "download_xtm_hub_resource"}
    assert curated <= set(names)
    for op, (tool_name, summary) in api.OPERATIONS.items():
        assert "_xtm_hub_" in tool_name and tool_name.islower(), tool_name
        assert summary.endswith("."), tool_name
        expected = "read" if api.is_read(op) else "write"
        assert infer_tool_type(tool_name) == expected, (op, tool_name)
        assert by_name[tool_name].read_only is (expected == "read")
        assert by_name[tool_name].priority == 2


def test_the_only_query_that_writes_is_never_offered_to_an_api_token() -> None:
    assert set(api.WRITE_QUERIES) <= set(api.root_fields("Query"))
    assert not set(api.WRITE_QUERIES) & set(api.OPERATIONS)


def test_system_and_platform_credential_operations_are_never_tools() -> None:
    # The Hub answers them to its own credentials only: a personal API token is
    # always refused, so a tool would only ever fail (the deployed Hub, 20 Nov 2026).
    for op in (
        "deploymentRequests",
        "updateDeploymentRequest",
        "ingestManifestFragments",
        "updateOpenCTIManifest",
        "consumeProvisionedNewsFeedItems",
        "createApiToken",
    ):
        assert op in api.NOT_EXPOSED, op
        assert op not in api.OPERATIONS, op


def test_the_vendored_schema_is_the_api_token_era_hub() -> None:
    types = api.schema()
    assert "currentApiToken" in api.root_fields("Query")
    assert {"read", "read_write", "admin"} <= set(types["ApiTokenScope"].values)
    assert "capabilities" in {f.name for f in types["Settings"].fields}
    document = {f.name for f in types["Document"].fields}
    assert "status" in document and "active" not in document


def test_every_destructive_tool_says_what_it_deletes() -> None:
    for op, (tool_name, summary) in api.OPERATIONS.items():
        if tool_name.startswith("delete_"):
            assert "delete" in summary.lower(), tool_name
        if op.startswith("delete") and not tool_name.startswith("delete_"):
            pytest.fail(f"{op} is not named delete_: {tool_name}")


def test_no_tool_offers_or_selects_a_credential() -> None:
    for kind in ("Query", "Mutation"):
        for op, root in api.root_fields(kind).items():
            if op not in api.OPERATIONS:
                continue
            schema = json.dumps(api.input_schema(op, root))
            assert "password" not in schema.lower(), op
            body = api.selection(root.type.named)
            assert "token" not in body.lower() and "password" not in body.lower(), op


def test_bulk_tools_take_explicit_ids_only() -> None:
    root = api.root_fields("Mutation")["bulkAcceptPendingUserInOrganization"]
    schema = api.input_schema("bulkAcceptPendingUserInOrganization", root)
    inner = schema["properties"]["input"]
    assert list(inner["properties"]) == ["ids"]
    assert inner["required"] == ["ids"]
    assert inner["properties"]["ids"]["minItems"] == 1


def test_paging_and_ordering_arguments_are_optional_with_defaults() -> None:
    root = api.root_fields("Query")["users"]
    schema = api.input_schema("users", root)
    assert schema["required"] == []
    assert schema["properties"]["orderMode"]["enum"] == ["asc", "desc"]
    assert schema["properties"]["first"]["maximum"] == api._MAX_FIRST


def test_a_connection_selects_its_nodes_and_page_info() -> None:
    body = api.selection("UserConnection")
    assert body.startswith("totalCount edges { cursor node { ")
    assert "pageInfo { hasNextPage hasPreviousPage startCursor endCursor }" in body


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


async def test_a_read_sends_the_token_and_defaults_and_quarantines_the_answer(fake_hub: _Install) -> None:
    fake = fake_hub(
        {
            "users": {
                "totalCount": 1,
                "edges": [{"cursor": "c", "node": {"id": "u1", "email": "a@b.c"}}],
                "pageInfo": {"hasNextPage": False},
            }
        }
    )
    executor, _ = _executor()
    out = await _call(executor, "list_xtm_hub_users", searchTerm="ada")
    call = fake.calls[-1]
    assert call["headers"]["authorization"] == f"Bearer {_TOKEN}"
    assert call["variables"] == {"first": 25, "orderBy": "email", "orderMode": "asc", "searchTerm": "ada"}
    assert call["query"].startswith("query XtmOneHub_users(")
    assert "u1" in out and "UNTRUSTED" in out.upper()


async def test_a_write_runs_as_a_mutation_with_its_input(fake_hub: _Install) -> None:
    fake = fake_hub({"editOrganization": {"id": "o1", "name": "Acme", "domains": ["acme.io"]}})
    executor, _ = _executor()
    out = await _call(executor, "update_xtm_hub_organization", id="o1", input={"name": "Acme", "domains": ["acme.io"]})
    call = fake.calls[-1]
    assert call["query"].startswith("mutation XtmOneHub_editOrganization($id: ID!, $input: OrganizationInput!)")
    assert call["variables"] == {"id": "o1", "input": {"name": "Acme", "domains": ["acme.io"]}}
    assert "acme.io" in out


async def test_a_missing_required_argument_is_refused_before_any_request(fake_hub: _Install) -> None:
    fake = fake_hub({})
    executor, _ = _executor()
    out = await _call(executor, "delete_xtm_hub_organization")
    assert out == "Error: id is required."
    assert fake.calls == []


async def test_a_bulk_call_without_ids_is_refused(fake_hub: _Install) -> None:
    fake = fake_hub({})
    executor, _ = _executor()
    out = await _call(executor, "accept_xtm_hub_pending_users", input={"searchTerm": "x"})
    assert "explicit ids only" in out
    assert fake.calls == []


async def test_without_a_token_the_tool_says_it_needs_one(fake_hub: _Install) -> None:
    fake_hub({})
    executor = CallableToolExecutor()
    hub.build_tools(f"{_BASE}||__no_auth__", executor)
    out = await _call(executor, "list_xtm_hub_organizations")
    assert out == hub._NEEDS_HUB_KEY


async def test_a_field_refusal_keeps_the_answer_of_the_root(fake_hub: _Install) -> None:
    fake = fake_hub(
        {
            "organizations": {
                "totalCount": 1,
                "edges": [{"cursor": "c", "node": {"id": "o1", "name": "Acme", "members_count": None}}],
                "pageInfo": {"hasNextPage": False},
            }
        },
        errors=[{"message": "FORBIDDEN_ACCESS", "path": ["organizations", "edges", 0, "node", "members_count"]}],
    )
    executor, _ = _executor()
    out = await _call(executor, "list_xtm_hub_organizations")
    assert fake.calls[-1]["variables"] == {"first": 25, "orderBy": "name", "orderMode": "asc"}
    assert "Acme" in out and not out.startswith("Error")


async def test_a_refusal_names_the_hub_error_code_and_nothing_else(fake_hub: _Install) -> None:
    fake_hub(
        {"organizations": None},
        errors=[{"message": "FORBIDDEN_ACCESS", "extensions": {"code": "FORBIDDEN_ACCESS"}}],
    )
    executor, _ = _executor()
    out = await _call(executor, "list_xtm_hub_organizations")
    assert out.startswith(hub._REFUSED)
    assert "Hub error: FORBIDDEN_ACCESS." in out and "BYPASS" in out


async def test_credential_fields_are_dropped_from_an_answer(fake_hub: _Install) -> None:
    fake_hub(
        {
            "deploymentRequestsList": {
                "totalCount": 1,
                "edges": [{"node": {"id": "d1", "platform_token": "secret-value"}}],
            }
        }
    )
    executor, _ = _executor()
    out = await _call(executor, "list_xtm_hub_deployment_queue")
    assert "d1" in out and "secret-value" not in out


async def test_an_upload_is_sent_as_a_multipart_request(fake_hub: _Install) -> None:
    fake = fake_hub({"uploadUserPicture": {"id": "u1"}})
    executor, _ = _executor()

    class _Files:
        async def resolve_upload(self, file_id: str) -> tuple[bytes, str, str]:
            assert file_id == "file-1"
            return b"\x89PNG", "me.png", "image/png"

    executor.file_context = _Files()  # type: ignore[assignment]
    out = await _call(executor, "upload_xtm_hub_my_picture", document="file-1")
    assert "u1" in out
    request = fake.multipart[-1]
    assert request.headers["apollo-require-preflight"] == "true"
    body = request.content.decode("latin-1")
    assert '"0": ["variables.document"]' in body and "me.png" in body
    assert fake.calls[-1]["variables"] == {"document": None}
