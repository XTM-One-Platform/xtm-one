"""The v1.0 JSON-RPC binding, end to end through the route.

The first real v1.0 client (ADK/Vertex via the reference SDK, 2026-08)
exposed that the surface advertised v1.0 while speaking only the v0.3
binding: it refused ``SendMessage`` outright, and — had dispatch existed —
would have answered with shapes (``"completed"``, ``structuredData``, a
bare-task send result) that ``json_format.ParseDict`` rejects. These tests
drive the actual route with both vocabularies and hand every v1.0 response
to the reference SDK's own parser, which is the only authority on what a
v1.0 partner accepts.
"""

import uuid

import pytest
from a2a.types import a2a_pb2
from fastapi import FastAPI
from google.protobuf import json_format

from app.a2a import protocol as proto
from app.a2a.providers import OAUTH_A2A_PROVIDER
from app.api.v1 import a2a_server
from app.core.database import get_db
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


class _Exposure:
    def __init__(self):
        self.id = uuid.uuid4()
        self.agent_id = uuid.uuid4()
        self.run_as_user_id = uuid.uuid4()
        self.revoked_at = None
        self.allowed_tools = None


class _Agent:
    def __init__(self):
        self.id = uuid.uuid4()
        self.name = "Exposed agent"
        self.enabled = True
        self.is_company_managed = False
        self.output_format = "markdown"


class _Task:
    def __init__(self):
        self.id = uuid.uuid4()
        self.context_id = uuid.uuid4()
        self.run_id = None
        self.rejected_reason = None
        self.cancel_requested_at = None
        self.client_id = "client-1"


class _AgentDB:
    """Returns the agent for the one query the mocked-out handler still runs."""

    def __init__(self, agent):
        self._agent = agent

    async def execute(self, _stmt):
        agent = self._agent

        class _R:
            def scalar_one_or_none(self):
                return agent

        return _R()

    async def commit(self):
        pass

    async def flush(self):
        pass

    async def rollback(self):
        # The blocking wait ends its transaction between polls so it does not
        # hold a pooled connection for the whole wait.
        pass

    async def refresh(self, _obj):
        # ...and reloads the task afterwards, because the rollback expired it.
        pass


def _harness(monkeypatch, *, states=None):
    """A route harness with everything behind the dispatch faked.

    The task services are patched on their source module because the handler
    imports them inside the function body; ``_render_task``'s artifact and
    state projection go through the REAL protocol shapers so these tests pin
    actual wire bytes, not fixture bytes.

    ``states`` is the sequence successive ``resolve_state`` calls see (the
    last value sticks), which is how the blocking-send tests prove a wait
    actually consumed the non-terminal states instead of rendering the first.
    """
    exposure, agent, task = _Exposure(), _Agent(), _Task()
    states = list(states or [proto.A2AState.COMPLETED])

    stub_chat_gate(monkeypatch)

    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "a2a_blocking_poll_interval_seconds", 0.001)
    monkeypatch.setattr(app_settings, "a2a_blocking_send_timeout_seconds", 5)

    import app.a2a.exposure as exposure_mod
    import app.a2a.tasks as tasks_mod
    import app.services.audit as audit_mod
    import app.services.quota as quota_mod
    from app.core import rate_limit as rate_limit_mod

    async def _get_exposure(_db, _token):
        return exposure

    monkeypatch.setattr(exposure_mod, "get_exposure_by_token", _get_exposure)

    async def _noop_preauth(_request):
        pass

    monkeypatch.setattr(a2a_server, "_enforce_preauth_rate_limit", _noop_preauth)

    seen_ops: list[str | None] = []

    async def _per_client(_client_id, op):
        seen_ops.append(op)

    monkeypatch.setattr(a2a_server, "_enforce_a2a_rate_limit", _per_client)

    async def _refund(_request, _key):
        pass

    monkeypatch.setattr(rate_limit_mod, "refund_rate_limit", _refund)

    async def _auth(_request, _db, _exposure):
        return "client-1", "", OAUTH_A2A_PROVIDER

    monkeypatch.setattr(a2a_server, "_authenticate", _auth)

    async def _no_audit(*_a, **_k):
        pass

    monkeypatch.setattr(audit_mod, "log_audit", _no_audit)

    async def _quota_ok(_db, _user_id):
        pass

    monkeypatch.setattr(quota_mod, "check_agentic_quota", _quota_ok)

    async def _no_existing(_db, _client_id, _message_id):
        return None

    monkeypatch.setattr(tasks_mod, "find_existing_task", _no_existing)

    async def _create(_db, **_kw):
        return task, True

    monkeypatch.setattr(tasks_mod, "create_task", _create)

    async def _run(_task_id):
        pass

    monkeypatch.setattr(tasks_mod, "execute_task_run", _run)

    async def _announce(_task_id):
        pass

    monkeypatch.setattr(tasks_mod, "announce_state_by_id", _announce)

    async def _get_task(_db, _task_id, _client_id):
        return task

    monkeypatch.setattr(tasks_mod, "get_task", _get_task)

    async def _cancel(_db, _task, *, cancelled_out=None):
        return True

    monkeypatch.setattr(tasks_mod, "request_cancel", _cancel)

    async def _state(_db, _task):
        return states.pop(0) if len(states) > 1 else states[0]

    monkeypatch.setattr(tasks_mod, "resolve_state", _state)

    async def _artifacts(_db, _task, _agent, version):
        # Through the real shapers, so the test pins what actually ships.
        return [
            {
                "artifactId": "a1",
                "name": "result",
                "parts": [proto.text_part("the answer", version)],
            }
        ]

    monkeypatch.setattr(tasks_mod, "build_artifacts", _artifacts)

    app = FastAPI()
    app.include_router(a2a_server.router)
    app.dependency_overrides[get_db] = lambda: _AgentDB(agent)
    return partner_client(app), task, seen_ops


