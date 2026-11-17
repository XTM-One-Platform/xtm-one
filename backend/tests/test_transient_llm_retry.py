"""Automatic retry of transient LLM provider failures.

A run that failed because the model provider was unhealthy (rate limit /
quota, overload, outage, connection failure) is retried automatically — no
``retry_on_failure`` opt-in — a bounded number of times with an exponential,
jittered delay so the retry never lands in the same rate-limit window.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.assignment_engine import (
    _is_transient_llm_failure,
    _transient_retry_delay_seconds,
)

# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        # Anthropic SDK strings after retry_llm_call + failover exhaustion
        "Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error'}}",
        "Error code: 529 - {'type': 'error', 'error': {'type': 'overloaded_error'}}",
        "Error code: 500 - internal server error",
        # OpenAI-style
        "Error code: 429 - You exceeded your current quota, please check your plan",
        "insufficient_quota: You exceeded your current quota",
        "Connection error.",
        "APIConnectionError: api connection failed",
        # Gemini-style
        "429 RESOURCE_EXHAUSTED. Quota exceeded for quota metric",
        "503 The service is temporarily unavailable",
        # Bedrock-style
        "ThrottlingException: Rate exceeded",
        "Too many requests, please wait before trying again",
        # Generic gateway
        "502 Bad Gateway",
        "Service Unavailable",
    ],
)
def test_transient_provider_errors_are_detected(error: str):
    assert _is_transient_llm_failure(error) is True


@pytest.mark.parametrize(
    "error",
    [
        None,
        "",
        "Assignment is disabled",
        "Agent not found or disabled",
        "Error code: 401 - invalid api key",
        "Error code: 400 - bad request",
        "KeyError: 'foo'",
        # Token counts in overflow errors must never look like status codes
        "Context overflow: the input (~12,429 tokens) exceeds the model's "
        "context window (8,192 tokens for gpt-4).",
        "Tool overflow: the agent's tools (~1,500 tokens) exceed the model's "
        "context window (429,000 tokens for claude).",
        "Request too large: the assignment's accumulated context exceeds the "
        "provider's payload limit.",
    ],
)
def test_non_transient_errors_are_not_detected(error: str | None):
    assert _is_transient_llm_failure(error) is False


def test_standalone_status_code_still_matches_next_to_punctuation():
    assert _is_transient_llm_failure("HTTP 429: slow down") is True
    assert _is_transient_llm_failure("(status 503)") is True


# ---------------------------------------------------------------------------
# Backoff + jitter
# ---------------------------------------------------------------------------


def test_delay_is_exponential_and_capped(monkeypatch):
    monkeypatch.setattr(settings, "assignment_transient_retry_base_seconds", 100)
    monkeypatch.setattr(settings, "assignment_transient_retry_max_delay_seconds", 300)
    # Freeze the jitter at the top of the range to check the raw slots.
    monkeypatch.setattr(
        "app.services.assignment_engine.random.uniform", lambda a, b: b
    )
    assert _transient_retry_delay_seconds(0) == 100  # base * 2^0
    assert _transient_retry_delay_seconds(1) == 200  # base * 2^1
    assert _transient_retry_delay_seconds(2) == 300  # capped
    assert _transient_retry_delay_seconds(10) == 300  # still capped


def test_delay_jitter_stays_within_half_to_full_slot(monkeypatch):
    monkeypatch.setattr(settings, "assignment_transient_retry_base_seconds", 120)
    monkeypatch.setattr(settings, "assignment_transient_retry_max_delay_seconds", 900)
    for _ in range(200):
        delay = _transient_retry_delay_seconds(0)
        assert 60 <= delay <= 120
        delay = _transient_retry_delay_seconds(1)
        assert 120 <= delay <= 240


def test_delay_never_below_one_second(monkeypatch):
    monkeypatch.setattr(settings, "assignment_transient_retry_base_seconds", 1)
    monkeypatch.setattr(settings, "assignment_transient_retry_max_delay_seconds", 1)
    monkeypatch.setattr(
        "app.services.assignment_engine.random.uniform", lambda a, b: a
    )
    assert _transient_retry_delay_seconds(0) >= 1
