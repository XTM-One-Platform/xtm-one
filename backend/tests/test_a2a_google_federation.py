"""Google federated partner authentication on the inbound A2A surface.

The property under test throughout: everything the verification trusts —
audience and allowed caller — comes from the BOUND INBOUND ``google_a2a``
integration, never from the token. A token can present any audience or caller
it likes; only a stored pin admits it.

And the property that makes this mechanism DIFFERENT from its Entra sibling,
which several tests below exist solely to hold: Google publishes one keyset
for every principal, so a valid signature proves only "some Google identity
minted this". The caller pin is the entire narrowing, which is why a config
without one is refused rather than defaulted.

Mirrors the repo's fake-session convention. RSA signing is real (the
``cryptography`` dependency), only the JWKS fetch is stubbed — the network is
the one thing these tests must not touch.
Config encryption is real too (``encrypt_value``), because the resolution path
under test decrypts for itself.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

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

from app.a2a import google_federation as gf
from app.a2a import providers
from app.core.security import encrypt_value
from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND

AUDIENCE = "https://xtm.example.com/a2a"
CALLER = "partner-agent@partner-project.iam.gserviceaccount.com"
CALLER_SUB = "104729384756102938475"

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
_PUBLIC_PEM = _key.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
)


def _claims(**overrides):
    now = datetime.now(UTC)
    base = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "sub": CALLER_SUB,
        "email": CALLER,
        "email_verified": True,
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
    """Stub the JWKS fetch; record every URL a resolution was sent to."""
    seen = {"urls": []}

    async def _resolve(jwks_url, _token, *, timeout=30):
        seen["urls"].append(jwks_url)
        return _Key()

    monkeypatch.setattr(gf, "resolve_signing_key", _resolve)
    return seen


def _pins(**overrides) -> gf.GooglePins:
    kwargs = {"audience": AUDIENCE, "caller": CALLER}
    kwargs.update(overrides)
    return gf.GooglePins(**kwargs)


async def _verify(token, pins=None):
    return await gf.verify_google_token(token, [pins or _pins()])


class TestVerifyGoogleToken:
    async def test_valid_token_is_accepted(self, jwks):
        verified = await _verify(_token())
        assert verified is not None
        claims, pins = verified
        assert claims["email"] == CALLER
        assert pins.audience == AUDIENCE

    async def test_caller_may_be_pinned_by_numeric_subject(self, jwks):
        """An operator reads whichever identifier their console shows; the pin
        must not require knowing which claim the token carries it in."""
        assert await _verify(_token(), _pins(caller=CALLER_SUB)) is not None

    async def test_caller_email_comparison_is_case_insensitive(self, jwks):
        assert await _verify(_token(email=CALLER.upper())) is not None

    async def test_unverified_email_is_refused(self, jwks):
        """Google sets ``email_verified`` on every service account token, so
        requiring it costs a correct setup nothing — and refuses the one
        shape where an address means less than it looks like it does. The
        numeric subject is unaffected: it needs no corroboration."""
        assert await _verify(_token(email_verified=False)) is None
        assert (
            await _verify(_token(email_verified=False), _pins(caller=CALLER_SUB))
            is not None
        )

    async def test_wrong_caller_is_refused(self, jwks):
        """The pin that carries the whole trust decision. A token signed by
        Google for SOME OTHER identity verifies perfectly — nothing but this
        comparison separates the partner from every other Google account."""
        other = "someone-else@other-project.iam.gserviceaccount.com"
        assert await _verify(_token(email=other, sub="99999999999999999999")) is None

    async def test_wrong_audience_is_refused(self, jwks):
        assert await _verify(_token(aud="https://elsewhere.example/a2a")) is None

    async def test_list_valued_audience_matches_on_any_entry(self, jwks):
        """``aud`` is legally a string or a list; a correct setup must not be
        refused for the shape its minting code happened to produce."""
        assert await _verify(_token(aud=["https://other", AUDIENCE])) is not None

    async def test_missing_audience_is_refused(self, jwks):
        """Audience verification is done here rather than by PyJWT (one decode
        has to serve every bound config), so the absent case needs its own
        guard — a token with no ``aud`` must not fall through as a match."""
        assert await _verify(_token(aud=None)) is None

    async def test_expired_token_is_refused(self, jwks):
        assert await _verify(_token(exp=datetime.now(UTC) - timedelta(minutes=5))) is None

    async def test_foreign_issuer_is_refused(self, jwks):
        assert await _verify(_token(iss="https://evil.example")) is None

    async def test_both_google_issuer_spellings_are_accepted(self, jwks):
        assert await _verify(_token(iss="accounts.google.com")) is not None

    async def test_jwks_url_is_a_constant_the_token_cannot_influence(self, jwks):
        """The keyset-substitution guard, which this mechanism gets for free:
        there is no stored or claimed value anywhere in the URL."""
        await _verify(_token(iss="https://evil.example/certs"))
        assert jwks["urls"] == ["https://www.googleapis.com/oauth2/v3/certs"]

    async def test_unreachable_jwks_refuses_rather_than_raises(self, monkeypatch):
        async def _resolve(_url, _token, *, timeout=30):
            raise PyJWKClientError("connection refused")

        monkeypatch.setattr(gf, "resolve_signing_key", _resolve)
        assert await _verify(_token()) is None

    async def test_algorithm_confusion_is_refused(self, jwks):
        """An HS256 token keyed on the PUBLIC key is the classic downgrade.

        Forged by hand — PyJWT itself refuses to mint one, but an attacker is
        not using PyJWT. The fixed RS256 allow-list must refuse it before any
        signature is considered."""
        import base64
        import hashlib
        import hmac

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

    async def test_no_pins_costs_no_jwks_work(self, jwks):
        assert await gf.verify_google_token(_token(), []) is None
        assert jwks["urls"] == []

    async def test_many_bound_partners_cost_one_signature_check(self, jwks):
        """The shape the constant keyset makes possible, and the reason there
        is no ``narrow_by_claimed_*`` helper here: the signature is verified
        once and the surviving claims are compared against each pin."""
        pins = [
            _pins(audience=f"https://other-{n}.example") for n in range(4)
        ] + [_pins()]
        assert await gf.verify_google_token(_token(), pins) is not None
        assert len(jwks["urls"]) == 1

    async def test_the_admitting_pin_is_returned_not_just_the_claims(self, jwks):
        """The caller has to know WHICH config admitted the token: that
        integration is the partner's identity on the task surface."""
        integration_id = uuid.uuid4()
        wanted = _pins(integration_id=integration_id)
        verified = await gf.verify_google_token(
            _token(), [_pins(audience="https://other.example"), wanted]
        )
        assert verified is not None
        assert verified[1].integration_id == integration_id