def _rpc(client, method, params, headers=None):
    return client.post(
        "/a2a/tok/jsonrpc",
        json={"jsonrpc": "2.0", "id": "r1", "method": method, "params": params},
        headers={"Authorization": "Bearer x", **(headers or {})},
    )


def _send_params():
    return {
        "message": {
            "messageId": "m-1",
            "role": "ROLE_USER",
            "parts": [{"text": "hello"}],
        }
    }


class TestV1Dispatch:
    def test_send_message_result_parses_as_the_reference_response(self, monkeypatch):
        client, task, _ = _harness(monkeypatch)
        body = _rpc(client, "SendMessage", _send_params()).json()

        parsed = json_format.ParseDict(
            body["result"], a2a_pb2.SendMessageResponse()
        )
        assert parsed.task.id == str(task.id)
        assert parsed.task.status.state == a2a_pb2.TASK_STATE_COMPLETED
        assert parsed.task.artifacts[0].parts[0].text == "the answer"
        # The mirror is version-shaped too: proto role, no v0.3 discriminator.
        assert parsed.task.status.message.role == a2a_pb2.ROLE_AGENT
        assert parsed.task.history[0].parts[0].text == "the answer"

    def test_get_task_result_is_a_bare_task(self, monkeypatch):
        client, task, _ = _harness(monkeypatch)
        body = _rpc(client, "GetTask", {"id": str(task.id)}).json()

        parsed = json_format.ParseDict(body["result"], a2a_pb2.Task())
        assert parsed.id == str(task.id)
        assert parsed.status.state == a2a_pb2.TASK_STATE_COMPLETED

    def test_cancel_task_result_is_a_bare_task(self, monkeypatch):
        client, task, _ = _harness(monkeypatch, states=[proto.A2AState.CANCELED])
        body = _rpc(client, "CancelTask", {"id": str(task.id)}).json()

        parsed = json_format.ParseDict(body["result"], a2a_pb2.Task())
        assert parsed.status.state == a2a_pb2.TASK_STATE_CANCELED

    def test_unserved_v1_methods_get_the_refusal_envelope(self, monkeypatch):
        # Real v1.0 methods this surface deliberately does not serve must be
        # refused exactly like unknown strings — not crash on dispatch.
        client, _, _ = _harness(monkeypatch)
        body = _rpc(client, "ListTasks", {}).json()
        assert body["error"]["code"] == proto.UNSUPPORTED_OPERATION

    def test_rate_limit_buckets_on_the_operation_not_the_name(self, monkeypatch):
        # SendMessage must share message/send's create budget — a second
        # vocabulary must not be a second allowance.
        client, task, seen_ops = _harness(monkeypatch)
        _rpc(client, "SendMessage", _send_params())
        _rpc(client, "message/send", _send_params())
        _rpc(client, "GetTask", {"id": str(task.id)})
        _rpc(client, "tasks/get", {"id": str(task.id)})
        assert seen_ops == ["send", "send", "get", "get"]


