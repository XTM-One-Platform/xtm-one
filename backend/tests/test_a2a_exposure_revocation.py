"""Revocation, and the card token whose retirement it enforces.

Revocation is entirely automatic now: exposure follows an A2A integration
binding, so there is no manual revoke endpoint to test, and every path that
stops an agent being reachable runs through ``hooks._revoke_live_exposures``.
The security-relevant case is still the involuntary one. Switching a
company-managed agent to personal WIDENS what external work can reach — from
company-managed resources only to that owner's entire personal and
group-shared visibility — and nobody re-approves the exposure at that moment,
so it is revoked rather than migrated, in both directions of the switch.

``hooks._revocation_reason`` decides WHETHER to revoke and is covered in
``test_a2a_hooks``. What is pinned here is what revoking actually writes, and
that a retired token can never resolve again.
"""

import uuid

from app.a2a import exposure as expo
from app.a2a import hooks
from app.models.a2a import (
    REVOKED_SHARING_CHANGE,
    REVOKED_TIER_CHANGE,
)
from tests.a2a_session_doubles import TxnSurface


class _Exposure:
    def __init__(self):
        self.id = uuid.uuid4()
        self.agent_id = uuid.uuid4()
        self.card_token = "tok"
        self.run_as_user_id = uuid.uuid4()
        self.revoked_at = None
        self.revoked_reason = None


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Returns pre-baked results; records flushes."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.flushes = 0
        self.added = []

    async def execute(self, _stmt):
        return _Result(self._results.pop(0) if self._results else None)

    async def flush(self):
        self.flushes += 1

    def add(self, obj):
        self.added.append(obj)


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return self._rows


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _Session(TxnSurface):
    """Sync session double — ``_revoke_live_exposures`` runs in before_flush."""

    def __init__(self, live):
        self._live = live
        self.added = []
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        return _ScalarResult(self._live)

    def add(self, obj):
        self.added.append(obj)


class _Agent:
    def __init__(self, **kw):
        self.id = uuid.uuid4()
        self.name = "agent"
        self.user_id = uuid.uuid4()
        self.is_company_managed = kw.get("is_company_managed", False)


class TestRevocationWritesTheReason:
    def test_revoke_sets_timestamp_and_reason(self):
        exp = _Exposure()
        agent = _Agent()
        exp.run_as_user_id = agent.user_id
        hooks._revoke_live_exposures(_Session([exp]), agent, REVOKED_TIER_CHANGE)
        assert exp.revoked_at is not None
        assert exp.revoked_reason == REVOKED_TIER_CHANGE

    def test_reason_is_recorded_per_path(self):
        exp = _Exposure()
        agent = _Agent()
        exp.run_as_user_id = agent.user_id
        hooks._revoke_live_exposures(_Session([exp]), agent, REVOKED_SHARING_CHANGE)
        assert exp.revoked_reason == REVOKED_SHARING_CHANGE

    def test_an_already_revoked_exposure_is_not_re_stamped(self):
        """Automatic revocation on one replica and another change landing on a
        second can race. The loser must not overwrite the original timestamp,
        because that timestamp is the audit record of when reachability
        actually stopped."""
        exp = _Exposure()
        agent = _Agent()
        exp.run_as_user_id = agent.user_id
        hooks._revoke_live_exposures(_Session([exp]), agent, REVOKED_TIER_CHANGE)
        first_at, first_reason = exp.revoked_at, exp.revoked_reason
        # The live query returns nothing the second time — the row is revoked.
        hooks._revoke_live_exposures(_Session([]), agent, REVOKED_SHARING_CHANGE)
        assert exp.revoked_at == first_at
        assert exp.revoked_reason == first_reason

    def test_no_live_exposure_is_not_an_error(self):
        hooks._revoke_live_exposures(_Session([]), _Agent(), REVOKED_TIER_CHANGE)


class TestCardTokens:
    def test_each_exposure_mints_a_fresh_token(self):
        """A re-bind must never reissue a retired card URL — the retained
        revoked row guarantees the old one stays dead only if the new one
        differs."""
        first, second = expo._mint_card_token(), expo._mint_card_token()
        assert first != second

    def test_a_token_is_long_enough_to_resist_enumeration(self):
        """The specification forbids listing exposed agents, which an
        enumerable token would reconstruct."""
        assert len(expo._mint_card_token()) >= 32


class TestTokenResolution:
    async def test_revoked_token_resolves_to_none(self):
        """A revoked row is retained for audit and to guarantee the token is
        never reissued - NOT so a probing caller gets a different answer than
        for a token that never existed."""
        db = _FakeDB(results=[None])
        assert await expo.get_exposure_by_token(db, "retired-token") is None

    async def test_unknown_token_resolves_to_none_identically(self):
        db = _FakeDB(results=[None])
        assert await expo.get_exposure_by_token(db, "never-existed") is None
