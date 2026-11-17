"""No 500 from a LinkedIn challenge or a non-object body.

- A LinkedIn GET challenge whose client secret cannot be decrypted is answered the way
  the POST answers: a 503 and an ERROR naming the OAuth default, nothing of
  the secret in the answer or the log.
- The direct-assignment and legacy agent webhook routes give a JSON array or scalar body
  the provider route's shape instead of a 500 on the first key written into it.

The database, Redis and the assignment engine are replaced by fakes.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.v1 import integration_notifications, webhooks


class _Session:
    """``async_session()`` answering each query with the next row given (the last repeats)."""

    def __init__(self, rows: list[object]):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, *_args, **_kwargs):
        row = self._rows.pop(0) if len(self._rows) > 1 else self._rows[0]
        rows = row if isinstance(row, list) else [row]
        return SimpleNamespace(scalar_one_or_none=lambda: row, scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def commit(self):
        return None


def _request(body: bytes = b"", *, query: bytes = b"") -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/hooks/test",
        "headers": [(b"content-type", b"application/json")],
        "query_string": query,
        "client": ("203.0.113.1", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope, receive)


# ---------------------------------------------------------------------------
# LinkedIn GET challenges
# ---------------------------------------------------------------------------

UNREADABLE = SimpleNamespace(id=uuid.uuid4(), provider="linkedin", client_secret_enc="not-a-fernet-token")


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(lambda r: webhooks.validate_provider_webhook("linkedin", r), id="provider-hook"),
        pytest.param(lambda r: integration_notifications.linkedin_webhook_validation(r), id="notifications"),
    ],
)
async def test_a_linkedin_challenge_with_an_unreadable_secret_is_a_503(monkeypatch, caplog, answer):
    for module in (webhooks, integration_notifications):
        monkeypatch.setattr(module, "async_session", lambda: _Session([UNREADABLE]))
    with caplog.at_level(logging.ERROR), pytest.raises(HTTPException) as refused:
        await answer(_request(query=b"challengeCode=abc"))
    assert refused.value.status_code == 503
    assert "cannot be answered" in refused.value.detail
    (error,) = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert str(UNREADABLE.id) in error
    assert "not-a-fernet-token" not in error and "not-a-fernet-token" not in refused.value.detail


# ---------------------------------------------------------------------------
# JSON arrays and scalars on the direct-assignment and legacy routes
# ---------------------------------------------------------------------------


class _TriggerReachedError(Exception):
    def __init__(self, payload):
        super().__init__("trigger reached")
        self.payload = payload


@pytest.fixture
def hooks(monkeypatch):
    async def _none(*_a, **_k):
        return None

    async def _claim(*_a, **_k):
        return False, None, "dedup-key"

    async def _finalize(_key, response):
        return response

    async def _trigger(**kwargs):
        raise _TriggerReachedError(kwargs["payload"])

    for name in ("enforce_rate_limit", "_clear_direct_webhook_drift_state", "_release_webhook_dedup_slot"):
        monkeypatch.setattr(webhooks, name, _none)
    monkeypatch.setattr(webhooks, "_verify_assignment_webhook_secret", lambda *_a, **_k: None)
    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _claim)
    monkeypatch.setattr(webhooks, "_finalize_dedup", _finalize)
    monkeypatch.setattr(webhooks, "trigger_or_queue_assignment", _trigger)
    monkeypatch.setattr(webhooks.settings, "assignment_queue_enabled", True)

    def _serve(*rows):
        monkeypatch.setattr(webhooks, "async_session", lambda: _Session(list(rows)))

    return _serve


BODIES = [
    pytest.param(b"[1, 2]", "[1, 2]", id="array"),
    pytest.param(b'"a string"', "a string", id="string"),
    pytest.param(b"42", "42", id="number"),
    pytest.param(b"null", "None", id="null"),
]


@pytest.mark.parametrize(("body", "raw"), BODIES)
async def test_the_direct_assignment_route_takes_a_body_that_is_not_an_object(hooks, body, raw):
    agent = SimpleNamespace(id=uuid.uuid4(), enabled=True, name="A")
    # Ownerless, so the owner-deactivation rule exempts it without a query.
    assignment = SimpleNamespace(
        id=uuid.uuid4(), trigger_type="webhook", enabled=True, trigger_config={}, agent_id=agent.id, name="WF",
        is_company_managed=False, user_id=None,
    )
    hooks(assignment, assignment, agent)
    with pytest.raises(_TriggerReachedError) as reached:
        await webhooks.receive_direct_assignment_webhook(str(assignment.id), _request(body))
    assert reached.value.payload["raw_body"] == raw
    assert reached.value.payload["_webhook_meta"]["assignment_id"] == str(assignment.id)


@pytest.mark.parametrize(("body", "raw"), BODIES)
async def test_the_legacy_agent_route_takes_a_body_that_is_not_an_object(hooks, body, raw):
    disabled = SimpleNamespace(id=uuid.uuid4(), enabled=False, name="A")
    hooks([], disabled)
    answer = await webhooks.receive_webhook(str(disabled.id), "custom", _request(body))
    assert answer["status"] == "ignored"


async def test_an_object_body_is_unchanged_on_the_direct_route(hooks):
    agent = SimpleNamespace(id=uuid.uuid4(), enabled=True, name="A")
    assignment = SimpleNamespace(
        id=uuid.uuid4(), trigger_type="webhook", enabled=True, trigger_config={}, agent_id=agent.id, name="WF",
        is_company_managed=False, user_id=None,
    )
    hooks(assignment, assignment, agent)
    with pytest.raises(_TriggerReachedError) as reached:
        await webhooks.receive_direct_assignment_webhook(str(assignment.id), _request(b'{"alert": 1}'))
    assert reached.value.payload["alert"] == 1
    assert "raw_body" not in reached.value.payload



async def test_a_busy_assignment_queues_the_legacy_agent_webhook(hooks, monkeypatch):
    """What the busy-event queue answers is what the receiver reports."""
    agent = SimpleNamespace(id=uuid.uuid4(), enabled=True, name="A", integration_bindings={})
    assignment = SimpleNamespace(
        id=uuid.uuid4(), trigger_type="webhook", enabled=True, name="WF", agent_id=agent.id,
        trigger_config={"path": "custom"},
    )
    queued: list[dict] = []

    async def _busy(**_kwargs):
        return None

    async def _queue(wf, agent_, payload, *, db, trigger_context):
        queued.append(payload)
        return "run-queued"

    monkeypatch.setattr(webhooks, "trigger_assignment", _busy)
    monkeypatch.setattr(webhooks, "queue_busy_pushed_event", _queue, raising=False)
    hooks()
    shared = _Session([[assignment], agent, [assignment]])
    monkeypatch.setattr(webhooks, "async_session", lambda: shared)
    answer = await webhooks.receive_webhook(str(agent.id), "custom", _request(b'{"alert": 1}'))
    assert answer["triggered"] == [{"assignment": "WF", "run_id": "run-queued"}]
    assert queued and queued[0]["alert"] == 1


@pytest.mark.parametrize("event_queue", [False, True])
async def test_a_busy_assignments_agent_webhook_is_queued_only_with_the_event_queue_on(hooks, monkeypatch, event_queue):
    """Off (the default), an assignment with every slot taken skips the
    delivery and nothing is queued (a free slot would still start it)."""
    from app.services import assignment_engine as ae

    agent = SimpleNamespace(id=uuid.uuid4(), enabled=True, name="A", integration_bindings={})
    assignment = SimpleNamespace(
        id=uuid.uuid4(), trigger_type="webhook", enabled=True, name="WF", agent_id=agent.id,
        trigger_config={"path": "custom"},
    )
    calls: list[tuple[str | None, bool]] = []

    async def _busy(**_kwargs):
        return None

    async def _queue(*_args, overflow=None, queue=True, **_kwargs):
        calls.append((overflow, queue))
        if not queue:
            return ae.QueuedTriggerResult(status="dropped")
        return ae.QueuedTriggerResult(status="queued", run_id="run-queued", queue_depth=1)

    monkeypatch.setattr(webhooks, "trigger_assignment", _busy)
    monkeypatch.setattr(ae, "trigger_or_queue_assignment", _queue)
    monkeypatch.setattr(ae.settings, "integration_event_queue_enabled", event_queue)
    hooks()
    shared = _Session([[assignment], agent, [assignment]])
    monkeypatch.setattr(webhooks, "async_session", lambda: shared)
    answer = await webhooks.receive_webhook(str(agent.id), "custom", _request(b'{"alert": 1}'))
    expected = [{"assignment": "WF", "run_id": "run-queued"}] if event_queue else []
    assert (answer["triggered"], answer["count"]) == (expected, len(expected))
    assert calls == [("persist", event_queue)]
