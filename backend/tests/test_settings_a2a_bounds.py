"""Every A2A bound fails closed on a misconfigured value (Constitution II).

These are controls, not tuning knobs. A zero task lifetime reaps every task on
creation; a zero callback allowance advertises a capability on the card
and then refuses it. A typo belongs at startup, not in behaviour nobody notices.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

#: Spec 010's four bounds, plus one of the pre-existing client bounds to pin
#: that they share a single validator rather than drifting into two.
_BOUNDS = (
    "a2a_task_max_lifetime_seconds",
    "a2a_push_max_attempts",
    "a2a_push_configs_per_task",
    "a2a_client_breaker_threshold",
    "a2a_client_delegation_abandoned_seconds",
    "a2a_cancel_poll_seconds",
)


@pytest.mark.parametrize("field", _BOUNDS)
@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_bound_is_refused_at_startup(field, bad):
    with pytest.raises(ValidationError) as exc:
        Settings(**{field: bad})
    assert field.upper() in str(exc.value)


@pytest.mark.parametrize("field", _BOUNDS)
def test_a_positive_bound_is_accepted(field):
    assert getattr(Settings(**{field: 7}), field) == 7


def test_the_task_lifetime_exceeds_the_lease_it_supplements():
    """The lease says "this run is alive"; the lifetime bounds the whole task,
    including time spent waiting on delegated work. A lifetime below the lease
    would reap a task that is merely running."""
    s = Settings()
    assert s.a2a_task_max_lifetime_seconds > s.a2a_run_lease_seconds


def test_the_floor_waits_longer_than_one_attempt():
    """The floor's age is patience for someone ELSE to report: a registered
    callback, or the agent collecting the task id it was handed. At or below
    the ceiling it would chase the moment an attempt gave up, pre-empting
    both — the callback would rarely be the mechanism it was registered as."""
    s = Settings()
    assert (
        s.a2a_client_floor_interval_seconds
        > s.a2a_client_delegation_ceiling_seconds
    )


def test_every_new_bound_shares_the_one_validator():
    """A second validator would let the two disagree about what a bound means."""
    validated = {
        name
        for decorator in Settings.__pydantic_decorators__.field_validators.values()
        for name in decorator.info.fields
    }
    assert set(_BOUNDS) <= validated


def test_abandonment_waits_longer_than_the_attempt_and_the_floor():
    """Abandonment measures SILENCE, not one attempt's wait — the distinction
    the setting exists to make. At or below the ceiling it would presume a
    partner gone the moment a single attempt handed back, which is the unit
    error it was split out to correct; at or below the floor interval it would
    fire before the floor had asked even once."""
    s = Settings()
    assert (
        s.a2a_client_delegation_abandoned_seconds
        > s.a2a_client_delegation_ceiling_seconds
    )
    assert (
        s.a2a_client_delegation_abandoned_seconds
        > s.a2a_client_floor_interval_seconds
    )


def test_a_partner_is_not_presumed_gone_before_we_would_give_up_ourselves():
    """The benchmark that made the old bound wrong: ``a2a_task_max_lifetime``
    is how long a task may live on the surface THIS platform serves. Presuming
    a partner gone sooner has the two halves of one feature disagreeing about
    the same piece of work — and the e2e pair runs them against each other."""
    s = Settings()
    assert (
        s.a2a_client_delegation_abandoned_seconds >= s.a2a_task_max_lifetime_seconds
    )


def test_the_cancel_poll_leaves_room_for_the_lease():
    """The poll floors the heartbeat cadence, so a poll above a quarter-lease
    has the supervisor beating slower than the reaper reaps — and a live run
    is taken as abandoned. A typo belongs at startup, not in behaviour nobody
    notices."""
    with pytest.raises(ValidationError):
        Settings(a2a_cancel_poll_seconds=3600, a2a_run_lease_seconds=900)


def test_a_poll_at_the_quarter_lease_is_accepted():
    """The bound is the quarter-lease itself, not below it: at exactly a
    quarter the run still beats four times inside one lease."""
    s = Settings(a2a_cancel_poll_seconds=225, a2a_run_lease_seconds=900)
    assert s.a2a_cancel_poll_seconds == 225
