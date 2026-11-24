"""QuotaPath provider: auth, paging, every operation, writes gated, derived reads, events, catalog."""

from __future__ import annotations

import json

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.integrations.providers import quotapath
from app.integrations.providers.quotapath import _client, _tools, events
from app.services.integration_read_only import integration_tool_access, withhold_write_tools
from app.services.integration_tools import PROVIDER_TOOLS
from app.tools.base import CallableToolExecutor

KEY = "qp-secret-key"

#: The operations of the published OpenAPI document (https://api.quotapath.com/docs/, 21 Nov 2026).
DOCUMENTED = {
    ("GET", "/user/"),
    ("GET", "/team/"),
    ("GET", "/plan/"),
    ("GET", "/deal/"),
    ("POST", "/deal/"),
    ("POST", "/deal/bulk/"),
    ("GET", "/deal/{uuid}/"),
    ("PUT", "/deal/{uuid}/"),
    ("PATCH", "/deal/{uuid}/"),
    ("DELETE", "/deal/{uuid}/"),
    ("GET", "/path/{path_uuid}/quota/assignments/"),
    ("POST", "/path/{path_uuid}/quota/assignments/"),
    ("GET", "/path/{path_uuid}/quota/values/"),
    ("POST", "/path/{path_uuid}/quota/values/"),
    ("GET", "/payout/"),
    ("GET", "/payout/resolved/"),
    ("GET", "/payout/unresolved/"),
    ("POST", "/data/bulk-import/"),
}

#: Recorded answers, shaped as the OpenAPI schemas (ExternalUser, ExternalTeam, ExternalPayout, ExternalUnresolved).
USERS = {
    "count": 2,
    "next": None,
    "previous": None,
    "results": [
        {"id": "u-1", "name": "Ada Rep", "email": "ada@example.com"},
        {"id": "u-2", "name": "Bob Rep", "email": "bob@example.com"},
    ],
}
TEAMS = {
    "count": 1,
    "next": None,
    "previous": None,
    "results": [
        {
            "id": 7,
            "name": "EMEA",
            "current_members": [{"email": "ada@example.com"}],
            "current_managers": [{"email": "m@example.com"}],
        },
    ],
}
PAYOUTS = {
    "count": 3,
    "next": None,
    "previous": None,
    "results": [
        {
            "id": "p-1",
            "target_user_name": "Ada Rep",
            "target_user_email": "ada@example.com",
            "plan_name": "AE 2026",
            "path_name": "New ARR",
            "deal_uuid": "d-1",
            "date": "2026-07-15",
            "value": 1000.0,
            "updated_ts": 100,
        },
        {
            "id": "p-2",
            "target_user_name": "Ada Rep",
            "target_user_email": "ada@example.com",
            "plan_name": "AE 2026",
            "path_name": "New ARR",
            "deal_uuid": "d-2",
            "date": "2026-08-01",
            "value": 500.5,
            "updated_ts": 101,
        },
        {
            "id": "p-3",
            "target_user_name": "Bob Rep",
            "target_user_email": "bob@example.com",
            "plan_name": "AE 2026",
            "path_name": "New ARR",
            "deal_uuid": "d-3",
            "date": "2026-10-02",
            "value": 200.0,
            "updated_ts": 102,
        },
    ],
}


class _Recorder:
    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1")
        answer = self.routes.get(f"{request.method} {path}", self.routes.get(path))
        if isinstance(answer, list) and answer and isinstance(answer[0], httpx.Response):
            return answer.pop(0)
        if isinstance(answer, httpx.Response):
            return answer
        if answer is None:
            return httpx.Response(404, json={"detail": "Not found."})
        return httpx.Response(200, json=answer)


@pytest.fixture
def quotapath_api(monkeypatch):
    def _install(routes):
        recorder = _Recorder(routes)
        transport = httpx.MockTransport(recorder)

        def _http(*args, **kwargs):
            kwargs.pop("transport", None)
            return httpx.AsyncClient(*args, transport=transport, **kwargs)

        monkeypatch.setattr(_client, "egress_client", _http)
        return recorder

    return _install


def _tools_for(key: str = KEY, persist_file_fn=None):
    executor = CallableToolExecutor()
    return quotapath.build_tools(key, executor, persist_file_fn=persist_file_fn), executor


async def _call(executor, name, **kwargs):
    return await executor.handler(name)(**kwargs)


# --- auth and paging -------------------------------------------------------------


async def test_token_auth_and_the_key_is_never_echoed(quotapath_api):
    recorder = quotapath_api({"/user/": USERS})
    ok, message = await quotapath.validate_connection(KEY)
    assert ok and "2 user(s)" in message
    assert KEY not in message
    request = recorder.requests[0]
    assert request.headers["authorization"] == f"Token {KEY}"
    assert request.url.host == "api.quotapath.com"
    assert request.url.params["limit"] == "1"


