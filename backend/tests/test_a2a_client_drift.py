"""A registration verified once and trusted forever is fail-open with steps.

FR-019, FR-050, FR-052 / SC-008, SC-017. Answering benchmark finding F7 at
first registration is the easy half; answering it continuously is what these
tests pin.

The sharpest assertion in the file is that a stale verdict which cannot be
re-verified REFUSES the delegation. That is the one moment where falling back
to what the verdict used to say feels harmless — the partner was fine an hour
ago, the check is a formality, the work is already in flight — and it is
precisely the case a stale verdict cannot see.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.a2a.client import registration
from app.a2a.client.sdk import PartnerCard
from app.a2a.client.verification import (
    Verdict,
    accept_card,
    apply_verdict,
    detect_downgrade,
)
from app.models.a2a import (
    DIRECTION_OUTBOUND,
    SIGNATURE_UNSIGNED,
    SIGNATURE_VERIFIED,
    USABILITY_UNUSABLE,
    USABILITY_USABLE,
)


def _row(**kw):
    base = dict(
        integration_id=uuid.uuid4(),
        direction=DIRECTION_OUTBOUND,
        usability=USABILITY_USABLE,
        unusable_reason=None,
        card_url="https://p.example/c.json",
        invocation_url="https://p.example/rpc",
        pinned_origin="https://p.example",
        agreed_protocol_version="1.0",
        signature_state=SIGNATURE_UNSIGNED,
        card_fingerprint="fp-1",
        accepted_card_fingerprint="fp-1",
        declared_name=None,
        declared_description=None,
        declared_skills=None,
        declared_input_types=None,
        declared_output_types=None,
        last_verified_at=datetime.now(UTC),
        last_verification_error=None,
        consecutive_transient_failures=0,
        circuit_open_until=None,
        last_failure_kind=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _card(**kw):
    base = dict(
        name="P",
        description=None,
        skills=[],
        input_types=["text/plain"],
        output_types=["text/plain"],
        invocation_url="https://p.example/rpc",
        auth_scheme="oauth2",
        protocol_versions=["1.0", "0.3"],
        signature_verified=False,
    )
    base.update(kw)
    return PartnerCard(**base)


def _verdict(**kw):
    base = dict(
        usability=USABILITY_USABLE,
        signature_state=SIGNATURE_UNSIGNED,
        agreed_version="1.0",
        pinned_origin="https://p.example",
        fingerprint="fp-1",
        card=_card(),
    )
    base.update(kw)
    return Verdict(**base)


class TestMaterialChange:
    def test_an_unchanged_card_stays_usable(self):
        row = _row()
        apply_verdict(row, _verdict())
        assert row.usability == USABILITY_USABLE
        assert row.unusable_reason is None

    def test_a_changed_fingerprint_suspends_until_accepted(self):
        # Suspended, not merely flagged: delegation must STOP, or the
        # acceptance step would be advisory.
        row = _row()
        apply_verdict(row, _verdict(fingerprint="fp-2"))
        assert row.usability == USABILITY_UNUSABLE
        assert row.unusable_reason == "card_change_unaccepted"

    def test_the_first_verification_accepts_what_it_saw(self):
        # A partner is not "changed" the first time we look at it, and
        # demanding an acceptance click on registration would be a gate that
        # fires on every registration and assures nothing.
        row = _row(accepted_card_fingerprint=None, card_fingerprint=None)
        apply_verdict(row, _verdict(fingerprint="fp-new"))
        assert row.accepted_card_fingerprint == "fp-new"
        assert row.usability == USABILITY_USABLE

    def test_accepting_resumes_delegation(self):
        row = _row()
        apply_verdict(row, _verdict(fingerprint="fp-2"))
        assert row.usability == USABILITY_UNUSABLE
        # What the accept-card endpoint does, and nothing else runs before
        # the next delegation reads the row.
        accept_card(row)
        assert row.usability == USABILITY_USABLE
        assert row.unusable_reason is None
        assert row.accepted_card_fingerprint == "fp-2"
        # And the next verification of the same card agrees.
        apply_verdict(row, _verdict(fingerprint="fp-2"))
        assert row.usability == USABILITY_USABLE

    def test_accepting_lifts_only_the_suspension_it_answers(self):
        # The reason is a fact about the partner, not a gate this side imposed.
        row = _row(
            usability=USABILITY_UNUSABLE,
            unusable_reason="card_unfetchable",
            last_verification_error="HTTP 503 from card URL",
        )
        accept_card(row)
        assert row.usability == USABILITY_UNUSABLE
        assert row.unusable_reason == "card_unfetchable"
        assert row.last_verification_error == "HTTP 503 from card URL"

    def test_accepting_a_withdrawn_proof_resumes_too(self):
        # A downgrade suspends "until an actor with authority accepts what
        # the partner withdrew" — so accepting must end it, as for a change.
        row = _row(signature_state=SIGNATURE_VERIFIED)
        apply_verdict(row, _verdict(signature_state=SIGNATURE_UNSIGNED, fingerprint="fp-2"))
        assert row.unusable_reason == "signature_downgraded"
        accept_card(row)
        assert row.usability == USABILITY_USABLE


class TestDowngrades:
    def test_losing_a_signature_is_a_downgrade_not_a_new_normal(self):
        # An unsigned card is the state of the art; a card that once carried
        # a VERIFYING signature and now does not is evidence — the partner
        # stopped doing something it demonstrably could.
        row = _row(signature_state=SIGNATURE_VERIFIED)
        assert detect_downgrade(row, _verdict(signature_state=SIGNATURE_UNSIGNED)).reason == (
            "signature_downgraded"
        )

    def test_dropping_the_agreed_version_is_a_downgrade(self):
        row = _row(agreed_protocol_version="1.0")
        v = _verdict(agreed_version="0.3", card=_card(protocol_versions=["0.3"]))
        assert detect_downgrade(row, v).reason == "protocol_version_downgraded"

    def test_a_patch_level_declaration_is_not_a_downgrade(self):
        """A patch level is not a withdrawal of the version it belongs to.

        Negotiation normalises a partner's patch level and stores the
        normalised value, so a card declaring ``0.3.0`` is agreed as ``0.3``.
        Compared by exact string those two differ, which reports every Vertex
        partner as having stopped serving what it still serves - and only on
        the second verification, once a first has been accepted, which is the
        worst possible moment to learn it.
        """
        row = _row(agreed_protocol_version="0.3")
        v = _verdict(agreed_version="0.3", card=_card(protocol_versions=["0.3.0"]))
        assert detect_downgrade(row, v) is None

    def test_a_real_withdrawal_still_suspends_and_says_what_changed(self):
        row = _row(agreed_protocol_version="1.0")
        v = _verdict(agreed_version="0.3", card=_card(protocol_versions=["0.3.0"]))
        downgrade = detect_downgrade(row, v)
        assert downgrade.reason == "protocol_version_downgraded"
        # The detail names both sides. A reason alone sent an operator to ask a
        # partner about a change the partner had not made.
        assert "1.0" in downgrade.detail and "0.3.0" in downgrade.detail

    def test_a_downgrade_suspends_with_its_own_reason(self):
        # Its own reason, not "card changed": the operator needs to know a
        # capability was withdrawn, which is a different conversation with
        # the partner.
        row = _row(signature_state=SIGNATURE_VERIFIED)
        apply_verdict(row, _verdict(signature_state=SIGNATURE_UNSIGNED, fingerprint="fp-2"))
        assert row.unusable_reason == "signature_downgraded"

    def test_gaining_a_signature_is_not_a_downgrade(self):
        row = _row(signature_state=SIGNATURE_UNSIGNED)
        assert detect_downgrade(row, _verdict(signature_state=SIGNATURE_VERIFIED)) is None

    def test_an_upgrade_needs_no_permission(self):
        # A newer version appearing is not something to ask an operator
        # about; only weaker terms are.
        row = _row(agreed_protocol_version="0.3")
        v = _verdict(agreed_version="1.0", card=_card(protocol_versions=["1.0", "0.3"]))
        assert detect_downgrade(row, v) is None

    def test_an_upgrade_that_drops_the_old_version_is_still_an_upgrade(self):
        """A partner that moves OFF the agreed version has not withdrawn it.

        Membership alone cannot tell the two apart - 0.3 is absent either
        way - and reading it as a withdrawal suspends every registration
        that agreed 0.3 the moment its partner goes v1.0-only.
        """
        row = _row(agreed_protocol_version="0.3")
        v = _verdict(agreed_version="1.0", card=_card(protocol_versions=["1.0"]))
        assert detect_downgrade(row, v) is None

    def test_a_junk_version_beside_a_newer_one_does_not_raise(self):
        # The list comes from a partner's card. ``str.isdigit`` is true for
        # characters ``int`` rejects, and detect_downgrade is reached from
        # preflight, which is documented never to raise.
        row = _row(agreed_protocol_version="0.3")
        v = _verdict(agreed_version="1.0", card=_card(protocol_versions=["1.0", "1.\u00b2"]))
        assert detect_downgrade(row, v) is None

    def test_an_overlong_version_component_does_not_raise_either(self):
        # ASCII digits are not enough: ``int`` refuses a decimal string past
        # its conversion limit, which a card can declare as easily.
        row = _row(agreed_protocol_version="0.3")
        v = _verdict(
            agreed_version="1.0",
            card=_card(protocol_versions=["1.0", "1." + "9" * 5000]),
        )
        assert detect_downgrade(row, v) is None

    def test_that_upgrade_renegotiates_and_is_not_called_a_withdrawal(self):
        # The new card still needs accepting, because it changed. What it
        # must not say is that the partner withdrew something: that sends the
        # operator to ask about a capability the partner in fact gained.
        row = _row(agreed_protocol_version="0.3")
        apply_verdict(
            row,
            _verdict(
                agreed_version="1.0",
                card=_card(protocol_versions=["1.0"]),
                fingerprint="fp-2",
            ),
        )
        assert row.agreed_protocol_version == "1.0"
        assert row.unusable_reason == "card_change_unaccepted"


class TestStaleness:
    def test_a_fresh_verdict_is_read_not_re_checked(self):
        # Keeping the common case free is why the threshold exists at all: a
        # partner's card endpoint must not be on the critical path of every
        # run.
        assert registration.verdict_is_stale(_row()) is False

    def test_an_unverified_registration_is_stale(self):
        assert registration.verdict_is_stale(_row(last_verified_at=None)) is True

    def test_a_verdict_past_the_threshold_is_stale(self):
        from app.core.config import settings as app_settings

        old = datetime.now(UTC) - timedelta(
            seconds=app_settings.a2a_client_verdict_staleness_seconds + 60
        )
        assert registration.verdict_is_stale(_row(last_verified_at=old)) is True

    def test_a_naive_timestamp_does_not_raise(self):
        # Postgres can hand back a naive datetime; comparing it to an aware
        # "now" raises, and a raise inside the pre-flight would be read as
        # "cannot evaluate" — which fails closed and would strand a partner.
        naive = datetime.now(UTC).replace(tzinfo=None)
        assert registration.verdict_is_stale(_row(last_verified_at=naive)) is False


class TestTheFailClosedMoment:
    @pytest.mark.asyncio
    async def test_a_stale_verdict_that_cannot_re_verify_refuses(self, monkeypatch):
        # SC-017, and the assertion this whole file exists for. A delegation
        # proceeding here would be the fail-closed rule bending at exactly
        # the moment it is tempting to.
        from app.a2a.client import delegation

        monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: True)

        async def _failed_reverify(_integration, target):
            # As production does: the verdict is written onto the row.
            verdict = Verdict(
                usability=USABILITY_UNUSABLE,
                unusable_reason="card_unfetchable",
                error_detail="partner did not answer",
            )
            apply_verdict(target, verdict)
            return verdict

        monkeypatch.setattr(delegation, "reverify_before_delegating", _failed_reverify)

        with pytest.raises(delegation.PreflightRefusedError) as exc:
            await delegation._check_freshness(None, object(), _row())
        assert exc.value.reason == "card_unfetchable"

    @pytest.mark.asyncio
    async def test_a_downgrade_found_by_the_re_verification_refuses(self, monkeypatch):
        # ``apply_verdict`` returns a USABLE verdict while suspending the row
        # for a downgrade, and the usability gate ran before this. Read from
        # the verdict, the delegation went out under the withdrawn terms —
        # the acceptance FR-052 exists to require, skipped.
        from app.a2a.client import delegation

        monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: True)

        async def _renegotiated(_integration, target):
            verdict = _verdict(agreed_version="0.3", card=_card(protocol_versions=["0.3"]))
            apply_verdict(target, verdict)
            return verdict

        monkeypatch.setattr(delegation, "reverify_before_delegating", _renegotiated)
        row = _row(agreed_protocol_version="1.0")
        with pytest.raises(delegation.PreflightRefusedError) as exc:
            await delegation._check_freshness(None, object(), row)
        assert exc.value.reason == "protocol_version_downgraded"

    @pytest.mark.asyncio
    async def test_a_stale_verdict_that_re_verifies_proceeds(self, monkeypatch):
        from app.a2a.client import delegation

        monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: True)

        async def _ok(_integration, _target):
            return None

        monkeypatch.setattr(delegation, "reverify_before_delegating", _ok)
        # No exception is the assertion.
        await delegation._check_freshness(None, object(), _row())

    @pytest.mark.asyncio
    async def test_a_contended_verdict_write_refuses_as_retryable(self, monkeypatch):
        # Another session holds the target row: the delegation refuses with a
        # named transient reason in seconds, never waits out the statement
        # timeout that rolls its session back.
        from app.a2a.client import delegation

        monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: True)

        async def _contended(_integration, _target):
            raise registration.VerdictWriteContendedError("held")

        monkeypatch.setattr(delegation, "reverify_before_delegating", _contended)
        with pytest.raises(delegation.PreflightRefusedError) as exc:
            await delegation._check_freshness(None, object(), _row())
        assert exc.value.reason == delegation.OUTCOME_VERIFICATION_CONTENDED

    @pytest.mark.asyncio
    async def test_the_delegation_session_never_writes_the_verdict(self, monkeypatch):
        # The bookkeeping write is not the send transaction's: the session the
        # delegation was given is never flushed by the re-verification.
        from app.a2a.client import delegation

        monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: True)

        async def _ok(_integration, _target):
            return None

        class _NoWrites:
            async def flush(self):
                raise AssertionError("the delegation session was flushed")

            async def execute(self, *_a, **_k):
                raise AssertionError("the delegation session was queried")

        monkeypatch.setattr(delegation, "reverify_before_delegating", _ok)
        await delegation._check_freshness(_NoWrites(), object(), _row())


class TestSweepIsNotTheBindingControl:
    def test_the_sweep_only_claims_target_rows(self):
        import inspect

        from app.a2a.client import sweep

        src = inspect.getsource(sweep.due_targets)
        # Inbound registrations are excluded by CONSTRUCTION rather than by a
        # predicate: they own no targets, having no card to verify, so they
        # cannot enter this query however many partner credentials exist for
        # the other direction.
        assert "A2AClientTarget" in src
        assert "A2AIntegrationSettings" not in src
        # SKIP LOCKED so concurrent replicas divide the work rather than
        # duplicating it.
        assert "skip_locked=True" in src

    def test_the_stalest_is_served_first(self):
        # No registration can be starved by a busier neighbour.
        import inspect

        from app.a2a.client import sweep

        assert "nulls_first" in inspect.getsource(sweep.due_targets)

    def test_one_failing_partner_does_not_stop_the_batch(self):
        import inspect

        from app.a2a.client import sweep

        src = inspect.getsource(sweep.sweep_once)
        assert "continue" in src or "except" in src


class TestAVerdictWithNoEndpointIsStale:
    """Fresh by the clock, useless in practice.

    A target that is `usable` with no invocation endpoint has a verdict written
    before the endpoint was recorded, so delegation has nowhere to send work.
    Treating it as stale is what makes such a row heal on the next delegation
    rather than refusing with `endpoint_unknown` until its verdict ages out,
    with an operator told to press Test Connection on every one. The state is
    still reachable, so the guard stays.
    """

    def test_a_usable_row_with_no_endpoint_is_stale(self):
        from app.a2a.client.registration import verdict_is_stale

        row = _row(invocation_url=None)
        row.last_verified_at = datetime.now(UTC)  # just verified
        assert verdict_is_stale(row) is True

    def test_a_usable_row_with_an_endpoint_is_not(self):
        from app.a2a.client.registration import verdict_is_stale

        row = _row(invocation_url="https://p.example/rpc")
        row.last_verified_at = datetime.now(UTC)
        assert verdict_is_stale(row) is False

    def test_an_unusable_row_is_judged_on_age_alone(self):
        # The endpoint check keys on `usable`: an unusable registration has
        # other reasons to re-verify, and folding this in would make an
        # unrelated refusal look like a missing endpoint.
        from app.a2a.client.registration import verdict_is_stale
        from app.models.a2a import USABILITY_UNUSABLE

        row = _row(invocation_url=None, usability=USABILITY_UNUSABLE)
        row.last_verified_at = datetime.now(UTC)
        assert verdict_is_stale(row) is False
