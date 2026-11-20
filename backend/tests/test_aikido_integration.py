"""Aikido Security provider: credential, auth, paging, rate limit, exports, webhooks, coverage."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import Counter

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.integrations.providers import aikido
from app.integrations.providers.aikido import _client, _tools
from app.integrations.providers.aikido._client import (
    REGIONS,
    AikidoClient,
    build_aikido_token,
    parse_aikido_token,
    token_from_config,
)
from app.integrations.providers.aikido.webhooks import EVENT_TYPES, verify_signature
from app.services.integration_read_only import integration_tool_access, withhold_write_tools
from app.services.integration_tools import PROVIDER_TOOLS
from app.tools.base import CallableToolExecutor

TOKEN = build_aikido_token("us", "cid", "csecret")

#: The 28 sections of the public API reference, with their operation count (20 Nov 2026).
SECTIONS = {
    "Containers": 26,
    "Issues": 22,
    "Code repositories": 20,
    "Zen": 17,
    "Autofix": 13,
    "Domains & APIs": 12,
    "Teams": 11,
    "Compliance": 9,
    "Task tracking": 9,
    "Clouds": 8,
    "Pull & Merge requests": 8,
    "Endpoint Protection": 8,
    "Users": 7,
    "Reports": 6,
    "Research": 6,
    "Custom SAST rules": 5,
    "Pentesting & AI code analysis": 5,
    "Workspace": 5,
    "Licenses & SBOM": 3,
    "Code quality": 3,
    "Webhooks": 3,
    "Broker": 3,
    "Virtual machines": 2,
    "Code coverage": 2,
    "Request inbox": 2,
    "Local scanning": 1,
    "Scanning access tokens": 1,
    "Bug bounty": 1,
}


class _Recorder:
    """An httpx transport: the token endpoint, then API answers by path (a list is served in order)."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth/token":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"bearer-{self.token_calls}", "expires_in": 3600})
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/public/v1")
        answer = self.routes.get(path)
        if isinstance(answer, list) and answer and isinstance(answer[0], httpx.Response):
            return answer.pop(0)
        if isinstance(answer, httpx.Response):
            return answer
        if answer is None:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json=answer)


@pytest.fixture
def aikido_api(monkeypatch):
    from app.integrations import oauth_token_grants

    oauth_token_grants.clear_token_cache()

    def _install(routes):
        recorder = _Recorder(routes)
        transport = httpx.MockTransport(recorder)

        def _http(*args, **kwargs):
            kwargs.pop("transport", None)
            return httpx.AsyncClient(*args, transport=transport, **kwargs)

        async def _no_dns(url, *args, **kwargs):
            return None

        monkeypatch.setattr(_client, "egress_client", _http)
        monkeypatch.setattr(oauth_token_grants, "egress_client", _http)
        monkeypatch.setattr(oauth_token_grants, "validate_outbound_url_async", _no_dns)
        return recorder

    yield _install
    oauth_token_grants.clear_token_cache()


def _tools_for(token: str = TOKEN):
    executor = CallableToolExecutor()
    return aikido.build_tools(token, executor), executor


async def _call(executor, name, **kwargs):
    return await executor.handler(name)(**kwargs)


# --- credential ----------------------------------------------------------------


def test_token_round_trip_and_region_hosts():
    creds = parse_aikido_token(TOKEN)
    assert (creds.region, creds.client_id, creds.client_secret) == ("us", "cid", "csecret")
    assert creds.base_url == "https://app.us.aikido.dev/api/public/v1"
    assert creds.token_url == "https://app.us.aikido.dev/api/oauth/token"
    assert set(REGIONS) == {"eu", "us", "au", "me"}


def test_unknown_region_falls_back_to_europe_never_to_a_typed_host():
    creds = parse_aikido_token(token_from_config("s", {"region": "https://evil.example", "client_id": "c"}))
    assert creds.host == "app.aikido.dev"


def test_secret_with_separator_round_trips():
    assert parse_aikido_token(build_aikido_token("eu", "c", "a||b")).client_secret == "a||b"


def test_missing_parts_are_named():
    assert parse_aikido_token(token_from_config("", {})).missing() == ["client ID", "client secret"]


async def test_a_missing_credential_is_named_at_call_time():
    _, executor = _tools_for(build_aikido_token("eu", "", ""))
    answer = await _call(executor, "get_aikido_workspace_info")
    assert "missing its client ID, client secret" in answer


# --- auth ---------------------------------------------------------------------


