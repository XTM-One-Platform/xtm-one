"""The OpenAEV module health reads of the module checker (#101 S15b-2c,
platform-health.md 13.5): registered, read-only by name and by method, allow-listed,
and answering what the module holds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.providers.openaev import _common as openaev_common
from app.integrations.providers.openaev import build_tools, health_reads
from app.services.health import module_analyst
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"
GET_NAMES = ("get_openaev_platform_health", "list_openaev_expectations_due")
SEARCH_NAMES = ("get_openaev_data_health", "get_openaev_error_rate")
HEALTH_NAMES = GET_NAMES + SEARCH_NAMES
SEARCH_PATHS = {"/api/scenarios/search", "/api/exercises/search", "/api/atomic-testings/search"}


class _Resp:
    def __init__(self, body: Any) -> None:
        self._body = body

    def json(self) -> Any:
        return self._body


class _FakeApi:
    """Answers by path; records every (method, path) the reads send."""

    #: ``GET /api/me``: an admin by default, so the reads keep their full-scope answer.
    ME: tuple[Any, str | None] = ({"user_admin": True}, None)

    def __init__(self, answers: dict[str, tuple[Any, str | None]], me: tuple[Any, str | None] | None = None) -> None:
        self.answers = answers
        self.me = me or self.ME
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict[str, Any]] = []

    async def __call__(
        self, base_url: str, headers: Any, method: str, path: str, action: str, **kwargs: Any
    ) -> tuple[_Resp | None, str | None]:
        if path == "/api/me":
            body, err = self.me
            return (None, err) if err else (_Resp(body), None)
        self.calls.append((method, path))
        if "json" in kwargs:
            self.bodies.append(kwargs["json"])
        body, err = self.answers.get(path, (None, "404 not found"))
        return (None, err) if err else (_Resp(body), None)


async def _call(monkeypatch: pytest.MonkeyPatch, fake: _FakeApi, name: str) -> str:
    monkeypatch.setattr(health_reads, "_api", fake)
    # The read scope (GET /api/me) goes through the shared helper.
    monkeypatch.setattr(openaev_common, "_api", fake)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="c", name=name, arguments={}))
    assert not result.is_error, result.content
    return result.content


def test_the_module_health_reads_are_registered_read_only_and_allow_listed() -> None:
    executor = CallableToolExecutor()
    defs = {t.name: t for t in build_tools(TOKEN, executor)}
    for name in HEALTH_NAMES:
        assert name in defs and name in executor._handlers, name
        assert infer_tool_type(name) == "read", name
        assert module_analyst.refuse_module_tool("openaev", name) is None, name
        assert defs[name].input_schema["properties"] == {}, name
        assert len(defs[name].description) <= 200, name


async def test_every_request_of_the_reads_is_a_get(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi(
        {"/api/settings": ({}, None), "/api/tenant-settings": ({}, None), "/api/injects/expectations": ([], None)}
    )
    for name in GET_NAMES:
        await _call(monkeypatch, fake, name)
    assert fake.calls and all(method == "get" for method, _ in fake.calls)


class _Counts(_FakeApi):
    """Answers every search with ``totalElements`` from ``count(path, filters)``."""

    def __init__(self, count: Any, me: tuple[Any, str | None] | None = None) -> None:
        super().__init__({}, me)
        self.count = count

    async def __call__(
        self, base_url: str, headers: Any, method: str, path: str, action: str, **kwargs: Any
    ) -> tuple[_Resp | None, str | None]:
        if path == "/api/me":
            return await super().__call__(base_url, headers, method, path, action, **kwargs)
        await super().__call__(base_url, headers, method, path, action, **kwargs)
        filters = (kwargs["json"].get("filterGroup") or {}).get("filters", [])
        value = self.count(path, filters)
        return (None, value) if isinstance(value, str) else (_Resp({"totalElements": value, "content": []}), None)


async def test_the_search_reads_post_size_one_searches_with_known_operators(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Counts(lambda path, filters: 3)
    for name in SEARCH_NAMES:
        await _call(monkeypatch, fake, name)
    assert fake.calls and all(m == "post" and p in SEARCH_PATHS for m, p in fake.calls)
    assert all(body["size"] == 1 for body in fake.bodies)
    for body in fake.bodies:
        if "filterGroup" in body:
            assert openaev_common._validate_filter_group(body["filterGroup"]) == ""


async def test_data_health_counts_totals_weekly_updates_and_stuck_simulations(monkeypatch: pytest.MonkeyPatch) -> None:
    def count(path: str, filters: list[dict[str, Any]]) -> int:
        keys = {f["key"]: f for f in filters}
        if "exercise_status" in keys:
            return 2 if keys["exercise_status"]["values"] == ["RUNNING"] else 1
        if not filters:
            return 40
        return 6 if len(filters) == 1 else 9

    out = await _call(monkeypatch, _Counts(count), "get_openaev_data_health")
    assert "Scenarios: total 40; updated last 7 days 6, weekly mean of the 3 weeks before 3.0" in out
    assert "Atomic testings: total 40" in out
    assert "Simulations running over 24 h past their end: 2" in out
    assert "Simulations canceled in the last 24 h: 1" in out
    assert "not exposed by the OpenAEV search API" in out


async def test_data_health_keeps_going_past_a_refused_search(monkeypatch: pytest.MonkeyPatch) -> None:
    def count(path: str, filters: list[dict[str, Any]]) -> Any:
        return "403 forbidden" if path == "/api/scenarios/search" else 5

    out = await _call(monkeypatch, _Counts(count), "get_openaev_data_health")
    assert "Scenarios: 403 forbidden" in out
    assert "Simulations: total 5" in out


async def test_error_rate_counts_errors_per_day_with_the_mean(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[dict[str, Any]]] = []

    def count(path: str, filters: list[dict[str, Any]]) -> int:
        seen.append(filters)
        return len(seen)

    out = await _call(monkeypatch, _Counts(count), "get_openaev_error_rate")
    assert len(seen) == 7
    assert all(f[0] == {"key": "inject_status", "operator": "eq", "values": ["ERROR", "PARTIAL"]} for f in seen)
    assert "7-day daily mean 4.0; today 7" in out
    assert "not exposed by the OpenAEV API" in out
    out = await _call(monkeypatch, _Counts(lambda path, filters: "403 forbidden"), "get_openaev_error_rate")
    assert out == "OpenAEV error rate: 403 forbidden"


# ── The caller's read scope (#1444): OpenAEV answers an empty page, never a 403 ──

LIMITED = ({"user_admin": False, "user_is_admin_or_bypass": False, "user_capabilities": ["ACCESS_DASHBOARDS"]}, None)


async def test_data_health_under_a_grant_limited_identity_says_the_zero_is_not_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = await _call(monkeypatch, _Counts(lambda path, filters: 0, me=LIMITED), "get_openaev_data_health")
    assert out.startswith("Scope: this OpenAEV identity reads only what it holds a grant on")
    assert "Scenarios: total 0 visible to this identity (scope-limited; not proof the platform is empty)" in out
    assert "Simulations running over 24 h past their end: 0 visible to this identity" in out
    assert "total 0;" not in out


async def test_data_health_under_an_admin_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _call(monkeypatch, _Counts(lambda path, filters: 0), "get_openaev_data_health")
    assert out.startswith("OpenAEV data health:")
    assert "Scenarios: total 0; updated last 7 days 0" in out
    assert "Scope:" not in out


@pytest.mark.parametrize(
    "me",
    [
        ({"user_admin": False, "user_capabilities": ["ACCESS_ASSESSMENT"]}, None),
        ({"user_admin": False, "user_is_admin_or_bypass": True, "user_capabilities": []}, None),
    ],
    ids=["access-assessment", "bypass"],
)
async def test_a_capability_that_reads_everything_is_full_scope(
    monkeypatch: pytest.MonkeyPatch, me: tuple[Any, str | None]
) -> None:
    out = await _call(monkeypatch, _Counts(lambda path, filters: 0, me=me), "get_openaev_error_rate")
    assert "Scope:" not in out
    assert "visible to this identity" not in out


async def test_a_failed_me_read_is_an_unknown_scope_never_a_full_one(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Counts(lambda path, filters: 0, me=(None, "OpenAEV error (HTTP 500)"))
    out = await _call(monkeypatch, fake, "get_openaev_error_rate")
    assert out.startswith("Scope: unknown (GET /api/me failed: OpenAEV error (HTTP 500))")
    assert "0 visible to this identity (scope unknown; not proof the platform is empty)" in out


async def test_the_scope_is_read_once_per_tool_set(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[str] = []

    class _Me(_Counts):
        async def __call__(
            self, base_url: str, headers: Any, method: str, path: str, action: str, **kwargs: Any
        ) -> tuple[_Resp | None, str | None]:
            if path == "/api/me":
                reads.append(path)
            return await super().__call__(base_url, headers, method, path, action, **kwargs)

    fake = _Me(lambda path, filters: 0, me=LIMITED)
    monkeypatch.setattr(health_reads, "_api", fake)
    monkeypatch.setattr(openaev_common, "_api", fake)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    for name in SEARCH_NAMES:
        await executor.execute_tool(ToolCall(id="c", name=name, arguments={}))
    assert reads == ["/api/me"]


async def test_platform_health_answers_version_licence_services_and_links(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi(
        {
            "/api/settings": (
                {
                    "platform_version": "3.261116.1",
                    "platform_license": {
                        "license_type": "standard",
                        "license_is_enterprise": True,
                        "license_is_validated": True,
                        "license_is_expired": False,
                        "license_expiration_date": "2027-01-31T00:00:00Z",
                    },
                    "xtm_composer_version": "1.2.0",
                    "xtm_hub_enable": True,
                    "xtm_hub_reachable": False,
                    "smtp_service_available": "true",
                },
                None,
            ),
            "/api/tenant-settings": (
                {"xtm_opencti_enable": True, "xtm_opencti_url": "https://cti.example", "xtm_opencrq_enable": False},
                None,
            ),
        }
    )
    out = await _call(monkeypatch, fake, "get_openaev_platform_health")
    assert "Version: 3.261116.1" in out
    assert "enterprise true, validated true, expired false, expires 2027-01-31T00:00:00Z" in out
    assert "XTM Hub reachable: false" in out
    assert "IMAP available: unknown" in out
    assert "OpenCTI link: enabled true, URL set true, last call result not exposed by OpenAEV" in out
    assert "OpenCRQ link: enabled false, URL set false" in out


async def test_platform_health_keeps_going_past_a_refused_read(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi(
        {"/api/settings": (None, "403 forbidden"), "/api/tenant-settings": ({"xtm_opencti_enable": False}, None)}
    )
    out = await _call(monkeypatch, fake, "get_openaev_platform_health")
    assert "Platform settings: 403 forbidden" in out
    assert "OpenCTI link: enabled false" in out


async def test_expectations_due_count_by_type_overdue_and_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    rows = [
        {
            "inject_expectation_type": "DETECTION",
            "inject_expectation_created_at": (now - timedelta(hours=10)).isoformat(),
            "inject_expiration_time": 3600,
        },
        {
            "inject_expectation_type": "PREVENTION",
            "inject_expectation_created_at": (now - timedelta(minutes=5)).isoformat(),
            "inject_expiration_time": 3600,
        },
        {"inject_expectation_type": "MANUAL"},
    ]
    fake = _FakeApi({"/api/injects/expectations": (rows, None)})
    out = await _call(monkeypatch, fake, "list_openaev_expectations_due")
    assert "3 (DETECTION 1, MANUAL 1, PREVENTION 1)" in out
    assert "1 past their expiration time" in out
    assert "oldest created " + (now - timedelta(hours=10)).isoformat()[:16] in out


async def test_expectations_due_none_and_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _call(monkeypatch, _FakeApi({"/api/injects/expectations": ([], None)}), "list_openaev_expectations_due")
    assert out == "OpenAEV expectations due: none waiting for a result."
    out = await _call(monkeypatch, _FakeApi({}), "list_openaev_expectations_due")
    assert out == "OpenAEV expectations due: 404 not found"
