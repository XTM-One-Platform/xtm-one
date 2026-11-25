"""Receiving a partner's ticket status push (#2276).

A partner XTM One tells every callback of the task that filed a ticket when
the ticket changes status (``app.a2a.ticket_push``). The delegation that filed
it has concluded by then, so the delegation path refuses it. These tests pin
the receiving half: the same single refusal for anything not authenticated or
not about this delegation's partner task, a platform event with ids and
statuses only for the rest, and the task callback path left untouched.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.a2a import ticket_push
from app.a2a.client import callbacks as callback_svc
from app.api.v1 import a2a_callbacks
from app.services import platform_events
from tests.a2a_server_stubs import partner_client

_WORK_ITEM = str(uuid.uuid4())


def _row(**kw):
    fields = {
        "id": uuid.uuid4(),
        "target_id": None,
        "agent_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "run_ref": uuid.uuid4(),
        "partner_task_id": "t-1",
        "outcome": "completed",
        "callback_secret": callback_svc.store_secret("the-secret"),
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


def _body(**kw):
    body = {
        "kind": ticket_push.KIND,
        "taskId": "t-1",
        "contextId": "c-1",
        "workItemId": _WORK_ITEM,
        "status": "done",
        "previousStatus": "in_progress",
        "token": "the-secret",
    }
    body.update(kw)
    return body


class TestTheBodyIsCheckedNotTrusted:
    def test_the_kind_is_the_one_the_sender_writes(self):
        assert callback_svc.TICKET_PUSH_KIND == ticket_push.KIND
        assert ticket_push.NOTIFIED_STATUSES == callback_svc.TICKET_STATUSES

    def test_a_task_callback_is_not_a_ticket_push(self):
        assert callback_svc.is_ticket_push({"taskId": "t-1", "state": "completed"}) is False
        assert callback_svc.is_ticket_push(None) is False

    def test_a_valid_push_names_the_work_item_and_statuses(self):
        assert callback_svc.ticket_push_fields(_body(), _row()) == {
            "work_item_id": _WORK_ITEM,
            "status": "done",
            "previous_status": "in_progress",
        }

    def test_another_task_is_refused(self):
        assert callback_svc.ticket_push_fields(_body(taskId="t-2"), _row()) is None

    def test_a_delegation_without_a_partner_task_is_refused(self):
        assert callback_svc.ticket_push_fields(_body(), _row(partner_task_id=None)) is None

    @pytest.mark.parametrize("work_item", ["", "not-a-uuid", None, 12])
    def test_a_work_item_that_is_no_id_is_refused(self, work_item):
        assert callback_svc.ticket_push_fields(_body(workItemId=work_item), _row()) is None

    @pytest.mark.parametrize("status", ["claimed", "merged", "", None])
    def test_a_status_the_sender_never_announces_is_refused(self, status):
        assert callback_svc.ticket_push_fields(_body(status=status), _row()) is None

    def test_an_unknown_previous_status_is_dropped_not_echoed(self):
        fields = callback_svc.ticket_push_fields(_body(previousStatus="<script>"), _row())
        assert fields is not None and fields["previous_status"] is None


class TestTheEvent:
    def test_it_is_in_the_catalog_with_its_filter_keys(self):
        entry = platform_events.PLATFORM_EVENTS[platform_events.A2A_TICKET_STATUS_CHANGED]
        assert {"delegation_id", "work_item_id", "status"} <= set(entry["filter_keys"])

    def test_its_payload_carries_ids_and_statuses_only(self):
        row = _row()
        data = platform_events.a2a_ticket_status_data(
            delegation_id=row.id,
            agent_id=row.agent_id,
            user_id=row.user_id,
            run_id=row.run_ref,
            partner="Partner",
            partner_task_id="t-1",
            work_item_id=_WORK_ITEM,
            status="done",
            previous_status="in_progress",
        )
        assert data["personal_owner_id"] == str(row.user_id)
        assert data["participant_agent_ids"] == [str(row.agent_id)]
        assert data["actor_agent_id"] is None
        assert not {"title", "result", "note", "token"} & set(data)

    @pytest.mark.asyncio
    async def test_one_change_is_one_event_id(self, monkeypatch):
        published: list = []

        async def _publish(name, data, *, event_id=None):
            published.append((name, data, event_id))
            return event_id

        async def _target(_db, _tid):
            return SimpleNamespace(declared_name="Partner")

        monkeypatch.setattr(platform_events, "publish", _publish)
        monkeypatch.setattr(callback_svc, "_target_of", _target)
        row = _row()
        fields = callback_svc.ticket_push_fields(_body(), row)
        await callback_svc.publish_ticket_status(object(), row, fields)
        await callback_svc.publish_ticket_status(object(), row, fields)
        assert [p[0] for p in published] == [platform_events.A2A_TICKET_STATUS_CHANGED] * 2
        assert published[0][2] == published[1][2] == f"a2a-ticket:{row.id}:{_WORK_ITEM}:done"
        assert published[0][1]["partner"] == "Partner"

    @pytest.mark.asyncio
    async def test_a_failure_to_publish_never_raises(self, monkeypatch):
        async def _boom(*_a, **_k):
            raise RuntimeError("bus down")

        monkeypatch.setattr(callback_svc, "_target_of", _boom)
        row = _row()
        fields = {"work_item_id": _WORK_ITEM, "status": "done", "previous_status": None}
        assert await callback_svc.publish_ticket_status(object(), row, fields) is None


class _FakeSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def client(monkeypatch):
    async def _noop(*_a, **_k):
        return None

    async def _enabled(_db):
        return True

    monkeypatch.setattr(a2a_callbacks, "_enforce_preauth_rate_limit", _noop)
    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _enabled)
    monkeypatch.setattr(a2a_callbacks, "async_session", _FakeSession)
    monkeypatch.setattr(callback_svc, "spawn_partner_finished", lambda *_a: None)
    app = FastAPI()
    app.include_router(a2a_callbacks.router)
    return partner_client(app)


class TestTheRoute:
    @pytest.fixture
    def published(self, monkeypatch):
        seen: list = []

        async def _publish(_db, row, fields):
            seen.append((row.id, fields))

        monkeypatch.setattr(callback_svc, "publish_ticket_status", _publish)
        return seen

    def _admit(self, monkeypatch, row):
        async def _found(_db, _did):
            return row

        async def _never(*_a, **_k):
            raise AssertionError("a ticket push never takes the delegation path")

        monkeypatch.setattr(callback_svc, "ticket_push_delegation", _found)
        monkeypatch.setattr(callback_svc, "addressed_delegation", _never)

    def test_a_concluded_delegation_accepts_its_ticket_push(self, client, monkeypatch, published):
        row = _row(outcome="completed")
        self._admit(monkeypatch, row)
        r = client.post(f"/a2a/callbacks/{row.id}", json=_body())
        assert r.status_code == 200
        assert published == [(row.id, {"work_item_id": _WORK_ITEM, "status": "done", "previous_status": "in_progress"})]

    def test_a_wrong_secret_is_the_usual_refusal(self, client, monkeypatch, published):
        row = _row()
        self._admit(monkeypatch, row)
        r = client.post(f"/a2a/callbacks/{row.id}", json=_body(token="guessed"))
        assert r.status_code == 404
        assert published == []

    def test_an_unknown_delegation_is_the_usual_refusal(self, client, monkeypatch, published):
        self._admit(monkeypatch, None)
        assert client.post("/a2a/callbacks/d-1", json=_body()).status_code == 404
        assert published == []

    def test_a_push_about_another_task_is_the_usual_refusal(self, client, monkeypatch, published):
        row = _row()
        self._admit(monkeypatch, row)
        r = client.post(f"/a2a/callbacks/{row.id}", json=_body(taskId="t-9"))
        assert r.status_code == 404
        assert published == []

    def test_a_task_callback_still_takes_the_delegation_path(self, client, monkeypatch, published):
        row = _row(outcome=None)
        spawned: list = []

        async def _addressed(_db, _did):
            return row

        async def _never(*_a, **_k):
            raise AssertionError("a task callback never takes the ticket path")

        monkeypatch.setattr(callback_svc, "addressed_delegation", _addressed)
        monkeypatch.setattr(callback_svc, "ticket_push_delegation", _never)
        monkeypatch.setattr(callback_svc, "spawn_partner_finished", lambda i: spawned.append(i))
        r = client.post(
            f"/a2a/callbacks/{row.id}",
            json={"taskId": "t-1", "state": "completed", "token": "the-secret"},
        )
        assert r.status_code == 200
        assert spawned == [row.id]
        assert published == []

    def test_the_ticket_path_authenticates_before_it_publishes(self):
        import inspect

        src = inspect.getsource(a2a_callbacks._receive_ticket_push)
        assert src.index("presented_secret_matches") < src.index("ticket_push_fields")
        assert src.index("ticket_push_fields") < src.index("publish_ticket_status")
