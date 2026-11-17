"""Tests for the ``QuotaConfig`` schema no longer having a ``sandbox`` field
(spec 002-sandbox-quota-usage-tracking, FR-011, revised).

Sandbox usage has no enforceable limit or configurable period of its own —
it always shares the agentic entry's limit and resolved period. A stray
``"sandbox"`` key in a raw dict (e.g. left over from before this change, in
a stored ``User.quota_config``/group/platform row) must be silently ignored
by ``QuotaConfig(**dict)``, not rejected or resurrected as an attribute.
"""

from __future__ import annotations

from app.api.v1.schemas.settings import QuotaConfig, QuotaTypeConfig


def test_quota_config_has_no_sandbox_field():
    config = QuotaConfig(llm_proxy=QuotaTypeConfig(period="daily"))

    assert not hasattr(config, "sandbox")


def test_quota_config_ignores_stray_sandbox_key_in_raw_dict():
    """QuotaConfig(**raw_dict) — the pattern used by both response-construction
    call sites — silently drops a leftover "sandbox" key rather than erroring."""
    raw = {
        "llm_proxy": {"period": "monthly"},
        "sandbox": {"period": "yearly"},
    }

    config = QuotaConfig(**raw)

    assert not hasattr(config, "sandbox")
    assert config.llm_proxy is not None
    assert config.llm_proxy.period == "monthly"
