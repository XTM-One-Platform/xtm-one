"""Tests for SEC-5: assignment webhooks unauthenticated without a secret.

Pins the three-part remediation contract:

  * ``POST /api/v1/assignments`` auto-generates a ``webhook_secret`` for new
    webhook-triggered assignments when the caller did not provide one, and the
    secret is returned to the owner in the API response (``trigger_config`` is
    not redacted) so the sending system can be configured.
  * ``_verify_assignment_webhook_secret`` behaviour for secretless
    assignments now depends on ``WEBHOOK_REQUIRE_SECRET``:
      - default (False): the delivery is allowed but a WARNING is logged,
        rate-limited to once per assignment per process;
      - strict (True): the delivery is rejected with 401.
  * The existing signed paths (OpenCTI token, GitHub, Slack, generic
    ``X-Signature`` HMAC) are untouched.
  * The static-token schemes a no-code SIEM/SOAR webhook action can actually
    emit — ``X-Webhook-Token`` (alias ``X-Webhook-Secret``) and
    ``Authorization: Bearer`` — authenticate against the same secret,
    ``Authorization`` being consulted last so a gateway-injected credential
    never shadows a valid signature.
  * The security audit reports webhook assignments without a secret as a
    warning finding (and a pass finding when every webhook assignment has
    one).
  * ``webhook_secret`` is treated as a credential everywhere it is
    serialised: stripped from export bundles / catalog downloads
    (``strip_webhook_secret``) and replaced with a fresh secret on
    clone / duplicate / import (``ensure_webhook_secret``).
  * Credential-bearing request headers (``X-Webhook-Token`` /
    ``X-OpenCTI-Token`` are the raw secret, as is a bearer
    ``Authorization``; HMAC signature headers usable for replay)
    are never persisted into a run's trigger payload
    (``_safe_webhook_headers``).
  * The agent-builder tool mirrors the REST contracts: ``get_assignment``
    redacts the secret for callers who cannot manage the assignment, and
    ``update_assignment`` runs trigger edits through
    ``ensure_webhook_secret``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from app.api import deps
from app.api.deps import require_agentic_ai
from app.api.v1 import assignments as assignments_api
from app.api.v1 import security_audit, webhooks
from app.core.database import get_db
from app.core.utils import ensure_webhook_secret, strip_webhook_secret


def _make_request(headers: dict | None = None) -> SimpleNamespace:
    """Minimal ``fastapi.Request`` stand-in (helpers only read ``.headers``)."""
    return SimpleNamespace(headers=headers or {})


@pytest.fixture(autouse=True)
def _reset_warned_set(monkeypatch):
    """Isolate the per-process warn rate-limit set between tests."""
    monkeypatch.setattr(webhooks, "_secretless_assignments_warned", set())


# ---------------------------------------------------------------------------
# ensure_webhook_secret (shared create/update/import/clone helper)
# ---------------------------------------------------------------------------


def test_ensure_webhook_secret_generates_for_webhook_without_secret():
    config = ensure_webhook_secret("webhook", {})
    assert config is not None
    secret = config.get("webhook_secret")
    assert isinstance(secret, str)
    # token_urlsafe(32) yields ~43 chars; pin a sane lower bound.
    assert len(secret) >= 40


def test_ensure_webhook_secret_handles_none_config():
    config = ensure_webhook_secret("webhook", None)
    assert config is not None
    assert config.get("webhook_secret")


def test_ensure_webhook_secret_preserves_existing_secret():
    config = ensure_webhook_secret(
        "webhook", {"webhook_secret": "user-chosen", "events": ["x"]}
    )
    assert config["webhook_secret"] == "user-chosen"
    assert config["events"] == ["x"]


def test_ensure_webhook_secret_passthrough_for_non_webhook_triggers():
    assert ensure_webhook_secret("manual", {}) == {}
    assert ensure_webhook_secret("schedule", None) is None
    cfg = {"cron": "* * * * *"}
    assert ensure_webhook_secret("schedule", cfg) is cfg


def test_ensure_webhook_secret_does_not_mutate_input():
    original: dict = {}
    config = ensure_webhook_secret("webhook", original)
    assert original == {}
    assert config is not original


def test_strip_webhook_secret_removes_key_entirely():
    stripped = strip_webhook_secret({"webhook_secret": "s3cr3t", "events": ["a"]})
    assert stripped == {"events": ["a"]}
    # Configs without a secret (and None) are returned untouched.
    cfg = {"cron": "x"}
    assert strip_webhook_secret(cfg) is cfg
    assert strip_webhook_secret(None) is None


def test_strip_webhook_secret_does_not_mutate_input():
    original = {"webhook_secret": "s3cr3t"}
    stripped = strip_webhook_secret(original)
    assert original == {"webhook_secret": "s3cr3t"}
    assert stripped == {}


def test_strip_then_ensure_yields_fresh_secret():
    """Clone/duplicate contract: the copy never reuses the source secret."""
    source = {"webhook_secret": "publisher-secret", "events": ["push"]}
    copied = ensure_webhook_secret("webhook", strip_webhook_secret(source))
    assert copied["events"] == ["push"]
    assert copied["webhook_secret"]
    assert copied["webhook_secret"] != "publisher-secret"


def test_redact_trigger_config_masks_secret_for_audit():
    redacted = assignments_api._redact_trigger_config(
        {"webhook_secret": "s3cr3t", "events": ["a"]}
    )
    assert redacted == {"webhook_secret": "***", "events": ["a"]}
    # Configs without a secret are returned untouched.
    assert assignments_api._redact_trigger_config({"cron": "x"}) == {"cron": "x"}
    assert assignments_api._redact_trigger_config(None) is None


# ---------------------------------------------------------------------------
# POST /api/v1/assignments — creation auto-generates the secret
# ---------------------------------------------------------------------------


class _CapturingDb:
    """Fake async session capturing added rows + mimicking flush defaults."""

    def __init__(self):
        self.added: list[object] = []

    def add(self, instance):
        self.added.append(instance)

    async def flush(self):
        now = datetime.now(UTC)
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            obj.created_at = now
            obj.updated_at = now
            if getattr(obj, "execution_count", None) is None:
                obj.execution_count = 0

    async def commit(self):
        pass


@pytest.fixture
def assignments_client(monkeypatch):
    fake_user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Admin")
    fake_db = _CapturingDb()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=fake_user.id,
        is_company_managed=False,
        name="Agent",
    )

    async def _groups(_db, _uid):
        return []

    async def _get_agent(_db, _uid, _agent_id, *_a, **_k):
        return agent

    async def _run_as(_db, _agent, _value):
        return None

    async def _audit(*_a, **_k):
        return None

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(assignments_api, "get_user_agent", _get_agent)
    monkeypatch.setattr(
        assignments_api, "assert_writable_agent", lambda *_a, **_k: None
    )
    monkeypatch.setattr(assignments_api, "_resolve_run_as_user_id", _run_as)
    monkeypatch.setattr(assignments_api, "log_audit", _audit)

    app = FastAPI()
    app.include_router(assignments_api.router, prefix="/api/v1/assignments")

    async def _get_db():
        yield fake_db

    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[require_agentic_ai] = lambda: None
    with TestClient(app) as client:
        yield client, fake_db, agent


def test_create_webhook_assignment_auto_generates_secret(assignments_client):
    client, fake_db, agent = assignments_client

    resp = client.post(
        "/api/v1/assignments",
        json={
            "agent_id": str(agent.id),
            "name": "Webhook assignment",
            "prompt": "do the thing",
            "trigger_type": "webhook",
            "trigger_config": {},
        },
    )

    assert resp.status_code == 201, resp.text
    # The secret must be persisted on the ORM row...
    stored = fake_db.added[0].trigger_config
    assert stored.get("webhook_secret")
    assert len(stored["webhook_secret"]) >= 40
    # ...and visible to the owner in the API response (not redacted) so the
    # external sender can be configured with it.
    assert resp.json()["trigger_config"]["webhook_secret"] == stored[
        "webhook_secret"
    ]


def test_create_webhook_assignment_keeps_caller_secret(assignments_client):
    client, fake_db, agent = assignments_client

    resp = client.post(
        "/api/v1/assignments",
        json={
            "agent_id": str(agent.id),
            "name": "Webhook assignment",
            "prompt": "do the thing",
            "trigger_type": "webhook",
            "trigger_config": {"webhook_secret": "caller-chosen"},
        },
    )

    assert resp.status_code == 201, resp.text
    assert fake_db.added[0].trigger_config["webhook_secret"] == "caller-chosen"


def test_create_manual_assignment_gets_no_secret(assignments_client):
    client, fake_db, agent = assignments_client

    resp = client.post(
        "/api/v1/assignments",
        json={
            "agent_id": str(agent.id),
            "name": "Manual assignment",
            "prompt": "do the thing",
            "trigger_type": "manual",
            "trigger_config": {},
        },
    )

    assert resp.status_code == 201, resp.text
    assert "webhook_secret" not in (fake_db.added[0].trigger_config or {})


# ---------------------------------------------------------------------------
# _verify_assignment_webhook_secret — secretless behaviour
# ---------------------------------------------------------------------------


def test_secretless_allowed_and_warns_in_default_mode(monkeypatch, caplog):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    assignment_id = str(uuid.uuid4())

    with caplog.at_level(logging.WARNING, logger="app.api.v1.webhooks"):
        webhooks._verify_assignment_webhook_secret(
            b'{"x":1}', _make_request(), None, assignment_id=assignment_id
        )

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and assignment_id in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "no webhook_secret" in warnings[0].getMessage()


def test_secretless_warning_rate_limited_per_assignment(monkeypatch, caplog):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    assignment_id = str(uuid.uuid4())

    with caplog.at_level(logging.WARNING, logger="app.api.v1.webhooks"):
        for _ in range(3):
            webhooks._verify_assignment_webhook_secret(
                b"{}", _make_request(), "", assignment_id=assignment_id
            )

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and assignment_id in r.getMessage()
    ]
    assert len(warnings) == 1

    # A different assignment still gets its own (single) warning.
    other_id = str(uuid.uuid4())
    with caplog.at_level(logging.WARNING, logger="app.api.v1.webhooks"):
        webhooks._verify_assignment_webhook_secret(
            b"{}", _make_request(), None, assignment_id=other_id
        )
    assert any(other_id in r.getMessage() for r in caplog.records)


def test_secretless_warning_without_id_never_poisons_the_cache(
    monkeypatch, caplog
):
    """An id-less call must not suppress warnings for real assignments."""
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)

    with caplog.at_level(logging.WARNING, logger="app.api.v1.webhooks"):
        # Call without an assignment_id (defaults to "").
        webhooks._verify_assignment_webhook_secret(b"{}", _make_request(), None)

    # The empty-string key is never cached...
    assert "" not in webhooks._secretless_assignments_warned
    assert any("<unknown>" in r.getMessage() for r in caplog.records)

    # ...so a real assignment afterwards still gets its warning.
    real_id = str(uuid.uuid4())
    with caplog.at_level(logging.WARNING, logger="app.api.v1.webhooks"):
        webhooks._verify_assignment_webhook_secret(
            b"{}", _make_request(), None, assignment_id=real_id
        )
    assert any(real_id in r.getMessage() for r in caplog.records)


def test_secretless_rejected_401_when_require_secret(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", True)

    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(
            b'{"x":1}',
            _make_request(),
            None,
            assignment_id=str(uuid.uuid4()),
        )
    assert ei.value.status_code == 401
    assert "WEBHOOK_REQUIRE_SECRET" in ei.value.detail


def test_require_secret_does_not_affect_signed_requests(monkeypatch):
    """Strict mode only gates secretless assignments — valid signatures pass."""
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", True)
    body = b"payload"
    secret = "abc"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = _make_request({"X-Signature": sig})
    webhooks._verify_assignment_webhook_secret(
        body, request, secret, assignment_id=str(uuid.uuid4())
    )


# ---------------------------------------------------------------------------
# Signed paths untouched (regression: HMAC verification still enforced)
# ---------------------------------------------------------------------------


def test_valid_github_signature_still_passes(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    body = b'{"action":"opened"}'
    secret = "s3cr3t"
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = _make_request({"X-Hub-Signature-256": sig})
    webhooks._verify_assignment_webhook_secret(body, request, secret)


def test_invalid_github_signature_still_raises_401(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    request = _make_request({"X-Hub-Signature-256": "sha256=deadbeef"})
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(b'{"x":1}', request, "s3cr3t")
    assert ei.value.status_code == 401


def test_unsigned_delivery_with_secret_still_raises_401(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(
            b"{}", _make_request({}), "s3cr3t"
        )
    assert ei.value.status_code == 401


def test_unsigned_rejection_names_the_static_token_scheme(monkeypatch):
    """The 401 must tell a SIEM/SOAR operator what header to send.

    Discoverability is the whole point of the static-token scheme: an
    integrator who only sees "Missing webhook signature" concludes the
    endpoint needs an HMAC they cannot compute, and falls back to polling.
    """
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(
            b"{}", _make_request({}), "s3cr3t"
        )
    assert "X-Webhook-Token" in ei.value.detail
    assert "X-Webhook-Secret" in ei.value.detail
    assert "Authorization: Bearer" in ei.value.detail


# ---------------------------------------------------------------------------
# Static-token schemes — what a no-code SIEM/SOAR webhook action can send
# ---------------------------------------------------------------------------


def test_webhook_token_header_accepts_the_raw_secret(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    webhooks._verify_assignment_webhook_secret(
        b'{"alert":"new"}',
        _make_request({"X-Webhook-Token": "s3cr3t"}),
        "s3cr3t",
    )


def test_webhook_token_header_rejects_a_wrong_secret(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(
            b"{}", _make_request({"X-Webhook-Token": "nope"}), "s3cr3t"
        )
    assert ei.value.status_code == 401


def test_webhook_secret_header_accepts_the_raw_secret(monkeypatch):
    """The Trigger card labels the value "Webhook secret" — senders spell it that way.

    A customer wiring a Google SecOps SOAR playbook sent ``X-Webhook-Secret``
    and read the resulting "Missing webhook signature" as a wrong secret
    rather than a wrong header name.
    """
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    webhooks._verify_assignment_webhook_secret(
        b'{"alert":"new"}',
        _make_request({"X-Webhook-Secret": "s3cr3t"}),
        "s3cr3t",
    )


def test_webhook_secret_header_rejects_a_wrong_secret(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(
            b"{}", _make_request({"X-Webhook-Secret": "nope"}), "s3cr3t"
        )
    assert ei.value.status_code == 401


def test_canonical_token_header_wins_over_the_alias(monkeypatch):
    """The alias is one scheme under two names, not a second chance.

    A sender supplying both must authenticate on the canonical spelling, so a
    stray ``X-Webhook-Secret`` (injected by a gateway, left over from an older
    playbook step) can neither authenticate on its own nor invalidate a valid
    ``X-Webhook-Token``.
    """
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    request = SimpleNamespace(
        headers=Headers(
            {"X-Webhook-Token": "s3cr3t", "X-Webhook-Secret": "stale-value"}
        )
    )
    webhooks._verify_assignment_webhook_secret(b"{}", request, "s3cr3t")


def test_empty_canonical_header_does_not_shadow_the_alias(monkeypatch):
    """An empty ``X-Webhook-Token`` proves nothing, so it blocks nothing.

    Senders emit empty headers on their own: a SOAR template with an unset
    field, a gateway that strips a value it does not recognise. Resolving the
    pair on presence rather than on a value would 401 a delivery that carries
    the right secret under the alias — the exact failure this alias exists to
    remove.
    """
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    request = SimpleNamespace(
        headers=Headers({"X-Webhook-Token": "", "X-Webhook-Secret": "s3cr3t"})
    )
    webhooks._verify_assignment_webhook_secret(b"{}", request, "s3cr3t")


def test_opencti_token_header_still_accepted_for_compat(monkeypatch):
    """The original spelling keeps working — existing senders must not break."""
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    webhooks._verify_assignment_webhook_secret(
        b"{}", _make_request({"X-OpenCTI-Token": "s3cr3t"}), "s3cr3t"
    )


def test_bearer_authorization_accepts_the_raw_secret(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    webhooks._verify_assignment_webhook_secret(
        b'{"alert":"new"}',
        _make_request({"Authorization": "Bearer s3cr3t"}),
        "s3cr3t",
    )


def test_bearer_authorization_rejects_a_wrong_secret(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(
            b"{}", _make_request({"Authorization": "Bearer nope"}), "s3cr3t"
        )
    assert ei.value.status_code == 401


def test_non_bearer_authorization_falls_through_to_unsigned(monkeypatch):
    """A Basic/Digest credential is not the webhook secret.

    It must reach the "unsigned delivery" rejection (which names the schemes
    to use) rather than a misleading "invalid bearer token".
    """
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(
            b"{}", _make_request({"Authorization": "Basic dXNlcjpwdw=="}), "s3cr3t"
        )
    assert ei.value.status_code == 401
    assert "X-Webhook-Token" in ei.value.detail


def test_signature_wins_over_an_unrelated_bearer_token(monkeypatch):
    """Regression: Authorization is consulted last, on purpose.

    An API gateway in front of the platform may inject its own
    ``Authorization`` header. Before the bearer scheme existed such a request
    authenticated on ``X-Signature``; it must keep doing so instead of being
    rejected because the gateway's credential is not the webhook secret.
    """
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    body = b'{"alert":"new"}'
    secret = "s3cr3t"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = _make_request(
        {"X-Signature": sig, "Authorization": "Bearer gateway-credential"}
    )
    webhooks._verify_assignment_webhook_secret(body, request, secret)


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_bearer_scheme_name_is_case_insensitive(monkeypatch, scheme):
    """RFC 7235 §2.1 — senders spell the scheme however they like."""
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    webhooks._verify_assignment_webhook_secret(
        b"{}", _make_request({"Authorization": f"{scheme} s3cr3t"}), "s3cr3t"
    )


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("x-webhook-token", "s3cr3t"),
        ("X-WEBHOOK-TOKEN", "s3cr3t"),
        ("x-webhook-secret", "s3cr3t"),
        ("X-WEBHOOK-SECRET", "s3cr3t"),
        ("authorization", "Bearer s3cr3t"),
    ],
)
def test_static_token_header_names_are_case_insensitive(
    monkeypatch, header, value
):
    """Uses a real ``Headers`` mapping, not the plain-dict stub.

    HTTP header names are case-insensitive and senders vary; the plain dict
    in ``_make_request`` would let a case-sensitive lookup pass unnoticed.
    """
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    request = SimpleNamespace(headers=Headers({header: value}))
    webhooks._verify_assignment_webhook_secret(b"{}", request, "s3cr3t")


def test_static_token_does_not_bypass_a_missing_secret(monkeypatch):
    """Strict mode still rejects a secretless assignment, token or not."""
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", True)
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(
            b"{}",
            _make_request({"X-Webhook-Token": "anything"}),
            None,
            assignment_id=str(uuid.uuid4()),
        )
    assert ei.value.status_code == 401
    assert "WEBHOOK_REQUIRE_SECRET" in ei.value.detail


# ---------------------------------------------------------------------------
# Non-ASCII header values → 401, never an unhandled 500
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("X-Webhook-Token", "caf\xe9"),
        ("X-Webhook-Secret", "caf\xe9"),
        ("X-OpenCTI-Token", "caf\xe9"),
        ("Authorization", "Bearer caf\xe9"),
        ("X-Signature", "caf\xe9"),
    ],
)
def test_non_ascii_header_value_is_a_clean_401(monkeypatch, header, value):
    """``hmac.compare_digest`` raises TypeError on non-ASCII ``str``.

    Starlette decodes headers as latin-1, so a sender can put any byte
    >= 0x80 in the header and get a non-ASCII string here. Comparing it with
    ``compare_digest`` would surface as an unauthenticated 500 rather than a
    401 — hence ``timing_safe_str_eq``, which keeps the constant-time
    property and turns crafted input into a plain mismatch.
    """
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    request = SimpleNamespace(headers=Headers({header: value}))
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(b"{}", request, "s3cr3t")
    assert ei.value.status_code == 401


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("x-hub-signature-256", b"sha256=caf\xc3\xa9"),
        ("x-slack-signature", b"v0=caf\xc3\xa9"),
    ],
)
def test_non_ascii_signature_after_a_valid_prefix_is_a_clean_401(
    monkeypatch, header, value
):
    """The two signature branches reach ``core/security.py``'s verifiers.

    Their ``startswith`` prefix guard lets a non-ASCII tail through to the
    comparison. Raw UTF-8 bytes, as a real sender puts them on the wire, and a
    current Slack timestamp: a stale one is refused before the comparison runs.
    """
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    raw = [(header.encode(), value)]
    if header == "x-slack-signature":
        raw.append((b"x-slack-request-timestamp", str(int(time.time())).encode()))
    request = SimpleNamespace(headers=Headers(raw=raw))
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(b"{}", request, "s3cr3t")
    assert ei.value.status_code == 401


def test_non_ascii_secret_still_authenticates(monkeypatch):
    """A caller-supplied secret is not charset-validated on the way in.

    ``ensure_webhook_secret`` only fills a *missing* secret, so
    ``trigger_config.webhook_secret`` can hold any string. A matching
    delivery must still authenticate rather than 500 on the comparison.
    """
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    secret = "sécret-café"
    request = SimpleNamespace(headers=Headers({"X-Webhook-Token": secret}))
    webhooks._verify_assignment_webhook_secret(b"{}", request, secret)


def test_bearer_credentials_parsing():
    assert webhooks._bearer_credentials("Bearer abc") == "abc"
    assert webhooks._bearer_credentials("bearer  abc ") == "abc"
    assert webhooks._bearer_credentials("Basic abc") == ""
    assert webhooks._bearer_credentials("abc") == ""
    assert webhooks._bearer_credentials("") == ""


# ---------------------------------------------------------------------------
# Security audit — secretless webhook assignments finding
# ---------------------------------------------------------------------------


class _AuditDb:
    """Fake session returning canned aggregate counts and capturing the queries.

    ``_check_assignments`` computes both counts in a single aggregate SQL
    statement (COUNT + conditional SUM over a JSON key check) so the fake
    returns one ``(webhook_assignments, secretless)`` row.
    """

    def __init__(self, webhook_assignments: int, secretless: int):
        self._row = (webhook_assignments, secretless)
        self.queries: list = []

    async def execute(self, query):
        self.queries.append(query)
        row = self._row

        class _Result:
            def one(self):
                return row

            def all(self):
                # The rows holding a surrogate escape SQL cannot parse: none here.
                return []

        return _Result()


@pytest.mark.asyncio
async def test_audit_warns_on_secretless_webhook_assignments():
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_assignments(_AuditDb(3, 2), findings)

    warning = next(
        f
        for f in findings
        if f.severity == "warning" and "without a webhook secret" in f.title
    )
    assert warning.title.startswith("2 ")
    assert "WEBHOOK_REQUIRE_SECRET" in (warning.recommendation or "")
    # The generic exposure info finding is still present alongside it.
    assert any(f.severity == "info" for f in findings)


@pytest.mark.asyncio
async def test_audit_passes_when_all_webhook_assignments_have_secrets():
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_assignments(_AuditDb(2, 0), findings)

    assert not [f for f in findings if f.severity == "warning"]
    assert any(
        f.severity == "pass" and "webhook secret" in f.title.lower()
        for f in findings
    )


@pytest.mark.asyncio
async def test_audit_silent_when_no_webhook_assignments():
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_assignments(_AuditDb(0, 0), findings)
    assert findings == []


@pytest.mark.asyncio
async def test_audit_counts_are_aggregated_in_sql():
    """The DB does the counting — the only per-row fetch of trigger_config is of the
    rows SQL cannot parse."""
    from sqlalchemy.dialects import postgresql

    db = _AuditDb(0, 0)
    await security_audit._check_assignments(db, [])

    compiled = [
        str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})).lower()
        for query in db.queries
    ]
    [sql] = [statement for statement in compiled if "count(" in statement]
    assert "webhook_secret" in sql
    # Aggregate-only select: the raw JSON config column itself is never
    # returned as a result column.
    select_clause = sql.split(" from ")[0]
    assert "assignments.trigger_config," not in select_clause
    assert all("[dd][89a-fa-f]" in statement for statement in compiled if statement is not sql)


# ---------------------------------------------------------------------------
# API response redaction — webhook_secret hidden from non-managers
# ---------------------------------------------------------------------------


def _make_viewer(*, is_admin: bool = False, user_id=None):
    """User stand-in with the attributes ``webhook_secret_visible`` reads."""
    return SimpleNamespace(
        id=user_id or uuid.uuid4(),
        is_admin=is_admin,
        role="admin" if is_admin else "member",
    )


def _make_shared_agent(owner_id, group_id: str, permission: str):
    """Agent stand-in shared with *group_id* at *permission* tier."""
    return SimpleNamespace(
        is_company_managed=False,
        user_id=owner_id,
        shared_group_ids=[group_id],
        shared_group_permissions={group_id: permission},
    )


def test_visible_trigger_config_redacts_for_non_admin_on_company_managed():
    assignment = SimpleNamespace(
        is_company_managed=True,
        user_id=uuid.uuid4(),
        trigger_config={"webhook_secret": "s3cr3t", "events": ["push"]},
    )
    non_admin = _make_viewer()
    visible = assignments_api._visible_trigger_config(assignment, non_admin)
    assert visible == {"events": ["push"]}
    # The stored config is never mutated.
    assert assignment.trigger_config["webhook_secret"] == "s3cr3t"


def test_visible_trigger_config_keeps_secret_for_admin_and_owner():
    config = {"webhook_secret": "s3cr3t"}
    company = SimpleNamespace(
        is_company_managed=True, user_id=uuid.uuid4(), trigger_config=config
    )
    admin = _make_viewer(is_admin=True)
    assert assignments_api._visible_trigger_config(company, admin) is config

    owner = _make_viewer()
    personal = SimpleNamespace(
        is_company_managed=False, user_id=owner.id, trigger_config=config
    )
    assert assignments_api._visible_trigger_config(personal, owner) is config


def test_visible_trigger_config_redacts_for_read_only_group_viewer():
    """A ``read_only`` group member can VIEW a shared assignment but must not
    receive its webhook credential (mirrors ``can_manage``)."""
    owner_id = uuid.uuid4()
    config = {"webhook_secret": "s3cr3t", "events": ["push"]}
    assignment = SimpleNamespace(
        is_company_managed=False, user_id=owner_id, trigger_config=config
    )
    viewer = _make_viewer()
    agent = _make_shared_agent(owner_id, "g1", "read_only")
    visible = assignments_api._visible_trigger_config(
        assignment, viewer, agent, ["g1"]
    )
    assert visible == {"events": ["push"]}


def test_visible_trigger_config_keeps_secret_for_config_tier_group_member():
    """``config``-tier group members manage the assignment, so they keep the
    full config (they configure the sending system)."""
    owner_id = uuid.uuid4()
    config = {"webhook_secret": "s3cr3t"}
    assignment = SimpleNamespace(
        is_company_managed=False, user_id=owner_id, trigger_config=config
    )
    member = _make_viewer()
    agent = _make_shared_agent(owner_id, "g1", "config")
    assert (
        assignments_api._visible_trigger_config(assignment, member, agent, ["g1"])
        is config
    )


def test_visible_trigger_config_fails_closed_without_agent_row():
    """When the parent agent row is unavailable the group tier cannot be
    evaluated — non-owners must not get the secret."""
    config = {"webhook_secret": "s3cr3t"}
    assignment = SimpleNamespace(
        is_company_managed=False, user_id=uuid.uuid4(), trigger_config=config
    )
    viewer = _make_viewer()
    visible = assignments_api._visible_trigger_config(
        assignment, viewer, None, ["g1"]
    )
    assert "webhook_secret" not in (visible or {})


# ---------------------------------------------------------------------------
# Run payload header redaction — credentials never persisted on runs
# ---------------------------------------------------------------------------


def test_every_static_token_header_is_scrubbed_from_stored_payloads():
    """The accept path and the scrubbing denylist share one definition.

    Two hand-maintained lists would drift: an alias added to the accepted set
    and forgotten here persists the raw secret into every run's stored trigger
    payload, readable by anyone who can read the run and replayable against the
    live endpoint once the dedup TTL expires.
    """
    for header in (*webhooks._STATIC_TOKEN_HEADERS, webhooks._OPENCTI_HEADER):
        assert header.lower() in webhooks._SENSITIVE_WEBHOOK_HEADERS


def test_safe_webhook_headers_strips_credential_and_signature_headers():
    """``_webhook_meta.headers`` is stored on the run and surfaced through
    the run APIs to viewers who may NOT see the webhook secret — credential
    and replayable signature headers must never be persisted."""
    request = _make_request(
        {
            "content-type": "application/json",
            "x-github-event": "issues",
            "user-agent": "GitHub-Hookshot/1",
            "X-Webhook-Token": "raw-webhook-secret",
            "X-Webhook-Secret": "raw-webhook-secret",
            "X-OpenCTI-Token": "raw-webhook-secret",
            "authorization": "Bearer abc",
            "proxy-authorization": "Basic xyz",
            "cookie": "session=1",
            "x-api-key": "k3y",
            "x-hub-signature": "sha1=deadbeef",
            "x-hub-signature-256": "sha256=deadbeef",
            "x-slack-signature": "v0=deadbeef",
            "x-signature": "deadbeef",
            # Provider signatures verified by this module that are body-bound
            # HMACs without a timestamp component - replayable indefinitely
            # if ever persisted (LinkedIn, Perk, HubSpot v1/v2) - plus the
            # timestamp-bound HubSpot v3 for consistency.
            "X-LI-Signature": "hmacsha256=deadbeef",
            "Tk-Signature": "deadbeef",
            "X-HubSpot-Signature": "deadbeef",
            "X-HubSpot-Signature-v2": "deadbeef",
            "X-HubSpot-Signature-v3": "ZGVhZGJlZWY=",
            # Header names are case-insensitive and HTTP/1.1 servers surface
            # them as sent — mixed-case proxy headers must be dropped too.
            "X-Forwarded-For": "1.2.3.4",
            "X-Forwarded-Proto": "https",
            "x-forwarded-host": "internal.example",
        }
    )
    safe = webhooks._safe_webhook_headers(request)
    assert safe == {
        "content-type": "application/json",
        "x-github-event": "issues",
        "user-agent": "GitHub-Hookshot/1",
    }


# ---------------------------------------------------------------------------
# Agent-builder tool parity — redaction + ensure on trigger edits
# ---------------------------------------------------------------------------


class _BuilderSeqDb:
    """Fake async session yielding canned ``scalar_one_or_none`` results."""

    def __init__(self, results):
        self._results = list(results)
        self.committed = False

    async def execute(self, _query):
        value = self._results.pop(0)

        class _Result:
            def scalar_one_or_none(self):
                return value

            def all(self):
                return value

        return _Result()

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _builder_handler(name: str, user_id: str):
    from app.services.builtin_tools import build_agent_builder_tool

    _tools, executor = build_agent_builder_tool(None, user_id)
    return executor._handlers[name]


def _make_builder_assignment(user_id, **overrides):
    base = dict(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=user_id,
        is_company_managed=False,
        name="Webhook assignment",
        description=None,
        trigger_type="webhook",
        trigger_config={"webhook_secret": "s3cr3t", "events": ["push"]},
        delay=None,
        enabled=True,
        human_in_the_loop=False,
        ai_label=False,
        activation_prompt=None,
        prompt="do the thing",
        ai_label_prompt=None,
        human_in_the_loop_prompt=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_builder_agent(assignment, **overrides):
    """Parent-agent stand-in with the attributes ``can_manage`` reads."""
    base = dict(
        id=assignment.agent_id,
        name="Agent",
        user_id=assignment.user_id,
        is_company_managed=assignment.is_company_managed,
        shared_group_ids=[],
        shared_group_permissions={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def _builder_env(monkeypatch):
    """Wire the agent-builder closures to fakes; returns a setup callable."""
    from app.api.v1 import helpers as helpers_mod
    from app.core import database as database_mod
    from app.services import builtin_tools

    def _setup(db, caller, group_ids: list[str] | None = None):
        async def _groups(_db, _uid):
            return list(group_ids or [])

        async def _load_user(_db, _uid):
            return caller

        monkeypatch.setattr(helpers_mod, "get_user_group_ids", _groups)
        monkeypatch.setattr(database_mod, "async_session", lambda: db)
        monkeypatch.setattr(
            builtin_tools, "_load_caller_user_for_builder", _load_user
        )
        return builtin_tools

    return _setup


@pytest.mark.asyncio
async def test_builder_get_assignment_redacts_secret_for_non_manager(
    _builder_env,
):
    """A non-admin viewer of a company-managed assignment must not see the
    secret through the builder read tool (REST `_visible_trigger_config`
    parity)."""
    caller_id = uuid.uuid4()
    assignment = _make_builder_assignment(
        user_id=uuid.uuid4(), is_company_managed=True
    )
    agent = _make_builder_agent(assignment)
    db = _BuilderSeqDb([assignment, agent, None])
    _builder_env(db, _make_viewer(user_id=caller_id))

    handler = _builder_handler("get_assignment", str(caller_id))
    result = await handler(str(assignment.id))

    assert "s3cr3t" not in result
    assert "webhook_secret" not in result
    # The rest of the config stays visible and the row is never mutated.
    assert "push" in result
    assert assignment.trigger_config["webhook_secret"] == "s3cr3t"


@pytest.mark.asyncio
async def test_builder_get_assignment_keeps_secret_for_owner_and_admin(
    _builder_env,
):
    caller_id = uuid.uuid4()
    owned = _make_builder_assignment(user_id=caller_id)
    agent = _make_builder_agent(owned)
    _builder_env(_BuilderSeqDb([owned, agent, None]), _make_viewer(user_id=caller_id))
    handler = _builder_handler("get_assignment", str(caller_id))
    assert "s3cr3t" in await handler(str(owned.id))

    company = _make_builder_assignment(
        user_id=uuid.uuid4(), is_company_managed=True
    )
    agent2 = _make_builder_agent(company)
    _builder_env(_BuilderSeqDb([company, agent2, None]), _make_viewer(is_admin=True))
    handler = _builder_handler("get_assignment", str(uuid.uuid4()))
    assert "s3cr3t" in await handler(str(company.id))


@pytest.mark.asyncio
async def test_builder_get_assignment_redacts_for_read_only_group_member(
    _builder_env,
):
    """A ``read_only`` group member can view the assignment through the
    builder tool but must not receive the webhook credential (same
    ``can_manage`` tier contract as the REST API)."""
    caller_id = uuid.uuid4()
    assignment = _make_builder_assignment(user_id=uuid.uuid4())
    agent = _make_builder_agent(
        assignment,
        shared_group_ids=["g1"],
        shared_group_permissions={"g1": "read_only"},
    )
    # The last result is the owner-deactivation rule's: not paused.
    db = _BuilderSeqDb([assignment, agent, [], None])
    _builder_env(db, _make_viewer(user_id=caller_id), group_ids=["g1"])

    handler = _builder_handler("get_assignment", str(caller_id))
    result = await handler(str(assignment.id))

    assert "s3cr3t" not in result
    assert "webhook_secret" not in result
    assert "push" in result


@pytest.mark.asyncio
async def test_builder_get_assignment_keeps_secret_for_config_group_member(
    _builder_env,
):
    """``config``-tier group members manage the assignment and keep the
    full trigger config including the secret."""
    caller_id = uuid.uuid4()
    assignment = _make_builder_assignment(user_id=uuid.uuid4())
    agent = _make_builder_agent(
        assignment,
        shared_group_ids=["g1"],
        shared_group_permissions={"g1": "config"},
    )
    # The last result is the owner-deactivation rule's: not paused.
    db = _BuilderSeqDb([assignment, agent, [], None])
    _builder_env(db, _make_viewer(user_id=caller_id), group_ids=["g1"])

    handler = _builder_handler("get_assignment", str(caller_id))
    assert "s3cr3t" in await handler(str(assignment.id))


@pytest.mark.asyncio
async def test_builder_update_assignment_ensures_secret_on_trigger_edit(
    monkeypatch, _builder_env,
):
    """A builder trigger edit that leaves a webhook assignment secretless
    gets a server-generated secret (REST `update_assignment` parity)."""
    from app.services import builtin_tools

    caller_id = uuid.uuid4()
    assignment = _make_builder_assignment(
        user_id=caller_id, trigger_type="manual", trigger_config={}
    )
    agent = _make_builder_agent(assignment)
    db = _BuilderSeqDb([])
    _builder_env(db, _make_viewer(user_id=caller_id))

    async def _load_writable(_db, _uid, _assignment_uuid, *, action="modify", written=frozenset()):
        return assignment, agent, None

    monkeypatch.setattr(
        builtin_tools,
        "_load_writable_assignment_for_builder",
        _load_writable,
    )

    handler = _builder_handler("update_assignment", str(caller_id))
    result = await handler(str(assignment.id), trigger_type="webhook")

    assert "updated" in result
    assert db.committed
    secret = (assignment.trigger_config or {}).get("webhook_secret")
    assert isinstance(secret, str) and len(secret) >= 40


@pytest.mark.asyncio
async def test_builder_update_without_trigger_edit_never_flips_legacy(
    monkeypatch, _builder_env,
):
    """An unrelated builder update (rename) must not silently add a secret
    to a legacy secretless webhook assignment."""
    from app.services import builtin_tools

    caller_id = uuid.uuid4()
    assignment = _make_builder_assignment(user_id=caller_id, trigger_config={})
    agent = _make_builder_agent(assignment)
    db = _BuilderSeqDb([])
    _builder_env(db, _make_viewer(user_id=caller_id))

    async def _load_writable(_db, _uid, _assignment_uuid, *, action="modify", written=frozenset()):
        return assignment, agent, None

    monkeypatch.setattr(
        builtin_tools,
        "_load_writable_assignment_for_builder",
        _load_writable,
    )

    handler = _builder_handler("update_assignment", str(caller_id))
    result = await handler(str(assignment.id), name="Renamed")

    assert "updated" in result
    assert "webhook_secret" not in (assignment.trigger_config or {})