class TestBlockingSend:
    """The reference SDK's client never polls — it makes ONE send call and
    renders what comes back as the final answer. Observed via ADK (2026-08):
    a task returned as SUBMITTED reached the user as an empty reply while
    the run completed unread. v1.0 therefore blocks by DEFAULT
    (``returnImmediately`` is the opt-out); v0.3 keeps Foundry's contract —
    non-blocking unless ``blocking: true`` is sent.
    """

    def test_v1_send_blocks_until_terminal_by_default(self, monkeypatch):
        client, _, _ = _harness(
            monkeypatch,
            states=[proto.A2AState.WORKING, proto.A2AState.COMPLETED],
        )
        body = _rpc(client, "SendMessage", _send_params()).json()
        parsed = json_format.ParseDict(
            body["result"], a2a_pb2.SendMessageResponse()
        )
        # COMPLETED, not the WORKING a render-immediately would have seen:
        # the wait consumed the non-terminal state.
        assert parsed.task.status.state == a2a_pb2.TASK_STATE_COMPLETED

    def test_v1_return_immediately_opts_out(self, monkeypatch):
        client, _, _ = _harness(
            monkeypatch,
            states=[proto.A2AState.WORKING, proto.A2AState.COMPLETED],
        )
        params = _send_params()
        params["configuration"] = {"returnImmediately": True}
        body = _rpc(client, "SendMessage", params).json()
        parsed = json_format.ParseDict(
            body["result"], a2a_pb2.SendMessageResponse()
        )
        assert parsed.task.status.state == a2a_pb2.TASK_STATE_WORKING

    def test_v1_timeout_returns_the_task_in_flight(self, monkeypatch):
        # A run that outlives the cap degrades to today's behaviour — the
        # non-terminal task — never to an error.
        client, _, _ = _harness(monkeypatch, states=[proto.A2AState.WORKING])
        from app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "a2a_blocking_send_timeout_seconds", 0)
        body = _rpc(client, "SendMessage", _send_params()).json()
        parsed = json_format.ParseDict(
            body["result"], a2a_pb2.SendMessageResponse()
        )
        assert parsed.task.status.state == a2a_pb2.TASK_STATE_WORKING

    def test_v03_stays_non_blocking_by_default(self, monkeypatch):
        # Foundry sends no flag and polls; its measured behaviour must not
        # change underneath it.
        client, _, _ = _harness(
            monkeypatch,
            states=[proto.A2AState.WORKING, proto.A2AState.COMPLETED],
        )
        body = _rpc(client, "message/send", _send_params()).json()
        assert body["result"]["status"]["state"] == "working"

    def test_v03_blocking_true_is_honoured(self, monkeypatch):
        client, _, _ = _harness(
            monkeypatch,
            states=[proto.A2AState.WORKING, proto.A2AState.COMPLETED],
        )
        params = _send_params()
        params["configuration"] = {"blocking": True}
        body = _rpc(client, "message/send", params).json()
        assert body["result"]["status"]["state"] == "completed"


class TestMethodOutranksHeader:
    def test_v1_method_with_a_legacy_header_answers_in_v1(self, monkeypatch):
        """The reference SDK sends no version header; anything a proxy or a
        misconfigured client puts there must not override what the method
        name proves about the caller's parser."""
        client, _, _ = _harness(monkeypatch)
        body = _rpc(
            client, "SendMessage", _send_params(), headers={"A2A-Version": "0.3"}
        ).json()
        json_format.ParseDict(body["result"], a2a_pb2.SendMessageResponse())

    def test_legacy_method_with_a_v1_header_answers_in_v03(self, monkeypatch):
        client, _, _ = _harness(monkeypatch)
        body = _rpc(
            client, "message/send", _send_params(), headers={"A2A-Version": "1.0"}
        ).json()
        result = body["result"]
        assert result["kind"] == "task"
        assert result["status"]["state"] == "completed"

    def test_garbage_header_still_refuses(self, monkeypatch):
        # The header keeps its contract even though the method outranks it: a
        # declared-but-unsupported version is refused, never guessed around.
        client, _, _ = _harness(monkeypatch)
        body = _rpc(
            client, "SendMessage", _send_params(), headers={"A2A-Version": "9.9"}
        ).json()
        assert body["error"]["code"] == proto.VERSION_NOT_SUPPORTED


class TestV03Regression:
    def test_legacy_send_is_byte_compatible_with_what_foundry_parsed(
        self, monkeypatch
    ):
        """The v0.3 shapes that survived live Foundry testing must be
        untouched by the v1.0 work: kind discriminators everywhere, lowercase
        states, nested file/data part spellings."""
        client, task, _ = _harness(monkeypatch)
        result = _rpc(client, "message/send", _send_params()).json()["result"]

        assert result["kind"] == "task"
        assert result["id"] == str(task.id)
        assert result["status"]["state"] == "completed"
        assert result["artifacts"][0]["parts"][0] == {
            "kind": "text",
            "text": "the answer",
        }
        message = result["status"]["message"]
        assert message["kind"] == "message"
        assert message["role"] == "agent"
        assert result["history"] == [message]

        from a2a.compat.v0_3 import types as v03

        parsed = v03.Task.model_validate(result)
        assert parsed.status.state is v03.TaskState.completed
