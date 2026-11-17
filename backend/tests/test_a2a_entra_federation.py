"""Entra federated partner authentication on the inbound A2A surface.

The property under test throughout: everything the verification trusts —
tenant, audience, allowed caller — comes from the BOUND FEDERATION
INTEGRATION (an ``entra_a2a`` integrations row bound through the agent's
ordinary ``integration_bindings``), never from the token. A token can
present any issuer it likes; the JWKS URL is built from the stored tenant,
and the presented issuer is only compared against the two spellings that
stored tenant admits. The pins are re-read and re-validated per request, so
a deleted or invalidated integration stops admitting tokens immediately.

Mirrors the repo's fake-session convention; the backend suite has no
real-DB harness. RSA signing is real (the `cryptography` dependency), only
the JWKS fetch is stubbed — the network is the one thing these tests must
not touch. Config encryption is real too (``encrypt_value``), because the
resolution path under test decrypts for itself.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from jwt import PyJWKClientError

from app.a2a import federation, providers
from app.core.security import encrypt_value

TENANT = uuid.uuid4()
CALLER = uuid.uuid4()
AUDIENCE = "api://xtm-one-a2a"

_V2_ISS = f"https://login.microsoftonline.com/{TENANT}/v2.0"
_V1_ISS = f"https://sts.windows.net/{TENANT}/"

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _key.private_bytes(
    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
)
_PUBLIC_PEM = _key.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
)


def _claims(**overrides):
    now = datetime.now(UTC)
    base = {
        "iss": _V2_ISS,
        "aud": AUDIENCE,
        "azp": str(CALLER),
        "iat": now,
        "exp": now + timedelta(minutes=10),
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _token(**overrides) -> str:
    return pyjwt.encode(_claims(**overrides), _PRIVATE_PEM, algorithm="RS256")


class _Key:
    key = _PUBLIC_PEM


@pytest.fixture
def jwks(monkeypatch):
    """Stub the JWKS fetch; record the URL each resolution was sent to."""
    seen = {"urls": []}

    async def _resolve(jwks_url, _token, *, timeout=30):
        seen["urls"].append(jwks_url)
        return _Key()

    monkeypatch.setattr(federation, "resolve_signing_key", _resolve)
    return seen


async def _verify(token, **overrides):
    kwargs = {
        "tenant_id": TENANT,
        "audience": AUDIENCE,
        "allowed_client": CALLER,
    }
    kwargs.update(overrides)
    return await federation.verify_entra_token(token, **kwargs)


class TestVerifyEntraToken:
    async def test_v2_token_is_accepted(self, jwks):
        claims = await _verify(_token())
        assert claims is not None
        assert claims["azp"] == str(CALLER)

    async def test_v1_token_is_accepted(self, jwks):
        """Which issuer spelling a token carries is decided by the customer's
        app registration (``accessTokenAcceptedVersion``), not by the caller,
        so a correct Azure-side setup must not be refused for it."""
        claims = await _verify(_token(iss=_V1_ISS, azp=None, appid=str(CALLER)))
        assert claims is not None

    async def test_caller_may_be_pinned_by_object_id(self, jwks):
        """``oid`` is what an operator most easily reads off a managed
        identity's portal page; the pin must not require knowing which claim
        the token version uses."""
        claims = await _verify(_token(azp=str(uuid.uuid4()), oid=str(CALLER)))
        assert claims is not None

    async def test_caller_comparison_is_case_insensitive(self, jwks):
        claims = await _verify(_token(azp=str(CALLER).upper()))
        assert claims is not None

    async def test_wrong_audience_is_refused(self, jwks):
        assert await _verify(_token(aud="api://someone-else")) is None

    async def test_wrong_audience_names_both_sides_in_the_log(self, jwks, caplog):
        """No test can prove the audience pin (that needs a credential in
        the partner's tenant), so the rejection log is where a wrong pin
        gets diagnosed — it must name what the token carried AND what is
        pinned. Safe to echo: the signature verified before the claim did
        not, so the aud comes from a genuinely tenant-signed token."""
        with caplog.at_level("WARNING"):
            assert await _verify(_token(aud="api://someone-else")) is None
        assert "audience mismatch" in caplog.text
        assert "api://someone-else" in caplog.text
        assert AUDIENCE in caplog.text

    async def test_expired_token_is_refused(self, jwks):
        past = datetime.now(UTC) - timedelta(minutes=5)
        assert await _verify(_token(exp=past)) is None

    async def test_foreign_tenant_issuer_is_refused(self, jwks):
        other = uuid.uuid4()
        token = _token(iss=f"https://login.microsoftonline.com/{other}/v2.0")
        assert await _verify(token) is None

    async def test_unpinned_caller_is_refused(self, jwks):
        assert await _verify(_token(azp=str(uuid.uuid4()))) is None

    async def test_jwks_url_comes_from_the_stored_tenant_not_the_token(self, jwks):
        """The SSRF/keyset-substitution guard. A token claiming a foreign
        issuer must still be verified against the CONFIGURED tenant's keys —
        the token chooses nothing about where trust is fetched from."""
        await _verify(_token(iss="https://evil.example/tenant/v2.0"))
        assert jwks["urls"] == [
            f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
        ]

    async def test_unreachable_jwks_refuses_rather_than_raises(self, monkeypatch):
        async def _resolve(_url, _token, *, timeout=30):
            raise PyJWKClientError("connection refused")

        monkeypatch.setattr(federation, "resolve_signing_key", _resolve)
        assert await _verify(_token()) is None

    async def test_algorithm_confusion_is_refused(self, jwks):
        """An HS256 token keyed on the PUBLIC key is the classic downgrade.

        Forged by hand — PyJWT itself refuses to mint one, but an attacker
        is not using PyJWT. The fixed RS256 allow-list must refuse it before
        any signature is considered."""
        import base64
        import hashlib
        import hmac
        import json

        def _b64(data: bytes) -> bytes:
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        claims = _claims()
        claims["iat"] = int(claims["iat"].timestamp())
        claims["exp"] = int(claims["exp"].timestamp())
        signing_input = (
            _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
            + b"."
            + _b64(json.dumps(claims).encode())
        )
        signature = hmac.new(_PUBLIC_PEM, signing_input, hashlib.sha256).digest()
        forged = (signing_input + b"." + _b64(signature)).decode()
        assert await _verify(forged) is None

    async def test_never_raises_on_garbage(self, jwks):
        assert await _verify("not-a-jwt-at-all") is None


# ─────────────────────────────────────────────────────────────────────────────
# Pins parsing and binding resolution (the integration side of the trust)
# ─────────────────────────────────────────────────────────────────────────────


def _pins_config(**overrides) -> dict:
    base = {
        "tenant_id": str(TENANT),
        "client_id": str(CALLER),
        "audience": AUDIENCE,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


class _Integration:
    """The three attributes ``load_entra_pins`` reads off the row."""

    def __init__(self, *, provider=providers.ENTRA_A2A_PROVIDER, config=...):
        self.id = uuid.uuid4()
        self.name = "Partner Foundry"
        self.provider = provider
        if config is ...:
            config = _pins_config()
        self.extra_config_enc = (
            encrypt_value(json.dumps(config)) if config is not None else None
        )


class TestParseEntraPins:
    def test_valid_config_parses(self):
        pins = federation.parse_entra_pins(_pins_config())
        assert (pins.tenant_id, pins.client_id) == (TENANT, CALLER)
        assert pins.audience == AUDIENCE

    def test_guids_tolerate_whitespace(self):
        """Pasted GUIDs arrive with stray whitespace; a pin that fails on a
        trailing space would be indistinguishable from a real refusal."""
        pins = federation.parse_entra_pins(
            _pins_config(tenant_id=f" {TENANT} ", audience=f" {AUDIENCE} ")
        )
        assert pins.tenant_id == TENANT
        assert pins.audience == AUDIENCE

    @pytest.mark.parametrize("missing", ["tenant_id", "client_id", "audience"])
    def test_missing_field_is_refused(self, missing):
        with pytest.raises(federation.EntraPinsError):
            federation.parse_entra_pins(_pins_config(**{missing: None}))

    @pytest.mark.parametrize("field", ["tenant_id", "client_id"])
    def test_non_guid_is_refused(self, field):
        with pytest.raises(federation.EntraPinsError):
            federation.parse_entra_pins(_pins_config(**{field: "not-a-guid"}))

    @pytest.mark.parametrize(
        "aud",
        [
            "https://graph.microsoft.com",
            "https://graph.microsoft.com/",
            "HTTPS://GRAPH.MICROSOFT.COM",
            "00000003-0000-0000-c000-000000000000",
            "https://management.azure.com",
        ],
    )
    def test_tenant_wide_audiences_are_refused(self, aud):
        """Any app in a tenant can mint a Graph/ARM token; accepting one as a
        partner credential would let an unrelated token double as one. The
        configuration is refused rather than trusting every operator to know
        this Entra subtlety — at bind time AND per request, since this parse
        runs on both paths."""
        with pytest.raises(federation.EntraPinsError):
            federation.parse_entra_pins(_pins_config(audience=aud))

    def test_dedicated_audiences_pass(self):
        assert federation.parse_entra_pins(_pins_config()).audience == AUDIENCE

    def test_empty_config_is_refused(self):
        with pytest.raises(federation.EntraPinsError):
            federation.parse_entra_pins(None)


class TestCatalogEntry:
    def test_provider_key_and_shape_match_the_trust_module(self):
        """The catalog entry and the auth path anchor on the same provider
        key, and the extra fields are exactly the keys ``parse_entra_pins``
        reads — a drift here would create integrations the bind endpoint
        refuses."""
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        entry = PROVIDER_CATALOG[providers.ENTRA_A2A_PROVIDER]
        assert entry["auth_type"] == "none"
        assert entry["fields"] == []
        assert set(entry["extra_fields"]) == {"tenant_id", "client_id", "audience"}
        # Beside the Microsoft Entra ID tool integration, deliberately.
        assert entry["integration_category"] == (
            PROVIDER_CATALOG["entra"]["integration_category"]
        )

    def test_listed_operations_are_exactly_what_the_protocol_serves(self):
        """The catalog's operation list is what the UI shows a partner they
        may call, so it must equal the protocol's served vocabulary — a
        method advertised here but refused there (or the reverse) is a
        support ticket. Pinned against ``_OPS_BY_METHOD`` rather than a
        hand-copied list so adding an operation to the surface fails this
        test until the catalog documents it."""
        from app.a2a.protocol import (
            _OPS_BY_METHOD,
            _PUSH_OPS_BY_METHOD,
            PROTOCOL_VERSION_0_3,
        )
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        listed = {
            tool["name"]
            for tool in PROVIDER_CATALOG[providers.ENTRA_A2A_PROVIDER][
                "available_tools"
            ]
        }
        # Named by their v0.3 spelling; every served operation appears once.
        # BOTH vocabularies: the push-notification operations are served on
        # the same endpoint and are as invokable as a submission.
        served_v03 = {
            method
            for method, (_op, version) in (
                _OPS_BY_METHOD | _PUSH_OPS_BY_METHOD
            ).items()
            if version == PROTOCOL_VERSION_0_3
        }
        assert listed == served_v03
        assert {op for _m, (op, _v) in _OPS_BY_METHOD.items()} == {
            "send",
            "get",
            "cancel",
        }

    def test_operations_are_typed_by_whether_they_change_state(self):
        """A submission starts a billable run and a cancel stops one; only
        polling is read-only. The badge an operator reads must say so."""
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        by_name = {
            tool["name"]: tool["tool_type"]
            for tool in PROVIDER_CATALOG[providers.ENTRA_A2A_PROVIDER][
                "available_tools"
            ]
        }
        assert by_name == {
            "message/send": "write",
            "tasks/get": "read",
            "tasks/cancel": "write",
            # Registering or dropping a callback changes what this surface
            # will do unprompted; reading one changes nothing.
            "tasks/pushNotificationConfig/set": "write",
            "tasks/pushNotificationConfig/get": "read",
            "tasks/pushNotificationConfig/list": "read",
            "tasks/pushNotificationConfig/delete": "write",
        }

    def test_every_listed_operation_names_its_v1_alias(self):
        """Both vocabularies are served and the method name decides the
        response dialect, so the v1.0 alias must be discoverable from the
        same row rather than only from the spec."""
        from app.a2a.protocol import (
            _OPS_BY_METHOD,
            _PUSH_OPS_BY_METHOD,
            PROTOCOL_VERSION_1_0,
        )
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        every = _OPS_BY_METHOD | _PUSH_OPS_BY_METHOD
        v1_by_op = {
            op: method
            for method, (op, version) in every.items()
            if version == PROTOCOL_VERSION_1_0
        }
        for tool in PROVIDER_CATALOG[providers.ENTRA_A2A_PROVIDER][
            "available_tools"
        ]:
            op = every[tool["name"]][0]
            assert v1_by_op[op] in tool["description"]


class TestLoadEntraPins:
    async def test_usable_integration_resolves(self):
        integration = _Integration()
        pins = await federation.load_entra_pins(_DB([integration]), integration.id)
        assert pins is not None
        assert (pins.tenant_id, pins.client_id) == (TENANT, CALLER)

    async def test_deleted_integration_refuses(self):
        """A binding entry may outlive its integration (bindings are JSON,
        not FKs) — the auth path must fail closed on a row that is gone."""
        assert await federation.load_entra_pins(_DB([None]), uuid.uuid4()) is None

    async def test_other_provider_is_refused(self):
        """Trust anchors on the row's provider: some unrelated integration id
        smuggled under the ``entra_a2a`` binding key must never yield pins,
        even if its config happens to carry the right keys."""
        impostor = _Integration(provider="entra")
        assert (
            await federation.load_entra_pins(_DB([impostor]), impostor.id) is None
        )

    async def test_invalidated_config_refuses(self):
        """An integration edited into an unusable state after binding stops
        admitting tokens on the next request — no cached acceptance."""
        broken = _Integration(config={"tenant_id": str(TENANT)})
        assert await federation.load_entra_pins(_DB([broken]), broken.id) is None

    async def test_unreadable_config_refuses_rather_than_raises(self):
        garbage = _Integration(config=None)
        garbage.extra_config_enc = "not-fernet-at-all"
        assert await federation.load_entra_pins(_DB([garbage]), garbage.id) is None


class _AgentRow:
    """The one attribute the binding reader touches."""

    def __init__(self, bindings):
        self.id = uuid.uuid4()
        self.integration_bindings = bindings


def _bindings(*integrations, extra_ids=()):
    """An agent row binding the given configs, as resolution reads it."""
    ids = [str(i.id) for i in integrations] + [str(x) for x in extra_ids]
    return _AgentRow({providers.ENTRA_A2A_PROVIDER: ids} if ids else {})


class TestLoadAgentEntraPins:
    async def test_bound_agent_resolves_its_configs_in_order(self):
        a, b = _Integration(), _Integration(config=_pins_config(audience="api://second"))
        pins = await federation.load_agent_entra_pins(
            _DB([_bindings(a, b), a, b]), uuid.uuid4()
        )
        assert [p.audience for p in pins] == [AUDIENCE, "api://second"]

    async def test_agent_without_bindings_resolves_nothing(self):
        assert await federation.load_agent_entra_pins(_DB([_AgentRow({})]), uuid.uuid4()) == []
        # No agent row at all (deleted mid-request) resolves to nothing.
        assert await federation.load_agent_entra_pins(_DB([None]), uuid.uuid4()) == []

    async def test_unusable_bindings_are_skipped_not_fatal(self):
        """One stale or broken binding must not take down a working partner
        bound beside it."""
        good = _Integration()
        broken = _Integration(config={"tenant_id": str(TENANT)})
        pins = await federation.load_agent_entra_pins(
            _DB([_bindings(broken, good), broken, good]), uuid.uuid4()
        )
        assert len(pins) == 1
        assert pins[0].audience == AUDIENCE

    async def test_malformed_binding_entries_are_skipped(self):
        good = _Integration()
        bindings = _AgentRow(
            {providers.ENTRA_A2A_PROVIDER: ["not-a-uuid", str(good.id)]}
        )
        pins = await federation.load_agent_entra_pins(
            _DB([bindings, good]), uuid.uuid4()
        )
        assert len(pins) == 1

    async def test_binding_list_is_capped(self):
        """Each candidate can cost a JWKS fetch on a bad token, so the list
        is bounded rather than trusted to stay small."""
        first = _Integration()
        extra = [uuid.uuid4() for _ in range(20)]
        # Only _MAX_BOUND_CONFIGS rows are consulted: the first resolves,
        # the rest of the capped window miss (None), and nothing past the
        # cap is queried — the _DB stub would answer None there anyway,
        # so the assertion is on the resolved count staying bounded work.
        db = _DB([_bindings(first, extra_ids=extra), first] + [None] * 30)
        pins = await federation.load_agent_entra_pins(db, uuid.uuid4())
        assert len(pins) == 1
        assert len(db._r) == 30 - (federation._MAX_BOUND_CONFIGS - 1)


# ─────────────────────────────────────────────────────────────────────────────
# The task-surface branch (_authenticate → _authenticate_entra)
# ─────────────────────────────────────────────────────────────────────────────


class _Exposure:
    def __init__(self):
        self.id = uuid.uuid4()
        self.agent_id = uuid.uuid4()


class _Client:
    def __init__(self, exposure_id):
        self.client_id = "a2a-abc"
        self.a2a_exposure_id = exposure_id


class _Result:
    def __init__(self, v):
        self._v = v

    def scalar_one_or_none(self):
        return self._v

    def scalars(self):
        return self

    def all(self):
        return self._v or []


class _DB:
    """Answers queries from a queued list, in order.

    The A2A direction lookup is answered separately and does NOT consume a
    queued result. Sequenced doubles are brittle in exactly this way: a new
    query anywhere upstream silently shifts every later answer by one, and
    the test then fails somewhere unrelated to the change. Recognising the
    settings query by the table it selects from keeps the queue meaning what
    the test author wrote it to mean.
    """

    def __init__(self, results, direction=None):
        self._r = list(results)
        # ``None`` means "no settings row", which reads as neither direction
        # — so these inbound probe tests keep exercising the inbound path.
        self._direction = direction

    async def execute(self, _s):
        if "a2a_integration_settings" in str(_s):
            return _Result(self._direction)
        return _Result(self._r.pop(0) if self._r else None)


class _Request:
    def __init__(self, token):
        self.headers = {"authorization": f"Bearer {token}"}


class TestAuthenticateEntra:
    async def test_unbound_agent_never_verifies(self, monkeypatch):
        """No ``entra_a2a`` binding on the agent, no federation — and no
        JWKS traffic either: one cheap bindings read, then refusal."""
        from app.api.v1 import a2a_server

        called = {"n": 0}

        async def _touch(*_a, **_k):
            called["n"] += 1

        monkeypatch.setattr(federation, "verify_entra_token", _touch)
        client_id, reason, _provider = await a2a_server._authenticate_entra(
            _DB([{}]), _Exposure(), _token()
        )
        assert client_id is None
        assert reason == "unauthenticated"
        assert called["n"] == 0

    async def test_verified_caller_resolves_to_the_registration(self, monkeypatch):
        """Federation is an alternative CREDENTIAL, not an alternative
        identity: the resolved id is the same registration the OAuth path
        returns, so both forms share one rate budget and one task scope."""
        from app.api.v1 import a2a_server

        integration = _Integration()
        exposure = _Exposure()

        async def _ok(*_a, **kwargs):
            # The pins handed to verification are the bound integration's,
            # freshly resolved — nothing lives on the exposure any more.
            assert kwargs["tenant_id"] == TENANT
            assert kwargs["allowed_client"] == CALLER
            assert kwargs["audience"] == AUDIENCE
            return {"azp": str(CALLER)}

        monkeypatch.setattr(federation, "verify_entra_token", _ok)
        client_id, reason, _provider = await a2a_server._authenticate_entra(
            _DB([_bindings(integration), integration, _Client(exposure.id)]),
            exposure,
            _token(),
        )
        assert client_id == "a2a-abc"
        assert reason == ""

    async def test_second_bound_partner_can_verify(self, monkeypatch):
        """Several partners may be bound to one agent; the token is tried
        against each config until one admits it."""
        from app.api.v1 import a2a_server

        first = _Integration()
        second = _Integration(config=_pins_config(audience="api://second"))
        exposure = _Exposure()

        async def _second_only(*_a, **kwargs):
            return {"azp": str(CALLER)} if kwargs["audience"] == "api://second" else None

        monkeypatch.setattr(federation, "verify_entra_token", _second_only)
        client_id, reason, _provider = await a2a_server._authenticate_entra(
            _DB([_bindings(first, second), first, second, _Client(exposure.id)]),
            exposure,
            _token(),
        )
        assert reason == ""
        # The id names the config that ADMITTED it, not the exposure. With two
        # partners bound there is no unambiguous legacy id to borrow.
        assert client_id == providers.partner_client_id(
            second.id, providers.ENTRA_A2A_PROVIDER
        )

    async def test_two_bound_partners_never_share_one_identity(self, monkeypatch):
        """The separation ``partner_client_id`` exists to give, on the shape
        that most easily loses it: a LEGACY exposure, which does own a
        per-exposure registration, with two federation configs bound.

        Borrowing that registration for both would hand them one task scope —
        ``tasks/get`` filters by ``client_id``, so one partner could read and
        cancel the other's work — one rate budget, and one dedup window.
        """
        from app.api.v1 import a2a_server

        first = _Integration()
        second = _Integration(config=_pins_config(audience="api://second"))
        exposure = _Exposure()

        async def _admits(audience):
            async def _verify(*_a, **kwargs):
                return (
                    {"azp": str(CALLER)}
                    if kwargs["audience"] == audience
                    else None
                )

            return _verify

        seen = []
        for integration, audience in ((first, AUDIENCE), (second, "api://second")):
            monkeypatch.setattr(
                federation, "verify_entra_token", await _admits(audience)
            )
            client_id, reason, _p = await a2a_server._authenticate_entra(
                _DB([_bindings(first, second), first, second, _Client(exposure.id)]),
                exposure,
                _token(),
            )
            assert reason == ""
            assert client_id == providers.partner_client_id(
                integration.id, providers.ENTRA_A2A_PROVIDER
            )
            seen.append(client_id)

        assert seen[0] != seen[1]

    async def test_a_lone_partner_still_inherits_the_legacy_identity(
        self, monkeypatch
    ):
        """The continuity the legacy branch exists for is unaffected: with ONE
        config bound the borrowing cannot be ambiguous, so a Foundry partner
        already in flight keeps its task scope across the upgrade."""
        from app.api.v1 import a2a_server

        only = _Integration()
        exposure = _Exposure()

        async def _ok(*_a, **_k):
            return {"azp": str(CALLER)}

        monkeypatch.setattr(federation, "verify_entra_token", _ok)
        client_id, reason, provider = await a2a_server._authenticate_entra(
            _DB([_bindings(only), only, _Client(exposure.id)]),
            exposure,
            _token(),
        )
        assert reason == ""
        assert client_id == "a2a-abc"
        assert provider == providers.ENTRA_A2A_PROVIDER

    async def test_a_legacy_registration_still_reports_the_entra_form(
        self, monkeypatch
    ):
        """The bug a user hit: a Foundry integration listing OAuth activity.

        On an exposure minted by the old explicit toggle, a federated caller
        deliberately reuses that exposure's OAuth-SHAPED registration id — one
        partner, one rate budget, one task scope, however they authenticate. So
        the id cannot be read back as "which partner is this": a genuine
        Foundry submission is indistinguishable from an OAuth one by shape, and
        every surface that displays origin reported it as the wrong partner.

        The credential form is known only here, at the moment it verifies, and
        is returned alongside the id precisely so it can be stamped on the run
        rather than re-derived later. This asserts the two DISAGREE and that
        each says its own thing."""
        from app.a2a import providers
        from app.api.v1 import a2a_server

        integration = _Integration()
        exposure = _Exposure()

        async def _ok(*_a, **_k):
            return {"azp": str(CALLER)}

        monkeypatch.setattr(federation, "verify_entra_token", _ok)
        client_id, reason, provider = await a2a_server._authenticate_entra(
            _DB([_bindings(integration), integration, _Client(exposure.id)]),
            exposure,
            _token(),
        )

        assert reason == ""
        # The id is the legacy OAuth-shaped one — that is the budget it shares,
        # and nothing about its shape says a federated caller presented it.
        assert client_id == "a2a-abc"
        assert providers.partner_integration_id(client_id) is None
        # …and the credential form is Entra, which is what the run records.
        assert provider == providers.ENTRA_A2A_PROVIDER

    async def test_verified_caller_identity_comes_from_the_integration(
        self, monkeypatch
    ):
        """Exposure is implicit now, so nothing mints a per-exposure OAuth
        registration to borrow an identity from. The partner IS the
        integration: one budget and one task scope per partner, across every
        agent it reaches."""
        from app.a2a import providers
        from app.api.v1 import a2a_server

        integration = _Integration()

        async def _ok(*_a, **_k):
            return {"azp": str(CALLER)}

        monkeypatch.setattr(federation, "verify_entra_token", _ok)
        client_id, reason, _provider = await a2a_server._authenticate_entra(
            # No legacy registration for this exposure.
            _DB([_bindings(integration), integration, None]),
            _Exposure(),
            _token(),
        )
        assert client_id == providers.partner_client_id(
            integration.id, providers.ENTRA_A2A_PROVIDER
        )
        assert reason == ""

    async def test_a_legacy_registration_still_wins(self, monkeypatch):
        """An exposure minted by the old explicit toggle keeps its
        registration id, so a partner already in flight does not lose sight
        of its own tasks when this change lands."""
        from app.api.v1 import a2a_server

        integration = _Integration()
        exposure = _Exposure()

        async def _ok(*_a, **_k):
            return {"azp": str(CALLER)}

        monkeypatch.setattr(federation, "verify_entra_token", _ok)
        client_id, _, _provider = await a2a_server._authenticate_entra(
            _DB([_bindings(integration), integration, _Client(exposure.id)]),
            exposure,
            _token(),
        )
        assert client_id == "a2a-abc"

    async def test_two_partners_on_one_agent_stay_separated(self, monkeypatch):
        """Distinct integrations must resolve to distinct identities, or two
        partners bound to the same agent would share a task scope."""
        from app.a2a import providers

        first, second = _Integration(), _Integration()
        assert providers.partner_client_id(
            first.id, providers.ENTRA_A2A_PROVIDER
        ) != providers.partner_client_id(second.id, providers.ENTRA_A2A_PROVIDER)

    async def test_rejected_token_is_the_uniform_refusal(self, monkeypatch):
        from app.api.v1 import a2a_server

        integration = _Integration()

        async def _no(*_a, **_k):
            return None

        monkeypatch.setattr(federation, "verify_entra_token", _no)
        client_id, reason, _provider = await a2a_server._authenticate_entra(
            _DB([_bindings(integration), integration]), _Exposure(), _token()
        )
        assert client_id is None
        assert reason == "unauthenticated"

    async def test_bound_but_unusable_integration_is_the_uniform_refusal(
        self, monkeypatch
    ):
        """A binding whose integration was deleted or invalidated behaves
        exactly like no opt-in at all — and never reaches verification."""
        from app.api.v1 import a2a_server

        async def _never(*_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("verification ran without usable pins")

        monkeypatch.setattr(federation, "verify_entra_token", _never)
        integration = _Integration(config={"tenant_id": str(TENANT)})
        client_id, reason, _provider = await a2a_server._authenticate_entra(
            _DB([_bindings(integration), integration]), _Exposure(), _token()
        )
        assert client_id is None
        assert reason == "unauthenticated"

    async def test_non_platform_bearer_falls_through_to_entra(self, monkeypatch):
        """The wiring inside ``_authenticate``: a bearer our own decode cannot
        read is handed to the federation branch rather than refused outright.
        """
        from app.api.v1 import a2a_server

        integration = _Integration()
        exposure = _Exposure()

        async def _ok(*_a, **_k):
            return {"azp": str(CALLER)}

        monkeypatch.setattr(federation, "verify_entra_token", _ok)
        client_id, _, _provider = await a2a_server._authenticate(
            _Request(_token()),
            _DB([_bindings(integration), integration, _Client(exposure.id)]),
            exposure,
        )
        assert client_id == "a2a-abc"

    async def test_platform_key_still_short_circuits_before_entra(self, monkeypatch):
        from app.api.v1 import a2a_server

        async def _never(*_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("federation consulted for a platform key")

        monkeypatch.setattr(federation, "verify_entra_token", _never)
        client_id, reason, _provider = await a2a_server._authenticate(
            _Request("fcp-secret"), _DB([]), _Exposure()
        )
        assert client_id is None
        assert reason == "rejected_platform_key"


# ─────────────────────────────────────────────────────────────────────────────
# Admin surface — the status carries no federation state by design
# ─────────────────────────────────────────────────────────────────────────────


class TestExposureStatusProjection:
    async def test_status_reads_back_the_oauth_identifiers_never_the_secret(
        self, monkeypatch
    ):
        """A status read carries the re-displayable identifiers a LEGACY
        per-exposure credential has — client id, token URL — and never a
        secret: there is no field for one, because nothing on this read-only
        router can mint one. Federation state is deliberately absent too: its
        single home is the agent's integration bindings, and a mirror here
        would be a second copy to drift."""
        from app.api.v1 import a2a_admin
        from app.models.a2a import A2AExposure

        monkeypatch.setattr(
            a2a_admin, "external_base", lambda _r: "https://xtm.example"
        )
        exposure = A2AExposure(
            card_token="tok-abc",
            run_as_user_id=uuid.uuid4(),
        )
        status = await a2a_admin._status_of(None, _DB(["a2a-abc"]), exposure)
        assert status.client_id == "a2a-abc"
        assert status.token_url == "https://xtm.example/mcp/oauth/token"
        assert status.card_url.endswith(
            "/a2a/tok-abc/.well-known/agent-card.json"
        )
        assert not hasattr(status, "client_secret")
        assert not hasattr(status, "entra")

    async def test_status_token_url_is_the_card_token_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The inbound integration page shows the status ``token_url`` for the
        partner to paste into its outbound form (#12333): it must be the very
        ``tokenUrl`` the agent card publishes under the same deployment
        origin, in both protocol versions, or the copied value would drift
        from the one the partner's discovery reads."""
        from app.a2a.card import build_agent_card
        from app.api.v1 import a2a_admin
        from app.models.a2a import A2AExposure

        origin = "https://lab.example/xtm-one"
        monkeypatch.setattr(a2a_admin, "external_base", lambda _r: origin)
        exposure = A2AExposure(card_token="tok-abc", run_as_user_id=uuid.uuid4())
        db: Any = _DB([])  # type: ignore[no-untyped-call]
        status = await a2a_admin._status_of(cast(Any, None), db, exposure)
        agent: Any = SimpleNamespace(
            id=uuid.uuid4(),
            name="A",
            description="",
            output_format="markdown",
            output_schema=None,
            builtin_tool_ids=[],
            custom_tool_ids=[],
            mcp_server_ids=[],
            knowledge_base_ids=[],
            integration_bindings={},
        )
        for version in ("1.0", "0.3"):
            card = build_agent_card(agent, exposure, base_url=origin, version=version, app_version="1")
            flows = card["securitySchemes"]["oauth2ClientCredentials"]["flows"]
            assert status.token_url == flows["clientCredentials"]["tokenUrl"]
        assert status.token_url == f"{origin}/mcp/oauth/token"


# ─────────────────────────────────────────────────────────────────────────────
# The connectivity probe — what "Test Connection" can honestly verify
# ─────────────────────────────────────────────────────────────────────────────


class _BoundAgent:
    def __init__(self, integration_id, *, under=providers.ENTRA_A2A_PROVIDER):
        self.id = uuid.uuid4()
        self.integration_bindings = {under: [str(integration_id)]}


class _LiveExposure:
    def __init__(self, agent_id):
        self.agent_id = agent_id


def _probe_ctx(integration=None, *, extra=..., db=None):
    from app.integrations.connectivity import ProbeContext

    if extra is ...:
        extra = _pins_config()
    return ProbeContext(
        provider=providers.ENTRA_A2A_PROVIDER,
        name="Partner Foundry",
        token=None,
        extra=extra,
        db=db,
        integration=integration,
    )


@pytest.fixture
def probe(monkeypatch):
    """The probe with chat enabled and the JWKS fetch stubbed
    reachable — each test overrides the part it is about."""
    from app.integrations.providers import entra_a2a
    from tests.a2a_chat_gate import stub_chat_gate

    stub_chat_gate(monkeypatch)

    async def _reachable(_tenant):
        return None, 3

    monkeypatch.setattr(entra_a2a, "_fetch_tenant_keys", _reachable)
    return entra_a2a


class TestConnectivityProbe:
    def test_probe_is_registered(self):
        """The generic test path only reports a misleading credential
        failure for this provider; registration is what routes the button
        to a verdict that means something."""
        import app.integrations.providers.entra_a2a  # noqa: F401
        from app.integrations.connectivity import has_connectivity_probe

        assert has_connectivity_probe(providers.ENTRA_A2A_PROVIDER)

    async def test_without_chat_fails_before_any_work(self, probe, monkeypatch):
        # A2A is offered exactly while agentic chat is: the verdict says so and
        # nothing is fetched. With a session: a context that has none cannot
        # ask, and reports nothing.
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch, enabled=False)

        async def _never(_tenant):  # pragma: no cover - must not run
            raise AssertionError("JWKS fetched with A2A not offered")

        monkeypatch.setattr(probe, "_fetch_tenant_keys", _never)
        result = await probe.check_connectivity(_probe_ctx(db=object()))
        assert result.status == "error"
        assert "Agentic chat is unavailable" in result.message

    async def test_invalid_pins_fail_without_network(self, probe, monkeypatch):
        """The same parser the auth path runs — a config the probe passes is
        exactly one whose tokens the task surface will consider."""

        async def _never(_tenant):  # pragma: no cover - must not run
            raise AssertionError("JWKS fetched for an invalid config")

        monkeypatch.setattr(probe, "_fetch_tenant_keys", _never)
        result = await probe.check_connectivity(
            _probe_ctx(extra={"tenant_id": str(TENANT)})
        )
        assert result.status == "error"
        assert "allowed caller" in result.message

    @pytest.mark.parametrize("aud", ["foundry-a2a", "api:/xtm-one", "xtm one"])
    async def test_impossible_audience_shape_fails_without_network(
        self, probe, monkeypatch, aud
    ):
        """The audience's CORRECTNESS is unprovable without a partner-tenant
        credential, but a value outside every legal aud shape is a typo no
        token will ever match — the one audience mistake a test can catch."""

        async def _never(_tenant):  # pragma: no cover - must not run
            raise AssertionError("JWKS fetched for an impossible audience")

        monkeypatch.setattr(probe, "_fetch_tenant_keys", _never)
        result = await probe.check_connectivity(
            _probe_ctx(extra=_pins_config(audience=aud))
        )
        assert result.status == "error"
        assert "aud claim" in result.message

    @pytest.mark.parametrize(
        "aud",
        [
            "api://xtm-one-a2a",
            "https://contoso.com/xtm",
            "urn:xtm:a2a",
            "spn:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ],
    )
    async def test_legal_audience_shapes_pass(self, probe, aud):
        result = await probe.check_connectivity(
            _probe_ctx(extra=_pins_config(audience=aud))
        )
        assert result.status == "ok"

    async def test_green_verdict_states_what_it_could_not_prove(self, probe):
        """A passing test must not read as more than it proved — a
        well-shaped but WRONG audience still passes every check here, and
        only the partner's first token exposes it."""
        result = await probe.check_connectivity(_probe_ctx())
        assert result.status == "ok"
        assert "first real token" in result.message

    async def test_unreachable_tenant_fails(self, probe, monkeypatch):
        async def _down(_tenant):
            return "Microsoft Entra answered HTTP 400 for tenant x.", 0

        monkeypatch.setattr(probe, "_fetch_tenant_keys", _down)
        result = await probe.check_connectivity(_probe_ctx())
        assert result.status == "error"
        assert "HTTP 400" in result.message

    async def test_reachable_without_row_reports_ok(self, probe):
        """Provider unit-test path: no db/integration in the context — the
        probe reports what it could verify instead of dereferencing None."""
        result = await probe.check_connectivity(_probe_ctx())
        assert result.status == "ok"
        assert "3 signing keys" in result.message

    async def test_unbound_config_is_degraded(self, probe):
        integration = _Integration()
        result = await probe.check_connectivity(
            _probe_ctx(integration, db=_DB([[]]))
        )
        assert result.status == "degraded"
        assert "Tools tab" in result.message

    async def test_bound_but_unexposed_is_degraded(self, probe):
        integration = _Integration()
        agent = _BoundAgent(integration.id)
        result = await probe.check_connectivity(
            _probe_ctx(integration, db=_DB([[agent], []]))
        )
        assert result.status == "degraded"
        assert "none is exposed" in result.message

    async def test_bound_and_exposed_is_ok(self, probe):
        integration = _Integration()
        agent = _BoundAgent(integration.id)
        stray = _BoundAgent(integration.id, under="entra")
        result = await probe.check_connectivity(
            _probe_ctx(
                integration,
                db=_DB([[agent, stray], [_LiveExposure(agent.id)]]),
            )
        )
        assert result.status == "ok"
        assert "Bound to 1 agent, 1 exposed" in result.message


class TestNarrowByClaimedTenant:
    """Candidate selection from an UNVERIFIED claim — never a trust decision.

    ``verify_entra_token`` must resolve a signing key before it can reject
    anything, and an unknown ``kid`` makes PyJWKClient refetch even a cached
    keyset — so a garbage bearer against an agent binding several partners
    costs one outbound fetch per config. The card token addressing that surface
    is not a secret, so anyone can reach it. Narrowing makes the honest case one
    resolution while never removing a candidate that could have verified.
    """

    @staticmethod
    def _pins(*tenants):
        return [
            federation.EntraPins(
                tenant_id=t, client_id=CALLER, audience=AUDIENCE
            )
            for t in tenants
        ]

    def test_a_matching_claim_selects_its_tenant(self):
        other = uuid.uuid4()
        pins = self._pins(other, TENANT)
        got = federation.narrow_by_claimed_tenant(_token(tid=str(TENANT)), pins)
        assert [p.tenant_id for p in got] == [TENANT]

    def test_case_is_not_significant(self):
        """Entra mints lowercase GUIDs, but a pin typed by hand may not be —
        a mismatch on letter case would silently cost the fetches this exists
        to avoid."""
        pins = self._pins(TENANT, uuid.uuid4())
        got = federation.narrow_by_claimed_tenant(
            _token(tid=str(TENANT).upper()), pins
        )
        assert [p.tenant_id for p in got] == [TENANT]

    def test_a_claim_matching_nothing_falls_back_to_every_candidate(self):
        """Fail OPEN, on purpose. This is an optimisation, so it must never
        remove a candidate that would have verified — the rate limiter is what
        bounds the adversarial case."""
        pins = self._pins(uuid.uuid4(), uuid.uuid4())
        got = federation.narrow_by_claimed_tenant(
            _token(tid=str(uuid.uuid4())), pins
        )
        assert got == pins

    def test_no_claim_falls_back(self):
        pins = self._pins(uuid.uuid4(), uuid.uuid4())
        assert federation.narrow_by_claimed_tenant(_token(), pins) == pins

    def test_an_unreadable_token_falls_back(self):
        """Refusal is the verifier's job, not this function's — it must not
        raise on a bearer that is not a JWT at all."""
        pins = self._pins(uuid.uuid4(), uuid.uuid4())
        assert federation.narrow_by_claimed_tenant("not-a-jwt", pins) == pins

    def test_a_single_candidate_is_never_examined(self):
        """The common shape: nothing to choose between, so the token is not
        decoded at all."""
        pins = self._pins(TENANT)
        assert federation.narrow_by_claimed_tenant("not-a-jwt", pins) == pins

    def test_narrowing_cannot_admit_a_token_the_pin_would_refuse(self):
        """The safety property. A forged ``tid`` only ever changes WHICH pinned
        configs are tried; every fact that admits a token — signing keys,
        issuer, audience, caller — still comes from the stored pin, so the
        worst a liar achieves is being refused by fewer configs."""
        real, forged = TENANT, uuid.uuid4()
        pins = self._pins(real)
        got = federation.narrow_by_claimed_tenant(_token(tid=str(forged)), pins)
        assert [p.tenant_id for p in got] == [real]
