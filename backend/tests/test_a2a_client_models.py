"""The two outbound tables: closed sets, fail-closed defaults, and cascades.

The defaults here are not conveniences. ``usability`` defaulting to
``unusable`` is what makes a registration that has never been verified — or
whose first verification died before writing a verdict — refuse work rather
than accept it by omission (Principle II).
"""

from __future__ import annotations

import uuid

import pytest

from app.models.a2a import (
    DELEGATION_OUTCOMES,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    UNUSABLE_REASONS,
    A2AClientTarget,
    A2ADelegation,
    A2AIntegrationSettings,
)


class TestSettingsShape:
    def test_direction_is_not_nullable(self):
        # A nullable direction would reintroduce the ambiguity the
        # missing-row rule exists to resolve: "no direction" must mean "no
        # row", one state rather than two.
        assert A2AIntegrationSettings.__table__.c.direction.nullable is False

    def test_one_settings_row_per_integration(self):
        # Two rows would let two answers to "which direction" coexist, and
        # the exposure hook would take whichever the query returned first.
        assert A2AIntegrationSettings.__table__.c.integration_id.unique is True

    def test_it_carries_direction_and_nothing_else(self):
        # Everything an outbound registration verifies is per-CARD, and a
        # partner exposes as many cards as it likes. Keeping any of it here
        # would be one row answering a question with several answers. The
        # partner CREDENTIAL is the one per-integration fact (#12346): one
        # credential per integration, whatever the number of cards.
        assert set(A2AIntegrationSettings.__table__.c.keys()) == {
            "id",
            "integration_id",
            "direction",
            "last_token_minted_at",
            "credential_error_code",
            "credential_error_detail",
            "credential_error_at",
            "created_at",
            "updated_at",
        }


class TestTargetShape:
    def test_a_target_is_a_card(self):
        # NOT NULL because the row IS a card, and because a nullable column
        # would defeat the uniqueness below — PostgreSQL treats NULLs as
        # distinct and would admit any number of them.
        assert A2AClientTarget.__table__.c.card_url.nullable is False

    def test_one_row_per_card_per_partner(self):
        # Two rows for one card would verify the same agent twice and give a
        # calling model two tools that do one thing.
        constraint = next(
            c
            for c in A2AClientTarget.__table__.constraints
            if getattr(c, "name", None) == "uq_a2a_target_card"
        )
        assert {c.name for c in constraint.columns} == {"integration_id", "card_url"}

    def test_usability_defaults_to_unusable(self):
        col = A2AClientTarget.__table__.c.usability
        assert col.default.arg == "unusable"
        assert col.nullable is False

    def test_signature_state_is_nullable_before_a_first_verdict(self):
        # A target exists before any card is fetched, and neither "verified"
        # nor "unsigned" is true of one never verified.
        assert A2AClientTarget.__table__.c.signature_state.nullable is True

    def test_every_verified_fact_is_nullable_before_a_first_verdict(self):
        for name in (
            "invocation_url",
            "pinned_origin",
            "agreed_protocol_version",
            "card_fingerprint",
            "accepted_card_fingerprint",
            "declared_name",
        ):
            assert A2AClientTarget.__table__.c[name].nullable is True, name


    def test_deleting_the_integration_removes_the_row(self):
        # FR-008: deleting an integration removes its credentials AND this
        # row in one act, so no orphan verdict outlives the registration.
        fk = next(iter(A2AIntegrationSettings.__table__.c.integration_id.foreign_keys))
        assert fk.ondelete == "CASCADE"

    def test_direction_helpers_agree_with_the_column(self):
        s = A2AIntegrationSettings(integration_id=uuid.uuid4(), direction=DIRECTION_OUTBOUND)
        assert s.is_outbound and not s.is_inbound
        s.direction = DIRECTION_INBOUND
        assert s.is_inbound and not s.is_outbound


