"""The breaker counts transient failures only, and both surfaces agree.

FR-030, FR-053 / SC-010, SC-019. The interesting property is not that a
breaker exists but that it and the health verdict read ONE classification —
two surfaces answering one question from different data disagree the moment
either is used.

Circuit state is per registration. A failing partner is a fact about that
partner, not about the agent that happens to bind it — so two partners on one
agent must never suppress each other.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.a2a.client import breaker, failures
from app.core.config import settings as app_settings
from app.models.a2a import UNUSABLE_CREDENTIAL_REJECTED


def _row(**kw):
    base = dict(
        integration_id=uuid.uuid4(),
        consecutive_transient_failures=0,
        circuit_open_until=None,
        last_failure_kind=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestOnlyTransientCounts:
    def test_a_durable_failure_never_increments_the_counter(self):
        # Retrying a rejected credential cannot succeed, so counting it toward
        # a cooldown would delay the operator's only real signal — the
        # registration going unusable with a reason they can act on.
        row = _row()
        opened = breaker.record_failure(
            row, failures.durable(UNUSABLE_CREDENTIAL_REJECTED)
        )
        assert opened is False
        assert row.consecutive_transient_failures == 0
        assert row.circuit_open_until is None
        # It IS recorded, so the health verdict can read the same kind.
        assert row.last_failure_kind == "durable"

    def test_transient_failures_accumulate(self):
        row = _row()
        for i in range(1, app_settings.a2a_client_breaker_threshold):
            assert breaker.record_failure(row, failures.TRANSIENT) is False
            assert row.consecutive_transient_failures == i
            assert row.circuit_open_until is None

    def test_the_threshold_opens_the_circuit(self):
        row = _row()
        for _ in range(app_settings.a2a_client_breaker_threshold - 1):
            breaker.record_failure(row, failures.TRANSIENT)
        assert breaker.record_failure(row, failures.TRANSIENT) is True
        assert breaker.is_open(row)


class TestBackoff:
    def test_each_further_breach_waits_longer(self):
        row = _row()
        waits = []
        for _ in range(app_settings.a2a_client_breaker_threshold + 3):
            breaker.record_failure(row, failures.TRANSIENT)
            if row.circuit_open_until:
                waits.append(row.circuit_open_until)
        # A partner down for an hour should not be probed every five minutes.
        gaps = [
            (w - datetime.now(UTC)).total_seconds() for w in waits
        ]
        assert gaps == sorted(gaps)
        assert gaps[-1] > gaps[0]

    def test_the_backoff_is_capped(self):
        # A long outage must not push the next retry beyond the point where an
        # operator would reasonably expect the platform to try again itself.
        row = _row(consecutive_transient_failures=100)
        breaker.record_failure(row, failures.TRANSIENT)
        wait = (row.circuit_open_until - datetime.now(UTC)).total_seconds()
        assert wait <= 3600 + 5


class TestRecovery:
    def test_success_forgets_the_run_of_failures(self):
        # Consecutive means consecutive: a partner that fails four times and
        # then answers has not banked four strikes toward a future cooldown.
        row = _row(consecutive_transient_failures=4)
        breaker.record_success(row)
        assert row.consecutive_transient_failures == 0
        assert row.circuit_open_until is None
        assert row.last_failure_kind is None

    def test_an_operator_fix_closes_the_circuit_without_waiting(self):
        # FR-030's escape hatch: someone who has just corrected a credential
        # must not wait out a cooldown that was counting a symptom of the
        # thing they fixed.
        row = _row(
            consecutive_transient_failures=9,
            circuit_open_until=datetime.now(UTC) + timedelta(hours=1),
        )
        breaker.close(row)
        assert not breaker.is_open(row)

    def test_an_expired_cooldown_is_closed(self):
        row = _row(circuit_open_until=datetime.now(UTC) - timedelta(seconds=1))
        assert not breaker.is_open(row)

    def test_a_naive_timestamp_is_not_treated_as_open_forever(self):
        # Postgres can hand back a naive datetime depending on the column and
        # driver; comparing that to an aware "now" raises, and a raise here
        # would be read by the caller as "cannot evaluate" — which fails
        # closed and would strand a partner permanently.
        row = _row(circuit_open_until=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1))
        assert not breaker.is_open(row)


class TestPerRegistration:
    def test_two_partners_do_not_suppress_each_other(self):
        # SC-010's cross-suppression clause. State lives on the registration
        # row precisely so this is structural rather than something the
        # delegation path has to remember.
        first, second = _row(), _row()
        for _ in range(app_settings.a2a_client_breaker_threshold):
            breaker.record_failure(first, failures.TRANSIENT)
        assert breaker.is_open(first)
        assert not breaker.is_open(second)
        assert second.consecutive_transient_failures == 0


class TestOneClassificationTwoConsumers:
    def test_the_breaker_reads_the_same_object_the_verdict_does(self):
        # Not "the same value" — the same Classification. Two consumers, one
        # source, so they cannot drift.
        row = _row()
        classification = failures.CREDENTIAL_CANNOT_MINT
        breaker.record_failure(row, classification)
        assert row.last_failure_kind == classification.kind
        assert classification.reason in {
            r for r in __import__("app.models.a2a", fromlist=["x"]).UNUSABLE_REASONS
        }