# ─────────────────────────────────────────────────────────────────────────────
# Pins parsing and binding resolution (the integration side of the trust)
# ─────────────────────────────────────────────────────────────────────────────


def _pins_config(**overrides) -> dict:
    base = {"audience": AUDIENCE, "service_account": CALLER}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


class TestParseGooglePins:
    def test_valid_config_parses(self):
        pins = gf.parse_google_pins(_pins_config())
        assert (pins.audience, pins.caller) == (AUDIENCE, CALLER)

    def test_values_tolerate_whitespace(self):
        pins = gf.parse_google_pins(
            _pins_config(audience=f" {AUDIENCE} ", service_account=f" {CALLER}\n")
        )
        assert (pins.audience, pins.caller) == (AUDIENCE, CALLER)

    def test_numeric_subject_is_a_valid_caller(self):
        assert gf.parse_google_pins(
            _pins_config(service_account=CALLER_SUB)
        ).caller == CALLER_SUB

    @pytest.mark.parametrize("missing", ["audience", "service_account"])
    def test_missing_field_is_refused(self, missing):
        with pytest.raises(gf.GooglePinsError):
            gf.parse_google_pins(_pins_config(**{missing: None}))

    def test_the_missing_caller_message_says_why_it_matters(self):
        """An operator who leaves it blank is one step from an open door, so
        the refusal explains rather than just naming the field."""
        with pytest.raises(gf.GooglePinsError, match="one keyset"):
            gf.parse_google_pins(_pins_config(service_account=None))

    @pytest.mark.parametrize(
        "caller", ["partner-agent", "no domain here", "@nolocal.com", "a@b"]
    )
    def test_caller_shapes_no_token_could_carry_are_refused(self, caller):
        with pytest.raises(gf.GooglePinsError):
            gf.parse_google_pins(_pins_config(service_account=caller))

    def test_implausibly_long_values_are_refused(self):
        with pytest.raises(gf.GooglePinsError):
            gf.parse_google_pins(_pins_config(audience="x" * 500))
        with pytest.raises(gf.GooglePinsError):
            gf.parse_google_pins(
                _pins_config(service_account="x" * 400 + "@y.example.com")
            )

    def test_empty_config_is_refused(self):
        with pytest.raises(gf.GooglePinsError):
            gf.parse_google_pins(None)