async def test_client_credentials_bearer_on_the_regional_host(aikido_api):
    api = aikido_api({"/workspace": {"name": "Filigran"}})
    _, executor = _tools_for()
    answer = await _call(executor, "get_aikido_workspace_info")
    assert "Filigran" in answer
    request = api.requests[0]
    assert request.url.host == "app.us.aikido.dev"
    assert request.headers["authorization"] == "Bearer bearer-1"


async def test_token_is_cached_across_calls(aikido_api):
    api = aikido_api({"/workspace": {"name": "W"}})
    _, executor = _tools_for()
    await _call(executor, "get_aikido_workspace_info")
    await _call(executor, "get_aikido_workspace_info")
    assert api.token_calls == 1


async def test_a_401_mints_a_new_token_once(aikido_api):
    api = aikido_api({"/workspace": [httpx.Response(401, json={}), httpx.Response(200, json={"name": "W"})]})
    _, executor = _tools_for()
    answer = await _call(executor, "get_aikido_workspace_info")
    assert "W" in answer
    assert api.token_calls == 2
    assert api.requests[1].headers["authorization"] == "Bearer bearer-2"


async def test_validate_connection_reads_the_workspace(aikido_api):
    aikido_api({"/workspace": {"name": "Acme"}})
    ok, message = await aikido.validate_connection(TOKEN)
    assert ok
    assert "Acme" in message
    assert "csecret" not in message


async def test_validate_connection_names_missing_parts():
    ok, message = await aikido.validate_connection(build_aikido_token("eu", "", ""))
    assert not ok
    assert message == "Missing client ID, client secret."


# --- requests -----------------------------------------------------------------


async def test_path_and_query_parameters_and_list_values(aikido_api):
    api = aikido_api({"/clouds/assets": []})
    _, executor = _tools_for()
    await _call(executor, "get_aikido_cloud_assets", provider=["aws", "gcp"], region=["eu-west-1"])
    query = api.requests[0].url.params
    assert query.get_list("provider[]") == ["aws", "gcp"]
    assert query.get_list("region[]") == ["eu-west-1"]


async def test_a_path_segment_is_encoded(aikido_api):
    api = aikido_api({"/issues/12%2F3": {}})
    _, executor = _tools_for()
    await _call(executor, "get_aikido_issue_detail", issue_id="12/3")
    assert api.requests[0].url.raw_path.endswith(b"/issues/12%2F3")


async def test_a_json_body_is_spread_into_arguments(aikido_api):
    api = aikido_api({"/issues/7/snooze": {}})
    _, executor = _tools_for()
    answer = await _call(executor, "snooze_aikido_issue", issue_id=7, snooze_until=1800000000, reason="later")
    request = api.requests[0]
    assert request.method == "PUT"
    assert json.loads(request.content) == {"snooze_until": 1800000000, "reason": "later"}
    assert "Snooze an issue" in answer


async def test_a_non_object_body_is_one_body_argument(aikido_api):
    api = aikido_api({"/firewall/apps/3/bot-lists": {}})
    names = {op["name"]: op for op in _tools.operations()}
    op = names["update_aikido_bot_lists"]
    _, executor = _tools_for()
    path_args = {p["name"]: 3 for p in op["params"] if p["in"] == "path"}
    await _call(executor, "update_aikido_bot_lists", body=[{"code": "ai", "mode": "block"}], **path_args)
    assert json.loads(api.requests[0].content) == [{"code": "ai", "mode": "block"}]


async def test_secrets_are_redacted_from_answers(aikido_api):
    aikido_api({"/firewall/apps/5/token": {"token": "AIK_RUNTIME_SECRET", "id": 5}})
    _, executor = _tools_for()
    op = next(op for op in _tools.operations() if op["name"] == "rotate_aikido_app_token")
    args = {p["name"]: 5 for p in op["params"] if p["in"] == "path"}
    answer = await _call(executor, "rotate_aikido_app_token", **args)
    assert "AIK_RUNTIME_SECRET" not in answer
    assert "[redacted]" in answer


async def test_an_api_error_is_a_clear_message(aikido_api):
    aikido_api({"/issues/1": httpx.Response(403, json={"message": "missing scope issues:read"})})
    _, executor = _tools_for()
    answer = await _call(executor, "get_aikido_issue_detail", issue_id=1)
    assert answer.startswith("Error:")
    assert "403" in answer or "missing scope" in answer


# --- pagination ---------------------------------------------------------------


