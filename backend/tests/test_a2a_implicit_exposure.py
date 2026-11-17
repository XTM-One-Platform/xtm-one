"""Exposure follows A2A integration bindings, with no switch of its own.

The property under test: an agent is reachable by partners exactly while it
binds at least one A2A integration (``a2a.providers``). Binding is the whole
opt-in — a partner credential that reaches nothing, and an exposed agent no
partner can reach, are both states an operator would have to reconcile by
hand.

The binding side is exercised through ``a2a.providers`` and the run-as rule
through ``resolve_run_as``; the SQLAlchemy flush hook that ties them together
is covered by the live lifecycle rather than re-simulated here (the suite has
no real-DB harness).
"""

import uuid
from contextlib import nullcontext

import pytest

from app.a2a import providers
from app.a2a.exposure import RunAsRequiredError, resolve_run_as


class _Agent:
    def __init__(self, bindings=None, *, company_managed=False, owner=True):
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4() if owner else None
        self.is_company_managed = company_managed
        self.integration_bindings = bindings


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _DirectionSession:
    """Reports every A2A binding on *agent* as INBOUND.

    These tests predate direction and assert the binding-is-the-opt-in rule,
    which still holds — for an inbound integration. Stating that explicitly
    keeps them testing what they were written to test;
    ``test_a2a_direction_regression.py`` owns the outbound case.

    Derives the ids from the agent rather than parsing the SELECT, because a
    double that reaches into SQLAlchemy internals breaks on an unrelated
    query-construction change and teaches nothing when it does.
    """

    no_autoflush = nullcontext()

    def __init__(self, agent=None, direction="inbound"):
        self.agent = agent
        self.direction = direction

    def execute(self, _stmt):
        ids = providers.bound_integration_ids(self.agent) if self.agent else []
        return _Rows([(i, self.direction) for i in ids])


class TestBindingIsTheOptIn:
    def test_either_provider_exposes(self):
        for provider in (providers.OAUTH_A2A_PROVIDER, providers.ENTRA_A2A_PROVIDER):
            agent = _Agent({provider: [str(uuid.uuid4())]})
            assert providers.binds_a2a(_DirectionSession(agent), agent), provider

    def test_no_a2a_binding_is_not_exposed(self):
        assert not providers.binds_a2a(_DirectionSession(_Agent(None)), _Agent(None))
        assert not providers.binds_a2a(_DirectionSession(_Agent({})), _Agent({}))
        # A non-A2A integration must never expose an agent.
        non_a2a = _Agent({"entra": [str(uuid.uuid4())]})
        assert not providers.binds_a2a(_DirectionSession(non_a2a), non_a2a)

    def test_empty_list_under_the_key_is_not_a_binding(self):
        """Unbinding usually leaves ``{provider: []}`` rather than removing
        the key — that must read as "not exposed", or unbinding would never
        take effect."""
        agent = _Agent({providers.OAUTH_A2A_PROVIDER: []})
        assert not providers.binds_a2a(_DirectionSession(agent), agent)

    def test_both_providers_can_be_bound_at_once(self):
        oauth, entra = str(uuid.uuid4()), str(uuid.uuid4())
        agent = _Agent(
            {
                providers.OAUTH_A2A_PROVIDER: [oauth],
                providers.ENTRA_A2A_PROVIDER: [entra],
            }
        )
        ids = {str(i) for i in providers.bound_integration_ids(agent)}
        assert ids == {oauth, entra}

    def test_narrowing_to_one_provider(self):
        oauth, entra = str(uuid.uuid4()), str(uuid.uuid4())
        agent = _Agent(
            {
                providers.OAUTH_A2A_PROVIDER: [oauth],
                providers.ENTRA_A2A_PROVIDER: [entra],
            }
        )
        got = providers.bound_integration_ids(agent, providers.OAUTH_A2A_PROVIDER)
        assert [str(i) for i in got] == [oauth]

    def test_malformed_entries_are_skipped_not_fatal(self):
        """Bindings are JSON the API writes; a bad entry is data, and one bad
        entry must not hide a good one."""
        good = str(uuid.uuid4())
        agent = _Agent({providers.OAUTH_A2A_PROVIDER: ["not-a-uuid", good]})
        assert [str(i) for i in providers.bound_integration_ids(agent)] == [good]


