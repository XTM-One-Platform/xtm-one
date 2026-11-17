"""Which external traffic belongs to which A2A integration.

The property under test: **two A2A integrations never report each other's
work.** That is not automatic. Both are bound to an agent the same way every
other integration is, so every agent-scoped query treats them as
interchangeable — and binding both to one agent is the ordinary case, since a
single agent may serve a Google partner and an Azure one at once. An activity
view built on "runs of the agents this integration is bound to" shows each of
them the other's submissions plus every internal run of those agents.

Attribution therefore keys on the **calling registration** stamped on each run
and task, which ``partner_client_ids`` resolves — the one definition shared by
the integration's last-activity figure and its activity list, so the two can
never disagree about what a partner has done.
"""

import uuid

import pytest

from app.a2a.providers import (
    ENTRA_A2A_PROVIDER,
    OAUTH_A2A_PROVIDER,
    partner_client_id,
    partner_client_ids,
    partner_integration_id,
    resolve_submitter_providers,
)


class TestAFederatedIdCarriesItsIntegration:
    """A federated registration id is derived, so it round-trips.

    That is what lets a federated caller be attributed with no lookup —
    ``resolve_submitter_providers`` takes this branch before it touches the
    database at all.
    """

    def test_a_federated_id_round_trips_to_its_integration(self):
        integ = uuid.uuid4()
        assert (
            partner_integration_id(partner_client_id(integ, ENTRA_A2A_PROVIDER))
            == integ
        )

    def test_a_minted_id_carries_no_integration(self):
        """An OAuth registration is random; its owner lives in a column."""
        assert partner_integration_id("a2a-Xy7QpLm3nR8vTk2w") is None

    def test_the_entra_prefix_alone_does_not_make_it_federated(self):
        """An OAuth registration is 16 bytes of url-safe randomness, which can
        begin ``entra-`` by chance. Only a real integration id follows the
        prefix, so the check is a prefix AND a parse."""
        assert partner_integration_id("a2a-entra-notauuid") is None

    def test_non_a2a_callers_carry_nothing(self):
        for value in (None, "", "fcp-internal", "mcp-something", "user-42"):
            assert partner_integration_id(value) is None


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _SequencedDB:
    """Returns each prepared result in the order the queries are issued."""

    def __init__(self, *results):
        self._results = list(results)
        self.calls = 0

    async def execute(self, _query):
        self.calls += 1
        return _Result(self._results.pop(0) if self._results else [])


class _Integration:
    def __init__(self, provider):
        self.id = uuid.uuid4()
        self.provider = provider


@pytest.mark.asyncio
class TestPartnerClientIds:
    async def test_oauth_integration_owns_its_minted_credentials(self):
        integ = _Integration(OAUTH_A2A_PROVIDER)
        db = _SequencedDB(["a2a-minted-one", "a2a-minted-two"])

        ids = await partner_client_ids(db, integ)

        assert "a2a-minted-one" in ids and "a2a-minted-two" in ids

    async def test_entra_integration_owns_its_derived_identity(self):
        integ = _Integration(ENTRA_A2A_PROVIDER)
        db = _SequencedDB([], [])

        ids = await partner_client_ids(db, integ)

        assert partner_client_id(integ.id, ENTRA_A2A_PROVIDER) in ids

    async def test_two_integrations_share_no_registration(self):
        """The property the whole feature turns on. If these sets ever
        intersect, each integration's activity view contains the other's."""
        one, two = _Integration(OAUTH_A2A_PROVIDER), _Integration(ENTRA_A2A_PROVIDER)

        first = set(await partner_client_ids(_SequencedDB(["a2a-only-mine"]), one))
        second = set(await partner_client_ids(_SequencedDB([], []), two))

        assert not (first & second)

    async def test_legacy_exposure_credentials_count_for_entra_only(self):
        """A federated partner predating the integration model still calls under
        the credential its exposure minted, so excluding those would erase its
        whole history."""
        integ = _Integration(ENTRA_A2A_PROVIDER)
        db = _SequencedDB([], ["a2a-legacy-exposure"])

        ids = await partner_client_ids(db, integ)

        assert "a2a-legacy-exposure" in ids

    async def test_oauth_never_claims_a_legacy_exposure_credential(self):
        """The misattribution this exists to avoid: a per-exposure credential
        was never owned by an OAuth A2A integration — those always carry
        ``a2a_integration_id`` — so claiming it would report a partner created
        this morning as already active. Only ONE query is issued."""
        integ = _Integration(OAUTH_A2A_PROVIDER)
        db = _SequencedDB([], ["a2a-legacy-exposure"])

        ids = await partner_client_ids(db, integ)

        assert "a2a-legacy-exposure" not in ids
        assert db.calls == 1

    async def test_a_partner_that_never_connected_still_resolves(self):
        """Its derived identity exists before any traffic does, so the caller
        gets a non-empty list that simply matches no runs — never a ``None``
        that would read as 'no filter' downstream."""
        integ = _Integration(ENTRA_A2A_PROVIDER)

        ids = await partner_client_ids(_SequencedDB([], []), integ)

        assert ids == [partner_client_id(integ.id, ENTRA_A2A_PROVIDER)]


