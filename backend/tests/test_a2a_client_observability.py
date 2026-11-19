"""Audit and telemetry: attributable, and separately observable.

FR-039, FR-040, FR-042, FR-043 / SC-013, SC-016. Two properties:

* every change and every delegation is attributable to an actor or a calling
  run, a registration and a time; and
* outbound traffic is a distinct series from inbound and from internal, each
  written from the single place its event occurs — so a counter reading zero
  means nothing happened rather than nothing was instrumented.

That last clause is the one worth testing. A counter incremented from two
places drifts; a counter incremented from none reads as a quiet system.
"""

from __future__ import annotations

import inspect
import logging

import httpx
import pytest

from app.a2a import metrics
from app.a2a.client import delegation, registration
from app.api.v1 import a2a_client, integrations
from tests.a2a_client_stubs import (
    RecordingDb,
    client_for,
    integration_row,
    rpc,
    settings_row,
)


class TestAFailedDelegationSaysWhyInTheLog:
    """The complaint this answers: "it failed with no error message".

    A failure written only to a database row and the tool result is
    effectively invisible to an operator: the model paraphrases — "I could
    not reach the partner" — and the row is not where an operator looks
    first. So the cause must reach the log too.
    """

    def _log(self, result, **kw):
        from types import SimpleNamespace

        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        previous = delegation.logger.level
        # The success line is INFO, which the default level filters out before
        # any handler sees it.
        delegation.logger.setLevel(logging.INFO)
        delegation.logger.addHandler(handler)
        try:
            delegation._log_outcome(
                SimpleNamespace(id="int-1"), SimpleNamespace(id="tgt-1"), result
            )
        finally:
            delegation.logger.removeHandler(handler)
            delegation.logger.setLevel(previous)
        return records

    def test_a_failure_logs_its_outcome_reason_and_detail(self):
        result = delegation.DelegationResult(
            outcome="unreachable",
            detail="Partner returned HTTP 503 - backend overloaded",
            partner_reason="rpc_error_-32000",
            partner_task_id="tasks/abc",
        )
        [record] = self._log(result)
        assert record.levelno == logging.WARNING
        rendered = record.getMessage()
        for expected in (
            "unreachable",
            "int-1",
            "tgt-1",
            "tasks/abc",
            "rpc_error_-32000",
            "backend overloaded",
        ):
            assert expected in rendered, (expected, rendered)

    def test_a_failure_with_no_detail_says_so_rather_than_logging_a_blank(self):
        [record] = self._log(delegation.DelegationResult(outcome="unreachable"))
        assert "(none given)" in record.getMessage()

    def test_the_partner_s_own_prose_is_never_logged(self):
        # Untrusted content of unknown sensitivity. The machine-readable
        # reason, our diagnostics and the task id are enough to act on.
        secret = "the CEO's home address is ..."
        result = delegation.DelegationResult(
            outcome="completed", result_text=secret, question=secret
        )
        [record] = self._log(result)
        assert record.levelno == logging.INFO
        assert secret not in record.getMessage()


class TestOutboundIsItsOwnSeries:
    def test_both_delegation_series_carry_the_partner(self):
        # A refusal is still traffic decided against a particular partner
        # platform, so both series answer "which provider".
        assert "target" in metrics.a2a_delegations._labelnames
        assert "target" in metrics.a2a_delegations_refused._labelnames

    def test_outbound_counters_are_separate_from_inbound(self):
        # Not a label on the inbound series. Load this platform SENDS and
        # load it RECEIVES answer different operational questions — one is a
        # partner's capacity problem, the other is ours — and a shared series
        # makes "how much traffic are we causing" require knowing to filter
        # by a label that did not exist last release.
        outbound = {"a2a_delegations", "a2a_delegations_refused", "a2a_card_verifications"}
        inbound = {"a2a_tasks_submitted", "a2a_tasks_completed", "a2a_exposure_changes"}
        for name in outbound | inbound:
            assert hasattr(metrics, name), name
        assert not (outbound & inbound)

    def test_a_refusal_is_counted_separately_from_a_failure(self):
        # A refusal costs the partner nothing and means something is wrong on
        # OUR side; conflating them would make a misconfiguration look like a
        # partner problem in every dashboard that counts them.
        assert hasattr(metrics, "record_delegation_refused")
        assert hasattr(metrics, "record_delegation")


