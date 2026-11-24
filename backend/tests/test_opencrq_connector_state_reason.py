"""list_opencrq_connectors says why a connector stands where it does.

OpenCRQ derives a connector's state from the flags it answers with it
(module connectors/status.ts, first match wins): platformConfigured,
tenantConfigured, enabled, then the runs. The tool passes that reason
through so an agent can explain an "unavailable" or "not_configured"
connector without a person opening OpenCRQ.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.opencrq.connectors import _state_reason


@pytest.mark.parametrize(
    ("card", "reason"),
    [
        (
            {"source": "openaev", "state": "unavailable", "platformConfigured": False},
            "the OpenCRQ deployment holds no credentials for it for this tenant",
        ),
        (
            {
                "source": "openaev",
                "state": "not_configured",
                "platformConfigured": True,
                "tenantConfigured": False,
            },
            "no service account bound in this tenant",
        ),
        (
            {
                "source": "opencti",
                "state": "not_configured",
                "platformConfigured": True,
                "tenantConfigured": False,
            },
            "no live stream and service account selected in this tenant",
        ),
        (
            {"source": "drata", "state": "never_synced", "enabled": False},
            "switched off in this tenant",
        ),
        (
            {"source": "nist", "state": "never_synced", "enabled": True},
            "configured, no sync has finished yet",
        ),
        (
            {"source": "openaev", "state": "failing", "enabled": True},
            "the last sync failed",
        ),
        (
            {"source": "opencti", "state": "failing"},
            "OpenCTI rejected the stream token",
        ),
    ],
)
def test_each_state_carries_the_reason_opencrq_derived_it_from(card, reason):
    assert _state_reason(card) == reason


@pytest.mark.parametrize(
    "card",
    [
        {"source": "nist", "state": "healthy", "enabled": True},
        {"source": "nist", "state": "syncing", "enabled": True},
        # A state whose flag is missing or contradicts it gives no guess.
        {"source": "openaev", "state": "unavailable"},
        {"source": "openaev", "state": "not_configured", "tenantConfigured": True},
        # A stream that is configured but not enabled has no run to wait for.
        {"source": "opencti", "state": "never_synced", "enabled": True},
    ],
)
def test_no_reason_is_invented(card):
    assert _state_reason(card) == ""
