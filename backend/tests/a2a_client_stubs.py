"""Shared doubles for the outbound (client-side) A2A test suite.

Two halves:

* The card-fetch stubs — ``StubPartner`` and the named partner factories —
  that ``test_a2a_client_verification.py`` and
  ``test_a2a_client_card_interfaces.py`` build on. They exist so card
  verification can be validated end to end **without a Vertex or Foundry
  account** — if validating it required a vendor Preview account, most of it
  would never be validated at all.

* The delegation-path doubles — ``settings_row``, ``integration_row``,
  ``client_for``, ``rpc``, ``RecordingDb`` — shared by the delegation-side
  files (delegation, continuation, attribution, payload boundary, protocol
  refusals, run paths, REST transport).

The stubs are deliberately plain objects with an ``httpx``-compatible
transport rather than a live server: the tests assert what this platform
*does with* a partner's answers, and a real socket would add flakiness
without adding coverage. Where a real connection genuinely matters — DNS,
TLS, the destination guard — those paths are tested directly in
``test_a2a_client_destinations.py``.

Not covered here, and deliberately: the OAuth target can be exercised against
this platform's OWN inbound surface, which is a better test than any stub
because it is a real A2A server. That lives in
``test_a2a_client_self_federation.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx

from app.models.a2a import DIRECTION_OUTBOUND, USABILITY_USABLE

#: A card that verifies, speaks both protocol versions, and accepts text.
GOOD_CARD: dict[str, Any] = {
    "name": "Partner Triage Agent",
    "description": "Triages an alert and returns a verdict.",
    "url": "https://partner.example/rpc",
    "skills": [{"name": "triage", "description": "Triage an alert"}],
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "protocolVersions": ["1.0", "0.3"],
    "securitySchemes": {"oauth2": {"type": "oauth2"}},
    "capabilities": {"pushNotifications": False},
}


def card_without(*keys: str) -> dict[str, Any]:
    """The good card minus some fields — for the tolerance cases."""
    out = dict(GOOD_CARD)
    for k in keys:
        out.pop(k, None)
    return out


def card_with(**overrides: Any) -> dict[str, Any]:
    out = dict(GOOD_CARD)
    out.update(overrides)
    return out


def card_declaring(*, callbacks: bool = False) -> dict[str, Any]:
    """The good card, saying whether this partner will call back.

    Its own helper because the default card declares nothing, and the cases
    that matter are about the difference.
    """
    return card_with(capabilities={"pushNotifications": callbacks})


class SlowPartnerTask:
    """A partner that answers a submission before the work is done.

    The shape this platform's own inbound surface has: it detaches the run and
    replies ``working``, so a caller waiting for a terminal response on the
    submission waits forever. ``finish()`` makes the next read terminal.

    Serves the submission and every subsequent read from one task, and records
    the methods it was called with — so a test can assert what the platform
    did, not only what it concluded.
    """

    def __init__(self, task_id: str = "partner-task-1", *, context_id: str = "ctx-1"):
        self.task_id = task_id
        self.context_id = context_id
        self.state = "working"
        self.result_text: str | None = None
        self.calls: list[str] = []

    @property
    def operations(self) -> list[str]:
        """What was called, as operations rather than method names.

        The vocabulary a caller uses is chosen by the agreed version, so an
        assertion written against one version's method names passes
        vacuously under the other instead of failing.
        """
        from app.a2a.protocol import resolve_method

        resolved = (resolve_method(method) for method in self.calls)
        return [operation for operation, _ in filter(None, resolved)]

    def finish(self, text: str = "the answer") -> None:
        self.state, self.result_text = "completed", text

    def _task(self) -> dict[str, Any]:
        task: dict[str, Any] = {
            "id": self.task_id,
            "contextId": self.context_id,
            "status": {"state": self.state},
        }
        if self.result_text is not None:
            task["artifacts"] = [
                {
                    "name": "result",
                    "parts": [{"kind": "text", "text": self.result_text}],
                }
            ]
        return task

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        try:
            body = _json.loads(request.content or b"{}")
        except ValueError:
            body = {}
        self.calls.append(str(body.get("method") or request.url.path))
        return rpc(self._task())

    def client(self, **kw) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), **kw)


@dataclass
class StubPartner:
    """A configurable A2A partner's card surface.

    Every knob here maps to a case some requirement names, so a test reads as
    "the partner does X" rather than as HTTP plumbing.
    """

    card: dict[str, Any] | None = field(default_factory=lambda: dict(GOOD_CARD))
    #: 200 serves the card; any other status code serves that error instead.
    card_status: int = 200
    #: Serve a body that is not JSON at all — the ``card_malformed`` case.
    card_malformed: bool = False

    #: Served at ``/.well-known/jwks.json`` when set, so a signed card's
    #: ``jku`` resolves to real keys over the same mock transport.
    jwks: dict[str, Any] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.jwks is not None and request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json=self.jwks)
        if self.card_status != 200:
            return httpx.Response(self.card_status)
        if self.card_malformed:
            return httpx.Response(200, content=b"<html>not a card</html>")
        if self.card is None:
            return httpx.Response(404)
        return httpx.Response(200, json=self.card)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self, **kw) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport(), **kw)



def signed_card(card: dict[str, Any] | None = None, *, jku: str | None = None):
    """A card carrying a real detached JWS, and the JWKS that verifies it.

    Real crypto rather than a stub: the point of the signature path is that it
    agrees with a partner's signer byte for byte over an RFC 8785
    canonicalisation, which a fake verifier cannot demonstrate.

    Returns ``(card, jwks)``. ``jku`` defaults to the partner's own origin;
    pass another to exercise the off-origin refusal.
    """
    import copy
    import json

    from a2a.client.card_resolver import parse_agent_card
    from a2a.utils.signing import _canonicalize_agent_card
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric import utils as asym
    from jwt.algorithms import ECAlgorithm
    from jwt.utils import base64url_encode

    body = dict(card or GOOD_CARD)
    key = ec.generate_private_key(ec.SECP256R1())
    public = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    public.update({"kid": "k1", "use": "sig", "alg": "ES256"})

    header = {
        "alg": "ES256",
        "kid": "k1",
        "jku": jku or "https://partner.example/.well-known/jwks.json",
    }
    protected = base64url_encode(json.dumps(header).encode()).decode()
    # A copy: the parser mutates its argument, and ``body`` is what gets served.
    payload = base64url_encode(
        _canonicalize_agent_card(parse_agent_card(copy.deepcopy(body))).encode()
    ).decode()
    der = key.sign(f"{protected}.{payload}".encode(), ec.ECDSA(hashes.SHA256()))
    r, sig_s = asym.decode_dss_signature(der)
    signature = base64url_encode(
        r.to_bytes(32, "big") + sig_s.to_bytes(32, "big")
    ).decode()

    body["signatures"] = [{"protected": protected, "signature": signature}]
    return body, {"keys": [public]}

# --- named partners, one per case a requirement calls out -------------------

def legacy_only_partner() -> StubPartner:
    """Serves only the preceding protocol version."""
    return StubPartner(card=card_with(protocolVersions=["0.3"]))


def no_common_version_partner() -> StubPartner:
    return StubPartner(card=card_with(protocolVersions=["9.9"]))


def off_origin_partner() -> StubPartner:
    """Card declares an endpoint on another origin — refused (FR-034)."""
    return StubPartner(card=card_with(url="https://elsewhere.example/rpc"))


# --- delegation-path doubles -------------------------------------------------

def settings_row(**overrides: Any) -> SimpleNamespace:
    """One outbound-target row, shaped as ``delegate()``/``preflight()`` read it."""
    base = dict(
        integration_id=uuid.uuid4(),
        direction=DIRECTION_OUTBOUND,
        usability=USABILITY_USABLE,
        unusable_reason=None,
        card_url="https://partner.example/.well-known/agent-card.json",
        invocation_url="https://partner.example/rpc",
        pinned_origin="https://partner.example",
        agreed_protocol_version="1.0",
        signature_state="unsigned",
        card_fingerprint="abc",
        accepted_card_fingerprint="abc",
        declared_input_types=["text/plain"],
        declared_output_types=["text/plain"],
        consecutive_transient_failures=0,
        circuit_open_until=None,
        last_failure_kind=None,
        last_verified_at=None,
        last_verification_error=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def integration_row(provider: str = "oauth_a2a", **overrides: Any) -> SimpleNamespace:
    """The integration row a delegation runs under."""
    base = dict(
        id=uuid.uuid4(),
        provider=provider,
        client_id_enc=None,
        client_secret_enc=None,
        extra_config_enc=None,
        token_url="https://partner.example/token",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def resolve_public(monkeypatch) -> None:
    """Stand in for DNS: every name resolves to one public address.

    The destination guard resolves before a credential is sent anywhere, and
    ``partner.example`` resolves to nothing.
    """
    from app.a2a.client import destinations

    async def _resolve(_host, _port):
        return [(None, None, None, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(destinations, "_resolve", _resolve)


def client_for(handler):
    """An ``httpx.AsyncClient`` subclass pinned to a mock transport.

    Dropping ``timeout``/``follow_redirects`` lets it stand in wherever the
    code under test constructs its own client with those kwargs.
    """

    class _Client(httpx.AsyncClient):
        def __init__(self, **kw):
            kw.pop("timeout", None)
            kw.pop("follow_redirects", None)
            super().__init__(transport=httpx.MockTransport(handler), **kw)

    return _Client


class PollClock:
    """The delegation's view of ``asyncio``, on a clock only its polls move.

    ``delegate`` reads its deadline from the event loop clock, and the tests
    that reach a hand-back set the ceiling to 50 ms. On the real clock, a
    loaded runner can spend those 50 ms before the first status read (minting
    headers, building the wire, the submission), and the attempt then ends at
    ``_within`` without reading the task at all: a live task came back as a
    timeout, or a finished one as ``still_running``, on CI that way. Here time
    passes only when the poll loop sleeps between reads, so the deadline is
    reached after a read, never instead of one, whatever the runner's speed.
    Anything else is the real module.

    Install it with ``monkeypatch.setattr(delegation, "asyncio", PollClock())``.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __getattr__(self, name: str):
        return getattr(asyncio, name)

    def get_running_loop(self) -> PollClock:
        return self

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)

    async def wait_for(self, awaitable, timeout: float):
        # The stub partner answers at once; the bound under test is the
        # deadline ``_within`` checks on this clock, not a real-time race.
        return await awaitable