class TestImplicitRunAs:
    def test_personal_agent_runs_as_its_owner(self):
        agent = _Agent({providers.OAUTH_A2A_PROVIDER: ["x"]})
        assert resolve_run_as(agent) == agent.user_id

    def test_company_managed_inherits_its_owner(self):
        """Accepted trade-off: exposure is implicit, so there is no moment at
        which to demand a choice — the parameter that once carried one went
        with the expose endpoint. Documented at ``resolve_run_as`` and
        surfaced in the UI because it is inherited rather than chosen."""
        agent = _Agent({providers.OAUTH_A2A_PROVIDER: ["x"]}, company_managed=True)
        assert resolve_run_as(agent) == agent.user_id

    def test_ownerless_agent_cannot_be_exposed(self):
        """Fail closed rather than invent an identity — the binding simply
        produces no exposure."""
        agent = _Agent({providers.OAUTH_A2A_PROVIDER: ["x"]}, owner=False)
        with pytest.raises(RunAsRequiredError):
            resolve_run_as(agent)


class TestProviderBootstrapsAreRegistered:
    """The A2A providers register their seams as an import side-effect.

    Those imports bind no name anyone reads, so every static measure calls
    them unused and an auto-fix will happily delete them — which unregisters
    the probes with NO error: Test Connection silently verifies nothing. This
    test is what turns that into a failure.
    """

    def test_importing_the_router_registers_hooks_and_probes(self):
        import app.api.v1.integrations  # noqa: F401 - side-effectful import
        from app.integrations.connectivity import has_connectivity_probe
        from app.integrations.lifecycle import _ON_CONNECT

        for key in providers.A2A_PROVIDERS:
            assert has_connectivity_probe(key), f"{key} lost its probe"
        # And OAuth A2A registers NO on-connect hook, deliberately. A secret
        # minted at create time is returned by no response — create answers
        # with the integration, not the credential — so minting is
        # operator-driven from the Config tab, where the secret is shown at
        # the moment it is asked for.
        assert providers.OAUTH_A2A_PROVIDER not in _ON_CONNECT


class TestConvergencePredicate:
    """The migration's "is this agent bound?" test must agree with the
    runtime one AS IT STOOD WHEN THE MIGRATION RAN — a mismatch would either
    revoke a working exposure or leave an unbound agent reachable.

    That qualifier is the whole subtlety. The predicate spells its provider
    keys as SQL literals on purpose: a migration must keep meaning what it
    meant on the day it ran, so it cannot read a constant that later changes
    underneath it. When ``google_a2a`` gained an inbound half it joined
    ``providers.A2A_PROVIDERS`` and the predicate correctly stayed at two
    keys — there were no Google bindings in any database this revision
    touched, and a revision that has already run is not re-run for a provider
    added afterwards.

    So the pin has two halves now. The literals must be exactly the set that
    existed at that revision, and they must never name a key the runtime set
    does not — the second half is what still catches a typo or a key removed
    from the product.
    """

    #: The inbound-capable set on the day ``a2a2intmodel1`` ran. Spelled here
    #: rather than imported for the same reason the migration spells it: this
    #: is a historical fact, and importing today's constant would make the
    #: test agree with any future change by construction.
    PROVIDERS_AT_THAT_REVISION = {"oauth_a2a", "entra_a2a"}

    def test_migration_predicate_matches_binds_a2a(self):
        import importlib.util
        import pathlib
        import re

        # ``alembic/versions`` is not a package, so load the revision by path
        # the way Alembic itself does.
        path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "a2a2intmodel1_a2a_partner_relationship_on_integrations.py"
        )
        spec = importlib.util.spec_from_file_location("_a2a_intmodel", path)
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)

        keys = set(re.findall(r"'(\w+_a2a)'", mig._BINDS_A2A))
        assert keys == self.PROVIDERS_AT_THAT_REVISION
        # And never a key the product does not have: the predicate may lag the
        # runtime set (a provider added after it ran), never lead it.
        assert keys <= set(providers.A2A_PROVIDERS)
        # Empty arrays must NOT count as bound: unbinding through the API
        # leaves ``{provider: []}`` rather than dropping the key, and mere
        # key presence would read that as still exposed.
        # (The keys are literals in the migration, never interpolated, so
        # this reads them back out rather than comparing constants.)
        assert "jsonb_array_length" in mig._BINDS_A2A
        assert "> 0" in mig._BINDS_A2A
        # Guarded against a malformed (non-array) value, which would
        # otherwise raise and abort the whole migration.
        assert "jsonb_typeof" in mig._BINDS_A2A