async def test_a_rejected_key_fails_the_connection_without_echoing_it(quotapath_api):
    quotapath_api({"/user/": httpx.Response(401, json={"detail": "Invalid token."})})
    ok, message = await quotapath.validate_connection(KEY)
    assert not ok
    assert KEY not in message


async def test_a_missing_key_is_named_at_call_time():
    _, executor = _tools_for("")
    assert "no API key" in await _call(executor, "list_quotapath_users")


async def test_all_pages_follows_limit_offset(quotapath_api):
    page1 = {"count": 3, "next": "x", "results": [{"id": 1}, {"id": 2}]}
    page2 = {"count": 3, "next": None, "results": [{"id": 3}]}
    recorder = quotapath_api({"/deal/": [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]})
    _, executor = _tools_for()
    answer = await _call(executor, "list_quotapath_deals", all_pages=True, user_email="ada@example.com")
    assert "3 rows" in answer
    assert [r.url.params["offset"] for r in recorder.requests] == ["0", "2"]
    assert all(r.url.params["user_email"] == "ada@example.com" for r in recorder.requests)


# --- operations -----------------------------------------------------------------


def test_every_documented_operation_has_one_tool():
    assert {(op.method, op.path) for op in _tools.operations()} == DOCUMENTED
    assert len({op.name for op in _tools.operations()}) == len(DOCUMENTED)


def test_registered_and_introspectable_with_the_catalog_dummy():
    assert PROVIDER_TOOLS["quotapath"] is quotapath.build_tools
    defs = PROVIDER_TOOLS["quotapath"](build_dummy_token("quotapath"), CallableToolExecutor())
    assert len(defs) == len(DOCUMENTED) + 3


def test_read_or_write_follows_the_http_method_and_writes_always_ask():
    defs, _ = _tools_for()
    by_name = {d.name: d for d in defs}
    for op in _tools.operations():
        expected = "read" if op.method == "GET" else "write"
        assert integration_tool_access(op.name) == expected, op.name
        assert by_name[op.name].always_ask is (op.method != "GET"), op.name
    for derived in ("get_quotapath_commission_totals", "export_quotapath_payouts", "export_quotapath_deals"):
        assert integration_tool_access(derived) == "read"
        assert by_name[derived].always_ask is False


def test_a_read_only_binding_withholds_every_write():
    defs, executor = _tools_for()
    kept = {d.name for d in withhold_write_tools(defs, executor, integration_label="QuotaPath")}
    assert not kept & {op.name for op in _tools.operations() if op.write}
    assert {op.name for op in _tools.operations() if not op.write} <= kept


def test_schemas_are_closed_and_path_parameters_required():
    defs, _ = _tools_for()
    by_name = {d.name: d for d in defs}
    for op in _tools.operations():
        schema = by_name[op.name].input_schema
        assert schema["additionalProperties"] is False
        for param in op.path_params:
            assert param in schema["required"], (op.name, param)


async def test_a_write_sends_its_json_body_and_path(quotapath_api):
    recorder = quotapath_api({"PATCH /deal/d-1/": {"id": "d-1", "state": "closed_won"}})
    _, executor = _tools_for()
    answer = await _call(executor, "update_quotapath_deal_fields", uuid="d-1", state="closed_won")
    assert "closed_won" in answer
    request = recorder.requests[0]
    assert request.method == "PATCH"
    assert json.loads(request.content) == {"state": "closed_won"}


async def test_a_delete_answers_done(quotapath_api):
    quotapath_api({"DELETE /deal/d-1/": httpx.Response(204)})
    _, executor = _tools_for()
    assert "done" in await _call(executor, "delete_quotapath_deal", uuid="d-1")


async def test_quota_values_take_the_documented_free_body(quotapath_api):
    recorder = quotapath_api({"POST /path/p-9/quota/values/": {"ok": True}})
    _, executor = _tools_for()
    await _call(executor, "set_quotapath_quota_values", path_uuid="p-9", body={"value": 100000})
    assert json.loads(recorder.requests[0].content) == {"value": 100000}


async def test_payouts_pass_updated_since(quotapath_api):
    recorder = quotapath_api({"/payout/resolved/": PAYOUTS})
    _, executor = _tools_for()
    await _call(executor, "list_quotapath_resolved_payouts", updated_since=1700000000)
    assert recorder.requests[0].url.params["updated_since"] == "1700000000"


# --- derived reads -----------------------------------------------------------------


async def test_commission_totals_by_rep_within_a_period(quotapath_api):
    quotapath_api({"/payout/": PAYOUTS})
    _, executor = _tools_for()
    answer = await _call(
        executor, "get_quotapath_commission_totals", group_by="rep", date_from="2026-07-01", date_to="2026-09-30"
    )
    body = json.loads(answer.split("\n", 1)[1])
    assert body["total_value"] == 1500.5
    assert body["groups"] == [{"rep": "Ada Rep <ada@example.com>", "value": 1500.5, "payouts": 2, "deals": 2}]
    assert "bob@example.com" not in answer