class TestCatalogEntry:
    def test_the_extra_fields_are_exactly_what_the_parser_reads(self):
        """A drift here would create integrations the save endpoint refuses,
        or ask an operator for a value nothing consumes."""
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        entry = PROVIDER_CATALOG[providers.GOOGLE_A2A_PROVIDER]
        assert set(entry["extra_fields"]) == {"audience", "service_account"}
        assert entry["auth_type"] == "none"
        assert entry["fields"] == []

    def test_inbound_asks_for_pins_and_outbound_for_a_card_and_a_key(self):
        """The two directions share not one field, which is why the creation
        form must never merge them."""
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        entry = PROVIDER_CATALOG[providers.GOOGLE_A2A_PROVIDER]
        inbound = set(entry["extra_fields"])
        outbound = set(entry["variants"]["outbound"]["extra_fields"])
        # Two credential modes outbound, one of which must be configured: a
        # key to sign an assertion with, or an account to impersonate. The
        # second is the only mode available where the partner's organisation
        # forbids service account keys, which is Google's own recommended
        # baseline.
        assert outbound == {
            "card_urls",
            # A CHOICE plus the field each answer needs, rather than two
            # fields one of which must be left blank: both filled in is
            # refused, so an operator who did not know that met a refusal
            # instead of a form.
            "credential_mode",
            "service_account_json",
            "impersonate_service_account",
        }
        assert [o["value"] for o in entry["extra_field_options"]["credential_mode"]] == [
            "impersonation",
            "key",
        ]
        # Impersonation first, because it is the one that stores no secret and
        # the only one available under Google's own recommended key ban.
        assert entry["extra_field_visible_when"] == {
            "impersonate_service_account": {"credential_mode": ["impersonation"]},
            "service_account_json": {"credential_mode": ["key"]},
        }
        assert not (inbound & outbound)


class _Settings:
    def __init__(self, direction):
        self.direction = direction