def _where_of(**kwargs) -> str:
    """The compiled WHERE clause of the runs query, for filter assertions."""
    from app.api.v1.assignments import _filtered_runs_query

    class _User:
        id = uuid.uuid4()
        is_admin = False

    sql = str(
        _filtered_runs_query(_User, **kwargs).compile(
            compile_kwargs={"literal_binds": True}
        )
    )
    return sql[sql.index("WHERE") :]


class TestTheRunsQueryFiltersOnTheRegistration:
    def test_no_filter_when_not_asked_for(self):
        assert "->> 'client_id'" not in _where_of()
        assert "->> 'a2a_provider'" not in _where_of()

    def test_filters_on_the_registration_stamped_on_the_run(self):
        where = _where_of(a2a_client_ids=["a2a-partner-one"])
        assert "->> 'client_id'" in where
        assert "a2a-partner-one" in where

    def test_an_empty_list_matches_nothing(self):
        """A partner with no registration has submitted nothing. Falling
        through to "no filter" here would show a brand-new integration the
        entire history of every agent it is bound to — the exact confusion this
        filter exists to end, arriving on its first page load."""
        where = _where_of(a2a_client_ids=[])
        assert "1 != 1" in where

    def test_it_composes_with_the_visibility_filter(self):
        """Narrowing to a partner must never widen past what the caller may
        see — the registration filter is an AND, not a replacement."""
        where = _where_of(a2a_client_ids=["a2a-partner-one"])
        assert "is_company_managed" in where and "user_id" in where


class TestTheStampedCredentialFormSeparatesLegacyTraffic:
    """The registration alone cannot settle a legacy exposure's attribution.

    A federated caller there reuses the exposure's OAuth-shaped registration on
    purpose — one partner, one budget — so that id serves both credential forms
    and an integration claiming it claims the other partner's submissions. The
    form that verified is stamped on the run, and this is what reads it back.
    """

    def test_the_provider_filter_is_applied(self):
        where = _where_of(
            a2a_client_ids=["a2a-shared"], a2a_provider=ENTRA_A2A_PROVIDER
        )
        assert "->> 'a2a_provider'" in where
        assert ENTRA_A2A_PROVIDER in where

    def test_unstamped_runs_are_kept(self):
        """Runs predating the stamp carry no key, so ``->>`` yields NULL. They
        stay: a partner's history vanishing from a page that showed it
        yesterday is worse than the ambiguity, which is irreducible anyway —
        nothing recorded which form was presented."""
        where = _where_of(
            a2a_client_ids=["a2a-shared"], a2a_provider=ENTRA_A2A_PROVIDER
        )
        assert "IS NULL" in where

    def test_the_two_filters_compose(self):
        """Registration AND form, never either alone: the registration bounds it
        to this partner's budget, the form splits a shared legacy id."""
        where = _where_of(
            a2a_client_ids=["a2a-shared"], a2a_provider=ENTRA_A2A_PROVIDER
        )
        assert "->> 'client_id'" in where and "->> 'a2a_provider'" in where


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _LookupDB:
    """Answers the ownership lookup, then the provider lookup."""

    def __init__(self, owners=(), providers=()):
        self._results = [list(owners), list(providers)]
        self.calls = 0

    async def execute(self, _query):
        self.calls += 1
        return _Rows(self._results.pop(0) if self._results else [])


