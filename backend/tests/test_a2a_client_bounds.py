"""The outbound tuning bounds fail closed at startup."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_outbound_bounds_have_positive_defaults() -> None:
    s = Settings()
    assert s.a2a_client_sweep_interval_seconds > 0
    assert s.a2a_client_verdict_staleness_seconds > 0
    assert s.a2a_client_delegation_ceiling_seconds > 0
    assert s.a2a_client_response_max_bytes > 0
    assert s.a2a_client_breaker_threshold > 0
    assert s.a2a_client_breaker_cooldown_seconds > 0


def test_staleness_threshold_is_shorter_than_the_sweep() -> None:
    # Not arbitrary: the sweep is an optimisation and the delegation-time
    # threshold is the binding control (FR-050). If the threshold were the
    # longer of the two, a delegation could ride a verdict the sweep had
    # already superseded, and SC-008's promise would be measured against the
    # wrong number.
    s = Settings()
    assert s.a2a_client_verdict_staleness_seconds < s.a2a_client_sweep_interval_seconds


@pytest.mark.parametrize(
    "field",
    [
        "a2a_client_sweep_interval_seconds",
        "a2a_client_verdict_staleness_seconds",
        "a2a_client_delegation_ceiling_seconds",
        "a2a_client_response_max_bytes",
        "a2a_client_breaker_threshold",
        "a2a_client_breaker_cooldown_seconds",
    ],
)
@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_bound_fails_closed_at_startup(field: str, bad: int) -> None:
    # A typo in any of these is a startup error, not a silently weakened
    # control (constitution Principle II). Zero is included deliberately: it
    # is the value a shell exports for an unset-but-referenced variable.
    # ValidationError specifically: a bare ``Exception`` would also pass on a
    # typo in the field name, which is the opposite of what this asserts.
    with pytest.raises(ValidationError):
        Settings(**{field: bad})