async def test_one_page_names_the_next(aikido_api):
    aikido_api({"/open-issue-groups": httpx.Response(200, json=[{"id": 1}], headers={"X-Has-Next-Page": "true"})})
    _, executor = _tools_for()
    answer = await _call(executor, "list_aikido_open_issue_groups", page=0)
    assert "next page 1" in answer


async def test_all_pages_reads_until_the_last(aikido_api):
    api = aikido_api(
        {
            "/open-issue-groups": [
                httpx.Response(200, json=[{"id": 1}], headers={"X-Has-Next-Page": "true"}),
                httpx.Response(200, json=[{"id": 2}], headers={"X-Has-Next-Page": "false"}),
            ]
        }
    )
    _, executor = _tools_for()
    answer = await _call(executor, "list_aikido_open_issue_groups", all_pages=True)
    assert "(2 rows)" in answer
    assert [r.url.params["page"] for r in api.requests] == ["0", "1"]
    assert api.requests[0].url.params["per_page"] == "100"


async def test_all_pages_cap_names_the_next_unread_page(aikido_api, monkeypatch):
    monkeypatch.setattr(_tools, "MAX_PAGES", 2)
    api = aikido_api(
        {
            "/open-issue-groups": [
                httpx.Response(200, json=[{"id": 1}], headers={"X-Has-Next-Page": "true"}),
                httpx.Response(200, json=[{"id": 2}], headers={"X-Has-Next-Page": "true"}),
            ]
        }
    )
    _, executor = _tools_for()
    answer = await _call(executor, "list_aikido_open_issue_groups", all_pages=True)
    assert [r.url.params["page"] for r in api.requests] == ["0", "1"]
    assert "(2 rows; more from page 2)" in answer


# --- rate limit ---------------------------------------------------------------


async def test_a_429_is_retried_after_retry_after(aikido_api, monkeypatch):
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    import app.integrations.http as http_mod

    monkeypatch.setattr(http_mod.asyncio, "sleep", _sleep)
    aikido_api(
        {
            "/workspace": [
                httpx.Response(429, json={}, headers={"Retry-After": "2"}),
                httpx.Response(200, json={"name": "W"}),
            ]
        }
    )
    _, executor = _tools_for()
    answer = await _call(executor, "get_aikido_workspace_info")
    assert "W" in answer
    assert any(s >= 2 for s in slept)


def test_one_shared_bucket_per_api_client_at_twenty_calls_a_minute():
    first = AikidoClient(parse_aikido_token(TOKEN))._limiter()
    again = AikidoClient(parse_aikido_token(TOKEN))._limiter()
    other = AikidoClient(parse_aikido_token(build_aikido_token("us", "other", "s")))._limiter()
    assert first is again
    assert first is not other
    assert first._rate == pytest.approx(20 / 60)


# --- exports ------------------------------------------------------------------


async def test_a_csv_export_is_a_file(aikido_api):
    aikido_api(
        {"/issues/export": httpx.Response(200, content=b"id,severity\n1,high\n", headers={"content-type": "text/csv"})}
    )
    stored = {}

    async def _persist(name, data, content_type):
        stored.update(name=name, data=data, content_type=content_type)
        return None

    executor = CallableToolExecutor()
    aikido.build_tools(TOKEN, executor, persist_file_fn=_persist)
    await _call(executor, "export_aikido_issues", format="csv")
    assert stored["data"].startswith(b"id,severity")
    assert stored["content_type"] == "text/csv"
    assert stored["name"].endswith(".csv")


async def test_the_pdf_report_is_a_file(aikido_api):
    aikido_api(
        {"/report/export/pdf": httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})}
    )
    stored = {}

    async def _persist(name, data, content_type):
        stored.update(name=name, data=data, content_type=content_type)
        return None

    executor = CallableToolExecutor()
    aikido.build_tools(TOKEN, executor, persist_file_fn=_persist)
    await _call(executor, "export_aikido_report_pdf", included_sections="soc2")
    assert stored["data"] == b"%PDF-1.7"
    assert stored["content_type"] == "application/pdf"


# --- webhooks -----------------------------------------------------------------


SIGNING_KEY = "whsec"