class TestCatalogWiring:
    def test_every_provider_is_registered_and_toolless(self):
        """None of them contributes agent tools: binding one means a partner
        relationship, not capability. A tool builder here would make the
        binding ambiguous."""
        from app.api.v1.integration_catalog import PROVIDER_CATALOG
        from app.services.integration_tools import PROVIDER_TOOLS

        for key in providers.A2A_PROVIDERS:
            entry = PROVIDER_CATALOG[key]
            assert entry["auth_type"] == "none"
            assert entry["fields"] == []
            assert "a2a" in entry["integration_types"]
            assert key not in PROVIDER_TOOLS

    def test_every_a2a_catalog_key_is_named_by_the_endpoint(self):
        """The trap that has now caught two features.

        ``list_provider_catalog`` builds each item with EXPLICIT kwargs, so a
        key added to the catalog dict is silently dropped unless it is named
        there too — the endpoint answers 200 with the field absent and the UI
        renders empty. ``a2a_directions`` carries a comment about the first
        time; the direction panels were the second.

        Asserted generically rather than field by field, so the third is
        caught by this test rather than by someone looking at a blank panel.
        """
        import inspect

        from app.api.v1 import integrations
        from app.api.v1.integration_catalog import PROVIDER_CATALOG
        from app.api.v1.schemas.integration import ProviderCatalogItem

        built = inspect.getsource(integrations.list_provider_catalog)
        declared = set(ProviderCatalogItem.model_fields)
        for key in providers.A2A_PROVIDERS:
            for field in PROVIDER_CATALOG[key]:
                if field not in declared:
                    # Not part of the served shape at all; nothing to drop.
                    continue
                assert f"{field}=" in built, f"{key}.{field} never reaches the API"

    def test_both_direction_panels_reach_the_api(self):
        """What the panels read, through the model the endpoint returns."""
        from app.api.v1.integration_catalog import PROVIDER_CATALOG
        from app.api.v1.schemas.integration import ProviderCatalogItem

        for key in providers.A2A_PROVIDERS:
            served = ProviderCatalogItem(provider=key, **PROVIDER_CATALOG[key])
            assert [t.name for t in served.a2a_inbound_agent_tools or []] == [
                "ask_the_caller"
            ], key
            assert len(served.a2a_outbound_exposed or []) == 1, key
            # The inbound surface keeps carrying the operations a partner may
            # invoke; the two panels are additions, not a replacement.
            assert served.available_tools, key

    def test_every_provider_advertises_the_same_protocol_operations(self):
        """The operations belong to the protocol, not to any one credential
        mechanism — a partner reads the same surface whichever it uses."""
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        names = {
            key: [t["name"] for t in PROVIDER_CATALOG[key]["available_tools"]]
            for key in providers.A2A_PROVIDERS
        }
        assert len(set(map(tuple, names.values()))) == 1
        assert set(next(iter(names.values()))) == {
            "message/send",
            "tasks/get",
            "tasks/cancel",
            "tasks/pushNotificationConfig/set",
            "tasks/pushNotificationConfig/get",
            "tasks/pushNotificationConfig/list",
            "tasks/pushNotificationConfig/delete",
        }

    def test_every_provider_has_a_guide_per_direction(self):
        """The base guide is the inbound procedure and the ``outbound`` variant
        carries its own, so no step needs an INBOUND/OUTBOUND label."""
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        for key in providers.A2A_PROVIDERS:
            entry = PROVIDER_CATALOG[key]
            assert entry["variant_selector"] == "a2a_direction"
            inbound = entry["setup_guide"]
            outbound = entry["variants"]["outbound"]["setup_guide"]
            assert inbound["steps"] and outbound["steps"], key
            for step in inbound["steps"] + outbound["steps"]:
                assert not step.startswith(("INBOUND", "OUTBOUND")), (key, step)
            # Inbound hands out the card URL and says that binding exposes;
            # outbound pastes the partner's and ends with a delegation tool.
            assert any("card URL" in step for step in inbound["steps"]), key
            assert any("Binding is what exposes" in step for step in inbound["steps"]), key
            assert any("delegat" in step for step in outbound["steps"]), key
            assert not any("Binding is what exposes" in step for step in outbound["steps"]), key
