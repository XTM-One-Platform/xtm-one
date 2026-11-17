"""Only the composed message leaves the platform.

FR-032, FR-033 / SC-005. The failure this guards is silent: a delegation that
works perfectly while carrying the calling run's history or a platform token
to a third party looks identical, from inside, to one that does not.

Captured at the wire rather than asserted about the code, because the question
is what actually left — a helper that builds a clean payload and a caller that
adds a header afterwards would pass any structural check.
"""

from __future__ import annotations

import inspect
import json

import httpx
import pytest

from app.a2a.client import delegation
from tests.a2a_client_stubs import (
    RecordingDb,
    client_for,
    integration_row,
    rpc,
    settings_row,
)

# Things a run might plausibly hold that must never reach a partner.
PLATFORM_TOKEN = "xtm-platform-jwt-aaaabbbbcccc"
OTHER_CREDENTIAL = "github_pat_11ABCDEFG0123456789"
RUN_HISTORY = "Earlier the user said their merger target is Contoso."


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from tests.a2a_chat_gate import stub_chat_gate

    stub_chat_gate(monkeypatch)
    monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: False)

    async def _headers(_i, _c):
        # A minted partner bearer IS legitimate on the wire; a PLATFORM token
        # is not. The tests below distinguish the two.
        return {"Authorization": "Bearer partner-scoped-minted"}

    monkeypatch.setattr(delegation, "outbound_headers", _headers)


@pytest.fixture
def captured(monkeypatch):
    """Every request this platform sent, headers and body."""
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return rpc({"id": "t", "status": {"state": "completed"}})

    monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
    return seen


class TestOnlyTheComposedMessage:
    @pytest.mark.asyncio
    async def test_the_body_carries_the_message_and_nothing_else(self, captured):
        await delegation.delegate(
            RecordingDb(), integration_row(), settings_row(), message="triage alert 42"
        )
        body = json.loads(captured[0].content)
        parts = body["params"]["message"]["parts"]
        assert parts == [{"text": "triage alert 42"}]
        # The params carry the message and, when continuing, a task id. There
        # is no room for a context field to appear unnoticed.
        assert set(body["params"]) <= {"message", "taskId"}

    @pytest.mark.asyncio
    async def test_no_run_context_reaches_the_partner(self, captured):
        # The delegate() signature takes no history parameter at all, which is
        # the structural half; this is the observed half.
        await delegation.delegate(
            RecordingDb(), integration_row(), settings_row(), message="triage alert 42"
        )
        raw = captured[0].content.decode()
        for secret in (PLATFORM_TOKEN, OTHER_CREDENTIAL, RUN_HISTORY):
            assert secret not in raw
        # The minted bearer belongs in the Authorization header, never the
        # body — nor does anything smelling of credentials or run context.
        for forbidden in ("Bearer", "client_secret", "conversation", "history"):
            assert forbidden not in raw, forbidden

    @pytest.mark.asyncio
    async def test_history_cannot_be_smuggled_through_the_signature(self):
        # FR-032 is enforced by there being nowhere to put it: a caller that
        # wanted to forward history would have to change this signature, which
        # is a reviewable act rather than an accident.
        params = inspect.signature(delegation.delegate).parameters
        for forbidden in ("history", "context", "messages", "transcript", "system"):
            assert forbidden not in params


class TestHeaders:
    @pytest.mark.asyncio
    async def test_only_the_partner_scoped_bearer_is_sent(self, captured):
        await delegation.delegate(RecordingDb(), integration_row(), settings_row(), message="x")
        auth = captured[0].headers.get("authorization")
        assert auth == "Bearer partner-scoped-minted"
        assert PLATFORM_TOKEN not in (auth or "")

    @pytest.mark.asyncio
    async def test_no_platform_identity_header_travels(self, captured):
        # The cross-platform identity token is a real header elsewhere in this
        # codebase; a partner must never see one.
        await delegation.delegate(RecordingDb(), integration_row(), settings_row(), message="x")
        keys = {k.lower() for k in captured[0].headers}
        for forbidden in ("x-platform-token", "x-xtm-token", "cookie", "x-api-key"):
            assert forbidden not in keys

    @pytest.mark.asyncio
    async def test_the_agreed_version_is_the_only_added_header(self, captured):
        await delegation.delegate(RecordingDb(), integration_row(), settings_row(), message="x")
        assert captured[0].headers.get("a2a-version") == "1.0"


class TestContinuationHoldsTheSameLine:
    @pytest.mark.asyncio
    async def test_an_answer_carries_only_the_answer(self, captured):
        # The easier one to forget: a continuation is still an outbound
        # payload, and the natural implementation appends context to it.
        await delegation.delegate(
            RecordingDb(),
            integration_row(),
            settings_row(),
            message="the tenant is contoso",
            continue_task_id="task-9",
        )
        body = json.loads(captured[0].content)
        assert body["params"]["message"]["parts"] == [{"text": "the tenant is contoso"}]
        # On the MESSAGE, which is where both versions carry it: v0.3's
        # ``MessageSendParams`` is message/configuration/metadata and v1.0's
        # ``SendMessageRequest`` adds only ``tenant``, so neither has a task id
        # of its own. Beside the message it is a field a tolerant partner
        # ignores — every continuation would open a new task.
        assert body["params"]["message"]["taskId"] == "task-9"
        assert "taskId" not in body["params"]
        raw = captured[0].content.decode()
        assert RUN_HISTORY not in raw
        # An answer is still an outbound payload: no minted bearer in it.
        assert "Bearer" not in raw


class TestNoInwardReach:
    def test_a_registration_confers_no_platform_access(self):
        # FR-033: a delegation cannot send outward anything the calling run
        # could not itself see, because it never reads anything but its own
        # arguments and the registration's own row.
        src = inspect.getsource(delegation.delegate)
        for forbidden in ("select(Agent", "select(User", "user_visible", "is_admin"):
            assert forbidden not in src, forbidden