def rpc(result: dict) -> httpx.Response:
    """A JSON-RPC success envelope carrying *result*."""
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": result})


class RecordingDb:
    """A session double that records adds and honours ``begin_nested``.

    ``_record`` writes inside a SAVEPOINT so a bookkeeping failure cannot
    poison the caller's transaction. Modelled here so the double takes the
    same path production does — without it the session silently records
    nothing and assertions on ``added`` pass for the wrong reason.
    ``fail=True`` makes every ``add`` raise, for the best-effort cases.
    """

    def __init__(
        self,
        fail: bool = False,
        owns_continuation: bool = True,
        outstanding: Any = None,
    ):
        self.added: list = []
        self._fail = fail
        #: What the FR-046 ownership lookup finds. ``True`` models the ordinary
        #: case — this run started the task it is continuing — so a test about
        #: continuation mechanics need not seed a delegation row to say so.
        self._owns_continuation = owns_continuation
        #: The unconcluded delegation row, if this exchange already has one.
        #: ``None`` is the ordinary case: nothing handed back yet.
        self._outstanding = outstanding

    async def execute(self, _stmt):
        owns, outstanding = self._owns_continuation, self._outstanding

        class _Scalars:
            def first(self):
                return outstanding

        class _Result:
            # The ownership lookup selects an id; the outstanding-row lookup
            # selects the entity. One double serves both because the two use
            # different accessors.
            def scalar_one_or_none(self):
                return uuid.uuid4() if owns else None

            def scalars(self):
                return _Scalars()

        return _Result()

    def add(self, obj):
        if self._fail:
            raise RuntimeError("bookkeeping is down")
        self.added.append(obj)

    def begin_nested(self):
        db = self

        class _Savepoint:
            async def __aenter__(self):
                self._mark = len(db.added)
                return self

            async def __aexit__(self, exc_type, exc, tb):
                if exc_type is not None:
                    del db.added[self._mark :]
                return False

        return _Savepoint()

    async def flush(self):
        return None