async def test_commission_totals_by_team_and_quarter(quotapath_api):
    quotapath_api({"/payout/": PAYOUTS, "/team/": TEAMS})
    _, executor = _tools_for()
    by_team = json.loads((await _call(executor, "get_quotapath_commission_totals", group_by="team")).split("\n", 1)[1])
    assert {g["team"]: g["value"] for g in by_team["groups"]} == {"EMEA": 1500.5, "(no team)": 200.0}
    by_quarter = json.loads(
        (await _call(executor, "get_quotapath_commission_totals", group_by="quarter", team="EMEA")).split("\n", 1)[1]
    )
    assert {g["quarter"]: g["value"] for g in by_quarter["groups"]} == {"2026-Q3": 1500.5}


async def test_commission_totals_refuse_a_bad_argument():
    _, executor = _tools_for()
    assert "group_by is one of" in await _call(executor, "get_quotapath_commission_totals", group_by="country")
    assert "YYYY-MM-DD" in await _call(executor, "get_quotapath_commission_totals", date_from="July")


async def test_payout_export_is_a_csv_file(quotapath_api):
    quotapath_api({"/payout/unresolved/": PAYOUTS})
    stored: dict = {}

    async def persist(filename, data, content_type):
        stored.update(data=data, filename=filename, content_type=content_type)
        return None

    _, executor = _tools_for(persist_file_fn=persist)
    await _call(executor, "export_quotapath_payouts", status="unresolved")
    assert stored["filename"].startswith("quotapath-unresolved-payouts-")
    assert stored["content_type"] == "text/csv"
    lines = stored["data"].decode().splitlines()
    assert lines[0].startswith("id,target_user_name") and len(lines) == 4


# --- events ---------------------------------------------------------------------


def test_events_are_polled_and_listed_in_the_catalog():
    catalog_events = PROVIDER_CATALOG["quotapath"]["available_events"]
    assert {e["event"] for e in catalog_events} == set(events.EVENTS)
    assert all(e["mechanism"] == "polling" for e in catalog_events)


def test_a_payout_event_names_the_rep_and_is_keyed_by_its_update():
    row = PAYOUTS["results"][0]
    payload = events.payout_event(row, "payout_updated")
    assert payload["message_id"] == "quotapath:payout_updated:p-1:100"
    assert payload["entity"]["rep_email"] == "ada@example.com"
    assert events.payout_event({**row, "updated_ts": 200}, "payout_updated")["message_id"] != payload["message_id"]


async def test_list_payouts_reads_from_the_cursor(quotapath_api):
    recorder = quotapath_api({"/payout/": PAYOUTS, "/payout/unresolved/": PAYOUTS})
    rows, err = await events.list_payouts(KEY, "payout_updated", 1234)
    assert err is None and len(rows) == 3
    assert recorder.requests[0].url.params["updated_since"] == "1234"
    await events.list_payouts(KEY, "payout_unresolved", 1234)
    assert "updated_since" not in recorder.requests[-1].url.params


class _Redis:
    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value


async def test_the_poller_starts_from_now_then_reads_from_its_cursor(monkeypatch):
    from types import SimpleNamespace

    from app.services import event_poller

    seen: dict = {}

    async def _list(api_key, event_type, since):
        seen.update(api_key=api_key, event_type=event_type, since=since)
        return list(PAYOUTS["results"]), None

    monkeypatch.setattr(events, "list_payouts", _list)
    wf = SimpleNamespace(id="wf-1")
    redis = _Redis()
    assert await event_poller._poll_quotapath_payouts(KEY, "payout_resolved", wf, redis) == []
    assert redis.values["event_cursor:wf-1"]
    assert not seen

    redis.values["event_cursor:wf-1"] = b"1700000000"
    polled = await event_poller._poll_quotapath_payouts(KEY, "payout_resolved", wf, redis)
    assert seen == {"api_key": KEY, "event_type": "payout_resolved", "since": 1700000000}
    assert [e["entity"]["id"] for e in polled] == ["p-1", "p-2", "p-3"]
    assert int(redis.values["event_cursor:wf-1"]) > 1700000000


def test_the_poller_routes_every_quotapath_event():
    from app.services import event_poller

    for event in events.EVENTS:
        assert event_poller.POLL_INTERVALS[event] == events.POLL_INTERVAL_SECONDS


# --- catalog --------------------------------------------------------------------


def test_catalog_entry_shape():
    entry = PROVIDER_CATALOG["quotapath"]
    assert entry["logo"] == "/logos/quotapath.svg"
    assert entry["fields"] == ["api_key"]
    assert entry["auth_type"] == "api_key"
    assert "people data" in entry["description"]
    for permission in entry["setup_guide"]["permissions"]:
        assert len(permission["scope"]) <= 40