class _Integration:
    """The attributes ``load_google_pins`` reads off the row."""

    def __init__(self, *, provider=providers.GOOGLE_A2A_PROVIDER, config=...):
        self.id = uuid.uuid4()
        self.name = "Partner Vertex"
        self.provider = provider
        if config is ...:
            config = _pins_config()
        self.extra_config_enc = (
            encrypt_value(json.dumps(config)) if config is not None else None
        )


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

    The settings lookup is answered separately and does NOT consume a queued
    result — recognised by the table it selects from, exactly as the Entra
    suite's double does, so a new query upstream cannot silently shift every
    later answer by one.
    """

    def __init__(self, results, direction=DIRECTION_INBOUND):
        self._r = list(results)
        self._settings = _Settings(direction) if direction is not None else None

    async def execute(self, statement):
        if "a2a_integration_settings" in str(statement):
            return _Result(self._settings)
        return _Result(self._r.pop(0) if self._r else None)


class TestLoadGooglePins:
    async def test_bound_inbound_integration_resolves(self):
        row = _Integration()
        pins = await gf.load_google_pins(_DB([row]), row.id)
        assert pins is not None
        assert pins.integration_id == row.id

    async def test_a_deleted_row_resolves_to_nothing(self):
        assert await gf.load_google_pins(_DB([None]), uuid.uuid4()) is None

    async def test_a_row_of_another_provider_is_not_a_trust_anchor(self):
        """The binding key is data. A stale entry under the ``google_a2a``
        key must never turn some other integration's config into pins."""
        other = _Integration(provider="github")
        assert await gf.load_google_pins(_DB([other]), other.id) is None

    async def test_an_outbound_registration_never_admits_callers(self):
        """Its config is a service account key of OURS, not a set of pins.
        Refused on the direction rather than on the accident that its field
        names happen not to overlap."""
        row = _Integration()
        assert (
            await gf.load_google_pins(_DB([row], direction=DIRECTION_OUTBOUND), row.id)
            is None
        )

    async def test_a_registration_with_no_settings_row_is_refused(self):
        row = _Integration()
        assert await gf.load_google_pins(_DB([row], direction=None), row.id) is None

    async def test_an_invalidated_config_stops_admitting_immediately(self):
        """The parse runs per request, so an integration edited into an
        invalid state stops admitting tokens on the next one — no cached
        acceptance to wait out."""
        broken = _Integration(config={"audience": AUDIENCE})
        assert await gf.load_google_pins(_DB([broken]), broken.id) is None

    async def test_unreadable_config_refuses_rather_than_raises(self):
        garbage = _Integration(config=None)
        garbage.extra_config_enc = "not-fernet-at-all"
        assert await gf.load_google_pins(_DB([garbage]), garbage.id) is None


class _AgentRow:
    def __init__(self, bindings):
        self.id = uuid.uuid4()
        self.integration_bindings = bindings


def _bindings(*integrations, extra_ids=()):
    ids = [str(i.id) for i in integrations] + [str(x) for x in extra_ids]
    return _AgentRow({providers.GOOGLE_A2A_PROVIDER: ids} if ids else {})


class TestLoadAgentGooglePins:
    async def test_bound_agent_resolves_its_configs_in_order(self):
        a = _Integration()
        b = _Integration(config=_pins_config(audience="https://second.example"))
        pins = await gf.load_agent_google_pins(
            _DB([_bindings(a, b), a, b]), uuid.uuid4()
        )
        assert [p.audience for p in pins] == [AUDIENCE, "https://second.example"]

    async def test_agent_without_bindings_resolves_nothing(self):
        assert await gf.load_agent_google_pins(_DB([_AgentRow({})]), uuid.uuid4()) == []
        assert await gf.load_agent_google_pins(_DB([None]), uuid.uuid4()) == []

    async def test_unusable_bindings_are_skipped_not_fatal(self):
        good = _Integration()
        broken = _Integration(config={"audience": AUDIENCE})
        pins = await gf.load_agent_google_pins(
            _DB([_bindings(broken, good), broken, good]), uuid.uuid4()
        )
        assert [p.caller for p in pins] == [CALLER]

    async def test_malformed_binding_entries_are_skipped(self):
        good = _Integration()
        rows = _AgentRow({providers.GOOGLE_A2A_PROVIDER: ["not-a-uuid", str(good.id)]})
        pins = await gf.load_agent_google_pins(_DB([rows, good]), uuid.uuid4())
        assert len(pins) == 1

    async def test_binding_list_is_capped(self):
        """Cheaper per candidate than the Entra path (no per-config network
        call), bounded for the same reason: a list read from JSON should not
        decide how much work a request does."""
        first = _Integration()
        extra = [uuid.uuid4() for _ in range(20)]
        db = _DB([_bindings(first, extra_ids=extra), first] + [None] * 30)
        pins = await gf.load_agent_google_pins(db, uuid.uuid4())
        assert len(pins) == 1
        assert len(db._r) == 30 - (gf._MAX_BOUND_CONFIGS - 1)