@pytest.mark.asyncio
class TestOneAnswerPerRun:
    """A run must read the same on every page it appears on.

    It does not, if each surface guesses from the registration id's shape: both
    credential forms mint ``a2a-<random>``, so the same run reads "Foundry A2A"
    on the integration claiming it and "OAuth A2A" in the agent's own activity
    log. Resolving once, from what the registration is OWNED by, is what makes
    the two agree — and makes "cannot tell" an answer rather than a coin flip.
    """

    async def test_the_stamped_form_wins_over_the_ids_shape(self):
        """The legacy case, and the only thing that resolves it: a federated
        caller reusing an OAuth-shaped registration. The shape says OAuth; the
        stamp says Entra; the stamp is what actually verified."""
        run = uuid.uuid4()
        db = _LookupDB()

        got = await resolve_submitter_providers(
            db,
            {run: {"source": "a2a", "client_id": "a2a-legacy",
                   "a2a_provider": ENTRA_A2A_PROVIDER}},
        )

        assert got[run] == ENTRA_A2A_PROVIDER
        assert db.calls == 0, "a stamped run needs no lookup"

    async def test_a_derived_federated_id_needs_no_lookup(self):
        run, integ = uuid.uuid4(), uuid.uuid4()
        db = _LookupDB()

        got = await resolve_submitter_providers(
            db,
            {
                run: {
                    "source": "a2a",
                    "client_id": partner_client_id(integ, ENTRA_A2A_PROVIDER),
                }
            },
        )

        assert got[run] == ENTRA_A2A_PROVIDER
        assert db.calls == 0

    async def test_an_owned_registration_resolves_to_its_integrations_provider(self):
        """Exact, not a guess: ``a2a_integration_id`` IS the ownership."""
        run, integ = uuid.uuid4(), uuid.uuid4()
        db = _LookupDB(
            owners=[("a2a-minted", integ)], providers=[(integ, OAUTH_A2A_PROVIDER)]
        )

        got = await resolve_submitter_providers(
            db, {run: {"source": "a2a", "client_id": "a2a-minted"}}
        )

        assert got[run] == OAUTH_A2A_PROVIDER

    async def test_an_unowned_registration_is_left_unattributed(self):
        """A per-exposure credential served BOTH credential forms and no column
        records which was presented — so the honest answer is no answer. Naming
        one anyway is what put "OAuth A2A" on a Foundry page."""
        run = uuid.uuid4()

        got = await resolve_submitter_providers(
            _LookupDB(), {run: {"source": "a2a", "client_id": "a2a-legacy"}}
        )

        assert run not in got

    async def test_a_deleted_registration_is_left_unattributed(self):
        """Revoking an exposure deletes its credentials, so a historical run can
        outlive every row that said whose it was."""
        run = uuid.uuid4()

        got = await resolve_submitter_providers(
            _LookupDB(), {run: {"source": "a2a", "client_id": "a2a-long-gone"}}
        )

        assert run not in got

    async def test_internal_runs_are_untouched(self):
        """The badge exists to separate partner traffic from a schedule or a
        person clicking Run; those must never acquire one."""
        scheduled, webhook, manual = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        got = await resolve_submitter_providers(
            _LookupDB(),
            {
                scheduled: {"type": "schedule"},
                webhook: {"source": "webhook", "client_id": "a2a-lookalike"},
                manual: None,
            },
        )

        assert got == {}

    async def test_a_mixed_page_costs_two_queries(self):
        """Batched across the whole page, not one lookup per row."""
        stamped, derived, owned_a, owned_b = (uuid.uuid4() for _ in range(4))
        integ = uuid.uuid4()
        db = _LookupDB(
            owners=[("a2a-one", integ), ("a2a-two", integ)],
            providers=[(integ, OAUTH_A2A_PROVIDER)],
        )

        got = await resolve_submitter_providers(
            db,
            {
                stamped: {"source": "a2a", "client_id": "a2a-x",
                          "a2a_provider": ENTRA_A2A_PROVIDER},
                derived: {
                    "source": "a2a",
                    "client_id": partner_client_id(
                        uuid.uuid4(), ENTRA_A2A_PROVIDER
                    ),
                },
                owned_a: {"source": "a2a", "client_id": "a2a-one"},
                owned_b: {"source": "a2a", "client_id": "a2a-two"},
            },
        )

        assert len(got) == 4
        assert db.calls == 2
