"""Tests for the Sublime Security integration provider.

Covers base-URL resolution, header building, compound-token parsing, tool
registration/introspection, list/detail tool flows (params, paths, response
formatting), hunt-job polling, input guards, and connectivity validation.

Every API call goes through the shared ``safe_request`` helper, which is
monkeypatched here so no network is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import sublime
from app.integrations.providers.sublime import (
    SublimeIntegration,
    build_sublime_headers,
    build_tools,
    sublime_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://platform.sublime.security||key-abc"


class _StubResponse:
    """Minimal ``httpx.Response`` stand-in (status_code, content, json, text)."""

    def __init__(
        self,
        payload: Any,
        status_code: int = 200,
        content: bytes = b"{}",
        text: str = "",
    ):
        self._payload = payload
        self.status_code = status_code
        self.content = content
        self.text = text

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        payload = self._router(method, url)
        if isinstance(payload, _StubResponse):
            return payload, None
        return _StubResponse(payload), None


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(sublime, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_sublime_api_base_default_and_normalisation():
    assert sublime_api_base("") == "https://platform.sublime.security"
    assert sublime_api_base("   ") == "https://platform.sublime.security"
    assert (
        sublime_api_base("https://eu.platform.sublime.security/")
        == "https://eu.platform.sublime.security"
    )
    assert (
        sublime_api_base("  https://ca.platform.sublime.security  ")
        == "https://ca.platform.sublime.security"
    )


def test_build_sublime_headers_bearer_and_accept():
    headers = build_sublime_headers("  key-abc \n")
    assert headers["Authorization"] == "Bearer key-abc"
    assert headers["Accept"] == "application/json"


def test_build_tools_empty_key_returns_no_tools():
    executor = CallableToolExecutor()
    assert build_tools(token="https://platform.sublime.security||  ", executor=executor) == []


def test_build_tools_returns_definitions_with_dummy_token():
    """Introspection must not early-return: the dummy token still yields tools.

    The catalog introspects with ``build_dummy_token("sublime")`` (the
    URL-shaped compound dummy) - assert against the exact same token so the
    provider can never silently vanish from the catalog.
    """
    from app.api.v1.integration_catalog import build_dummy_token

    executor = CallableToolExecutor()
    tools = build_tools(token=build_dummy_token("sublime"), executor=executor)
    names = {t.name for t in tools}
    assert "get_sublime_status" in names
    assert "list_sublime_message_groups" in names
    assert "start_sublime_hunt" in names
    assert "analyze_sublime_link" in names
    assert "review_sublime_message_groups" in names
    assert "manage_sublime_rule" in names
    assert "manage_sublime_list" in names
    assert len(tools) == 28
    # Reads (0) sort before writes (1) before destructive (2).
    by_name = {t.name: t for t in tools}
    assert by_name["list_sublime_message_groups"].priority == 0
    assert by_name["review_sublime_message_groups"].priority == 1
    assert by_name["manage_sublime_message_group"].priority == 2
    assert by_name["manage_sublime_rule"].priority == 2
    assert by_name["manage_sublime_list"].priority == 2
    assert all(t.source == "integration:sublime" for t in tools)


# ---------------------------------------------------------------------------
# Message groups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_message_groups_builds_params_and_formats(monkeypatch):
    def router(method, url):
        return {
            "count": 1,
            "total": 3,
            "message_groups": [
                {
                    "id": "grp-1",
                    "state": "flagged",
                    "classification": None,
                    "flagged_rules": [
                        {"id": "r1", "name": "Credential phishing", "tags": []}
                    ],
                    "messages": [
                        {
                            "id": "msg-1",
                            "subject": "Urgent invoice",
                            "sender": {"email": "attacker@evil.example"},
                            "mailbox": {"email": "cfo@acme.com", "id": "mb-1"},
                            "recipients": [{"email": "cfo@acme.com"}],
                            "created_at": "2026-07-01T10:00:00Z",
                        }
                    ],
                    "user_reports": [],
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s1",
            name="list_sublime_message_groups",
            arguments={
                "flagged": True,
                "reviewed": False,
                "sender_domain": "evil.example",
                "flagged_rule_severity": "high",
                "created_after": "2026-07-01T00:00:00Z",
            },
        )
    )
    assert not result.is_error
    assert "grp-1" in result.content
    assert "Credential phishing" in result.content
    assert "attacker@evil.example" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/v0/message-groups")
    assert call["params"]["flagged"] is True
    assert call["params"]["reviewed"] is False
    assert call["params"]["sender_domain__is"] == "evil.example"
    assert call["params"]["flagged_rule_severity__is"] == "high"
    assert call["params"]["created_at__gte"] == "2026-07-01T00:00:00Z"


@pytest.mark.asyncio
async def test_search_message_groups_requires_a_criterion(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="s2", name="search_sublime_message_groups", arguments={})
    )
    assert "at least one" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_message_groups_rejects_query_with_attachment_filters(monkeypatch):
    """query is a standalone free-text search - combining it with the
    attachment filters must be rejected client-side (matching the tool
    description) instead of producing a confusing API-side result."""
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="s2b",
            name="search_sublime_message_groups",
            arguments={"query": "invoice", "attachment_sha256": "a" * 64},
        )
    )
    assert "cannot be combined" in result.content
    assert recorder.calls == []


@pytest.mark.parametrize(
    ("tool", "arg"),
    [
        ("get_sublime_message_group", "group_id"),
        ("get_sublime_message", "message_id"),
        ("get_sublime_message_eml", "message_id"),
        ("get_sublime_message_attack_score", "message_id"),
        ("get_sublime_task", "task_id"),
        ("get_sublime_hunt_results", "hunt_job_id"),
        ("get_sublime_rule", "rule_id"),
        ("get_sublime_list", "list_id"),
    ],
)
@pytest.mark.asyncio
async def test_get_by_id_tools_guard_blank_ids(monkeypatch, tool, arg):
    """A blank/whitespace id must short-circuit with a clear error instead of
    requesting a malformed path like ``/v0/messages/``."""
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id=f"g-{tool}", name=tool, arguments={arg: "   "})
    )
    assert f"{arg} is required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_search_message_groups_by_hash_surfaces_page_token(monkeypatch):
    def router(method, url):
        return {
            "message_groups": [
                {
                    "id": "grp-9",
                    "messages": [
                        {
                            "id": "m",
                            "subject": "Payload",
                            "sender": {"email": "a@b.c"},
                            "mailbox": {"email": "x@y.z", "id": "mb"},
                            "recipients": [],
                        }
                    ],
                }
            ],
            "next_page_token": "tok-123",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s3",
            name="search_sublime_message_groups",
            arguments={"attachment_sha256": "a" * 64},
        )
    )
    assert not result.is_error
    assert "grp-9" in result.content
    assert "tok-123" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/v0/message-groups/search")
    assert call["params"]["attachment_sha256"] == "a" * 64


@pytest.mark.asyncio
async def test_get_message_group_encodes_path_and_formats(monkeypatch):
    def router(method, url):
        return {
            "id": "grp 1/..",
            "state": "flagged",
            "review_status": None,
            "flagged_rules": [{"id": "r", "name": "Rule A", "tags": ["ASR"]}],
            "messages": [],
            "user_reports": [
                {
                    "reporter": "user@acme.com",
                    "channel": "o365_report_phishing",
                    "reported_at": "2026-07-02T08:00:00Z",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s4",
            name="get_sublime_message_group",
            arguments={"group_id": "grp 1/.."},
        )
    )
    assert not result.is_error
    assert "Rule A" in result.content
    assert "user@acme.com" in result.content
    # The id must be percent-encoded into a single path segment.
    assert recorder.calls[-1]["url"].endswith("/v0/message-groups/grp%201%2F..")


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_message_eml_truncates(monkeypatch):
    def router(method, url):
        return _StubResponse({}, text="X" * 10000, content=b"raw")

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s5",
            name="get_sublime_message_eml",
            arguments={"message_id": "msg-1", "max_chars": 4000},
        )
    )
    assert not result.is_error
    assert "truncated" in result.content
    assert "10000 chars" in result.content
    assert recorder.calls[-1]["url"].endswith("/v0/messages/msg-1/eml")


@pytest.mark.asyncio
async def test_attack_score_combines_asa_verdict(monkeypatch):
    def router(method, url):
        if url.endswith("/attack_score"):
            return {
                "score": 92.5,
                "graymail_score": 1.0,
                "verdict": "malicious",
                "top_signals": [
                    {"rank": 1, "category": "Sender", "description": "New sender domain"}
                ],
            }
        return {"verdict": "malicious"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s6",
            name="get_sublime_message_attack_score",
            arguments={"message_id": "msg-1"},
        )
    )
    assert not result.is_error
    assert "92.5/100" in result.content
    assert "New sender domain" in result.content
    assert "ASA verdict: malicious" in result.content
    urls = [c["url"] for c in recorder.calls]
    assert urls[0].endswith("/v0/messages/msg-1/attack_score")
    assert urls[1].endswith("/v0/messages/msg-1/asa_verdict")


@pytest.mark.asyncio
async def test_get_task_reports_state_and_error(monkeypatch):
    def router(method, url):
        return {
            "id": "task-1",
            "state": "failed",
            "created_at": "2026-07-01T00:00:00Z",
            "error": "mailbox unavailable",
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="s7", name="get_sublime_task", arguments={"task_id": "task-1"})
    )
    assert not result.is_error
    assert "failed" in result.content
    assert "mailbox unavailable" in result.content


# ---------------------------------------------------------------------------
# Hunt jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_hunt_requires_source_and_range(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="s8",
            name="start_sublime_hunt",
            arguments={"source": "", "range_start_time": "", "range_end_time": ""},
        )
    )
    assert "required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_start_hunt_polls_to_completion_and_returns_results(monkeypatch):
    monkeypatch.setattr(sublime, "_HUNT_POLL_INTERVAL", 0.0)

    def router(method, url):
        if method == "post":
            return _StubResponse({"hunt_job_id": "hunt-1"}, status_code=201)
        if url.endswith("/results"):
            return {
                "total_group_count": 1,
                "message_groups": [
                    {
                        "id": "grp-h",
                        "messages": [
                            {
                                "id": "m",
                                "subject": "Hit",
                                "sender": {"email": "a@b.c"},
                                "mailbox": {"email": "x@y.z", "id": "mb"},
                                "recipients": [],
                            }
                        ],
                    }
                ],
            }
        return {
            "id": "hunt-1",
            "status": "COMPLETED",
            "source": "type.inbound",
            "range_start_time": "2026-06-01T00:00:00Z",
            "range_end_time": "2026-07-01T00:00:00Z",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s9",
            name="start_sublime_hunt",
            arguments={
                "source": "type.inbound",
                "range_start_time": "2026-06-01T00:00:00Z",
                "range_end_time": "2026-07-01T00:00:00Z",
                "triage_flagged": True,
            },
        )
    )
    assert not result.is_error
    assert "COMPLETED" in result.content
    assert "grp-h" in result.content
    post = recorder.calls[0]
    assert post["method"] == "post"
    assert post["url"].endswith("/v0/hunt-jobs")
    assert post["json"]["source"] == "type.inbound"
    assert post["json"]["triage_flagged"] is True
    assert "triage_reported" not in post["json"]


@pytest.mark.asyncio
async def test_hunt_results_in_progress_short_circuits(monkeypatch):
    def router(method, url):
        return {
            "id": "hunt-2",
            "status": "IN_PROGRESS",
            "source": "type.inbound",
            "range_start_time": "2026-06-01T00:00:00Z",
            "range_end_time": "2026-07-01T00:00:00Z",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s10",
            name="get_sublime_hunt_results",
            arguments={"hunt_job_id": "hunt-2"},
        )
    )
    assert not result.is_error
    assert "IN_PROGRESS" in result.content
    # Only the status endpoint was called - never /results.
    assert len(recorder.calls) == 1


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_rules_params_and_pagination_hint(monkeypatch):
    def router(method, url):
        return {
            "count": 2,
            "total": 40,
            "rules": [
                {
                    "id": "r1",
                    "name": "Sendgrid voicemail phish",
                    "severity": "high",
                    "type": "detection",
                    "active": True,
                    "updated_at": "2026-06-30T00:00:00Z",
                },
                {
                    "id": "r2",
                    "name": "HTML smuggling",
                    "severity": "critical",
                    "type": "detection",
                    "active": False,
                    "updated_at": "2026-06-29T00:00:00Z",
                },
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s11",
            name="list_sublime_rules",
            arguments={"search": "phish", "limit": 2},
        )
    )
    assert not result.is_error
    assert "Sendgrid voicemail phish" in result.content
    assert "offset" in result.content  # pagination hint (total 40 > shown 2)
    call = recorder.calls[-1]
    assert call["url"].endswith("/v0/rules")
    assert call["params"]["search"] == "phish"
    assert call["params"]["limit"] == 2


@pytest.mark.asyncio
async def test_validate_rule_reports_validation_error(monkeypatch):
    def router(method, url):
        return {"validation_error": "unknown field 'sndr'"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s12",
            name="validate_sublime_rule",
            arguments={"source": "sndr.email.domain == 'x'"},
        )
    )
    assert not result.is_error
    assert "INVALID" in result.content
    assert "unknown field" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/v0/rules/validate")
    assert call["json"]["name"] == "Draft rule"


@pytest.mark.asyncio
async def test_validate_rule_requires_source(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="s13", name="validate_sublime_rule", arguments={"source": "  "})
    )
    assert "required" in result.content.lower()
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_lists_defaults_entry_type(monkeypatch):
    def router(method, url):
        return {
            "lists": [
                {
                    "id": "l1",
                    "name": "malicious_hashes",
                    "description": "Attachment hashes from TI",
                    "editable": True,
                    "entry_type": "string",
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="s14", name="list_sublime_lists", arguments={})
    )
    assert not result.is_error
    assert "malicious_hashes" in result.content
    assert recorder.calls[-1]["params"]["entry_type"] == "string"


@pytest.mark.asyncio
async def test_get_list_fetches_metadata_and_entries(monkeypatch):
    def router(method, url):
        if url.endswith("/entries"):
            return {"entries": ["a" * 64, "b" * 64]}
        return {
            "id": "l1",
            "name": "malicious_hashes",
            "description": "TI hashes",
            "editable": True,
            "entry_type": "string",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="s15", name="get_sublime_list", arguments={"list_id": "l1"})
    )
    assert not result.is_error
    assert "malicious_hashes" in result.content
    assert "Entries (2):" in result.content
    urls = [c["url"] for c in recorder.calls]
    assert urls[0].endswith("/v0/lists/l1")
    assert urls[1].endswith("/v0/lists/l1/entries")


# ---------------------------------------------------------------------------
# Visibility + enrichment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_user_reports_bracket_filters(monkeypatch):
    def router(method, url):
        return {
            "count": 1,
            "user_reports": [
                {
                    "reporter": "user@acme.com",
                    "channel": "gmail_report_phishing",
                    "reported_at": "2026-07-03T12:00:00Z",
                    "reported_message_canonical_id": "grp-7",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s16",
            name="list_sublime_user_reports",
            arguments={"reported_after": "2026-07-01T00:00:00Z"},
        )
    )
    assert not result.is_error
    assert "grp-7" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/v0/user-reports")
    assert call["params"]["reported_at[gte]"] == "2026-07-01T00:00:00Z"


@pytest.mark.asyncio
async def test_list_email_bombs_formats_review_state(monkeypatch):
    def router(method, url):
        return {
            "count": 1,
            "total": 1,
            "email_bombs": [
                {
                    "id": "bomb-1",
                    "active": True,
                    "message_count": 4200,
                    "mailbox": {"email": "victim@acme.com", "id": "mb-1"},
                    "start_time": "2026-07-05T01:00:00Z",
                    "end_time": "2026-07-05T03:00:00Z",
                    "reviewed_at": None,
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="s17", name="list_sublime_email_bombs", arguments={"active": True})
    )
    assert not result.is_error
    assert "victim@acme.com" in result.content
    assert "NOT reviewed" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/v0/messages/bombs")
    assert call["params"]["active"] is True


@pytest.mark.asyncio
async def test_analyze_link_requires_url(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="s18", name="analyze_sublime_link", arguments={"url": " "})
    )
    assert "required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_analyze_link_posts_body_and_formats(monkeypatch):
    def router(method, url):
        return {
            "effective_url": {"url": "https://phish.example/login"},
            "status_code": 200,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s19",
            name="analyze_sublime_link",
            arguments={"url": "https://t.co/x", "no_logo_detect": True},
        )
    )
    assert not result.is_error
    assert "phish.example/login" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/v0/enrichment/link_analysis/evaluate")
    assert call["json"] == {"url": "https://t.co/x", "no_logo_detect": True}


# ---------------------------------------------------------------------------
# Message group review + actions (write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_message_groups_posts_body_and_reports_task(monkeypatch):
    def router(method, url):
        return _StubResponse(
            {"actions": ["trash"], "task_id": "task-9"}, status_code=202
        )

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="w1",
            name="review_sublime_message_groups",
            arguments={
                "message_group_ids": "grp-1, grp-2",
                "classification": "malicious",
                "action": "trash",
                "review_comment": "confirmed phish",
            },
        )
    )
    assert not result.is_error
    assert "2 message group(s)" in result.content
    assert "task-9" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/v0/message-groups/review")
    assert call["json"] == {
        "message_group_ids": ["grp-1", "grp-2"],
        "classification": "malicious",
        "action": "trash",
        "review_comment": "confirmed phish",
    }


@pytest.mark.asyncio
async def test_review_message_groups_rejects_bad_classification(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="w2",
            name="review_sublime_message_groups",
            arguments={"message_group_ids": "grp-1", "classification": "evil"},
        )
    )
    assert "classification must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_message_group_trash_path_and_body(monkeypatch):
    def router(method, url):
        return _StubResponse({"task_id": "task-3"}, status_code=202)

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="w3",
            name="manage_sublime_message_group",
            arguments={
                "action": "trash",
                "group_id": "grp-1",
                "classification": "malicious",
                "report_label": "phishing",
            },
        )
    )
    assert not result.is_error
    assert "task-3" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/v0/message-groups/grp-1/trash")
    assert call["json"] == {"classification": "malicious", "report_label": "phishing"}


@pytest.mark.asyncio
async def test_manage_message_group_rejects_bad_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="w4",
            name="manage_sublime_message_group",
            arguments={"action": "explode", "group_id": "grp-1"},
        )
    )
    assert "action must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_act_on_message_posts_action(monkeypatch):
    def router(method, url):
        return _StubResponse({"task_id": "task-5"}, status_code=202)

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="w5",
            name="act_on_sublime_message",
            arguments={"action": "warning_banner", "message_id": "msg-1"},
        )
    )
    assert not result.is_error
    assert "task-5" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/v0/messages/msg-1/actions")
    assert call["json"] == {"action": "warning_banner"}


# ---------------------------------------------------------------------------
# Rules (write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rule_shapes_payload(monkeypatch):
    def router(method, url):
        return {"rule_id": "rule-1"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="w6",
            name="create_sublime_rule",
            arguments={
                "name": "HTML smuggling",
                "source": "type.inbound and any(attachments, .file_type == 'html')",
                "severity": "high",
                "tags": "Attack surface reduction, HTML",
                "false_positives": "Newsletters with HTML attachments; internal tooling",
            },
        )
    )
    assert not result.is_error
    assert "rule-1" in result.content
    assert "inactive" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/v0/rules")
    assert call["json"]["name"] == "HTML smuggling"
    assert call["json"]["active"] is False
    assert call["json"]["severity"] == "high"
    assert call["json"]["tags"] == ["Attack surface reduction", "HTML"]
    assert call["json"]["false_positives"] == [
        "Newsletters with HTML attachments",
        "internal tooling",
    ]


@pytest.mark.asyncio
async def test_create_rule_rejects_bad_severity(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="w7",
            name="create_sublime_rule",
            arguments={"name": "X", "source": "type.inbound", "severity": "extreme"},
        )
    )
    assert "severity must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_rule_requires_name_and_source(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="w8",
            name="update_sublime_rule",
            arguments={"rule_id": "rule-1", "name": "", "source": ""},
        )
    )
    assert "required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_rule_puts_definition(monkeypatch):
    def router(method, url):
        return {"id": "rule-1", "name": "Renamed"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="w9",
            name="update_sublime_rule",
            arguments={"rule_id": "rule-1", "name": "Renamed", "source": "type.inbound"},
        )
    )
    assert not result.is_error
    assert "Renamed" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "put"
    assert call["url"].endswith("/v0/rules/rule-1")
    assert call["json"] == {"name": "Renamed", "source": "type.inbound"}


@pytest.mark.asyncio
async def test_manage_rule_delete_uses_delete_verb(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _StubResponse({}, content=b""))
    result = await executor.execute_tool(
        ToolCall(
            id="w10",
            name="manage_sublime_rule",
            arguments={"action": "delete", "rule_id": "rule-1"},
        )
    )
    assert not result.is_error
    assert "permanently deleted" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "delete"
    assert call["url"].endswith("/v0/rules/rule-1")


@pytest.mark.asyncio
async def test_manage_rule_activate_path(monkeypatch):
    def router(method, url):
        return {"id": "rule-1", "name": "Rule A", "active": True}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="w11",
            name="manage_sublime_rule",
            arguments={"action": "activate", "rule_id": "rule-1"},
        )
    )
    assert not result.is_error
    assert "activated" in result.content
    assert recorder.calls[-1]["url"].endswith("/v0/rules/rule-1/activate")


# ---------------------------------------------------------------------------
# Lists (write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_list_create_requires_name_and_description(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="w12",
            name="manage_sublime_list",
            arguments={"action": "create", "name": "ti_hashes"},
        )
    )
    assert "required" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_list_add_entry_posts_string(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _StubResponse({}, content=b""))
    result = await executor.execute_tool(
        ToolCall(
            id="w13",
            name="manage_sublime_list",
            arguments={"action": "add_entry", "list_id": "l1", "entry": "a" * 64},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/v0/lists/l1/entries/entry")
    assert call["json"] == {"string": "a" * 64}


@pytest.mark.asyncio
async def test_manage_list_delete_entry_uses_query_param(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _StubResponse({}, content=b""))
    result = await executor.execute_tool(
        ToolCall(
            id="w14",
            name="manage_sublime_list",
            arguments={"action": "delete_entry", "list_id": "l1", "entry": "bad.example"},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "delete"
    assert call["url"].endswith("/v0/lists/l1/entries/entry")
    assert call["params"] == {"string": "bad.example"}


@pytest.mark.asyncio
async def test_manage_list_set_entries_overwrites(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _StubResponse({}, content=b""))
    result = await executor.execute_tool(
        ToolCall(
            id="w15",
            name="manage_sublime_list",
            arguments={
                "action": "set_entries",
                "list_id": "l1",
                "entries": "one.example\n\ntwo.example\n",
            },
        )
    )
    assert not result.is_error
    assert "OVERWRITTEN with 2" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "put"
    assert call["url"].endswith("/v0/lists/l1/entries")
    assert call["json"] == {"entries": ["one.example", "two.example"]}


@pytest.mark.asyncio
async def test_manage_list_requires_list_id_for_non_create(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(id="w16", name="manage_sublime_list", arguments={"action": "delete"})
    )
    assert "list_id is required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_list_entry_length_guard(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="w17",
            name="manage_sublime_list",
            arguments={"action": "add_entry", "list_id": "l1", "entry": "x" * 2001},
        )
    )
    assert "2000" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_list_set_entries_length_guard(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="w18",
            name="manage_sublime_list",
            arguments={
                "action": "set_entries",
                "list_id": "l1",
                "entries": "ok.example\n" + "x" * 2001,
            },
        )
    )
    assert "2000" in result.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Email bombs (write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismiss_email_bomb_posts_comment(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: _StubResponse({}, content=b""))
    result = await executor.execute_tool(
        ToolCall(
            id="w18",
            name="dismiss_sublime_email_bomb",
            arguments={"bomb_id": "bomb-1", "review_comment": "handled"},
        )
    )
    assert not result.is_error
    assert "dismissed" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/v0/messages/bombs/bomb-1/dismiss")
    assert call["json"] == {"review_comment": "handled"}


# ---------------------------------------------------------------------------
# Connectivity validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_connection_ok(monkeypatch):
    async def _ok(_client, method, url, _provider, action, **kwargs):
        assert url.endswith("/v0/rules")
        assert kwargs["params"] == {"limit": 1}
        return _StubResponse({"count": 0, "total": 0, "rules": []}), None

    monkeypatch.setattr(sublime, "safe_request", _ok)
    assert await SublimeIntegration().validate_connection(_TOKEN) is True


@pytest.mark.asyncio
async def test_validate_connection_failure(monkeypatch):
    async def _fail(_client, method, url, _provider, action, **kwargs):
        return None, "Sublime Security validate API key failed: 401"

    monkeypatch.setattr(sublime, "safe_request", _fail)
    assert await SublimeIntegration().validate_connection(_TOKEN) is False


@pytest.mark.asyncio
async def test_validate_connection_missing_key():
    assert await SublimeIntegration().validate_connection("https://x||  ") is False


@pytest.mark.asyncio
async def test_get_user_info_reports_base_url():
    info = await SublimeIntegration().get_user_info("||key")
    assert info == {
        "name": "Sublime Security",
        "base_url": "https://platform.sublime.security",
    }
