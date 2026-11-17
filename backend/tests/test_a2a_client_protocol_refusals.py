"""A partner response we do not fully understand is refused whole.

FR-026, FR-027 / SC-015. "Whole" is the load-bearing word. Partially
interpreting a response — taking the artifacts and ignoring an unrecognised
state, or mapping that state to the nearest one we know — is how a platform
reports success for work a partner did not finish. A best-effort parse is
machinery that reads as assurance.
"""

from __future__ import annotations

import httpx
import pytest

from app.a2a.client import delegation
from app.models.a2a import OUTCOME_COMPLETED, OUTCOME_PARTNER_FAILED
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


async def _delegate(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
    return await delegation.delegate(
        RecordingDb(), integration_row(), settings_row(), message="x"
    )


class TestUnrecognisedState:
    @pytest.mark.asyncio
    async def test_an_unknown_state_is_not_mapped_to_the_nearest(self, monkeypatch):
        result = await _delegate(
            monkeypatch,
            lambda _r: rpc({"id": "t", "status": {"state": "contemplating"}}),
        )
        assert result.outcome == OUTCOME_PARTNER_FAILED
        assert "unrecognised task state" in (result.detail or "")

    @pytest.mark.asyncio
    async def test_an_unknown_state_with_artifacts_still_refuses(self, monkeypatch):
        # THE case. A best-effort parse would take the artifacts and report
        # success for work the partner never said it finished.
        result = await _delegate(
            monkeypatch,
            lambda _r: rpc(
                {
                    "id": "t",
                    "status": {"state": "partially_done"},
                    "artifacts": [{"parts": [{"text": "half an answer"}]}],
                }
            ),
        )
        assert result.outcome != OUTCOME_COMPLETED
        assert result.result_text is None

    @pytest.mark.asyncio
    async def test_an_absent_state_is_unrecognised_not_assumed_complete(
        self, monkeypatch
    ):
        result = await _delegate(monkeypatch, lambda _r: rpc({"id": "t"}))
        assert result.outcome == OUTCOME_PARTNER_FAILED


class TestMalformedEnvelopes:
    @pytest.mark.asyncio
    async def test_a_non_json_body_is_a_failure_not_a_crash(self, monkeypatch):
        result = await _delegate(
            monkeypatch, lambda _r: httpx.Response(200, content=b"<html>nope</html>")
        )
        # Never raises: the calling agent gets an outcome it can reason about
        # for every case.
        assert result.outcome in {"unreachable", OUTCOME_PARTNER_FAILED}

    @pytest.mark.asyncio
    async def test_a_response_with_no_result_member_is_not_a_success(
        self, monkeypatch
    ):
        result = await _delegate(
            monkeypatch,
            lambda _r: httpx.Response(
                200, json={"jsonrpc": "2.0", "id": "1", "error": {"code": -32603}}
            ),
        )
        assert result.outcome != OUTCOME_COMPLETED


class TestNeverRaises:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler",
        [
            lambda _r: httpx.Response(500),
            lambda _r: httpx.Response(200, content=b""),
            lambda _r: rpc({"id": "t", "status": None}),
            lambda _r: rpc({"status": {"state": ["not", "a", "string"]}}),
        ],
    )
    async def test_every_shape_produces_an_outcome(self, monkeypatch, handler):
        # The contract the tool depends on. An exception escaping here would
        # surface as a tool error indistinguishable from a platform bug, and
        # the agent would have no reason to prefer one recovery over another.
        result = await _delegate(monkeypatch, handler)
        assert result.outcome
        from app.models.a2a import DELEGATION_OUTCOMES

        assert result.outcome in DELEGATION_OUTCOMES


class TestVersionShape:
    @pytest.mark.asyncio
    async def test_the_declared_version_travels_on_the_poll_too(self, monkeypatch):
        # FR-023 is per REQUEST, not per delegation: a poll that omitted it
        # would let a partner answer the follow-up in a different shape.
        seen = []

        def handler(request):
            seen.append(request.headers.get("a2a-version"))
            state = "working" if len(seen) == 1 else "completed"
            return rpc({"id": "t", "status": {"state": state}})

        await _delegate(monkeypatch, handler)
        assert len(seen) >= 2
        assert all(v == "1.0" for v in seen)
