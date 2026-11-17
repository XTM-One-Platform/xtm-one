"""Unit tests for the PII detection and redaction layer.

Tests cover:
  - All supported entity types (EMAIL, IP_ADDRESS, PHONE, CREDIT_CARD, SSN,
    JWT_TOKEN, AWS_KEY, API_KEY, PRIVATE_KEY, PASSWORD)
  - Synthetic log / alert payloads that mix multiple PII types
  - Empty / whitespace / non-PII input (no false positives)
  - RedactionConfig entity-type toggle (opt-out of specific types)
  - RedactionResult metadata (was_redacted, summary counts, events)
  - Integration: create_github_issue strips PII before the HTTP call
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.pii_redactor import (
    ALL_ENTITY_TYPES,
    RedactionConfig,
    RedactionResult,
    redact_pii,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_placeholder(result: RedactionResult, entity_type: str) -> None:
    """Assert that the redacted text contains the placeholder for *entity_type*."""
    placeholder = f"[{entity_type}]"
    assert placeholder in result.redacted_text, (
        f"Expected placeholder {placeholder!r} in redacted text: "
        f"{result.redacted_text!r}"
    )
    assert result.summary.get(entity_type, 0) >= 1
    assert result.was_redacted


def _assert_not_in_result(result: RedactionResult, sensitive: str) -> None:
    """Assert that *sensitive* text does not appear in the redacted output."""
    assert sensitive not in result.redacted_text, (
        f"Sensitive value {sensitive!r} should have been redacted but is "
        f"still present in: {result.redacted_text!r}"
    )


# ---------------------------------------------------------------------------
# Empty / safe inputs
# ---------------------------------------------------------------------------


def test_empty_string_returns_unchanged():
    result = redact_pii("")
    assert result.redacted_text == ""
    assert result.was_redacted is False
    assert result.summary == {}
    assert result.events == []


def test_non_pii_text_is_unchanged():
    text = "Service restarted successfully after 3 retries."
    result = redact_pii(text)
    assert result.redacted_text == text
    assert result.was_redacted is False


def test_original_length_preserved():
    text = "contact: alice@example.com"
    result = redact_pii(text)
    assert result.original_length == len(text)


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------


def test_email_simple():
    result = redact_pii("Send report to alice@example.com please.")
    _assert_placeholder(result, "EMAIL")
    _assert_not_in_result(result, "alice@example.com")
    assert "Send report to" in result.redacted_text


def test_email_multiple():
    result = redact_pii("From: a@b.com To: c@d.org CC: e@f.io")
    assert result.summary.get("EMAIL", 0) == 3
    _assert_not_in_result(result, "a@b.com")
    _assert_not_in_result(result, "c@d.org")
    _assert_not_in_result(result, "e@f.io")


def test_email_subdomain():
    result = redact_pii("user.name+tag@mail.internal.corp.com was involved")
    _assert_placeholder(result, "EMAIL")


# ---------------------------------------------------------------------------
# IP_ADDRESS (IPv4)
# ---------------------------------------------------------------------------


def test_ipv4_simple():
    result = redact_pii("Request came from 192.168.1.100 at 14:23 UTC.")
    _assert_placeholder(result, "IP_ADDRESS")
    _assert_not_in_result(result, "192.168.1.100")


def test_ipv4_loopback():
    result = redact_pii("Connecting to 127.0.0.1:8080")
    _assert_placeholder(result, "IP_ADDRESS")


def test_ipv4_multiple():
    result = redact_pii("10.0.0.1 → 172.16.254.1 → 203.0.113.5")
    assert result.summary.get("IP_ADDRESS", 0) == 3


# ---------------------------------------------------------------------------
# IP_ADDRESS (IPv6)
# ---------------------------------------------------------------------------


def test_ipv6_full_form():
    result = redact_pii("Source: 2001:0db8:85a3:0000:0000:8a2e:0370:7334")
    _assert_placeholder(result, "IP_ADDRESS")
    _assert_not_in_result(result, "2001:0db8:85a3:0000:0000:8a2e:0370:7334")


def test_ipv6_compressed():
    result = redact_pii("Peer address fe80::1%eth0 timed out")
    _assert_placeholder(result, "IP_ADDRESS")


# ---------------------------------------------------------------------------
# PHONE
# ---------------------------------------------------------------------------


def test_phone_us_dashes():
    result = redact_pii("Call support at 800-555-1234 for help.")
    _assert_placeholder(result, "PHONE")
    _assert_not_in_result(result, "800-555-1234")


def test_phone_with_country_code():
    result = redact_pii("Contact: +1 (415) 555-9876")
    _assert_placeholder(result, "PHONE")


def test_phone_dots():
    result = redact_pii("Fax: 212.555.4321")
    _assert_placeholder(result, "PHONE")


# ---------------------------------------------------------------------------
# CREDIT_CARD
# ---------------------------------------------------------------------------


def test_credit_card_visa():
    result = redact_pii("Card number: 4111111111111111 used for purchase.")
    _assert_placeholder(result, "CREDIT_CARD")
    _assert_not_in_result(result, "4111111111111111")


def test_credit_card_amex():
    result = redact_pii("Amex: 371449635398431 declined.")
    _assert_placeholder(result, "CREDIT_CARD")


def test_credit_card_mastercard():
    result = redact_pii("MC: 5500005555555559")
    _assert_placeholder(result, "CREDIT_CARD")


# ---------------------------------------------------------------------------
# SSN
# ---------------------------------------------------------------------------


def test_ssn_basic():
    result = redact_pii("Employee SSN: 123-45-6789 on file.")
    _assert_placeholder(result, "SSN")
    _assert_not_in_result(result, "123-45-6789")


def test_ssn_does_not_match_invalid():
    # Area 000 is invalid — should not be flagged
    result = redact_pii("Code: 000-12-3456 ignored")
    assert result.summary.get("SSN", 0) == 0


# ---------------------------------------------------------------------------
# JWT_TOKEN
# ---------------------------------------------------------------------------


def test_jwt_token():
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiJ1c2VyXzEyMyJ9"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    result = redact_pii(f"Authorization: Bearer {jwt}")
    _assert_placeholder(result, "JWT_TOKEN")
    _assert_not_in_result(result, jwt)


# ---------------------------------------------------------------------------
# AWS_KEY
# ---------------------------------------------------------------------------


def test_aws_access_key():
    result = redact_pii("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
    _assert_placeholder(result, "AWS_KEY")
    _assert_not_in_result(result, "AKIAIOSFODNN7EXAMPLE")


def test_aws_sts_key():
    result = redact_pii("STS session key: ASIAIOSFODNN7EXAMPLE")
    _assert_placeholder(result, "AWS_KEY")


# ---------------------------------------------------------------------------
# API_KEY (Bearer / Token / X-API-Key headers)
# ---------------------------------------------------------------------------


def test_bearer_token_header():
    result = redact_pii("Authorization: Bearer abc123def456ghi789jkl012mno")
    _assert_placeholder(result, "API_KEY")
    _assert_not_in_result(result, "abc123def456ghi789jkl012mno")


def test_x_api_key_header():
    # X-API-Key headers may be caught by the PASSWORD rule (api_key: <value>)
    # or the API_KEY header rule — either way the secret value must be gone.
    result = redact_pii("X-API-Key: supersecretapikey1234567890abcdef")
    assert result.was_redacted
    _assert_not_in_result(result, "supersecretapikey1234567890abcdef")


def test_x_auth_token_header():
    # Similarly for X-Auth-Token — the value must be redacted.
    result = redact_pii("X-Auth-Token: mysecrettokenvalue123456789012")
    assert result.was_redacted
    _assert_not_in_result(result, "mysecrettokenvalue123456789012")


# ---------------------------------------------------------------------------
# PRIVATE_KEY
# ---------------------------------------------------------------------------


def test_private_key_rsa_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA2a2rwplBQLF29amygykEMmYz0+Kcj3bKBp29\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = redact_pii(f"Key material:\n{pem}\n")
    _assert_placeholder(result, "PRIVATE_KEY")
    _assert_not_in_result(result, "MIIEowIBAAKCAQEA2a2rwplBQLF29amygykEMmYz0+Kcj3bKBp29")


def test_private_key_openssh_block():
    pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAA\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    result = redact_pii(pem)
    _assert_placeholder(result, "PRIVATE_KEY")


# ---------------------------------------------------------------------------
# PASSWORD
# ---------------------------------------------------------------------------


def test_password_equals():
    result = redact_pii("DB_PASSWORD=hunter2secret")
    _assert_placeholder(result, "PASSWORD")
    _assert_not_in_result(result, "hunter2secret")
    # Key name must be preserved
    assert "DB_PASSWORD=" in result.redacted_text


def test_password_colon():
    result = redact_pii("secret: 'myS3cr3tV@lue'")
    _assert_placeholder(result, "PASSWORD")
    _assert_not_in_result(result, "myS3cr3tV@lue")
    # Surrounding single quotes must survive — the previous greedy
    # ``\S{4,}`` regex was eating the trailing quote and producing
    # ``secret: '[PASSWORD]`` (unbalanced). The backreference-anchored
    # form preserves them.
    assert "secret: '[PASSWORD]'" in result.redacted_text


def test_password_colon_double_quoted_preserves_quotes():
    """Double-quoted secrets must keep their surrounding quotes after redaction."""
    result = redact_pii('password: "myS3cr3tV@lue"')
    _assert_placeholder(result, "PASSWORD")
    _assert_not_in_result(result, "myS3cr3tV@lue")
    assert 'password: "[PASSWORD]"' in result.redacted_text


def test_password_equals_quoted_preserves_quotes():
    """Quoted secrets in ``key=value`` form must keep their quotes."""
    result = redact_pii("api_key='abc12345'")
    _assert_placeholder(result, "PASSWORD")
    _assert_not_in_result(result, "abc12345")
    assert "api_key='[PASSWORD]'" in result.redacted_text


def test_api_token_assignment():
    result = redact_pii("api_token=ghp_1234567890abcdef")
    _assert_placeholder(result, "PASSWORD")
    _assert_not_in_result(result, "ghp_1234567890abcdef")
    # Unquoted form: nothing extra to preserve, just the bare placeholder
    # right after the ``=`` separator.
    assert "api_token=[PASSWORD]" in result.redacted_text


# ---------------------------------------------------------------------------
# Sequential-rule placeholder protection
#
# Pins the contract that more-specific rules running earlier in the
# pipeline (PRIVATE_KEY, EMAIL, IP_ADDRESS, AWS_KEY, JWT_TOKEN, ...)
# are NOT overwritten by later, broader rules (notably PASSWORD)
# whose regex would otherwise re-match the typed placeholder text on
# a second pass. Without the guard,
# ``private_key=<PEM block>`` would be redacted as ``[PRIVATE_KEY]``
# and then re-matched by the PASSWORD rule (whose key list includes
# ``private_key``), losing the classification and inflating the
# event count.
# ---------------------------------------------------------------------------


def test_private_key_assignment_keeps_specific_classification():
    """``private_key=<PEM block>`` must end up as ``private_key=[PRIVATE_KEY]``.

    The PRIVATE_KEY rule runs first and replaces the PEM block with
    ``[PRIVATE_KEY]``. The PASSWORD rule's key list includes
    ``private_key`` and its value class ``[^\\s'\"]{4,}`` would otherwise
    match the replacement text. The placeholder-skip guard in
    ``_replacer`` blocks this so the more-specific classification
    survives.
    """
    text = (
        "private_key=-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = redact_pii(text)
    assert "[PRIVATE_KEY]" in result.redacted_text
    assert "[PASSWORD]" not in result.redacted_text
    _assert_not_in_result(result, "MIIEpAIBAAKCAQEA1234567890abcdef")
    # The summary must classify this as PRIVATE_KEY only, not PASSWORD.
    assert result.summary.get("PRIVATE_KEY", 0) == 1
    assert "PASSWORD" not in result.summary


def test_already_redacted_placeholder_in_secret_assignment_is_preserved():
    """An input that already contains ``private_key=[PRIVATE_KEY]`` (e.g.
    text that was redacted upstream) must be left untouched by the
    PASSWORD rule on the second pass — no new ``[PASSWORD]`` event,
    no overwrite of the existing placeholder.

    Regression test for the same bug class as the previous test, but
    starting from already-redacted input rather than a raw PEM block.
    """
    text = "Config: private_key=[PRIVATE_KEY] password=hunter2secret"
    result = redact_pii(text)
    assert "private_key=[PRIVATE_KEY]" in result.redacted_text
    _assert_not_in_result(result, "hunter2secret")
    assert "password=[PASSWORD]" in result.redacted_text
    # Only the real ``hunter2secret`` value should produce a PASSWORD
    # event — the already-redacted ``[PRIVATE_KEY]`` must NOT.
    assert result.summary.get("PASSWORD", 0) == 1
    # And the input had no fresh PRIVATE_KEY block, so no PRIVATE_KEY
    # event either (the existing placeholder was already counted by
    # whoever produced the upstream redaction).
    assert "PRIVATE_KEY" not in result.summary


def test_api_key_assignment_does_not_overwrite_aws_key_placeholder():
    """``api_key=AKIAIOSFODNN7EXAMPLE`` runs through AWS_KEY first
    (replacing the value with ``[AWS_KEY]``) — the PASSWORD rule must
    NOT re-match ``api_key=[AWS_KEY]`` and rewrite it as
    ``api_key=[PASSWORD]``.
    """
    result = redact_pii("api_key=AKIAIOSFODNN7EXAMPLE")
    assert "api_key=[AWS_KEY]" in result.redacted_text
    assert "[PASSWORD]" not in result.redacted_text
    assert result.summary.get("AWS_KEY", 0) == 1
    assert "PASSWORD" not in result.summary


# ---------------------------------------------------------------------------
# Mixed / realistic log payloads
# ---------------------------------------------------------------------------


def test_mixed_log_line():
    """Realistic Grafana alert body with multiple PII types."""
    text = (
        "ALERT: High error rate detected\n"
        "User john.doe@company.com (IP: 10.10.20.30) triggered 47 errors.\n"
        "Session token: Bearer eyJhbGciOiJSUzI1NiJ9.eyJ1c2VyIjoiam9obiJ9.SIG\n"
        "AWS key in env: AKIAIOSFODNN7EXAMPLE\n"
        "Contact phone: 415-867-5309\n"
    )
    result = redact_pii(text)

    assert result.was_redacted
    _assert_not_in_result(result, "john.doe@company.com")
    _assert_not_in_result(result, "10.10.20.30")
    _assert_not_in_result(result, "AKIAIOSFODNN7EXAMPLE")
    _assert_not_in_result(result, "415-867-5309")

    assert "[EMAIL]" in result.redacted_text
    assert "[IP_ADDRESS]" in result.redacted_text
    assert "[AWS_KEY]" in result.redacted_text
    # Non-PII context must be preserved
    assert "ALERT: High error rate detected" in result.redacted_text


def test_stack_trace_with_ip():
    text = (
        "Exception in thread 'main' java.net.ConnectException: "
        "Connection refused: 172.31.14.99:5432\n"
        "  at sun.nio.ch.Net.pollConnect(Native Method)\n"
    )
    result = redact_pii(text)
    _assert_placeholder(result, "IP_ADDRESS")
    _assert_not_in_result(result, "172.31.14.99")
    assert "Connection refused" in result.redacted_text


# ---------------------------------------------------------------------------
# RedactionConfig – entity type toggles
# ---------------------------------------------------------------------------


def test_config_disable_email():
    config = RedactionConfig(
        enabled_types=ALL_ENTITY_TYPES - frozenset({"EMAIL"})
    )
    text = "Contact user@example.com at 10.0.0.1"
    result = redact_pii(text, config=config)
    # Email should NOT be redacted
    assert "user@example.com" in result.redacted_text
    # IP should still be redacted
    _assert_placeholder(result, "IP_ADDRESS")


def test_config_empty_enabled_types_no_redaction():
    config = RedactionConfig(enabled_types=frozenset())
    text = "User: admin@corp.com IP: 192.168.0.1 SSN: 123-45-6789"
    result = redact_pii(text, config=config)
    assert result.was_redacted is False
    assert result.redacted_text == text


def test_config_single_type_only():
    config = RedactionConfig(enabled_types=frozenset({"SSN"}))
    text = "SSN 234-56-7890, email: bob@corp.com, IP: 10.0.0.1"
    result = redact_pii(text, config=config)
    _assert_placeholder(result, "SSN")
    # Email and IP should remain untouched
    assert "bob@corp.com" in result.redacted_text
    assert "10.0.0.1" in result.redacted_text


# ---------------------------------------------------------------------------
# RedactionResult metadata
# ---------------------------------------------------------------------------


def test_summary_counts_multiple_emails():
    result = redact_pii("a@b.com and c@d.com and e@f.com")
    assert result.summary["EMAIL"] == 3


def test_events_list_populated():
    result = redact_pii("Login from 192.0.2.1 by user@test.org")
    assert len(result.events) >= 2
    entity_types = {ev.entity_type for ev in result.events}
    assert "EMAIL" in entity_types
    assert "IP_ADDRESS" in entity_types


def test_was_redacted_false_when_clean():
    result = redact_pii("Everything looks fine, no issues today.")
    assert result.was_redacted is False
    assert result.events == []
    assert result.summary == {}


# ---------------------------------------------------------------------------
# Integration: create_github_issue redacts PII
# ---------------------------------------------------------------------------


def _mock_http_response(payload: dict) -> httpx.Response:
    return httpx.Response(201, json=payload)


def _get_tool_fn(executor_mock: MagicMock, name: str):
    """Extract a registered async function from the executor mock by name."""
    from app.integrations.providers.github import build_tools

    build_tools("fake-token", executor_mock)
    for call in executor_mock.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(f"Tool {name!r} not registered")


@pytest.mark.asyncio
async def test_create_github_issue_redacts_email_in_title():
    """PII in the issue title should be stripped before the HTTP call."""
    executor = MagicMock()
    executor.register = MagicMock()
    fn = _get_tool_fn(executor, "create_github_issue")

    issue_response = {"number": 42, "html_url": "https://github.com/org/repo/issues/42"}

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_http_response(issue_response), None)
        result = await fn(
            repo="org/repo",
            title="Error for user hacker@evil.com on prod",
            body="Stack trace follows",
        )

    assert "42" in result
    # Inspect the JSON payload sent to GitHub
    call_kwargs = mock_req.call_args[1]
    sent_payload = call_kwargs["json"]
    assert "hacker@evil.com" not in sent_payload["title"]
    assert "[EMAIL]" in sent_payload["title"]


@pytest.mark.asyncio
async def test_create_github_issue_redacts_ip_in_body():
    """PII in the issue body should be stripped before the HTTP call."""
    executor = MagicMock()
    executor.register = MagicMock()
    fn = _get_tool_fn(executor, "create_github_issue")

    issue_response = {"number": 7, "html_url": "https://github.com/org/repo/issues/7"}

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_http_response(issue_response), None)
        await fn(
            repo="org/repo",
            title="Production alert",
            body="Request from 203.0.113.42 caused a 500 error.",
        )

    call_kwargs = mock_req.call_args[1]
    sent_payload = call_kwargs["json"]
    assert "203.0.113.42" not in sent_payload["body"]
    assert "[IP_ADDRESS]" in sent_payload["body"]


@pytest.mark.asyncio
async def test_create_github_issue_clean_input_unchanged():
    """When there is no PII, title and body are sent as-is."""
    executor = MagicMock()
    executor.register = MagicMock()
    fn = _get_tool_fn(executor, "create_github_issue")

    issue_response = {"number": 1, "html_url": "https://github.com/org/repo/issues/1"}

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_http_response(issue_response), None)
        await fn(
            repo="org/repo",
            title="Dependency upgrade: bump lodash to 4.17.21",
            body="Routine upgrade, no security impact.",
        )

    call_kwargs = mock_req.call_args[1]
    sent_payload = call_kwargs["json"]
    assert sent_payload["title"] == "Dependency upgrade: bump lodash to 4.17.21"
    assert sent_payload["body"] == "Routine upgrade, no security impact."


@pytest.mark.asyncio
async def test_create_github_issue_logs_warning_on_pii(caplog):
    """A warning should be emitted when PII is detected and redacted."""
    import logging

    executor = MagicMock()
    executor.register = MagicMock()
    fn = _get_tool_fn(executor, "create_github_issue")

    issue_response = {"number": 99, "html_url": "https://github.com/org/repo/issues/99"}

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_http_response(issue_response), None)
        with caplog.at_level(logging.WARNING, logger="app.integrations.providers.github.issues"):
            await fn(
                repo="org/repo",
                title="Incident for alice@corp.com",
                body="No PII here.",
            )

    # A warning must be emitted
    warning_records = [r for r in caplog.records if "PII redacted" in r.message]
    assert warning_records, "Expected a 'PII redacted' warning log record"

    # ``LogRecord.args`` carries only the % substitution arguments — the
    # format string itself lives in ``LogRecord.msg``. The production
    # call is ``_log.warning("... %s: %s", repo, redaction_summary)``,
    # so ``args == (repo, redaction_summary)`` and the summary dict is
    # at index 1.
    log_record = warning_records[0]
    summary_arg = log_record.args[1] if len(log_record.args) >= 2 else None
    assert isinstance(summary_arg, dict), (
        f"Expected redaction summary dict in log args, got: {log_record.args!r}"
    )
    assert summary_arg.get("title_EMAIL", 0) >= 1, (
        f"Expected title_EMAIL >= 1 in redaction summary, got: {summary_arg!r}"
    )
