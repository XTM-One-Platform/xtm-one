"""A2A over HTTP+JSON — the wire, once an interface has been chosen.

The gap this closes is not a nicety. Vertex AI Agent Engine publishes an A2A
card and a REST task surface (``/v1/message:send``, ``/v1/tasks/{id}``) and no
JSON-RPC endpoint at all, so the ``google_a2a`` provider verified a partner's
card and then failed every delegation against it, with a 404 for a reason.

The REST binding is proto-JSON throughout, which makes two readings that were
independently broken into the common case rather than the rare one: task states
arrive as enum NAMES (``TASK_STATE_COMPLETED``), and a send answers with a
oneof (``{"task": …}``) rather than a bare task.

Choosing the interface in the first place is
``test_a2a_client_card_interfaces``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.a2a.client import delegation
from app.a2a.protocol import TRANSPORT_JSONRPC, TRANSPORT_REST, resolve_method
from app.models.a2a import (
    OUTCOME_COMPLETED,
    OUTCOME_CREDENTIAL_REJECTED,
    OUTCOME_PARTNER_FAILED,
    OUTCOME_UNREACHABLE,
    USABILITY_USABLE,
)
from tests.a2a_client_stubs import (
    PollClock,
    RecordingDb,
    client_for,
    integration_row,
    settings_row,
)

ORIGIN = "https://partner.example"
A2A_BASE = f"{ORIGIN}/agents/triage/a2a"
CARD_URL = f"{A2A_BASE}/v1/card"


def _settings(**kw):
    """The shared row, re-aimed at a REST partner."""
    base = dict(
        card_url=CARD_URL,
        invocation_url=A2A_BASE,
        transport=TRANSPORT_REST,
        pinned_origin=ORIGIN,
        agreed_protocol_version="0.3",
    )
    base.update(kw)
    return settings_row(**base)


def _integration():
    return integration_row(provider="google_a2a", token_url=None)


@pytest.fixture(autouse=True)
def _fresh_verdict(monkeypatch):
    monkeypatch.setattr(delegation, "verdict_is_stale", lambda _s: False)


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    from tests.a2a_chat_gate import stub_chat_gate

    stub_chat_gate(monkeypatch)


@pytest.fixture(autouse=True)
def _minted(monkeypatch):
    async def _headers(_integration, _client):
        return {"Authorization": "Bearer minted"}

    monkeypatch.setattr(delegation, "outbound_headers", _headers)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(delegation.asyncio, "sleep", _instant)


def _rest_partner(*, states=("completed",), text="verdict: benign", monkeypatch=None):
    """A REST partner that walks *states*, one per poll, in proto-JSON."""
    seen: list[httpx.Request] = []
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/message:send"):
            return httpx.Response(
                200, json={"task": {"id": "task-1", "status": {"state": "TASK_STATE_SUBMITTED"}}}
            )
        if path.endswith(":cancel"):
            return httpx.Response(200, json={"id": "task-1", "status": {"state": "TASK_STATE_CANCELED"}})
        index = min(polls["n"], len(states) - 1)
        polls["n"] += 1
        state = states[index]
        body: dict = {"id": "task-1", "status": {"state": state}}
        if state == "TASK_STATE_COMPLETED":
            body["artifacts"] = [{"parts": [{"text": text}]}]
        return httpx.Response(200, json=body)

    if monkeypatch is not None:
        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
    return seen


class TestRestDelegation:
    @pytest.mark.asyncio
    async def test_a_completed_task_returns_its_text(self, monkeypatch):
        seen = _rest_partner(
            states=("TASK_STATE_WORKING", "TASK_STATE_COMPLETED"), monkeypatch=monkeypatch
        )
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="triage this"
        )
        assert result.outcome == OUTCOME_COMPLETED
        assert result.result_text == "verdict: benign"
        # The operation is the URL, not a method name in a body.
        assert [r.method + " " + r.url.path for r in seen] == [
            "POST /agents/triage/a2a/v1/message:send",
            "GET /agents/triage/a2a/v1/tasks/task-1",
            "GET /agents/triage/a2a/v1/tasks/task-1",
        ]

    @pytest.mark.asyncio
    async def test_the_body_is_a_send_request_not_a_jsonrpc_envelope(self, monkeypatch):
        seen = _rest_partner(states=("TASK_STATE_COMPLETED",), monkeypatch=monkeypatch)
        await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="just this"
        )
        body = json.loads(seen[0].content)
        assert "jsonrpc" not in body and "method" not in body
        assert body["message"]["messageId"]

    @pytest.mark.asyncio
    async def test_a_v0_3_rest_partner_is_still_sent_proto_shapes(self, monkeypatch):
        """A REST partner on **0.3** still speaks proto, not v0.3 JSON-RPC.

        Its binding is proto either way, and a proto parser rejects an
        unknown field rather than ignoring it:

            ParseError: Message type "a2a.v1.Message" has no field named
            "kind" at "SendMessageRequest.message"

        The rule the two assertions below pin, together: the VERSION decides the
        path, the BINDING decides the body. Getting either from the other is the
        same mistake in two directions, and this test fails if a future change
        re-couples them.
        """
        seen = _rest_partner(states=("TASK_STATE_COMPLETED",), monkeypatch=monkeypatch)
        await delegation.delegate(
            RecordingDb(), _integration(), _settings(agreed_protocol_version="0.3"),
            message="triage this", continue_task_id="tasks/prior",
        )

        body = json.loads(seen[0].content)
        message = body["message"]
        # No ``kind``, anywhere. It exists in no proto in either version.
        assert "kind" not in json.dumps(body), body
        # The proto enum NAME, not v0.3's lowercase spelling.
        assert message["role"] == "ROLE_USER"
        # ``content``, not ``parts``: the v0.3 PROTO - which is what a REST
        # partner parses - carries a message's content there, while v0.3's
        # pydantic models (its JSON-RPC binding) and the v1.0 proto both say
        # ``parts``. Sending ``parts`` here is an unknown field, and an unknown
        # field to a proto parser is HTTP 400.
        assert message["content"] == [{"text": "triage this"}]
        assert "parts" not in message
        # A continuation still rides on the message.
        assert message["taskId"] == "tasks/prior"
        # ...while the PATH still carries the v0.3 prefix, because that half
        # does follow the negotiated version.
        assert seen[0].url.path.endswith("/v1/message:send")

    @pytest.mark.asyncio
    async def test_a_v1_0_rest_partner_gets_parts_not_content(self, monkeypatch):
        # The third dialect. v1.0's proto renamed the field back to ``parts``,
        # so the container is not simply "whatever REST wants" - it moves with
        # the version, while the discriminator and the role spelling do not.
        seen = _rest_partner(states=("TASK_STATE_COMPLETED",), monkeypatch=monkeypatch)
        await delegation.delegate(
            RecordingDb(), _integration(), _settings(agreed_protocol_version="1.0"),
            message="triage this",
        )
        message = json.loads(seen[0].content)["message"]
        assert message["parts"] == [{"text": "triage this"}]
        assert "content" not in message
        # No ``/v1`` prefix at v1.0 - the path follows the version too.
        assert seen[0].url.path.endswith("/a2a/message:send")

    @pytest.mark.asyncio
    async def test_a_question_in_a_v0_3_proto_status_message_is_read(
        self, monkeypatch
    ):
        # The read side of the same field. An ``input-required`` task carries
        # the partner's question in ``status.message``, and a v0.3 proto
        # Message spells its content ``content`` - so reading only ``parts``
        # returned a task needing input with nothing to answer.
        def handler(request):
            if request.url.path.endswith("/message:send"):
                return httpx.Response(
                    200, json={"task": {"id": "t", "status": {"state": "TASK_STATE_SUBMITTED"}}}
                )
            return httpx.Response(
                200,
                json={"id": "t", "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {"role": "ROLE_AGENT",
                                "content": [{"text": "Which environment?"}]},
                }},
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="x"
        )
        assert result.question == "Which environment?"

    @pytest.mark.asyncio
    async def test_a_v0_3_jsonrpc_partner_still_gets_the_kind_discriminator(
        self, monkeypatch
    ):
        # The other half of the rule. Foundry's v0.3 parser is strict about the
        # discriminator being PRESENT, so this is not a shape we may drop
        # globally to satisfy the proto bindings.
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": "1", "result": {
                    "id": "t", "kind": "task", "status": {"state": "completed"},
                    "artifacts": [{"parts": [{"kind": "text", "text": "ok"}]}]}},
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        await delegation.delegate(
            RecordingDb(), _integration(),
            _settings(transport=TRANSPORT_JSONRPC, agreed_protocol_version="0.3",
                      invocation_url=f"{ORIGIN}/rpc"),
            message="triage this",
        )
        message = json.loads(seen[0].content)["params"]["message"]
        assert message["kind"] == "message"
        assert message["role"] == "user"
        assert message["parts"] == [{"kind": "text", "text": "triage this"}]

    @pytest.mark.asyncio
    async def test_proto_json_state_names_are_understood(self, monkeypatch):
        # The whole REST binding is proto-JSON, so this is not an edge case:
        # lowercasing ``TASK_STATE_COMPLETED`` matched nothing and reported a
        # completed delegation as an unrecognised state.
        _rest_partner(states=("TASK_STATE_COMPLETED",), monkeypatch=monkeypatch)
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="x"
        )
        assert result.outcome == OUTCOME_COMPLETED

    @pytest.mark.asyncio
    async def test_the_send_asks_to_block_so_a_v0_3_partner_never_needs_polling(
        self, monkeypatch
    ):
        """Not an optimisation - the only way to get an answer from Agent Engine.

        It keeps no task store between requests: a non-blocking send answers
        ``TASK_STATE_SUBMITTED`` with a task id, and every ``GET
        /v1/tasks/{id}`` for that id is 404, immediately and forever.
        """
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            body = json.loads(request.content)
            # A partner that was asked to block answers terminally.
            if (body.get("configuration") or {}).get("blocking"):
                return httpx.Response(
                    200,
                    json={"task": {"id": "t", "status": {"state": "TASK_STATE_COMPLETED"},
                                   "artifacts": [{"parts": [{"text": "Paris"}]}]}},
                )
            return httpx.Response(
                200, json={"task": {"id": "t", "status": {"state": "TASK_STATE_SUBMITTED"}}}
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="capital of France?"
        )
        assert result.outcome == OUTCOME_COMPLETED
        assert result.result_text == "Paris"
        # One request. No poll, because the send already answered.
        assert len(seen) == 1
        assert json.loads(seen[0].content)["configuration"] == {"blocking": True}

    @pytest.mark.asyncio
    async def test_v1_0_sends_no_configuration_because_blocking_is_its_default(
        self, monkeypatch
    ):
        # The flag is inverted in v1.0 (``returnImmediately``), so asking for
        # nothing asks for the same thing. Sending a v0.3 ``blocking`` there
        # would be an unknown field, which a proto parser refuses.
        seen = _rest_partner(states=("TASK_STATE_COMPLETED",), monkeypatch=monkeypatch)
        await delegation.delegate(
            RecordingDb(), _integration(), _settings(agreed_protocol_version="1.0"),
            message="x",
        )
        assert "configuration" not in json.loads(seen[0].content)

    @pytest.mark.asyncio
    async def test_a_lost_task_fails_the_delegation_not_the_registration(
        self, monkeypatch
    ):
        # A 404 on a POLL is a lost task, not a missing endpoint - the send
        # that created it answered 200 at the same origin moments earlier.
        # Classified as durable ``card_unfetchable`` it would suspend the
        # registration over one delegation.
        def handler(request):
            if request.url.path.endswith("/message:send"):
                return httpx.Response(
                    200,
                    json={"task": {"id": "t-lost",
                                   "status": {"state": "TASK_STATE_SUBMITTED"}}},
                )
            return httpx.Response(404, json={"error": {"message": "Task not found"}})

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        settings = _settings()
        result = await delegation.delegate(
            RecordingDb(), _integration(), settings, message="x"
        )
        assert result.outcome == OUTCOME_PARTNER_FAILED
        # Ours, bare: the diagnosis. Theirs, to be enveloped: the 404 body.
        assert "blocking send" in (result.detail or "")
        assert "Task not found" in (result.partner_reason or "")
        assert "Task not found" not in (result.detail or "")
        # The registration is untouched: the partner is plainly reachable.
        assert settings.usability == USABILITY_USABLE
        assert settings.unusable_reason is None

    @pytest.mark.asyncio
    async def test_a_partner_answering_outright_needs_no_polling(self, monkeypatch):
        # A send may answer with a Message instead of a Task, and the v1.0
        # default asks for exactly that. Read as a Task it had no id and no
        # status, so a successful delegation reported "<absent>".
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "message": {
                        "role": "ROLE_AGENT",
                        "messageId": "m-1",
                        "parts": [{"text": "benign, no action needed"}],
                    }
                },
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(agreed_protocol_version="1.0"),
            message="triage this",
        )
        assert result.outcome == OUTCOME_COMPLETED
        assert result.result_text == "benign, no action needed"
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_a_bare_proto_message_is_the_answer_not_an_absent_task(self, monkeypatch):
        # An un-discriminated Message spelled ``content`` is the answer, not
        # a Task with no state.
        def handler(_request):
            return httpx.Response(
                200,
                json={
                    "role": "ROLE_AGENT",
                    "messageId": "m-2",
                    "content": [{"text": "benign, no action needed"}],
                },
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="triage this"
        )
        assert result.outcome == OUTCOME_COMPLETED
        assert result.result_text == "benign, no action needed"

    @pytest.mark.asyncio
    async def test_a_partner_serving_the_other_path_prefix_is_found(self, monkeypatch):
        # Both conventions are live: v0.3's proto puts the version in the path,
        # v1.0's reference server expects it to be part of the declared URL.
        # A prefix disagreement must not make a partner permanently unusable.
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            path = request.url.path
            if path.startswith("/agents/triage/a2a/v1/"):
                return httpx.Response(404, json={"error": {"message": "not found"}})
            if path.endswith("/message:send"):
                return httpx.Response(
                    200,
                    json={"task": {"id": "t", "status": {"state": "TASK_STATE_COMPLETED"},
                                   "artifacts": [{"parts": [{"text": "ok"}]}]}},
                )
            return httpx.Response(404)

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="x"
        )
        assert result.outcome == OUTCOME_COMPLETED
        assert [r.url.path for r in seen] == [
            "/agents/triage/a2a/v1/message:send",
            "/agents/triage/a2a/message:send",
        ]

    @pytest.mark.asyncio
    async def test_a_task_returned_as_a_resource_name_is_still_addressable(self, monkeypatch):
        # The proto binding addresses a task by resource name (``tasks/{id}``)
        # while ``Task.id`` is the bare identifier, and a partner following
        # Google's API conventions may hand back either. Joining a resource
        # name onto the collection path gives ``/v1/tasks/tasks%2Fabc`` and a
        # 404 on every poll — found by pointing the harness at a partner
        # shaped like Agent Engine, which is what that harness is for.
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            if request.url.path.endswith("/message:send"):
                return httpx.Response(
                    200,
                    json={"task": {"id": "tasks/abc",
                                   "status": {"state": "TASK_STATE_SUBMITTED"}}},
                )
            if request.url.path.endswith("/v1/tasks/abc"):
                return httpx.Response(
                    200,
                    json={"id": "tasks/abc", "status": {"state": "TASK_STATE_COMPLETED"},
                          "artifacts": [{"parts": [{"text": "ok"}]}]},
                )
            return httpx.Response(404, json={"error": {"message": "no such task"}})

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="x"
        )
        assert result.outcome == OUTCOME_COMPLETED
        assert result.partner_task_id == "tasks/abc"
        assert seen[1].url.path == "/agents/triage/a2a/v1/tasks/abc"

    @pytest.mark.parametrize(
        ("status", "said"),
        [
            # Google Agent Engine, resuming a task this platform cancelled.
            (400, "Task t-77 is in terminal state: canceled"),
            (404, "Task t-77 not found"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_refused_continuation_fails_the_task_not_the_registration(
        self, monkeypatch, status, said
    ):
        # The request named one task, and the partner refused THAT task: it
        # has ended, or the partner never kept it. Marked unusable, the
        # registration refuses every fresh delegation until Verify now.
        def handler(_request):
            return httpx.Response(status, json={"error": {"message": said}})

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        settings = _settings()
        result = await delegation.delegate(
            RecordingDb(),
            _integration(),
            settings,
            message="Last 7 days",
            continue_task_id="t-77",
        )
        assert result.outcome == OUTCOME_PARTNER_FAILED
        assert said in (result.partner_reason or "")
        assert said not in (result.detail or "")
        assert result.partner_task_id == "t-77"
        assert settings.usability == USABILITY_USABLE
        assert settings.unusable_reason is None
        assert settings.consecutive_transient_failures == 0

    @pytest.mark.asyncio
    async def test_a_refused_fresh_submission_still_marks_the_registration(
        self, monkeypatch
    ):
        # No task named: the request itself is what the partner refused, and
        # it will refuse the next one the same way.
        def handler(_request):
            return httpx.Response(400, json={"error": {"message": "bad request"}})

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        settings = _settings()
        result = await delegation.delegate(
            RecordingDb(), _integration(), settings, message="x"
        )
        assert result.outcome == OUTCOME_UNREACHABLE
        assert settings.usability != USABILITY_USABLE

    @pytest.mark.asyncio
    async def test_a_rejected_credential_is_durable_and_named(self, monkeypatch):
        def handler(_request):
            return httpx.Response(
                401, json={"error": {"message": "Request had invalid credentials."}}
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        settings = _settings()
        result = await delegation.delegate(
            RecordingDb(), _integration(), settings, message="x"
        )
        assert result.outcome == OUTCOME_CREDENTIAL_REJECTED
        # The partner's own words travel as ``partner_reason``; ``detail`` names the status.
        assert "invalid credentials" in (result.partner_reason or "")
        assert "HTTP 401" in (result.detail or "")

    @pytest.mark.asyncio
    async def test_a_partner_error_is_transient_and_keeps_its_message(self, monkeypatch):
        def handler(_request):
            return httpx.Response(503, json={"error": {"message": "backend overloaded"}})

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="x"
        )
        assert result.outcome == OUTCOME_UNREACHABLE
        assert "backend overloaded" in (result.partner_reason or "")

    @pytest.mark.asyncio
    async def test_an_unrecognised_state_is_refused_whole(self, monkeypatch):
        _rest_partner(states=("TASK_STATE_CONTEMPLATING",), monkeypatch=monkeypatch)
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="x"
        )
        assert result.outcome == OUTCOME_PARTNER_FAILED
        assert "unrecognised" in (result.detail or "").lower()

    @pytest.mark.asyncio
    async def test_a_running_task_is_handed_back_not_cancelled(self, monkeypatch):
        """Spec 011 FR-001, over REST. This asserted a timeout-and-cancel
        until the attempt learned to hand back: a partner that answers
        ``working`` is working, and cancelling discards real work its operator
        has already paid for.

        On a clock only the poll loop moves: on the real one a loaded runner
        spent the 50 ms ceiling before the first status read and the attempt
        came back a plain timeout (CI run 41677317703)."""
        seen = _rest_partner(states=("TASK_STATE_WORKING",), monkeypatch=monkeypatch)
        monkeypatch.setattr(delegation, "asyncio", PollClock())
        monkeypatch.setattr(
            delegation.app_settings, "a2a_client_delegation_ceiling_seconds", 0.05
        )
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="x"
        )
        assert result.still_running
        assert result.partner_task_id == "task-1"
        assert not any(r.url.path.endswith("/v1/tasks/task-1:cancel") for r in seen)

    @pytest.mark.asyncio
    async def test_an_oversized_response_is_refused_rather_than_parsed(self, monkeypatch):
        # The ceiling was documented as a bound of this feature and enforced
        # nowhere. A truncated JSON body is not a smaller answer.
        def handler(_request):
            return httpx.Response(200, content=b"x" * 5000)

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        monkeypatch.setattr(
            delegation.app_settings, "a2a_client_response_max_bytes", 1000
        )
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="x"
        )
        assert result.outcome == OUTCOME_UNREACHABLE
        assert "ceiling" in (result.detail or "")


class TestJsonRpcStillReadsAV1Partner:
    """The same two v1.0 readings, on the binding that already worked."""

    @pytest.mark.asyncio
    async def test_a_oneof_send_result_and_enum_states(self, monkeypatch):
        calls: list[dict] = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            if resolve_method(body["method"])[0] == "send":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": "1",
                          "result": {"task": {"id": "t-9",
                                              "status": {"state": "TASK_STATE_WORKING"}}}},
                )
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": "1",
                      "result": {"id": "t-9", "status": {"state": "TASK_STATE_COMPLETED"},
                                 "artifacts": [{"parts": [{"text": "done"}]}]}},
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), _integration(),
            _settings(transport=TRANSPORT_JSONRPC, agreed_protocol_version="1.0",
                      invocation_url=f"{ORIGIN}/rpc"),
            message="x",
        )
        assert result.outcome == OUTCOME_COMPLETED
        assert result.result_text == "done"
        assert result.partner_task_id == "t-9"
        # v1.0 spells the role as the proto enum name.
        assert calls[0]["params"]["message"]["role"] == "ROLE_USER"


class TestAPartnerTaskIdCannotSteerTheUrl:
    """The one partner-authored string that becomes part of a URL.

    Everything else about a request is registration state: the endpoint is
    pinned, the version negotiated, the path derived from the binding. The
    task id is the exception — the partner chooses it and we address it back
    — and it is concatenated into a path, which is where a partner-supplied
    ``../`` or ``?`` would otherwise change which resource is fetched.
    """

    @pytest.mark.asyncio
    async def test_a_task_id_with_path_characters_is_percent_encoded(
        self, monkeypatch
    ):
        seen: list[httpx.Request] = []
        hostile = "../../../v1/admin?x=1#y"

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/message:send"):
                return httpx.Response(
                    200,
                    json={
                        "task": {
                            "id": hostile,
                            "status": {"state": "TASK_STATE_SUBMITTED"},
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": hostile,
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"parts": [{"text": "ok"}]}],
                },
            )

        monkeypatch.setattr(httpx, "AsyncClient", client_for(handler))
        result = await delegation.delegate(
            RecordingDb(), _integration(), _settings(), message="triage this"
        )
        assert result.outcome == OUTCOME_COMPLETED

        poll = seen[-1]
        # ``raw_path``, deliberately: ``url.path`` hands back the DECODED
        # form, so it reads ``/v1/tasks/../../../v1/admin?x=1#y`` and looks
        # like the bug even when the wire is correct. What is sent is what
        # matters, and it is one segment — ``rest_task_path`` quotes with
        # ``safe=''``.
        assert poll.url.raw_path == (
            b"/agents/triage/a2a/v1/tasks/..%2F..%2F..%2Fv1%2Fadmin%3Fx%3D1%23y"
        )
        # Not ``/v1/admin``, and nothing the partner appended became a query
        # or a fragment of its own.
        assert not poll.url.query and not poll.url.fragment
        # And the whole thing is still on the origin the registration pinned.
        assert f"{poll.url.scheme}://{poll.url.netloc.decode()}" == ORIGIN

    @pytest.mark.asyncio
    async def test_the_cancel_path_encodes_it_the_same_way(self, monkeypatch):
        # Cancel builds on the task path, so it inherits the quoting — pinned
        # because teardown cancels outstanding tasks (FR-025) on a path that
        # runs when nobody is watching.
        from app.a2a.protocol import rest_cancel_path

        assert rest_cancel_path("a/b?c", "0.3") == "/v1/tasks/a%2Fb%3Fc:cancel"
