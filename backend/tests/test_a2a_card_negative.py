"""Every negative outcome on the card surface must be indistinguishable.

Unknown token, revoked exposure, disabled agent: all the same JSON 404.
Telling them apart would let a caller reconstruct the listing of exposed
agents the specification forbids anywhere. The one stated refusal is the chat
gate — A2A is offered exactly while agentic chat is, and both halves of that
answer are public on ``GET /platform/config``, so saying it reveals nothing.
"""

import uuid

import pytest
from fastapi import FastAPI

from app.api.v1 import a2a_server
from app.core.database import get_db
from tests.a2a_chat_gate import stub_chat_gate
from tests.a2a_server_stubs import partner_client


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _stmt):
        return _Result(self._results.pop(0) if self._results else None)


class _Agent:
    def __init__(self, enabled=True):
        self.id = uuid.uuid4()
        self.name = "A"
        self.description = "d"
        self.enabled = enabled
        self.output_format = "markdown"
        self.output_schema = None
        self.builtin_tool_ids = []
        self.custom_tool_ids = []
        self.mcp_server_ids = []
        self.knowledge_base_ids = []
        self.integration_bindings = {}


class _Exposure:
    def __init__(self, agent_id):
        self.agent_id = agent_id
        self.card_token = "tok"
        self.boundary_kind = "owner"
        self.boundary_user_id = uuid.uuid4()
        self.allowed_tools = None


def _client(monkeypatch, *, results, enabled=True, exposure=None):
    stub_chat_gate(monkeypatch, enabled=enabled)

    # The pre-auth limiter is fail-closed and there is no Redis here; it has
    # its own tests in test_a2a_dedup_and_limits. Neutralised so these tests
    # keep asserting the uniform-404 behaviour they exist for.
    async def _no_limit(_request):
        return None

    monkeypatch.setattr(a2a_server, "_enforce_preauth_rate_limit", _no_limit)

    async def _get_exposure(_db, _token):
        return exposure

    monkeypatch.setattr(a2a_server, "get_exposure_by_token", _get_exposure)

    app = FastAPI()
    app.include_router(a2a_server.router)
    app.dependency_overrides[get_db] = lambda: _FakeDB(results)
    return partner_client(app)


CARD = "/a2a/{}/.well-known/agent-card.json"
UNIFORM = {"detail": "Not Found"}


class TestUniformNotFound:
    def test_unknown_token(self, monkeypatch):
        c = _client(monkeypatch, results=[], exposure=None)
        r = c.get(CARD.format("nope"))
        assert r.status_code == 404 and r.json() == UNIFORM

    def test_revoked_exposure_looks_identical(self, monkeypatch):
        # get_exposure_by_token already filters revoked rows out, so a revoked
        # token arrives here as None - the same as never having existed.
        c = _client(monkeypatch, results=[], exposure=None)
        r = c.get(CARD.format("retired"))
        assert r.status_code == 404 and r.json() == UNIFORM

    def test_disabled_agent_looks_identical(self, monkeypatch):
        agent = _Agent(enabled=False)
        c = _client(
            monkeypatch, results=[agent], exposure=_Exposure(agent.id)
        )
        r = c.get(CARD.format("tok"))
        assert r.status_code == 404 and r.json() == UNIFORM

    def test_deleted_agent_looks_identical(self, monkeypatch):
        c = _client(monkeypatch, results=[None], exposure=_Exposure(uuid.uuid4()))
        r = c.get(CARD.format("tok"))
        assert r.status_code == 404 and r.json() == UNIFORM

    def test_without_chat_the_card_is_the_one_stated_refusal(self, monkeypatch):
        """Without agentic chat the card answers 403 with the chat gate's
        reason, not the uniform 404: both halves of that answer are public on
        GET /platform/config, so this discloses nothing. Every other negative
        stays uniform."""
        agent = _Agent()
        c = _client(
            monkeypatch, results=[agent], exposure=_Exposure(agent.id), enabled=False
        )
        r = c.get(CARD.format("tok"))
        assert r.status_code == 403
        assert "license" in r.json()["detail"]

    def test_all_negative_paths_return_byte_identical_bodies(self, monkeypatch):
        bodies = set()
        for kwargs in (
            {"results": [], "exposure": None},
            {"results": [None], "exposure": _Exposure(uuid.uuid4())},
            {"results": [_Agent(enabled=False)], "exposure": _Exposure(uuid.uuid4())},
        ):
            c = _client(monkeypatch, **kwargs)
            bodies.add(c.get(CARD.format("t")).text)
        assert len(bodies) == 1, "negative outcomes are distinguishable"


class TestVersionRejection:
    def test_unrecognised_version_is_a_plain_client_error(self, monkeypatch):
        """Decided before any lookup, so it discloses nothing about what
        exists - hence 400 rather than the uniform 404."""
        c = _client(monkeypatch, results=[], exposure=None)
        r = c.get(CARD.format("tok"), headers={"A2A-Version": "9.9"})
        assert r.status_code == 400
        assert "1.0" in r.json()["detail"] and "0.3" in r.json()["detail"]


class TestSuccessPath:
    def test_live_exposure_serves_a_card(self, monkeypatch):
        agent = _Agent()
        c = _client(monkeypatch, results=[agent], exposure=_Exposure(agent.id))
        r = c.get(CARD.format("tok"))
        assert r.status_code == 200
        assert r.json()["name"] == "A"

    def test_absent_version_header_yields_the_v1_shape(self, monkeypatch):
        """Both platforms register by fetching the card with no version."""
        agent = _Agent()
        c = _client(monkeypatch, results=[agent], exposure=_Exposure(agent.id))
        body = c.get(CARD.format("tok")).json()
        assert body["protocolVersion"] == "1.0"
        assert body["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
        # And the v0.3 names beside them - the platforms fetching this way
        # do not share one parser.
        assert body["url"] == body["supportedInterfaces"][0]["url"]
        assert body["preferredTransport"] == "JSONRPC"


class TestNoEnumeration:
    @pytest.mark.parametrize("path", ["/a2a", "/a2a/", "/a2a/.well-known/agent-card.json"])
    def test_no_index_is_served(self, monkeypatch, path):
        c = _client(monkeypatch, results=[], exposure=None)
        assert c.get(path).status_code in (404, 405)
