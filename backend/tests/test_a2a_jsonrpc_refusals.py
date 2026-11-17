"""Route-level refusals on the task surface: throttling, quota, platform keys.

Three properties, each of which previously failed in a way only a partner (or
an auditor) would notice:

- a throttled request returns the JSON-RPC envelope with ``RateLimitedError``
  and keeps ``Retry-After`` — not FastAPI's bare ``{"detail": ...}`` body,
  which partner tooling that branches on ``error.code`` cannot read;
- an exhausted usage allowance refuses at submission with
  ``QuotaExhaustedError`` and is audit-logged naming the client (FR-036) —
  previously the run started, "completed", and delivered the refusal prose as
  its artifact;
- a rejected platform key is audit-logged and counted under its own metric
  label (FR-020) — previously indistinguishable from any bad token.
"""

import uuid

import pytest
from fastapi import FastAPI, HTTPException

from app.a2a.providers import OAUTH_A2A_PROVIDER
from app.api.v1 import a2a_server
from app.core.database import get_db
from app.core.utils import is_secure_transport
from app.services import ai_state as _ai_state
from tests.a2a_chat_gate import stub_chat_gate
from tests.a2a_server_stubs import partner_client


@pytest.fixture(autouse=True)
def _ai_enabled(monkeypatch):
    """AI on: the A2A surface refuses entirely when it is off (FR-010),
    and these suites are about the protocol binding behind that gate."""

    async def _enabled(_db):
        return True

    monkeypatch.setattr(_ai_state, "is_ai_enabled", _enabled)


class _FakeDB:
    def __init__(self):
        self.commits = 0

    async def execute(self, _stmt):
        class _R:
            def scalar_one_or_none(self):
                return None

        return _R()

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass


class _Exposure:
    def __init__(self):
        self.id = uuid.uuid4()
        self.agent_id = uuid.uuid4()
        self.run_as_user_id = uuid.uuid4()
        self.revoked_at = None
        self.allowed_tools = None


def _client(monkeypatch, *, exposure, preauth_exc=None, client_exc=None):
    stub_chat_gate(monkeypatch)

    async def _get_exposure(_db, _token):
        return exposure

    # The jsonrpc route imports this INSIDE the handler, so the patch has to
    # land on the source module (the card route's module-level import is why
    # test_a2a_card_negative patches ``a2a_server`` instead).
    import app.a2a.exposure as exposure_mod

    monkeypatch.setattr(exposure_mod, "get_exposure_by_token", _get_exposure)

    async def _preauth(_request):
        if preauth_exc:
            raise preauth_exc

    monkeypatch.setattr(a2a_server, "_enforce_preauth_rate_limit", _preauth)

    async def _per_client(_client_id, _method):
        if client_exc:
            raise client_exc

    monkeypatch.setattr(a2a_server, "_enforce_a2a_rate_limit", _per_client)

    app = FastAPI()
    app.include_router(a2a_server.router)
    app.dependency_overrides[get_db] = _FakeDB
    return partner_client(app)


def _rpc(c, card_token="tok", *, method="tasks/get", params=None, req_id="r1"):  # noqa: S107
    return c.post(
        f"/a2a/{card_token}/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        },
        headers={"Authorization": "Bearer whatever"},
    )


class TestOversizedBodyIsRefusedBeforeItIsParsed:
    """Starlette caps nothing by default, so without this one anonymous caller's
    multi-MB payload is fully buffered and parsed however tight the rate limit
    is. Declared size is checked first; the body is never read.
    """

    def test_oversized_content_length_gets_413_and_the_envelope(self, monkeypatch):
        from app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "a2a_max_request_bytes", 100)
        c = _client(monkeypatch, exposure=_Exposure())
        r = c.post(
            "/a2a/tok/jsonrpc",
            content=b"x" * 500,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer whatever",
            },
        )
        assert r.status_code == 413
        # The envelope, not FastAPI's bare {"detail": ...}: partner tooling on
        # this surface branches on error.code.
        assert r.json()["error"]["code"] == -32600

    def test_a_normal_body_is_unaffected(self, monkeypatch):
        c = _client(monkeypatch, exposure=_Exposure())
        assert _rpc(c).status_code != 413

    def test_the_declared_size_is_checked_before_the_limiter_charge(
        self, monkeypatch
    ):
        """An oversized body must not need the limiter to have been consulted —
        it is refused on the header alone."""
        from app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "a2a_max_request_bytes", 10)
        c = _client(monkeypatch, exposure=_Exposure())
        r = c.post(
            "/a2a/tok/jsonrpc",
            content=b"y" * 50,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer whatever",
            },
        )
        assert r.status_code == 413


