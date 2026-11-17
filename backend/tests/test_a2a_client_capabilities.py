"""Whether a partner will call back, recorded rather than assumed.

Spec 011 User Story 2. Nothing can choose between asking a partner repeatedly
and being told without knowing which it offers, and the card is the only thing
that says. The platform never probes for a capability and never infers one
from a call that happened to work.

The load-bearing test here is the fingerprint one. A capability changing must
not suspend a target: unlike an endpoint or a content type it changes how the
platform WAITS, not what it may send — and adding it to the material set would
re-fingerprint every existing registration and suspend them all on the next
sweep.
"""

from __future__ import annotations

import pytest

from app.a2a.client.sdk import project_card as _project_card
from app.a2a.client.verification import fingerprint_card
from tests.a2a_client_stubs import GOOD_CARD, card_declaring, card_without

pytestmark = pytest.mark.asyncio


def project_card(raw):
    """The projection under test. Signature state is a separate concern here —
    every case below is about ``capabilities``."""
    return _project_card(raw, signature_verified=False)


class TestReadingWhatACardDeclares:
    async def test_a_card_declaring_callbacks_is_read_as_declaring_them(self):
        card = project_card(card_declaring(callbacks=True))
        assert card.declares_callbacks is True

    async def test_a_card_declaring_none_is_read_as_declaring_none(self):
        card = project_card(card_declaring())
        assert card.declares_callbacks is False

    async def test_a_card_with_no_capabilities_declares_nothing(self):
        """Absent is not malformed. A partner that says nothing supports
        nothing extra, which is the safe reading and the common one."""
        card = project_card(card_without("capabilities"))
        assert card.declares_callbacks is False

    @pytest.mark.parametrize("junk", ["yes", 1, None, {}, []])
    async def test_a_non_boolean_declaration_is_not_a_declaration(self, junk):
        """Partner-authored, so anything can arrive. Only ``true`` declares."""
        card = project_card(dict(GOOD_CARD, capabilities={"pushNotifications": junk}))
        assert card.declares_callbacks is False


class TestACapabilityIsNotAMaterialChange:
    """FR-009. The fingerprint decides what suspends a target pending an
    operator's acceptance, and its own docstring gives the reason to keep this
    out: suspending for a change that alters nothing we send would train an
    operator to click accept without reading."""

    def test_gaining_callbacks_does_not_change_the_fingerprint(self):
        before = fingerprint_card(project_card(card_declaring()))
        after = fingerprint_card(project_card(card_declaring(callbacks=True)))
        assert before == after

    def test_losing_callbacks_does_not_change_the_fingerprint(self):
        before = fingerprint_card(project_card(card_declaring(callbacks=True)))
        after = fingerprint_card(project_card(card_declaring()))
        assert before == after

    def test_a_moved_endpoint_still_does(self):
        """The guard on the guard: if this passes while the two above pass,
        the fingerprint is still doing its job."""
        before = fingerprint_card(project_card(GOOD_CARD))
        after = fingerprint_card(
            project_card(dict(GOOD_CARD, url="https://elsewhere.example/rpc"))
        )
        assert before != after
