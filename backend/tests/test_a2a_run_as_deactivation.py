"""FR-007: a deactivated run-as user stops the exposure resolving.

The requirement is that an exposure "cease resolving when it becomes
ineligible - the agent disabled or deleted, or the user it executes as
deactivated or deleted - without falling back to any other identity".

Deletion was already covered by the ``ON DELETE CASCADE`` on
``run_as_user_id``: removing the user removes the exposure row. Deactivation was
NOT, because ``users.toggle-active`` only flips ``is_active`` and leaves the row
in place - so a partner kept minting tokens and tasking the agent as a departed
employee, reading that person's data.

Two properties are pinned here:

1. The external resolution paths - the card/task surface and the token endpoint
   - both filter on ``users.is_active``, and both go through the SAME predicate
   so they cannot drift. A credential that can still be minted must always
   address a surface that still answers.
2. The INTERNAL surface deliberately does not filter, or an operator could no
   longer see the exposure that has stopped resolving, nor revoke it.

The enforcement is a live filter rather than a revocation on purpose:
deactivation is reversible (``toggle-active`` is a toggle) and revocation is
not, so revoking would permanently destroy a partner integration over a
temporary suspension. See ``models.a2a`` for why no ``owner_deactivated``
revocation reason exists.

The filter is now the ONLY enforcement, and that is sufficient rather than a
gap. The explicit expose endpoint used to refuse a deactivated run-as user up
front, but that check existed to avoid minting a partner secret for an
exposure which would never resolve — and exposure follows an integration
binding now, which mints nothing. An exposure whose run-as user is inactive is
simply created and does not answer, which is exactly what these tests pin.
"""

import uuid

from sqlalchemy.dialects import postgresql

from app.a2a import exposure as expo


class _CapturingResult:
    def scalar_one_or_none(self):
        return None


class _CapturingDB:
    """Records the statement it was handed, so the SQL itself can be asserted."""

    def __init__(self):
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _CapturingResult()

    def compiled(self, index=0):
        return str(
            self.statements[index].compile(dialect=postgresql.dialect())
        ).replace("\n", " ")


class TestExternalResolutionRequiresAnActiveUser:
    async def test_card_token_lookup_joins_users_and_filters_is_active(self):
        db = _CapturingDB()
        await expo.get_exposure_by_token(db, "some-token")
        sql = db.compiled()
        assert "JOIN users" in sql
        assert "users.is_active IS true" in sql
        # The original condition must survive alongside the new one.
        assert "a2a_exposures.revoked_at IS NULL" in sql
        assert "a2a_exposures.card_token" in sql

    async def test_token_endpoint_lookup_uses_the_same_predicate(self):
        db = _CapturingDB()
        await expo.get_resolvable_exposure(db, uuid.uuid4())
        sql = db.compiled()
        assert "JOIN users" in sql
        assert "users.is_active IS true" in sql
        assert "a2a_exposures.revoked_at IS NULL" in sql

    async def test_an_inner_join_is_used_not_an_outer_one(self):
        """A LEFT JOIN would still return the row for a deactivated user and
        leave the caller to remember to check it - the failure mode this
        replaces."""
        db = _CapturingDB()
        await expo.get_exposure_by_token(db, "t")
        assert "LEFT OUTER JOIN" not in db.compiled()

    def test_both_paths_share_one_builder(self):
        """Source-level guard against the two surfaces drifting apart: a
        mintable credential addressing a dead surface, or the reverse."""
        import inspect as pyi

        for fn in (expo.get_exposure_by_token, expo.get_resolvable_exposure):
            src = pyi.getsource(fn)
            assert "_resolvable_exposures()" in src
            # Neither may hand-roll the conditions the builder owns.
            assert "revoked_at.is_(None)" not in src
            assert "is_active" not in src


class TestInternalSurfaceStillSeesTheExposure:
    async def test_live_exposure_lookup_does_not_filter_on_is_active(self):
        """An operator must still be able to see - and revoke - an exposure
        that has stopped resolving because its run-as user was deactivated."""
        db = _CapturingDB()
        await expo.get_live_exposure(db, uuid.uuid4())
        sql = db.compiled()
        assert "is_active" not in sql
        assert "JOIN users" not in sql
        assert "a2a_exposures.revoked_at IS NULL" in sql


class TestDeactivationIsFilteredNotRevoked:
    def test_no_owner_deactivated_revocation_reason_exists(self):
        """A constant for a reason nothing writes would advertise a revocation
        that does not happen. Deactivation is reversible; revocation is not."""
        import app.models.a2a as a2a_models

        assert not hasattr(a2a_models, "REVOKED_OWNER_DEACTIVATED")

    async def test_token_minting_refuses_when_the_exposure_stops_resolving(
        self, monkeypatch
    ):
        """The grant's rejection is the same uniform ``invalid_client`` it
        returns for every other failure - a distinguishable answer here would
        tell a caller that the agent exists and who stopped being active."""
        import json

        from app.a2a import grants

        class _Client:
            client_id = "a2a-abc"
            # A stand-in for a bcrypt hash; verification is stubbed out below.
            client_secret_hash = "h"
            grant_types = ("client_credentials",)
            a2a_exposure_id = uuid.uuid4()

        async def _authenticate_client(_db, _cid, _secret, *, grant_type):
            assert grant_type == "client_credentials"
            return _Client()

        async def _unresolvable(_db, _exposure_id):
            return None

        monkeypatch.setattr(
            "app.api.v1.mcp_oauth.authenticate_client", _authenticate_client
        )
        monkeypatch.setattr(
            "app.a2a.exposure.get_resolvable_exposure", _unresolvable
        )

        response = await grants.handle_client_credentials(
            {"client_id": "a2a-abc", "client_secret": "s"}, db=None
        )

        assert response.status_code == 401
        assert json.loads(response.body) == {"error": "invalid_client"}