# ─────────────────────────────────────────────────────────────────────────────
# The task-surface branch (_authenticate → _authenticate_federated → google)
# ─────────────────────────────────────────────────────────────────────────────


class _Exposure:
    def __init__(self):
        self.id = uuid.uuid4()
        self.agent_id = uuid.uuid4()


class TestAuthenticateGoogle:
    async def test_unbound_agent_never_verifies(self, monkeypatch):
        """No ``google_a2a`` binding on the agent, no federation — and no
        JWKS traffic either: one cheap bindings read, then refusal."""
        from app.api.v1 import a2a_server

        called = {"n": 0}

        async def _touch(*_a, **_k):
            called["n"] += 1
            return _Key()

        monkeypatch.setattr(gf, "resolve_signing_key", _touch)
        result = await a2a_server._authenticate_google(
            _DB([_AgentRow({})]), _Exposure(), _token()
        )
        assert result == (None, "unauthenticated", None)
        assert called["n"] == 0

    async def test_verified_caller_identity_comes_from_the_integration(self, jwks):
        from app.api.v1 import a2a_server

        row = _Integration()
        client_id, reason, provider = await a2a_server._authenticate_google(
            _DB([_bindings(row), row]), _Exposure(), _token()
        )
        assert reason == ""
        assert provider == providers.GOOGLE_A2A_PROVIDER
        assert client_id == providers.partner_client_id(
            row.id, providers.GOOGLE_A2A_PROVIDER
        )

    async def test_two_partners_on_one_agent_stay_separated(self, jwks):
        """``tasks/get`` scopes by ``client_id``, so a shared identity would
        let one partner read and cancel another's work."""
        from app.api.v1 import a2a_server

        one, two = _Integration(), _Integration()
        first = await a2a_server._authenticate_google(
            _DB([_bindings(one), one]), _Exposure(), _token()
        )
        second = await a2a_server._authenticate_google(
            _DB([_bindings(two), two]), _Exposure(), _token()
        )
        assert first[0] != second[0]

    async def test_a_google_identity_nothing_pins_is_refused(self, jwks):
        from app.api.v1 import a2a_server

        row = _Integration()
        result = await a2a_server._authenticate_google(
            _DB([_bindings(row), row]),
            _Exposure(),
            _token(email="stranger@elsewhere.iam.gserviceaccount.com", sub="1"),
        )
        assert result == (None, "unauthenticated", None)

    async def test_an_exposure_without_an_agent_refuses(self, jwks):
        from app.api.v1 import a2a_server

        class _Headless:
            id = uuid.uuid4()

        result = await a2a_server._authenticate_google(
            _DB([]), _Headless(), _token()
        )
        assert result == (None, "unauthenticated", None)