class TestLoopbackIsNotAnHttpsExemptionInProduction:
    """Whether transport is confirmed encrypted is what the operator is told,
    so it must not be decided by address. Treating loopback as secure on
    address alone reports confirmed encryption whenever a same-host reverse
    proxy fronts the app without an X-Forwarded-Proto rewrite,
    because every partner request then arrives from 127.0.0.1.
    """

    def _request(self, host, scheme="http"):
        class _URL:
            def __init__(self):
                self.scheme = scheme

        class _Client:
            def __init__(self):
                self.host = host

        class _Req:
            def __init__(self):
                self.url = _URL()
                self.client = _Client()
                self.headers = {}

        return _Req()

    def test_loopback_is_not_exempt_when_debug_is_off(self, monkeypatch):
        from app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "trust_proxy_headers", False)
        monkeypatch.setattr(app_settings, "debug", False)
        monkeypatch.setattr(app_settings, "a2a_allow_insecure_loopback", False)
        assert is_secure_transport(self._request("127.0.0.1")) is False

    def test_loopback_is_exempt_when_the_deployment_allows_it(self, monkeypatch):
        from app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "trust_proxy_headers", False)
        monkeypatch.setattr(app_settings, "debug", False)
        monkeypatch.setattr(app_settings, "a2a_allow_insecure_loopback", True)
        assert is_secure_transport(self._request("127.0.0.1")) is True
        assert is_secure_transport(self._request("::1")) is True

    def test_allowing_loopback_exempts_nothing_else(self, monkeypatch):
        from app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "trust_proxy_headers", False)
        monkeypatch.setattr(app_settings, "debug", False)
        monkeypatch.setattr(app_settings, "a2a_allow_insecure_loopback", True)
        assert is_secure_transport(self._request("10.0.0.5")) is False
        assert is_secure_transport(self._request("203.0.113.9")) is False

    def test_loopback_is_exempt_in_debug_builds(self, monkeypatch):
        from app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "trust_proxy_headers", False)
        monkeypatch.setattr(app_settings, "debug", True)
        assert is_secure_transport(self._request("127.0.0.1")) is True

    def test_https_still_passes_without_debug(self, monkeypatch):
        from app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "trust_proxy_headers", False)
        monkeypatch.setattr(app_settings, "debug", False)
        assert (
            is_secure_transport(self._request("10.0.0.5", scheme="https")) is True
        )


class TestThrottlingWearsTheEnvelope:
    def test_preauth_429_is_a_jsonrpc_error_with_retry_after(self, monkeypatch):
        c = _client(
            monkeypatch,
            exposure=None,
            preauth_exc=HTTPException(
                status_code=429,
                detail="Too many requests",
                headers={"Retry-After": "42"},
            ),
        )
        r = _rpc(c)
        assert r.status_code == 429
        assert r.headers["Retry-After"] == "42"
        body = r.json()
        assert body["error"]["code"] == -32029
        assert body["error"]["data"]["@type"] == "RateLimitedError"
        assert body["id"] == "r1"

    def test_fail_closed_503_keeps_the_envelope_too(self, monkeypatch):
        c = _client(
            monkeypatch,
            exposure=None,
            preauth_exc=HTTPException(status_code=503, detail="limiter down"),
        )
        r = _rpc(c)
        assert r.status_code == 503
        assert r.json()["error"]["code"] == -32603  # internal, not RateLimited

    def test_per_client_429_is_shaped_the_same_way(self, monkeypatch):
        exposure = _Exposure()

        async def _auth(_request, _db, _exposure):
            return "a2a-abc", "", OAUTH_A2A_PROVIDER

        c = _client(
            monkeypatch,
            exposure=exposure,
            client_exc=HTTPException(
                status_code=429,
                detail="slow down",
                headers={"Retry-After": "7"},
            ),
        )
        monkeypatch.setattr(a2a_server, "_authenticate", _auth)
        r = _rpc(c)
        assert r.status_code == 429
        assert r.json()["error"]["code"] == -32029


