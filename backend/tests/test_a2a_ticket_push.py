"""A partner is told when a ticket its A2A task filed changes status (#820)."""

from __future__ import annotations

import uuid

import pytest

from app.a2a import push as push_svc
from app.a2a import ticket_push as tp
from app.core.config import settings
from app.core.security import encrypt_value
from tests.a2a_server_stubs import FakeDb, task

pytestmark = pytest.mark.asyncio


def _config(**overrides):
    fields = {
        "id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "client_id": "partner-1",
        "url": "https://partner.example.com/hook",
        "token": None,
        "auth_scheme": None,
        "auth_credentials": None,
        "delivery_state": "delivered",
        "attempts": 1,
    }
    fields.update(overrides)
    return type("Config", (), fields)()


def _event(status: str = "done", previous: str | None = "in_progress", wid: str | None = None) -> dict:
    return {"work_item_id": wid or str(uuid.uuid4()), "status": status, "previous_status": previous}


def _capture(monkeypatch) -> list[tuple[object, dict, str]]:
    sent: list[tuple[object, dict, str]] = []

    async def _deliver(config, body, *, label):
        sent.append((config, body, label))
        return True

    monkeypatch.setattr(push_svc, "deliver_body", _deliver)
    return sent


class TestTheBody:
    def test_identity_and_status_only(self):
        t = task()
        body = tp.ticket_body(t, work_item_id="w-1", status="done", previous_status="blocked")
        assert body == {
            "kind": "work_item.status_changed",
            "taskId": str(t.id),
            "contextId": str(t.context_id),
            "workItemId": "w-1",
            "status": "done",
            "previousStatus": "blocked",
        }


class TestWhoIsTold:
    async def test_the_callbacks_of_the_originating_task_get_one_notification(self, monkeypatch):
        sent = _capture(monkeypatch)
        t = task()
        config = _config(task_id=t.id, client_id=t.client_id)
        origin = uuid.uuid4()
        # work item origin run -> the task that run maps to -> its callbacks
        db = FakeDb(origin, t, [config])
        event = _event()
        assert await tp.notify_ticket_status(db, event) == 1
        (got, body, _label) = sent[0]
        assert got is config
        assert body["workItemId"] == event["work_item_id"] and body["status"] == "done"
        # The task's announcement bookkeeping is untouched.
        assert config.attempts == 1 and config.delivery_state == "delivered"

    async def test_a_ticket_filed_by_a_spawned_run_finds_the_task_up_the_chain(self, monkeypatch):
        sent = _capture(monkeypatch)
        t = task()
        parent = uuid.uuid4()
        # origin run (no task), its parent_run_id, the task of the parent, the callbacks
        db = FakeDb(uuid.uuid4(), None, parent, t, [_config(task_id=t.id)])
        assert await tp.notify_ticket_status(db, _event()) == 1
        assert len(sent) == 1

    async def test_an_item_no_partner_task_filed_tells_nobody(self, monkeypatch):
        sent = _capture(monkeypatch)
        # origin run, no task, no parent
        db = FakeDb(uuid.uuid4(), None, None)
        assert await tp.notify_ticket_status(db, _event()) == 0
        assert sent == []

    async def test_an_item_with_no_origin_run_tells_nobody(self, monkeypatch):
        sent = _capture(monkeypatch)
        assert await tp.notify_ticket_status(FakeDb(None), _event()) == 0
        assert sent == []

    @pytest.mark.parametrize(("status", "previous"), [("claimed", "open"), ("done", "done"), ("weird", "open")])
    async def test_bookkeeping_and_no_change_are_not_sent(self, monkeypatch, status, previous):
        sent = _capture(monkeypatch)
        db = FakeDb(uuid.uuid4(), task(), [_config()])
        assert await tp.notify_ticket_status(db, _event(status, previous)) == 0
        assert sent == []

    async def test_a_bad_work_item_id_is_ignored(self, monkeypatch):
        sent = _capture(monkeypatch)
        assert await tp.notify_ticket_status(FakeDb(), _event(wid="not-a-uuid")) == 0
        assert sent == []

    async def test_a_lookup_failure_never_raises(self, monkeypatch):
        sent = _capture(monkeypatch)

        class _Boom(FakeDb):
            async def execute(self, _stmt):
                raise RuntimeError("db down")

        assert await tp.notify_ticket_status(_Boom(), _event()) == 0
        assert sent == []


class TestTheSend:
    def _transport(self, monkeypatch, *, status=200):
        sent: dict = {}

        class _Response:
            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")

        class _Client:
            def __init__(self, **_k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_e):
                return False

            async def post(self, url, json=None, headers=None):
                sent.update(url=url, json=json, headers=headers)
                return _Response()

        async def _check(url, **_k):
            return url

        monkeypatch.setattr(push_svc, "assert_allowed_async", _check)
        monkeypatch.setattr(push_svc.httpx, "AsyncClient", _Client)
        return sent

    async def test_the_partner_credential_is_presented_and_the_budget_kept(self, monkeypatch):
        sent = self._transport(monkeypatch)
        config = _config(
            token=encrypt_value("shared"), auth_scheme="Bearer", auth_credentials=encrypt_value("abc"), attempts=2
        )
        body = {"kind": tp.KIND, "workItemId": "w-1", "status": "done"}
        assert await push_svc.deliver_body(config, body, label="work item w-1") is True
        assert sent["json"]["token"] == "shared" and sent["headers"]["Authorization"] == "Bearer abc"
        assert "token" not in body  # the caller's body is not mutated
        assert config.attempts == 2 and config.delivery_state == "delivered"

    async def test_a_failed_send_is_swallowed_and_changes_nothing(self, monkeypatch):
        self._transport(monkeypatch, status=500)
        config = _config(attempts=settings.a2a_push_max_attempts - 1)
        assert await push_svc.deliver_body(config, {"kind": tp.KIND}, label="x") is False
        assert config.attempts == settings.a2a_push_max_attempts - 1 and config.delivery_state == "delivered"


class TestRegistration:
    async def test_the_listener_is_registered_once(self, monkeypatch):
        from app.services.event_bus import EventBus

        bus = EventBus()
        monkeypatch.setattr("app.services.event_bus.event_bus", bus)
        monkeypatch.setattr(tp, "_registered", False)
        tp.register_ticket_push()
        tp.register_ticket_push()
        assert bus._resolve_listeners(tp.STATUS_CHANGED_EVENT) == [tp.on_status_changed]
