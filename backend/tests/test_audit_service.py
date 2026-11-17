"""Behavioral tests for the audit logging service (``app.services.audit``).

Covers:

* ``log_audit`` — writes an ``AuditLog`` row with the expected fields
  (user id/email, action, entity coordinates, summary, details, IP).
* ``_get_client_ip`` — ``x-forwarded-for`` is read only when
  ``trust_proxy_headers`` is set (then its first hop wins); otherwise the
  peer address, which uvicorn corrects under ``FORWARDED_ALLOW_IPS``;
  graceful fallbacks when the header / client / request are missing, and
  never a value that is not an IP address (the parse itself is pinned
  in ``test_request_client_ip.py``).
* ``log_security_event`` — resolves the user internally, skips anonymous /
  non-UUID identifiers, and skips unknown user ids without writing a row.
* Every call in ``app/`` to ``log_audit``, ``log_system_audit``,
  ``audit_entry`` and ``log_security_event`` matches its signature: route tests
  stub them out, so a caller left behind by a signature change only
  fails in production.

Assessment finding: OBS-8 (critical untested modules).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.services.audit import (
    _get_client_ip,
    audit_entry,
    log_audit,
    log_security_event,
    log_system_audit,
)
from tests import app_sources

USER_UUID = uuid.UUID("00000000-0000-0000-0000-00000000eeee")


@pytest.fixture(autouse=True)
def _untrusted_proxy_headers(monkeypatch):
    """The shipped default: forwarding headers are caller-controlled."""
    monkeypatch.setattr(settings, "trust_proxy_headers", False)


def _user(
    user_id: uuid.UUID = USER_UUID, email: str = "auditor@example.com"
) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, email=email)


def _request(
    headers: dict[str, str] | None = None, client_host: str | None = "10.0.0.5"
) -> SimpleNamespace:
    """Request stub exposing only what ``_get_client_ip`` reads."""
    client = SimpleNamespace(host=client_host) if client_host else None
    return SimpleNamespace(headers=headers or {}, client=client)


class FakeDB:
    """AsyncSession stand-in recording added rows; user lookup configurable."""

    def __init__(self, user: SimpleNamespace | None = None) -> None:
        self.added: list[AuditLog] = []
        self.flushed = 0
        self.executed: list = []
        self._user = user

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def execute(self, stmt):
        self.executed.append(stmt)
        return SimpleNamespace(scalar_one_or_none=lambda: self._user)


# ---------------------------------------------------------------------------
# log_audit — field mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_audit_writes_expected_fields(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    db = FakeDB()
    entity_id = uuid.uuid4()
    details = {"old": {"enabled": False}, "new": {"enabled": True}}

    entry = await log_audit(
        db,  # type: ignore[arg-type]
        user=_user(),  # type: ignore[arg-type]
        action="update",
        entity_type="agent",
        entity_id=entity_id,
        entity_name="Mail Desk",
        summary="Enabled agent 'Mail Desk'",
        details=details,
        request=_request(headers={"x-forwarded-for": "203.0.113.7"}),
    )

    assert db.added == [entry]
    assert db.flushed == 1
    assert isinstance(entry, AuditLog)
    assert entry.user_id == USER_UUID
    assert entry.user_email == "auditor@example.com"
    assert entry.action == "update"
    assert entry.entity_type == "agent"
    assert entry.entity_id == entity_id
    assert entry.entity_name == "Mail Desk"
    assert entry.summary == "Enabled agent 'Mail Desk'"
    assert entry.details == details
    assert entry.ip_address == "203.0.113.7"


@pytest.mark.asyncio
async def test_log_audit_coerces_string_entity_id_to_uuid():
    db = FakeDB()
    entity_id = uuid.uuid4()

    entry = await log_audit(
        db,  # type: ignore[arg-type]
        user=_user(),  # type: ignore[arg-type]
        action="create",
        entity_type="prompt",
        entity_id=str(entity_id),
        summary="Created prompt",
    )

    assert entry.entity_id == entity_id
    assert isinstance(entry.entity_id, uuid.UUID)


@pytest.mark.asyncio
async def test_log_audit_global_action_without_entity_or_request():
    db = FakeDB()

    entry = await log_audit(
        db,  # type: ignore[arg-type]
        user=_user(),  # type: ignore[arg-type]
        action="login",
        entity_type="auth",
        summary="User logged in",
    )

    assert entry.entity_id is None
    assert entry.entity_name is None
    assert entry.details is None
    assert entry.ip_address is None


@pytest.mark.asyncio
async def test_log_audit_never_records_a_value_the_column_cannot_hold(monkeypatch):
    # Under the flag the leftmost hop is whatever the client sent. The column
    # is String(45): a longer value would make the flush - and with it the
    # audited action - fail, and any other text would be recorded verbatim.
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    db = FakeDB()

    entry = await log_audit(
        db,  # type: ignore[arg-type]
        user=_user(),  # type: ignore[arg-type]
        action="login",
        entity_type="auth",
        summary="User logged in",
        request=_request(headers={"x-forwarded-for": "x" * 200}, client_host="10.0.0.5"),
    )

    assert entry.ip_address == "10.0.0.5"
    assert len(entry.ip_address) <= AuditLog.__table__.c.ip_address.type.length


# ---------------------------------------------------------------------------
# _get_client_ip — x-forwarded-for precedence
# ---------------------------------------------------------------------------


class TestGetClientIP:
    def test_forwarded_for_is_ignored_without_trust_proxy_headers(self):
        # A caller must not be able to write the address the audit trail
        # records: the header is only believed when the operator says so.
        request = _request(
            headers={"x-forwarded-for": "6.6.6.6"},
            client_host="198.51.100.7",
        )
        assert _get_client_ip(request) == "198.51.100.7"  # type: ignore[arg-type]

    def test_forwarded_for_takes_precedence_with_trust_proxy_headers(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        request = _request(
            headers={"x-forwarded-for": "198.51.100.1"},
            client_host="10.0.0.5",
        )
        assert _get_client_ip(request) == "198.51.100.1"  # type: ignore[arg-type]

    def test_forwarded_for_chain_uses_first_hop(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        request = _request(
            headers={"x-forwarded-for": "198.51.100.1, 10.0.0.2 , 10.0.0.3"},
        )
        assert _get_client_ip(request) == "198.51.100.1"  # type: ignore[arg-type]

    def test_forwarded_for_value_is_stripped(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        request = _request(headers={"x-forwarded-for": "  198.51.100.9  "})
        assert _get_client_ip(request) == "198.51.100.9"  # type: ignore[arg-type]

    def test_empty_forwarded_for_falls_back_to_client(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        request = _request(headers={"x-forwarded-for": " , "}, client_host="10.0.0.5")
        assert _get_client_ip(request) == "10.0.0.5"  # type: ignore[arg-type]

    def test_the_proxy_corrected_peer_is_recorded(self):
        # FORWARDED_ALLOW_IPS: uvicorn has already rewritten scope["client"]
        # to the real client, and the raw header must not shadow it.
        from starlette.requests import Request
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        seen = {}

        async def app(scope, receive, send):
            seen["scope"] = scope

        scope = {
            "type": "http",
            "client": ("10.0.0.5", 1234),
            "headers": [(b"x-forwarded-for", b"6.6.6.6, 198.51.100.7")],
        }
        asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts="10.0.0.5")(scope, None, None))
        assert _get_client_ip(Request(seen["scope"])) == "198.51.100.7"

    def test_falls_back_to_direct_client_host(self):
        request = _request(client_host="10.0.0.5")
        assert _get_client_ip(request) == "10.0.0.5"  # type: ignore[arg-type]

    def test_no_client_and_no_header_returns_none(self):
        request = _request(client_host=None)
        assert _get_client_ip(request) is None  # type: ignore[arg-type]

    def test_none_request_returns_none(self):
        assert _get_client_ip(None) is None


# ---------------------------------------------------------------------------
# log_security_event — user resolution and anonymous skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_security_event_for_known_user_writes_entry():
    user = _user()
    db = FakeDB(user=user)

    entry = await log_security_event(
        db,  # type: ignore[arg-type]
        user_id=str(USER_UUID),
        action="security_alert",
        summary="Prompt injection detected",
        details={"severity": "high", "reasons": ["jailbreak_marker"]},
    )

    assert entry is not None
    assert entry.user_id == USER_UUID
    assert entry.user_email == "auditor@example.com"
    assert entry.entity_type == "security"
    assert entry.action == "security_alert"
    assert entry.details == {"severity": "high", "reasons": ["jailbreak_marker"]}
    assert db.added == [entry]


@pytest.mark.asyncio
async def test_security_event_skips_anonymous_user():
    db = FakeDB()

    entry = await log_security_event(
        db,  # type: ignore[arg-type]
        user_id="anonymous",
        action="security_alert",
        summary="Prompt injection detected",
    )

    assert entry is None
    # Anonymous skip happens before any DB traffic.
    assert db.executed == []
    assert db.added == []


@pytest.mark.asyncio
async def test_security_event_skips_non_uuid_identifier():
    db = FakeDB()

    entry = await log_security_event(
        db,  # type: ignore[arg-type]
        user_id="not-a-uuid",
        action="security_alert",
        summary="Suspicious payload",
    )

    assert entry is None
    assert db.executed == []
    assert db.added == []


@pytest.mark.asyncio
async def test_security_event_skips_unknown_user_id():
    db = FakeDB(user=None)  # lookup resolves no user

    entry = await log_security_event(
        db,  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
        action="security_alert",
        summary="Suspicious payload",
    )

    assert entry is None
    # The lookup ran, but no audit row was written.
    assert len(db.executed) == 1
    assert db.added == []


def _is_text(node: ast.expr | None) -> bool:
    """A ``details`` that is a string: a literal, an f-string, ``str(...)`` or a concatenation of one."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == "str"
    if isinstance(node, ast.BinOp):
        return _is_text(node.left) or _is_text(node.right)
    return False


@pytest.mark.parametrize(
    "writer", [log_audit, log_system_audit, audit_entry, log_security_event], ids=lambda f: f.__name__
)
def test_every_audit_call_matches_its_signature(writer):
    params = inspect.signature(writer).parameters
    keywords = {
        name for name, p in params.items() if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
    }
    required = {
        name
        for name, p in params.items()
        if p.kind is p.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    }

    checked, wrong = 0, []
    for rel, module in app_sources.modules():
        for node in app_sources.walk(module):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            passed = {k.arg: k.value for k in node.keywords}
            if name != writer.__name__ or None in passed:  # ``**kwargs`` is not readable here
                continue
            checked += 1
            if missing := required - passed.keys():
                wrong.append(f"{rel}:{node.lineno} misses {sorted(missing)}")
            if unknown := passed.keys() - keywords:
                wrong.append(f"{rel}:{node.lineno} passes unknown {sorted(unknown)}")
            if _is_text(passed.get("details")):
                wrong.append(f"{rel}:{node.lineno} passes a string details")

    assert checked, f"no call to {writer.__name__} found under app/: the scan reads nothing"
    assert wrong == []
