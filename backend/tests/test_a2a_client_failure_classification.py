"""One classification, two consumers, no second opinion.

FR-053 / SC-019. The circuit breaker and the health verdict ask different
questions of the same failure, and two surfaces answering one question from
different data disagree the moment either is used. These tests pin the
property that makes that impossible: there is exactly one classifier, and the
reason it produces comes from a closed set.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.a2a.client import failures
from app.a2a.client.destinations import DestinationRefusedError
from app.models.a2a import (
    FAILURE_DURABLE,
    FAILURE_TRANSIENT,
    UNUSABLE_CREDENTIAL_REJECTED,
    UNUSABLE_REASONS,
)


class TestClosedReasonSet:
    def test_a_reason_outside_the_set_is_refused(self):
        # What stops a closed set quietly opening: a call site inventing a
        # reason no surface knows how to explain.
        with pytest.raises(ValueError, match="closed unusable-reason set"):
            failures.durable("something_new")

    def test_every_set_member_classifies_as_durable(self):
        for reason in UNUSABLE_REASONS:
            c = failures.durable(reason)
            assert c.reason == reason
            assert c.kind == FAILURE_DURABLE
        assert failures.CREDENTIAL_CANNOT_MINT.reason in UNUSABLE_REASONS

    def test_every_reason_has_an_operator_next_action(self):
        # The health verdict renders ``probe._WHAT_TO_DO[reason]`` as the
        # operator's next step. A reason missing from that map reaches the
        # UI as the raw reason string — a verdict without a next action,
        # which sends an operator guessing.
        from app.a2a.client import probe

        assert set(probe._WHAT_TO_DO) >= UNUSABLE_REASONS

    def test_transient_carries_no_reason(self):
        # The reason set describes why a registration is UNUSABLE. A partner
        # that timed out is not unusable, it is unreachable right now, and
        # giving it a reason would put it on a surface that means something
        # else.
        assert failures.TRANSIENT.reason is None
        assert failures.TRANSIENT.kind == FAILURE_TRANSIENT


class TestHttpStatus:
    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_credential_is_durable(self, status):
        # Retrying cannot fix it, so it must never reach the breaker's
        # counter — it makes the registration unusable directly.
        c = failures.classify_http_status(status)
        assert c.is_durable
        assert c.reason == "credential_rejected_by_partner"

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_partner_server_error_is_transient(self, status):
        assert failures.classify_http_status(status).is_transient

    def test_not_found_is_durable_not_transient(self):
        # A partner endpoint that is not there is a configuration problem —
        # a wrong card URL, a deleted agent. Retrying it every run for a
        # cooldown is noise nobody sees; an unusable verdict is actionable.
        assert failures.classify_http_status(404).is_durable

    def test_other_client_errors_are_durable(self):
        # A 4xx that is not auth or not-found is us, not them, and a
        # malformed request will be malformed again next run.
        assert failures.classify_http_status(422).is_durable
        # ...and names the REQUEST, not the card. Reported as
        # ``card_malformed``, a request-shaping bug sends operators to
        # re-check a card URL that is fine.
        assert (
            failures.classify_http_status(400).reason == "request_rejected_by_partner"
        )
        assert failures.classify_http_status(422).reason == "request_rejected_by_partner"


class TestExceptions:
    @pytest.mark.parametrize(
        "exc", [TimeoutError("x"), ConnectionError("x"), OSError("x")]
    )
    def test_transport_failures_are_transient(self, exc):
        assert failures.classify_exception(exc).is_transient

    def test_refused_destination_keeps_the_guard_s_own_reason(self):
        # Three different operator problems reach here — a forbidden address,
        # an unencrypted URL, a name that will not resolve — and flattening
        # them to one reason would send an operator to fix the wrong thing.
        for reason in (
            "destination_not_permitted",
            "transport_unverified",
            "card_unfetchable",
        ):
            c = failures.classify_exception(
                DestinationRefusedError("nope", reason=reason)
            )
            assert c.is_durable
            assert c.reason == reason

    def test_unknown_exception_is_transient_by_design(self):
        # Deliberately the less-safe direction, once. A durable classification
        # takes a registration out of service and demands attention; reaching
        # that on an exception nobody anticipated would turn a library's odd
        # error into an outage of the partner relationship. A wrong transient
        # classification costs a cooldown, which self-heals.
        class WeirdError(Exception):
            pass

        assert failures.classify_exception(WeirdError()).is_transient


class TestTheTwoConsumersCannotDisagree:
    def test_durable_and_transient_are_mutually_exclusive(self):
        # The breaker keys on ``is_durable``; the health verdict reads the
        # reason. If a classification could be both durable and transient,
        # one failure would both open the circuit and mark the registration
        # unusable, which is the disagreement FR-053 exists to prevent.
        durable = failures.durable(UNUSABLE_CREDENTIAL_REJECTED)
        for c in (failures.TRANSIENT, durable):
            assert c.is_durable != c.is_transient

    def test_classification_is_immutable(self):
        # Two consumers read the same object; one of them mutating it would
        # be a disagreement created after the fact.
        # ``frozen=True`` raises FrozenInstanceError specifically; a bare
        # Exception would also pass on a typo in the attribute name.
        with pytest.raises(dataclasses.FrozenInstanceError):
            failures.TRANSIENT.kind = FAILURE_DURABLE


class TestTheMechanismIsInvisibleInTheResult:
    """Spec 011 FR-021 / SC-007. Three ways of waiting, one set of answers: a
    calling agent must not be able to tell how the platform waited from what it
    got back, or every prompt written against one mechanism breaks under
    another. The mechanism is recorded for an operator and nowhere else.
    """

    def test_the_result_carries_no_mechanism(self):
        """It is a fact about the exchange, not about the answer, and lives on
        the delegation row."""
        from app.a2a.client.delegation import DelegationResult

        fields = {f.name for f in dataclasses.fields(DelegationResult)}
        assert "mechanism" not in fields

    def test_what_the_model_sees_carries_no_mechanism(self):
        from app.a2a.client.delegation import DelegationResult
        from app.a2a.client.tool import render_result
        from app.models.a2a import (
            DELEGATION_MECHANISMS,
            OUTCOME_COMPLETED,
            OUTCOME_TIMEOUT,
        )

        for outcome in (OUTCOME_COMPLETED, OUTCOME_TIMEOUT):
            rendered = render_result(
                DelegationResult(outcome=outcome, result_text="x"),
                partner_name="Partner",
            )
            assert "mechanism" not in rendered
            for value in DELEGATION_MECHANISMS:
                assert value not in str(rendered)

    def test_every_recorded_mechanism_is_from_the_closed_set(self):
        """The same discipline as the failure classifier above: a value nobody
        declared is a value no surface can render."""
        import re
        from pathlib import Path

        from app.models.a2a import DELEGATION_MECHANISMS

        source = Path(__file__).resolve().parents[1] / "app" / "a2a" / "client"
        assigned = set()
        for path in source.glob("*.py"):
            assigned |= set(re.findall(r"mechanism=MECHANISM_([A-Z_]+)", path.read_text(encoding="utf-8")))
        assert assigned
        assert {m.lower() for m in assigned} <= DELEGATION_MECHANISMS