class TestQuotaGateAtSubmission:
    def _quota_client(self, monkeypatch, *, exhausted):
        from app.services import quota as quota_svc

        exposure = _Exposure()

        async def _auth(_request, _db, _exposure):
            return "a2a-abc", "", OAUTH_A2A_PROVIDER

        monkeypatch.setattr(a2a_server, "_authenticate", _auth)

        # The route imports the checker at request time, so the patch lands
        # on the quota module. Only the gate itself is under test here — the
        # accepted-submission path has its own tests.
        async def _check(_db, _uid):
            if exhausted:
                raise quota_svc.QuotaExceededError("agentic", 10, 10, "month")

        monkeypatch.setattr(quota_svc, "check_agentic_quota", _check)

        audits: list[dict] = []

        async def _log_audit(_db, **kw):
            audits.append(kw)

        import app.services.audit as audit_svc

        monkeypatch.setattr(audit_svc, "log_audit", _log_audit)
        return _client(monkeypatch, exposure=exposure), audits

    def test_exhausted_allowance_is_a_structured_refusal(self, monkeypatch):
        c, audits = self._quota_client(monkeypatch, exhausted=True)
        # Agent query returns a fake agent through the db override? No — the
        # quota gate must fire only after the agent resolves, so give the
        # route a real-enough agent.
        agent = type(
            "_Agent",
            (),
            {
                "id": uuid.uuid4(),
                "name": "Recon",
                "enabled": True,
                "user_id": uuid.uuid4(),
                "is_company_managed": False,
            },
        )()

        class _AgentDB(_FakeDB):
            async def execute(self, _stmt):
                class _R:
                    def scalar_one_or_none(self):
                        return agent

                return _R()

        # find_existing_task must say "new submission" for the gate to fire.
        from app.a2a import tasks as task_svc

        async def _none(_db, _cid, _mid):
            return None

        monkeypatch.setattr(task_svc, "find_existing_task", _none)

        app = c.app
        app.dependency_overrides[get_db] = _AgentDB

        r = _rpc(
            c,
            method="message/send",
            params={"message": {"messageId": "m1", "parts": [{"text": "hi"}]}},
        )
        body = r.json()
        assert body["error"]["code"] == -32030
        assert body["error"]["data"]["@type"] == "QuotaExhaustedError"
        assert len(audits) == 1
        assert audits[0]["details"]["client_id"] == "a2a-abc"


class TestPlatformKeyRefusalIsAccountable:
    def test_platform_key_is_audited_and_counted(self, monkeypatch):
        exposure = _Exposure()
        c = _client(monkeypatch, exposure=exposure)

        audits: list[dict] = []

        async def _log_audit(_db, **kw):
            audits.append(kw)

        import app.services.audit as audit_svc

        monkeypatch.setattr(audit_svc, "log_audit", _log_audit)

        refusals: list[str] = []
        from app.a2a import metrics as a2a_metrics

        monkeypatch.setattr(
            a2a_metrics, "record_refusal", lambda reason: refusals.append(reason)
        )

        r = c.post(
            "/a2a/tok/jsonrpc",
            json={"jsonrpc": "2.0", "id": "r1", "method": "tasks/get"},
            headers={"Authorization": "Bearer fcp-secret-key"},
        )
        # The RESPONSE is the same uniform 401 every refusal gets…
        assert r.status_code == 401
        # …but the metric and audit trail know it was a platform key.
        assert refusals == ["rejected_platform_key"]
        assert len(audits) == 1
        assert "platform API key" in audits[0]["summary"]
        # The key itself is never recorded.
        assert "fcp-" not in str(audits[0])
