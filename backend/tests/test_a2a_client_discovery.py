"""Following a partner's own list of agents, and what that must never do.

A card describes exactly one agent, so a partner exposing three publishes
three cards and a client has no standard way to learn about the second and
third. Between two XTM One deployments this closes that gap; everywhere else
the operator's list is what runs.

Always on where the partner implements it. Which agents a credential reaches
was always the PARTNER's decision — made when it binds an agent to the
registration that credential belongs to — so a list typed on this side was a
copy of it, and stale the moment the partner changed anything.

The refusals matter more than the happy path. Discovery decides what to LOOK
at and never what to trust: every URL it returns still runs the whole
verification, and one that leaves the origin we authenticated to is discarded
rather than followed.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.a2a.client import registration
from tests.a2a_client_stubs import resolve_public

ORIGIN = "https://partner.example"
CARD_A = f"{ORIGIN}/a2a/aaa/.well-known/agent-card.json"
CARD_B = f"{ORIGIN}/a2a/bbb/.well-known/agent-card.json"


class _Db:
    def __init__(self):
        self.added: list = []
        self.deleted: list = []

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        return None


def _integration():
    return SimpleNamespace(id="i-1", provider="oauth_a2a", extra_config_enc="enc")


@pytest.fixture
def partner(monkeypatch):
    """A partner that answers discovery with whatever the test supplies."""

    # ``existing`` is the stored targets, as rows: ``card_url`` plus the two
    # fields that decide whether this partner is asked at all — what its card
    # declared, and whether a card was ever read from it.
    state = {"existing": [CARD_A], "response": None, "asked": []}

    # The unsupported-partner backoff is a process-local cache keyed by
    # integration id, and every test here uses the same one — so without this
    # the 404 case below would mark the partner unsupported for the rest of
    # the session and its neighbours would pass for the wrong reason.
    registration._UNSUPPORTED.clear()

    resolve_public(monkeypatch)

    # The real class, captured BEFORE patching: `registration.httpx` is the
    # httpx module itself, so a replacement that called `httpx.AsyncClient`
    # would call itself.
    real_client = httpx.AsyncClient

    def _client(*_a, **_kw):
        async def handler(request):
            state["asked"].append(str(request.url))
            return state["response"] or httpx.Response(404)

        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(registration.httpx, "AsyncClient", _client)

    import app.a2a.client.credentials as credentials
    import app.a2a.providers as providers

    monkeypatch.setattr(credentials, "configured_card_urls", lambda _i: [])

    async def _headers(_integration, _client):
        return {"Authorization": "Bearer minted"}

    monkeypatch.setattr(credentials, "outbound_headers", _headers)

    async def _targets(_db, _id):
        return [_target(u) if isinstance(u, str) else u for u in state["existing"]]

    monkeypatch.setattr(providers, "outbound_targets", _targets)

    synced: dict = {}

    async def _sync(_db, _id, urls):
        synced["urls"] = list(urls)
        return (len(urls), 0)

    monkeypatch.setattr(providers, "sync_targets", _sync)
    state["synced"] = synced
    return state


def _target(card_url, *, declares=None, fingerprint=None):
    """One stored target, as ``_discovery_path`` reads it.

    Defaults are the never-verified shape — no declaration, no fingerprint.
    """
    return SimpleNamespace(
        card_url=card_url,
        partner_discovery_path=declares,
        card_fingerprint=fingerprint,
    )


def _agents(*urls):
    return httpx.Response(200, json={"agents": [{"name": "a", "card_url": u} for u in urls]})


class TestReadingThePartnersDeclaration:
    """What a partner's card says about discovery, projected off the card.

    A2A defines no method that enumerates agents, and
    ``capabilities.extensions`` is its sanctioned way to advertise a
    capability it does not define. Reading a partner's declaration is what
    turns our outbound call from a probe into a negotiated one.
    """

    def _card(self, extensions):
        from app.a2a.client.sdk import project_card

        return project_card(
            {"name": "Partner", "capabilities": {"extensions": extensions}},
            signature_verified=False,
        )

    def test_a_declared_path_is_read_from_the_extension(self):
        from app.a2a.extensions import PARTNER_DISCOVERY_URI

        card = self._card(
            [{"uri": PARTNER_DISCOVERY_URI, "params": {"path": "/elsewhere/agents"}}]
        )
        assert card.partner_discovery_path == "/elsewhere/agents"

    def test_a_card_that_declares_nothing_reads_as_none(self):
        assert self._card([]).partner_discovery_path is None
        from app.a2a.client.sdk import project_card

        assert (
            project_card({"name": "P"}, signature_verified=False).partner_discovery_path
            is None
        )

    def test_another_extension_is_not_ours(self):
        """Matched on the URI and nothing else — the URI IS the extension's
        name, so a partner declaring some unrelated extension must not be
        read as offering discovery."""
        card = self._card(
            [{"uri": "https://example.org/some/other/extension/v1", "params": {"path": "/x"}}]
        )
        assert card.partner_discovery_path is None

    def test_a_terse_declaration_falls_back_to_the_default_path(self):
        """Declaring the extension without params still SAYS the partner
        implements it, and the extension's own definition names the path.
        Reading that as "declares nothing" would refuse to ask a partner that
        just told us it answers."""
        from app.a2a.extensions import PARTNER_DISCOVERY_PATH, PARTNER_DISCOVERY_URI

        for entry in (
            {"uri": PARTNER_DISCOVERY_URI},
            {"uri": PARTNER_DISCOVERY_URI, "params": None},
            {"uri": PARTNER_DISCOVERY_URI, "params": {"path": 7}},
        ):
            assert self._card([entry]).partner_discovery_path == PARTNER_DISCOVERY_PATH

    @pytest.mark.parametrize(
        "path", ["//evil.example/agents", "https://evil.example/agents", "agents", ""]
    )
    def test_a_path_that_could_move_us_is_dropped(self, path):
        """The declaration is a PARTNER-authored value. The caller joins it to
        the origin it authenticated to, so a protocol-relative ``//host`` is
        the one shape that could still change the host it reaches — refused
        along with everything else that is not a single rooted path."""
        from app.a2a.extensions import PARTNER_DISCOVERY_PATH, PARTNER_DISCOVERY_URI

        card = self._card([{"uri": PARTNER_DISCOVERY_URI, "params": {"path": path}}])
        assert card.partner_discovery_path == PARTNER_DISCOVERY_PATH

    def test_our_own_card_reads_back_as_declaring_our_own_path(self):
        """The loop closed. Our server declares the extension and our client
        reads declarations; one XTM One deployment pointed at another must
        therefore find discovery without anyone probing."""
        import uuid

        from app.a2a.card import build_agent_card
        from app.a2a.client.sdk import project_card
        from app.a2a.extensions import PARTNER_DISCOVERY_PATH

        agent = SimpleNamespace(
            id=uuid.uuid4(), name="A", description="d", output_format="markdown",
            output_schema=None, builtin_tool_ids=[], custom_tool_ids=[],
            mcp_server_ids=[], knowledge_base_ids=[], integration_bindings={},
        )
        exposure = SimpleNamespace(
            card_token="tok", run_as_user_id=uuid.uuid4(), allowed_tools=None
        )
        ours = build_agent_card(
            agent, exposure, base_url=ORIGIN, version="1.0", app_version="1.0"
        )
        assert (
            project_card(ours, signature_verified=False).partner_discovery_path
            == PARTNER_DISCOVERY_PATH
        )


class TestAskingOnlyPartnersThatDeclareIt:
    """Three states, because "no declaration stored" has two meanings."""

    async def test_a_declaring_partner_is_asked_where_it_said(self, partner):
        partner["existing"] = [
            _target(CARD_A, declares="/custom/agents", fingerprint="fp")
        ]
        partner["response"] = _agents(CARD_A, CARD_B)
        added, _ = await registration.discover_partner_agents(_Db(), _integration())
        assert added == 2
        assert partner["asked"] == [f"{ORIGIN}/custom/agents"]

    async def test_a_partner_whose_card_declares_nothing_is_not_asked(self, partner):
        """The point of the extension. A partner that has told us what it
        implements is taken at its word instead of being probed on a path it
        never advertised — no token minted, no request made."""
        partner["existing"] = [_target(CARD_A, fingerprint="fp")]
        partner["response"] = _agents(CARD_A, CARD_B)
        assert await registration.discover_partner_agents(_Db(), _integration()) == (0, 0)
        assert partner["asked"] == []

    async def test_a_partner_whose_card_was_never_read_is_probed_once(self, partner):
        """Unavoidable, and the same behaviour as before any of this existed:
        a declaration can only be known after a card has been verified, and
        discovery runs before the first verification on a new registration."""
        from app.a2a.extensions import PARTNER_DISCOVERY_PATH

        partner["existing"] = [_target(CARD_A)]
        partner["response"] = _agents(CARD_A, CARD_B)
        added, _ = await registration.discover_partner_agents(_Db(), _integration())
        assert added == 2
        assert partner["asked"] == [f"{ORIGIN}{PARTNER_DISCOVERY_PATH}"]

    async def test_a_failed_verification_is_not_read_as_declaring_nothing(self, partner):
        """``card_fingerprint``, not ``last_verified_at``: a verification that
        failed stamps the time without ever parsing a card, and reading that
        as "the partner declares nothing" would switch discovery off for a
        partner whose card was merely unreachable for a while."""
        from app.a2a.extensions import PARTNER_DISCOVERY_PATH

        unverified = _target(CARD_A)
        unverified.last_verified_at = "2026-01-01T00:00:00Z"
        partner["existing"] = [unverified]
        partner["response"] = _agents(CARD_A)
        await registration.discover_partner_agents(_Db(), _integration())
        assert partner["asked"] == [f"{ORIGIN}{PARTNER_DISCOVERY_PATH}"]

    async def test_one_declaring_card_speaks_for_the_partner(self, partner):
        """The capability belongs to the deployment; the column records what
        one card said. A partner adding the extension starts being asked as
        soon as ANY of its cards is next verified, rather than all of them."""
        partner["existing"] = [
            _target(CARD_A, fingerprint="fp"),
            _target(CARD_B, declares="/custom/agents", fingerprint="fp"),
        ]
        partner["response"] = _agents(CARD_A)
        await registration.discover_partner_agents(_Db(), _integration())
        assert partner["asked"] == [f"{ORIGIN}/custom/agents"]


class TestTheVerdictRecordsTheDeclaration:
    def test_it_is_written_and_also_cleared(self):
        """Written on every successful verification, INCLUDING when the card
        no longer declares it — a partner that drops the extension must stop
        being asked, so a one-way flag would be a bug that never heals."""
        from app.a2a.client.sdk import PartnerCard
        from app.a2a.client.verification import Verdict, apply_verdict
        from app.models.a2a import USABILITY_USABLE

        row = SimpleNamespace(
            partner_discovery_path="/stale", accepted_card_fingerprint="fp",
            card_fingerprint="fp", usability=USABILITY_USABLE, signature_state=None,
            agreed_protocol_version=None, pinned_origin=None, invocation_url=None,
            declared_name=None, declared_description=None, declared_skills=None,
            declared_input_types=None, declared_output_types=None,
            unusable_reason=None, last_verified_at=None,
            last_verification_error=None,
        )

        def _verdict(path):
            return Verdict(
                usability=USABILITY_USABLE,
                fingerprint="fp",
                card=PartnerCard(name="P", description=None, partner_discovery_path=path),
            )

        apply_verdict(row, _verdict("/declared/agents"))
        assert row.partner_discovery_path == "/declared/agents"
        apply_verdict(row, _verdict(None))
        assert row.partner_discovery_path is None


class TestFollowingAPartnersAgents:
    async def test_an_agent_the_partner_added_becomes_a_target(self, partner):
        partner["response"] = _agents(CARD_A, CARD_B)
        added, _ = await registration.discover_partner_agents(_Db(), _integration())
        assert added == 2
        assert partner["synced"]["urls"] == [CARD_A, CARD_B]

    async def test_a_partner_that_does_not_implement_it_changes_nothing(self, partner):
        # Every non-XTM One deployment. The operator's list keeps running.
        partner["response"] = httpx.Response(404)
        assert await registration.discover_partner_agents(_Db(), _integration()) == (0, 0)
        assert "urls" not in partner["synced"]

    async def test_with_no_seed_there_is_nobody_to_ask(self, partner):
        # Discovery EXTENDS a relationship rather than establishing one.
        # Guessing an origin would be this platform inventing a partner
        # address and then authenticating to it.
        partner["existing"] = []
        partner["response"] = _agents(CARD_A)
        assert await registration.discover_partner_agents(_Db(), _integration()) == (0, 0)


class TestWhatDiscoveryRefuses:
    async def test_a_seed_the_destination_guard_refuses_is_never_asked(self, partner):
        # The GET carries a freshly minted bearer, and the seed is an
        # operator-typed URL that may never have been verified.
        partner["existing"] = [_target("http://127.0.0.1:8100/a2a/x/agent-card.json")]
        partner["response"] = _agents("http://127.0.0.1:8100/a2a/y/agent-card.json")
        assert await registration.discover_partner_agents(_Db(), _integration()) == (0, 0)
        assert partner["asked"] == []

    @pytest.mark.asyncio
    async def test_an_off_origin_url_is_discarded(self, partner):
        # The list arrives over an authenticated channel, but a partner naming
        # somewhere else is the redirect FR-034 refuses wearing a different
        # hat. The sibling on-origin entry still lands.
        partner["response"] = _agents(CARD_A, "https://elsewhere.example/a2a/x/card.json")
        await registration.discover_partner_agents(_Db(), _integration())
        assert partner["synced"]["urls"] == [CARD_A]

    async def test_an_empty_list_does_not_remove_everything(self, partner):
        # A partner that answered oddly must not silently strip every tool.
        # One that genuinely exposes nothing is left to the operator.
        partner["response"] = httpx.Response(200, json={"agents": []})
        assert await registration.discover_partner_agents(_Db(), _integration()) == (0, 0)
        assert "urls" not in partner["synced"]

    @pytest.mark.parametrize(
        "body",
        [
            {"agents": "not-a-list"},
            {"unexpected": []},
            [],
        ],
    )
    async def test_a_malformed_answer_changes_nothing(self, partner, body):
        partner["response"] = httpx.Response(200, json=body)
        assert await registration.discover_partner_agents(_Db(), _integration()) == (0, 0)

    async def test_a_partner_error_changes_nothing(self, partner):
        partner["response"] = httpx.Response(500, text="boom")
        assert await registration.discover_partner_agents(_Db(), _integration()) == (0, 0)

    async def test_discovery_never_confers_trust(self):
        # It chooses what to LOOK at. Every discovered URL still runs the
        # destination guard, TLS, the signature check, version negotiation and
        # the origin pin, because it becomes an ordinary target and nothing
        # else. Asserted on the code path rather than a comment: the function
        # ends by handing URLs to `sync_targets` and writes no verdict itself.
        import inspect

        src = inspect.getsource(registration.discover_partner_agents)
        assert "sync_targets" in src
        assert "usability" not in src
        assert "apply_verdict" not in src


class TestPartnersThatDoNotImplementItAreNotReProbed:
    """No vendor implements this, so the ordinary case must cost nothing.

    Discovery runs on the read path, and the verification card polls. Without
    a backoff every Foundry and Vertex registration anyone left open would
    mint a token and take a 404 every thirty seconds, forever, to learn a fact
    that never changes.
    """

    async def test_a_404_is_remembered(self, partner):
        partner["response"] = httpx.Response(404)
        assert await registration.discover_partner_agents(_Db(), _integration()) == (0, 0)

        # The partner would now answer — but it is not asked, because it said
        # 404 a moment ago.
        partner["response"] = _agents(CARD_A, CARD_B)
        assert await registration.discover_partner_agents(_Db(), _integration()) == (0, 0)
        assert "urls" not in partner["synced"]

    async def test_a_fresh_machine_probes_from_the_first_minute(
        self, partner, monkeypatch
    ):
        # ``time.monotonic`` counts from BOOT. An absent backoff mark must not
        # be read as "marked at boot": with a zero default every partner is
        # silently skipped for the machine's first ten minutes of uptime —
        # deterministic on a fresh CI runner, invisible on a dev box.
        import time as _time

        monkeypatch.setattr(_time, "monotonic", lambda: 42.0)
        partner["response"] = _agents(CARD_A, CARD_B)
        added, _ = await registration.discover_partner_agents(_Db(), _integration())
        assert added == 2

    async def test_the_backoff_expires(self, partner):
        partner["response"] = httpx.Response(404)
        await registration.discover_partner_agents(_Db(), _integration())
        registration._UNSUPPORTED.clear()  # stand-in for the window elapsing

        partner["response"] = _agents(CARD_A, CARD_B)
        added, _ = await registration.discover_partner_agents(_Db(), _integration())
        assert added == 2

    async def test_a_partner_that_starts_answering_is_no_longer_backed_off(
        self, partner
    ):
        # Enabling the feature on the partner's side must take effect, so a
        # successful answer clears the mark rather than leaving it to expire.
        partner["response"] = _agents(CARD_A)
        await registration.discover_partner_agents(_Db(), _integration())
        assert str(_integration().id) not in registration._UNSUPPORTED


class TestTheCachesAreBounded:
    """An entry past its window is never read again; without eviction a
    long-lived worker keeps one per integration ever seen."""

    @pytest.mark.asyncio
    async def test_the_backoff_cache_evicts_expired_entries(self, partner):
        import time

        registration._UNSUPPORTED["gone"] = (
            time.monotonic() - registration._UNSUPPORTED_BACKOFF_SECONDS - 1
        )
        partner["response"] = httpx.Response(404)
        await registration.discover_partner_agents(_Db(), _integration())
        assert "gone" not in registration._UNSUPPORTED
        assert _integration().id in registration._UNSUPPORTED

    @pytest.mark.asyncio
    async def test_the_read_path_throttle_evicts_expired_entries(self, monkeypatch):
        import time

        from app.api.v1 import a2a_client

        async def _quiet(_db, _integration):
            return (0, 0)

        monkeypatch.setattr(registration, "discover_partner_agents", _quiet)
        a2a_client._LAST_DISCOVERY.clear()
        a2a_client._LAST_DISCOVERY["gone"] = (
            time.monotonic() - a2a_client._DISCOVERY_THROTTLE_SECONDS - 1
        )
        await a2a_client._discover_if_due(_Db(), _integration())
        assert "gone" not in a2a_client._LAST_DISCOVERY
        assert _integration().id in a2a_client._LAST_DISCOVERY


class _ExpiringIntegration:
    """An ORM instance after a rollback: every read raises until refreshed."""

    def __init__(self):
        self.__dict__["_expired"] = False
        self.__dict__["_values"] = {
            "id": "i-1",
            "provider": "oauth_a2a",
            "name": "Partner",
            "extra_config_enc": "enc",
        }

    def __getattr__(self, name):
        if self.__dict__["_expired"]:
            raise RuntimeError(f"MissingGreenlet: {name!r} read on an expired instance")
        try:
            return self.__dict__["_values"][name]
        except KeyError:
            raise AttributeError(name) from None


class _RollingBackDb(_Db):
    def __init__(self, integration):
        super().__init__()
        self.integration = integration
        self.rolled_back = 0
        self.refreshed: list = []

    async def rollback(self):
        self.rolled_back += 1
        self.integration.__dict__["_expired"] = True

    async def refresh(self, obj):
        obj.__dict__["_expired"] = False
        self.refreshed.append(obj)


class TestAFailedDiscoveryLeavesTheSessionReadable:
    """The rollback expires the integration; reading it before a refresh is a 500."""

    @pytest.mark.asyncio
    async def test_the_read_path_reloads_what_the_rollback_expired(self, monkeypatch):
        from app.api.v1 import a2a_client

        async def _loses_the_race(_db, _integration):
            raise RuntimeError("duplicate key value violates uq_a2a_target_card")

        monkeypatch.setattr(registration, "discover_partner_agents", _loses_the_race)
        a2a_client._LAST_DISCOVERY.clear()
        integration = _ExpiringIntegration()
        db = _RollingBackDb(integration)

        # Must not raise, from the log line or the handler's next read.
        await a2a_client._discover_if_due(db, integration)

        assert db.rolled_back == 1
        assert db.refreshed == [integration]
        assert integration.id == "i-1"

    def test_the_verify_action_takes_the_same_path(self):
        # One function for both handlers: no second copy of the sequence.
        import inspect

        from app.api.v1 import a2a_client

        verify = inspect.getsource(a2a_client.verify_now)
        assert "_discover_tolerantly(" in verify
        assert "rollback" not in verify
        tolerant = inspect.getsource(a2a_client._discover_tolerantly)
        assert tolerant.index("await db.rollback()") < tolerant.index("await db.refresh(integration)")
        assert tolerant.index("await db.refresh(integration)") < tolerant.index("logger.warning(")