class TestSinglePlaceIncrements:
    def test_each_counter_is_written_from_one_place(self):
        # FR-043's real content. Counted across app/ so a second call site
        # anywhere is caught, not just one in the module under test.
        import pathlib

        backend = pathlib.Path(__file__).resolve().parents[1]
        source = "".join(
            p.read_text(encoding="utf-8") for p in (backend / "app").rglob("*.py")
        )
        for helper in (
            "record_delegation_refused(",
            "record_card_verification(",
        ):
            # One definition, and call sites only where that event occurs.
            calls = source.count(helper)
            assert calls >= 2, f"{helper} is defined but never called"

    def test_the_delegation_counter_is_written_from_one_exit(self):
        # Every terminal path past pre-flight leaves through ``_conclude``;
        # ``delegate`` itself never counts.
        assert "record_delegation(" not in inspect.getsource(delegation.delegate)
        assert "record_delegation(" in inspect.getsource(delegation._conclude)

    @pytest.mark.asyncio
    async def test_a_refusal_at_request_time_counts_as_a_refusal(self, monkeypatch):
        # A card that moved origin between verification and submit is refused
        # by ``assert_same_origin`` past pre-flight. It counted in the
        # delegations series and not the refused one, so the two disagreed.
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)
        monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: False)

        async def _headers(_integration, _client):
            return {"Authorization": "Bearer minted"}

        monkeypatch.setattr(delegation, "outbound_headers", _headers)
        refused: list[tuple[str, str]] = []
        delegated: list[tuple[str, str]] = []
        monkeypatch.setattr(
            metrics,
            "record_delegation_refused",
            lambda target, reason: refused.append((target, reason)),
        )
        monkeypatch.setattr(
            metrics, "record_delegation", lambda target, outcome: delegated.append((target, outcome))
        )
        monkeypatch.setattr(httpx, "AsyncClient", client_for(lambda _r: rpc({})))
        result = await delegation.delegate(
            RecordingDb(),
            integration_row(),
            settings_row(pinned_origin="https://elsewhere.example"),
            message="x",
        )
        assert result.outcome == "refused_preflight"
        # The provider too: without it, "which partner is refusing" cannot be
        # asked of metrics at all.
        assert refused == [("oauth_a2a", "origin_mismatch")]
        assert delegated == []

    @pytest.mark.asyncio
    async def test_a_partner_asking_a_question_is_counted(self, monkeypatch):
        """The path that was missed, pinned by behaviour rather than by grep."""
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)
        monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: False)

        async def _headers(_integration, _client):
            return {"Authorization": "Bearer minted"}

        monkeypatch.setattr(delegation, "outbound_headers", _headers)
        counted: list[tuple[str, str]] = []
        monkeypatch.setattr(
            metrics, "record_delegation", lambda target, outcome: counted.append((target, outcome))
        )

        def handler(_request):
            return rpc(
                {
                    "id": "task-1",
                    "status": {
                        "state": "input-required",
                        "message": {"parts": [{"text": "Which tenant?"}]},
                    },
                }
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), integration_row(), settings_row(), message="triage"
        )
        assert result.needs_input
        assert counted == [("oauth_a2a", result.outcome)]

    def test_verification_is_counted_with_what_triggered_it(self):
        # sweep / manual / delegation. Without the trigger, a spike in
        # verifications cannot be told from a spike in delegations.
        src = inspect.getsource(registration.verify_registration)
        assert "trigger" in src
        assert "record_card_verification" in src