class TestFederatedRouting:
    """Which federation checks a non-platform bearer.

    The issuer is read UNVERIFIED, which is safe for the same reason
    ``federation.narrow_by_claimed_tenant`` is: it only chooses which pinned
    configuration does the checking, and every fact that admits a token still
    comes from a stored pin.
    """

    async def test_a_google_issued_token_goes_to_the_google_path(self, monkeypatch):
        from app.api.v1 import a2a_server

        seen = []

        async def _google(_db, _exp, _tok):
            seen.append("google")
            return ("id", "", providers.GOOGLE_A2A_PROVIDER)

        async def _entra(_db, _exp, _tok):
            seen.append("entra")
            return (None, "unauthenticated", None)

        monkeypatch.setattr(a2a_server, "_authenticate_google", _google)
        monkeypatch.setattr(a2a_server, "_authenticate_entra", _entra)
        await a2a_server._authenticate_federated(_DB([]), _Exposure(), _token())
        assert seen == ["google"]

    @pytest.mark.parametrize(
        "token",
        [
            pyjwt.encode(
                {"iss": "https://login.microsoftonline.com/t/v2.0"},
                _PRIVATE_PEM,
                algorithm="RS256",
            ),
            "not-a-jwt-at-all",
        ],
        ids=["entra-token", "not-a-jwt"],
    )
    async def test_everything_else_still_goes_to_entra(self, monkeypatch, token):
        """The routing ADDS a destination; it must not move any token that
        already had one — including an unreadable bearer, which reached the
        Entra path (and its uniform refusal) before this split existed."""
        from app.api.v1 import a2a_server

        seen = []

        async def _google(_db, _exp, _tok):
            seen.append("google")
            return (None, "unauthenticated", None)

        async def _entra(_db, _exp, _tok):
            seen.append("entra")
            return (None, "unauthenticated", None)

        monkeypatch.setattr(a2a_server, "_authenticate_google", _google)
        monkeypatch.setattr(a2a_server, "_authenticate_entra", _entra)
        await a2a_server._authenticate_federated(_DB([]), _Exposure(), token)
        assert seen == ["entra"]


class TestDerivedIdentityNamespaces:
    """The two federated mechanisms must never share a registration id.

    Attribution reads the mechanism back off the PREFIX, so one shared
    "federated" prefix would make every Google submission read as Foundry
    traffic wherever origin is displayed.
    """

    def test_the_two_prefixes_are_distinct(self):
        integration_id = uuid.uuid4()
        assert providers.partner_client_id(
            integration_id, providers.GOOGLE_A2A_PROVIDER
        ) != providers.partner_client_id(
            integration_id, providers.ENTRA_A2A_PROVIDER
        )

    def test_a_derived_id_round_trips_to_its_integration(self):
        integration_id = uuid.uuid4()
        derived = providers.partner_client_id(
            integration_id, providers.GOOGLE_A2A_PROVIDER
        )
        assert providers.partner_integration_id(derived) == integration_id
        assert providers.federated_provider(derived) == providers.GOOGLE_A2A_PROVIDER

    def test_a_minted_id_belongs_to_no_mechanism(self):
        assert providers.federated_provider("a2a-Xy7QpLm3nR8vTk2w") is None

    def test_a_mechanism_with_no_derived_namespace_raises(self):
        """Rather than inventing a prefix: an ``oauth_a2a`` partner always
        carries a minted registration, and a silently-defaulted id would name
        the wrong mechanism everywhere origin is shown."""
        with pytest.raises(ValueError):
            providers.partner_client_id(uuid.uuid4(), providers.OAUTH_A2A_PROVIDER)

    async def test_the_integration_claims_its_own_derived_id(self):
        row = _Integration()

        class _Empty:
            calls = 0

            async def execute(self, _s):
                type(self).calls += 1
                return _Result([])

        ids = await providers.partner_client_ids(_Empty(), row)
        assert providers.partner_client_id(
            row.id, providers.GOOGLE_A2A_PROVIDER
        ) in ids

    async def test_a_google_submission_is_attributed_to_google(self):
        """The bug a shared prefix would cause: the same run reading "Foundry
        A2A" on one page and something else on another."""
        run, integration_id = uuid.uuid4(), uuid.uuid4()

        class _NoLookup:
            calls = 0

            async def execute(self, _s):
                type(self).calls += 1
                return _Result([])

        db = _NoLookup()
        got = await providers.resolve_submitter_providers(
            db,
            {
                run: {
                    "source": "a2a",
                    "client_id": providers.partner_client_id(
                        integration_id, providers.GOOGLE_A2A_PROVIDER
                    ),
                }
            },
        )
        assert got[run] == providers.GOOGLE_A2A_PROVIDER
        assert db.calls == 0
