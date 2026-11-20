# SPDX-License-Identifier: Apache-2.0

"""The Platform email integration: catalog rules, guards, audit, probe (S11 of the Platform Health plan)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.v1.integration_catalog import PROVIDER_CATALOG, invalid_extra_fields
from app.api.v1.integrations import _require_catalog_creation_rules
from app.integrations.connectivity import ProbeContext
from app.integrations.providers import platform_email
from app.integrations.providers.platform_email import (
    build_tools,
    check_connectivity,
    parse_token,
    platform_email_token,
    refused_addresses,
)
from app.services import email as email_service
from app.tools.base import CallableToolExecutor, ToolCall


class _Sent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []


def _tool(token: str, sent: _Sent, *, already: int = 0, monkeypatch: pytest.MonkeyPatch, fail: bool = False):
    async def _deliver(**kwargs: Any) -> None:
        if fail:
            raise email_service.EmailDeliveryError("550 mailbox unavailable", permanent=True)
        sent.calls.append(kwargs)

    async def _count(_agent_id: str | None) -> int:
        return already

    async def _record(**kwargs: Any) -> None:
        sent.records.append(kwargs)

    monkeypatch.setattr(email_service, "is_email_configured", lambda: True)
    monkeypatch.setattr(email_service, "deliver_email", _deliver)
    executor = CallableToolExecutor()
    defs = build_tools(
        token, executor, agent_id="agent-1", user_id="user-1", count_sends_fn=_count, record_send_fn=_record
    )
    return defs, executor


async def _call(executor: CallableToolExecutor, **kwargs: Any) -> str:
    result = await executor.execute_tool(ToolCall(id="c1", name="send_email", arguments=kwargs))
    return result.content


def test_the_catalog_entry_is_admin_company_managed_and_smtp_bound():
    entry = PROVIDER_CATALOG["platform_email"]
    assert entry["auth_type"] == "none"
    assert entry["requires_platform_smtp"] is True
    assert entry["company_managed_only"] is True
    assert set(entry["extra_fields"]) == {"allowed_domains", "daily_limit"}


@pytest.mark.parametrize(
    ("extra", "problems"),
    [
        ({"allowed_domains": "example.com\n@test.org.", "daily_limit": "1000"}, 0),
        ({"allowed_domains": "", "daily_limit": ""}, 0),
        ({"daily_limit": "5000"}, 1),
        ({"daily_limit": "-1"}, 1),
        ({"daily_limit": "fifty"}, 1),
        ({"allowed_domains": "bob@example.com"}, 1),
        ({"allowed_domains": "not a domain", "daily_limit": "5000"}, 2),
    ],
)
def test_typed_extra_fields_are_refused_outside_their_rule(extra, problems):
    assert len(invalid_extra_fields("platform_email", extra)) == problems


def test_providers_without_typed_fields_are_not_judged():
    assert invalid_extra_fields("rss", {"feed_urls": "not a url"}) == []


def test_the_catalog_types_its_fields_for_the_form():
    inputs = PROVIDER_CATALOG["platform_email"]["extra_field_inputs"]
    assert inputs["allowed_domains"]["kind"] == "domains"
    assert (inputs["daily_limit"]["minimum"], inputs["daily_limit"]["maximum"]) == (0, platform_email.MAX_DAILY_LIMIT)
    assert inputs["daily_limit"]["default"] == str(platform_email.DEFAULT_DAILY_LIMIT)


def test_creation_is_refused_without_smtp(monkeypatch):
    monkeypatch.setattr("app.api.v1.integrations.is_email_configured", lambda: False)
    admin = SimpleNamespace(is_admin=True)
    with pytest.raises(HTTPException) as exc:
        _require_catalog_creation_rules(PROVIDER_CATALOG["platform_email"], "platform_email", admin, True)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(("is_admin", "company"), [(False, True), (True, False), (False, False)])
def test_creation_is_admin_and_company_managed_only(monkeypatch, is_admin, company):
    monkeypatch.setattr("app.api.v1.integrations.is_email_configured", lambda: True)
    with pytest.raises(HTTPException) as exc:
        _require_catalog_creation_rules(
            PROVIDER_CATALOG["platform_email"], "platform_email", SimpleNamespace(is_admin=is_admin), company
        )
    assert exc.value.status_code == 403


def test_an_admin_creates_it_company_managed(monkeypatch):
    monkeypatch.setattr("app.api.v1.integrations.is_email_configured", lambda: True)
    _require_catalog_creation_rules(
        PROVIDER_CATALOG["platform_email"], "platform_email", SimpleNamespace(is_admin=True), True
    )


def test_other_providers_are_not_touched_by_the_rules(monkeypatch):
    monkeypatch.setattr("app.api.v1.integrations.is_email_configured", lambda: False)
    _require_catalog_creation_rules(PROVIDER_CATALOG["rss"], "rss", SimpleNamespace(is_admin=False), False)


def test_the_token_round_trips_domains_and_limit():
    token = platform_email_token({"allowed_domains": "Example.com\n@test.org, ", "daily_limit": "7"})
    assert parse_token(token) == (frozenset({"example.com", "test.org"}), 7)
    assert parse_token(platform_email_token({})) == (frozenset(), platform_email.DEFAULT_DAILY_LIMIT)
    assert parse_token("a.com||99999")[1] == platform_email.MAX_DAILY_LIMIT
    assert parse_token("__dummy__") == (frozenset(), platform_email.DEFAULT_DAILY_LIMIT)


def test_refused_addresses_are_exact_domain_matches():
    allowed = frozenset({"example.com"})
    assert refused_addresses(["a@example.com", "B@EXAMPLE.COM"], allowed) == []
    assert refused_addresses(["a@sub.example.com", "a@evil.com", "not-an-address"], allowed) == [
        "a@sub.example.com",
        "a@evil.com",
        "not-an-address",
    ]


def test_the_tool_always_asks_and_guards_duplicates(monkeypatch):
    defs, _ = _tool("example.com||5", _Sent(), monkeypatch=monkeypatch)
    (definition,) = defs
    assert definition.name == "send_email"
    assert definition.always_ask is True
    assert definition.send_guard is not None
    assert definition.source == "integration:platform_email"


def test_a_send_to_an_allowed_domain_is_delivered_and_audited(monkeypatch):
    sent = _Sent()
    _, executor = _tool("example.com||5", sent, already=2, monkeypatch=monkeypatch)
    out = asyncio.run(
        _call(executor, to="a@example.com", cc="b@example.com", bcc="c@example.com", subject="Hello", body="**Hi**")
    )
    assert "Email sent to 3 recipients" in out and "3 of 5" in out
    assert [c["to"] for c in sent.calls] == ["a@example.com", "c@example.com"]
    assert all("Bcc" not in (c["headers"] or {}) for c in sent.calls)
    assert sent.calls[0]["headers"]["Cc"] == "b@example.com"
    # The Bcc envelope carries no Cc header: Cc recipients get one copy only.
    assert "Cc" not in (sent.calls[1]["headers"] or {})
    assert "<strong>Hi</strong>" in sent.calls[0]["html"]
    (record,) = sent.records
    assert record["agent_id"] == "agent-1" and record["user_id"] == "user-1" and record["error"] is None


def test_a_recipient_outside_the_allowed_domains_is_refused(monkeypatch):
    sent = _Sent()
    _, executor = _tool("example.com||5", sent, monkeypatch=monkeypatch)
    out = asyncio.run(_call(executor, to="a@example.com, x@evil.com", subject="s", body="b"))
    assert "x@evil.com" in out and out.startswith("Error")
    assert sent.calls == [] and sent.records == []


def test_no_allowed_domain_refuses_every_send(monkeypatch):
    sent = _Sent()
    _, executor = _tool("||5", sent, monkeypatch=monkeypatch)
    out = asyncio.run(_call(executor, to="a@example.com", subject="s", body="b"))
    assert out.startswith("Error") and "allows no recipient domain" in out
    assert sent.calls == []


def test_the_daily_limit_refuses_the_next_send(monkeypatch):
    sent = _Sent()
    _, executor = _tool("example.com||3", sent, already=3, monkeypatch=monkeypatch)
    out = asyncio.run(_call(executor, to="a@example.com", subject="s", body="b"))
    assert out.startswith("Error") and "daily limit" in out
    assert sent.calls == []


def test_a_refused_delivery_is_audited_and_reported(monkeypatch):
    sent = _Sent()
    _, executor = _tool("example.com||3", sent, monkeypatch=monkeypatch, fail=True)
    out = asyncio.run(_call(executor, to="a@example.com", subject="s", body="b"))
    assert out.startswith("Error") and "550" in out
    (record,) = sent.records
    assert "550" in record["error"]


def test_without_smtp_the_tool_refuses(monkeypatch):
    sent = _Sent()
    _, executor = _tool("example.com||3", sent, monkeypatch=monkeypatch)
    monkeypatch.setattr(email_service, "is_email_configured", lambda: False)
    out = asyncio.run(_call(executor, to="a@example.com", subject="s", body="b"))
    assert out.startswith("Error") and "SMTP" in out


@pytest.mark.parametrize(
    ("configured", "extra", "status"),
    [
        (False, {"allowed_domains": "example.com"}, "error"),
        (True, {}, "degraded"),
        (True, {"allowed_domains": "example.com", "daily_limit": "0"}, "degraded"),
        (True, {"allowed_domains": "example.com"}, "ok"),
    ],
)
def test_the_probe_reports_smtp_and_guards(monkeypatch, configured, extra, status):
    monkeypatch.setattr(email_service, "is_email_configured", lambda: configured)
    ctx = ProbeContext(provider="platform_email", name="Platform email", token=None, extra=extra)
    result = asyncio.run(check_connectivity(ctx))
    assert result.status == status