def _signed(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    return body, hmac.new(SIGNING_KEY.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_signature_accepts_an_authentic_fresh_delivery():
    body, sig = _signed({"event_type": "issue.open.created", "dispatched_at": 1000})
    assert verify_signature(body, "whsec", sig, now=1010) == (True, "")


def test_webhook_signature_accepts_a_relay_reserialized_body():
    payload = {"event_type": "issue.closed", "dispatched_at": 1000}
    _, sig = _signed(payload)
    pretty = json.dumps(payload, indent=2).encode()
    assert verify_signature(pretty, "whsec", sig, now=1000)[0]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda b, s: (b, "0" * 64), "signature mismatch"),
        (lambda b, s: (b, ""), "missing"),
        (lambda b, s: (b.replace(b"open", b"shut"), s), "signature mismatch"),
    ],
)
def test_webhook_signature_refuses_forgeries(mutate, reason):
    body, sig = _signed({"event_type": "issue.open.created", "dispatched_at": 1000})
    body, sig = mutate(body, sig)
    ok, why = verify_signature(body, "whsec", sig, now=1000)
    assert not ok
    assert reason in why


def test_webhook_signature_refuses_a_replay_past_thirty_seconds():
    body, sig = _signed({"event_type": "issue.closed", "dispatched_at": 1000})
    ok, why = verify_signature(body, "whsec", sig, now=1031)
    assert not ok
    assert "30 seconds" in why


def test_webhook_signature_refuses_a_payload_without_dispatched_at():
    body, sig = _signed({"event_type": "issue.closed"})
    assert verify_signature(body, "whsec", sig, now=0) == (False, "payload carries no dispatched_at")


def test_every_subscribable_event_is_a_trigger_with_filter_keys():
    add = next(op for op in _tools.operations() if op["name"] == "add_aikido_webhook")
    documented = set(add["body"]["schema"]["properties"]["event_type"]["enum"])
    assert documented == set(EVENT_TYPES)
    events = PROVIDER_CATALOG["aikido"]["available_events"]
    assert {e["event"] for e in events} == documented
    assert all(e["mechanism"] == "webhook" for e in events)
    assert all({"event_type", "payload.severity"} <= set(e["filter_keys"]) for e in events)


# --- registry, coverage and read-only ---------------------------------------


def test_registered_and_introspectable_with_the_catalog_dummy():
    assert PROVIDER_TOOLS["aikido"] is aikido.build_tools
    defs = PROVIDER_TOOLS["aikido"](build_dummy_token("aikido"), CallableToolExecutor())
    assert len(defs) == len(_tools.operations()) == 218


def test_every_documented_section_and_operation_is_covered():
    assert Counter(op["section"] for op in _tools.operations()) == SECTIONS
    ids = [op["operation_id"] for op in _tools.operations()]
    assert len(ids) == len(set(ids))


def test_tool_names_are_unique_prefixed_and_bounded():
    defs, executor = _tools_for()
    names = [d.name for d in defs]
    assert len(names) == len(set(names))
    assert all("_aikido_" in n or n.endswith("_aikido") for n in names)
    assert all(len(n) <= 64 for n in names)
    assert all(executor.handler(n) is not None for n in names)


def test_every_get_is_a_read_and_every_other_method_a_write():
    for op in _tools.operations():
        expected = "read" if op["method"] == "GET" else "write"
        assert integration_tool_access(op["name"]) == expected, op["name"]


def test_a_read_only_binding_withholds_every_write():
    defs, executor = _tools_for()
    kept = withhold_write_tools(defs, executor, integration_label="Aikido")
    kept_names = {d.name for d in kept}
    gets = {op["name"] for op in _tools.operations() if op["method"] == "GET"}
    assert kept_names == gets


def test_destructive_operations_ask_a_person():
    defs, _ = _tools_for()
    asking = {d.name for d in defs if d.always_ask}
    assert {
        "delete_aikido_code_repo",
        "remove_aikido_webhook",
        "rotate_aikido_app_token",
        "disable_aikido_user",
        "delete_aikido_team",
    } <= asking
    assert all(integration_tool_access(n) == "write" for n in asking)


def test_schemas_are_closed_and_path_parameters_required():
    defs, _ = _tools_for()
    by_name = {d.name: d for d in defs}
    for op in _tools.operations():
        schema = by_name[op["name"]].input_schema
        assert schema["additionalProperties"] is False
        for param in op["params"]:
            if param["in"] == "path":
                assert param["name"] in schema.get("required", []), (op["name"], param["name"])


def test_catalog_entry_shape():
    entry = PROVIDER_CATALOG["aikido"]
    assert entry["logo"] == "/logos/aikido.svg"
    assert entry["fields"] == ["api_key"]
    assert {"region", "client_id", "webhook_secret"} <= set(entry["extra_fields"])
    assert {o["value"] for o in entry["extra_field_options"]["region"]} == set(REGIONS)