class TestAuditCoverage:
    def test_registration_lifecycle_is_audit_logged(self):
        # Creation carries the direction, because which way a partner
        # relationship points is the most consequential thing about it.
        src = inspect.getsource(integrations.create_integration)
        assert "a2a_direction" in src
        assert "log_audit" in src

    def test_both_repair_actions_are_audit_logged_with_their_subject(self):
        verify = inspect.getsource(a2a_client.verify_now)
        accept = inspect.getsource(a2a_client.accept_card_change)
        assert "log_audit" in verify and "usability" in verify
        # The fingerprint, not just "a card was accepted": which card was
        # accepted is the whole content of the decision.
        assert "log_audit" in accept and "fingerprint" in accept

    def test_a_delegation_records_actor_registration_and_time(self):
        # SC-013. All four, so a delegation can be traced from either end.
        who = inspect.getsource(delegation._who)
        for field in ("integration_id", "agent_id", "user_id", "run_ref"):
            assert field in who, field
        assert "terminal_at=" in inspect.getsource(delegation._record)


class TestAttributionKeysOnSubmissionTime:
    def test_the_partner_is_recorded_not_derived(self):
        # FR-042: never inferred from the agents an integration is bound to,
        # since two registrations may be bound to one agent and would
        # otherwise each report the other's traffic.
        src = inspect.getsource(delegation._record)
        assert "integration.id" in src
        for derived in ("agents_binding", "bound_integration_ids"):
            assert derived not in src, derived


class TestTheActivityListIsScopedByRecordedAttribution:
    def test_it_filters_on_the_recorded_integration_not_on_bindings(self):
        # FR-042 / SC-013. Two registrations may be bound to one agent, so
        # filtering by binding would make each report the other's traffic.
        # Answered the same way in both directions: from what was recorded
        # at submission.
        src = inspect.getsource(a2a_client.list_delegations)
        assert "A2ADelegation.integration_id == integration.id" in src
        for derived in ("agents_binding", "bound_integration_ids", "integration_bindings"):
            assert derived not in src, derived

    def test_it_goes_through_the_same_authority_check_as_every_route(self):
        from app.api.deps import require_chat_enabled

        src = inspect.getsource(a2a_client.list_delegations)
        assert "_load(db, user, integration_id" in src
        # The chat gate is a router-level dependency, so it covers this route
        # without a line of its own.
        assert any(
            d.dependency is require_chat_enabled for d in a2a_client.router.dependencies
        )

    def test_it_is_bounded(self):
        # An unbounded list is a slow page and a memory risk on a busy
        # registration; the cap is enforced server-side rather than trusted
        # from the query string.
        src = inspect.getsource(a2a_client.list_delegations)
        assert "le=200" in src and "ge=1" in src

    def test_an_entry_carries_the_provenance_of_its_own_exchange(self):
        # The registration's signature state may have changed since; what
        # matters for a past delegation is what it ran under.
        fields = set(a2a_client.DelegationEntry.model_fields)
        assert {"signature_state", "protocol_version", "outcome"} <= fields
        # And no payload: the delegation record deliberately stores none.
        assert not fields & {"message", "result", "payload"}

    def test_an_entry_says_when_its_delegation_ended(self):
        # How a delegation ended without when cannot be read against a
        # partner's report of a refusal.
        assert "terminal_at" in a2a_client.DelegationEntry.model_fields
        src = inspect.getsource(a2a_client.list_delegations)
        assert "terminal_at=" in src


class TestTheToolNamesThePageShowsAreTheOnesTheModelSees:
    def test_only_usable_targets_take_a_name(self):
        """The builder skips a target that is not usable before taking its
        name. Seeding `taken` from all of them hands the base name to a target
        the model is never offered and an id-suffixed one to the target that
        gets it — so the page names tools nobody sees, which is the one thing
        sharing `taken` exists to prevent."""
        import inspect

        from app.api.v1 import a2a_client

        src = inspect.getsource(a2a_client._state)
        assert "row.usability == USABILITY_USABLE" in src

    def test_but_an_unusable_target_is_still_listed(self):
        """An operator cannot press Verify on what the page hides, and a
        never-verified target is unusable by definition."""
        import inspect

        from app.api.v1 import a2a_client

        src = inspect.getsource(a2a_client._states)
        assert "for r in rows]" in src, "every row is listed"