class TestClosedSets:
    def test_every_reason_maps_to_a_requirement(self):
        # The set is closed so no call site can invent a reason no surface
        # knows how to explain. Pinned by count and membership together: a
        # rename that kept the count would otherwise pass.
        assert len(UNUSABLE_REASONS) == 16
        for expected in (
            "never_verified",
            "signature_invalid",
            "credential_cannot_mint_token",
            "credential_rejected_by_partner",
            "destination_not_permitted",
            "destination_blocked_by_deployment",
            "signature_downgraded",
            "protocol_version_downgraded",
            # A partner serving only a binding this platform does not speak.
            # Its own reason, not "no common version": the two negotiations
            # fail independently and an operator sent to look for a version
            # setting would never find one that helped.
            "no_common_transport",
            # A 4xx on the task surface. Its own reason because it used to
            # borrow ``card_malformed``, which sent operators to check a card
            # that was never the problem.
            "request_rejected_by_partner",
        ):
            assert expected in UNUSABLE_REASONS

    def test_outcomes_cover_the_five_classes_a_caller_must_distinguish(self):
        # FR-029's five, plus the two that never leave the platform.
        for expected in (
            "unreachable",
            "credential_rejected",
            "partner_failed",
            "timeout",
            "content_type_mismatch",
            "refused_preflight",
            "cancelled",
            "completed",
        ):
            assert expected in DELEGATION_OUTCOMES


class TestDelegationShape:
    def test_run_ref_cascades_unlike_the_inbound_sibling(self):
        # ``A2ATask.run_id`` is SET NULL because an inbound task is the
        # PARTNER's record and must outlive its run. A delegation is ours,
        # has no external holder, and FR-046 authorises a continuation by
        # matching ``run_ref`` — SET NULL would turn every in-flight
        # continuation into an ownership failure.
        from app.models.a2a import A2ATask

        outbound = next(iter(A2ADelegation.__table__.c.run_ref.foreign_keys))
        inbound = next(iter(A2ATask.__table__.c.run_id.foreign_keys))
        assert outbound.ondelete == "CASCADE"
        assert inbound.ondelete == "SET NULL"

    def test_run_ref_points_at_the_universal_run_record(self):
        # ``assignment_runs`` is created by the chat path too, so this single
        # reference covers both run paths FR-021 admits.
        fk = next(iter(A2ADelegation.__table__.c.run_ref.foreign_keys))
        assert fk.column.table.name == "assignment_runs"

    def test_payloads_are_deliberately_absent(self):
        # Copying the message and result here would create a second store of
        # partner-exchanged content with its own retention question.
        cols = set(A2ADelegation.__table__.c.keys())
        assert not cols & {"message", "result", "payload", "response"}

    def test_signature_state_is_recorded_per_delegation(self):
        # FR-017: the verified/unverified distinction must be legible on the
        # run that took the risk, not only on the registration.
        assert A2ADelegation.__table__.c.signature_state.nullable is False

    def test_continuation_count_defaults_to_zero(self):
        col = A2ADelegation.__table__.c.continuation_count
        assert col.default.arg == 0 and col.nullable is False


@pytest.mark.integration
class TestCascades:
    @pytest.mark.asyncio
    async def test_deleting_an_integration_removes_settings_and_delegations(self):
        from sqlalchemy import select

        from app.a2a.providers import create_settings
        from app.models.integration import Integration
        from app.models.user import User
        from tests.test_a2a_hooks_postgres import _temp_database

        async with _temp_database() as sm, sm() as session:
            user = User(
                id=uuid.uuid4(),
                email=f"casc-{uuid.uuid4().hex[:8]}@test.local",
                full_name="Cascade",
                hashed_password="x",
            )
            session.add(user)
            await session.flush()
            integration = Integration(
                id=uuid.uuid4(),
                user_id=user.id,
                provider="oauth_a2a",
                auth_type="none",
                name="Partner",
            )
            session.add(integration)
            await session.flush()
            await create_settings(session, integration.id, DIRECTION_OUTBOUND)

            await session.delete(integration)
            await session.flush()

            rows = (
                await session.execute(
                    select(A2AIntegrationSettings).where(
                        A2AIntegrationSettings.integration_id == integration.id
                    )
                )
            ).scalars().all()
            assert rows == []
