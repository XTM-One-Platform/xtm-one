"""Both run paths, both protocol versions, one behaviour.

FR-021, FR-051 / SC-014, SC-018.

The run-path half guards against admitting two run paths in the spec while
only one can actually delegate — a requirement written for a state the
implemented path never reaches.

The parity half is what makes supporting two protocol versions a claim rather
than an aspiration: the same suite must pass against a partner serving v1.0
and one serving only the preceding version, with no code change between them.
"""

from __future__ import annotations

import inspect
import uuid

import httpx
import pytest

from app.a2a.client import delegation
from app.models.a2a import OUTCOME_COMPLETED
from tests.a2a_client_stubs import (
    RecordingDb,
    client_for,
    integration_row,
    rpc,
    settings_row,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from tests.a2a_chat_gate import stub_chat_gate

    stub_chat_gate(monkeypatch)
    monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: False)

    async def _headers(_i, _c):
        return {"Authorization": "Bearer minted"}

    monkeypatch.setattr(delegation, "outbound_headers", _headers)


class TestBothRunPaths:
    def test_delegation_takes_no_run_type_argument(self):
        # The structural half, and the strongest form of the guarantee: there
        # is no parameter a caller could set differently for a chat run and
        # an assignment run, so the two CANNOT diverge.
        params = inspect.signature(delegation.delegate).parameters
        for forbidden in ("run_type", "is_chat", "source", "trigger", "interactive"):
            assert forbidden not in params

    def test_the_tool_is_contributed_by_the_shared_dispatcher(self):
        # Both run paths resolve tools through ``build_integration_tools``, so
        # a tool reaching one reaches the other. Anything conditional on a run
        # type would have to live here, and does not.
        from app.services import integration_tools

        # The platform's chat gate is a deployment fact, not a run path.
        src = inspect.getsource(integration_tools._build_a2a_delegation_tool).lower()
        src = src.replace("is_chat_enabled", "").replace("agentic chat", "")
        for forbidden in ("chat", "assignment", "run_type", "interactive"):
            assert forbidden not in src, forbidden

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "run_ref",
        [uuid.uuid4(), None],
        ids=["with-a-run-record", "before-run-resolution"],
    )
    async def test_delegation_behaves_identically_whatever_the_run(
        self, monkeypatch, run_ref
    ):
        # ``assignment_runs`` is the universal run record — the chat path
        # creates one too — so one reference covers both paths. A delegation
        # must work whether or not that row exists yet.
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            client_for(lambda _r: rpc({"id": "t", "status": {"state": "completed"}})),
        )
        db = RecordingDb()
        result = await delegation.delegate(
            db,
            integration_row(),
            settings_row(),
            message="x",
            calling=delegation.CallingRun(
                agent_id=uuid.uuid4(), user_id=uuid.uuid4(), run_ref=run_ref
            ),
        )
        assert result.outcome == OUTCOME_COMPLETED
        assert db.added[0].run_ref == run_ref

    @pytest.mark.asyncio
    async def test_input_required_needs_no_run_parking(self, monkeypatch):
        # The specific reachability claim FR-031 makes. If continuing needed
        # the assignment engine's human-input machinery, it would work on one
        # path and not the other — written down, and unreachable.
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            client_for(
                lambda _r: rpc(
                    {
                        "id": "t",
                        "status": {
                            "state": "input-required",
                            "message": {"parts": [{"text": "?"}]},
                        },
                    }
                )
            ),
        )
        result = await delegation.delegate(
            RecordingDb(), integration_row(), settings_row(), message="x"
        )
        assert result.needs_input
        src = inspect.getsource(delegation)
        for parking in ("human_input", "await_human", "park", "wait_for_user"):
            assert parking not in src.lower(), parking


class TestProtocolParity:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["1.0", "0.3"])
    async def test_the_same_delegation_works_on_either_version(
        self, monkeypatch, version
    ):
        # SC-018: measured on a partner serving v1.0 and one serving only the
        # preceding version, with no code change between them. The parametrize
        # IS the "no code change" claim.
        seen = []

        def handler(request):
            seen.append(request.headers.get("a2a-version"))
            return rpc(
                {
                    "id": "t",
                    "status": {"state": "completed"},
                    "artifacts": [{"parts": [{"text": "done"}]}],
                }
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), integration_row(), settings_row(agreed_protocol_version=version), message="x"
        )
        assert result.outcome == OUTCOME_COMPLETED
        assert result.result_text == "done"
        assert result.protocol_version == version
        # Declared explicitly, never left to the partner's default.
        assert all(v == version for v in seen)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["1.0", "0.3"])
    async def test_failures_are_identical_across_versions(self, monkeypatch, version):
        monkeypatch.setattr(
            httpx, "AsyncClient", client_for(lambda _r: httpx.Response(401))
        )
        result = await delegation.delegate(
            RecordingDb(), integration_row(), settings_row(agreed_protocol_version=version), message="x"
        )
        assert result.outcome == "credential_rejected"

    def test_the_shaper_is_shared_with_the_inbound_half(self):
        # One shaper, per app/a2a/protocol.py's own docstring: two would
        # eventually disagree about what v0.3 means, and the disagreement
        # would surface as a card we publish contradicting one we consume.
        from app.a2a import protocol

        assert set(protocol.SUPPORTED_VERSIONS) == {"1.0", "0.3"}
        assert "app.a2a.client.sdk" in (protocol.__doc__ or "")
